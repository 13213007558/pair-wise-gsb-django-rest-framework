import io
from unittest import mock

import pytest
from django.contrib.admin import site
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import RequestFactory, TestCase, override_settings
from django.urls import include, path

from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.authtoken.admin import TokenAdmin
from rest_framework.authtoken.digests import DIGEST_PREFIX, hash_key, is_digest
from rest_framework.authtoken.models import REDACTED_TOKEN, Token, TokenProxy
from rest_framework.authtoken.views import obtain_auth_token
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIClient

DIGEST_SETTING = {'AUTHTOKEN_DIGEST_ENABLED': True}

urlpatterns = [
    path('api-token-auth/', obtain_auth_token),
]


def make_legacy_token(user, raw=None):
    """Insert a row exactly as legacy deployments stored it: plaintext."""
    raw = raw or Token.generate_key()
    # bulk_create bypasses the digest-aware save() so the plaintext key
    # reaches the column untouched.
    Token.objects.bulk_create([Token(user=user, key=raw)])
    return Token.objects.get(pk=raw), raw


@override_settings(REST_FRAMEWORK=DIGEST_SETTING)
class DigestTokenCreationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='digest_user')

    def test_new_token_stores_only_digest_and_returns_plaintext_once(self):
        token = Token.objects.create(user=self.user)
        raw = token.raw_key

        assert len(raw) == 40
        assert is_digest(token.key)
        assert token.key == hash_key(raw)
        assert token.key != raw

        reloaded = Token.objects.get(user=self.user)
        assert reloaded.raw_key is None
        assert reloaded.key == token.key
        assert raw not in str(reloaded.__dict__)

    def test_refresh_from_db_forgets_plaintext(self):
        token = Token.objects.create(user=self.user)
        raw = token.raw_key

        token.refresh_from_db()

        assert token.raw_key is None
        assert token.key == hash_key(raw)

    def test_explicit_key_is_digested_on_save(self):
        raw = Token.generate_key()
        token = Token.objects.create(key=raw, user=self.user)

        assert token.raw_key == raw
        assert token.key == hash_key(raw)

    def test_string_representation_is_redacted(self):
        token = Token.objects.create(user=self.user)

        assert str(token) == REDACTED_TOKEN
        assert token.raw_key not in str(token)
        assert str(Token.objects.get(user=self.user)) == REDACTED_TOKEN

    def test_same_plaintext_key_collides_on_its_digest(self):
        other = User.objects.create_user(username='other')
        raw = Token.generate_key()
        Token.objects.create(key=raw, user=self.user)
        with pytest.raises(Exception):
            Token.objects.create(key=raw, user=other)


@override_settings(REST_FRAMEWORK=DIGEST_SETTING)
class DigestAuthenticationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='auth_user')
        self.token = Token.objects.create(user=self.user)
        self.raw_key = self.token.raw_key
        self.backend = TokenAuthentication()

    def test_plaintext_key_authenticates_against_digest(self):
        assert Token.objects.get_by_raw_key(self.raw_key) == self.token
        user, auth = self.backend.authenticate_credentials(self.raw_key)
        assert user == self.user
        assert auth == self.token

    def test_unknown_keys_fail_with_one_indistinguishable_message(self):
        wrong_suffix = self.raw_key[:-1] + ('0' if self.raw_key[-1] != '0' else '1')
        with pytest.raises(AuthenticationFailed) as near_err:
            self.backend.authenticate_credentials(wrong_suffix)
        with pytest.raises(AuthenticationFailed) as random_err:
            self.backend.authenticate_credentials('a' * 40)
        assert (
            str(near_err.value.detail)
            == str(random_err.value.detail)
            == 'Invalid token.'
        )

    def test_stored_digest_itself_is_not_a_valid_credential(self):
        with pytest.raises(AuthenticationFailed):
            self.backend.authenticate_credentials(self.token.key)

    def test_inactive_user_is_rejected(self):
        self.user.is_active = False
        self.user.save()
        with pytest.raises(AuthenticationFailed) as err:
            self.backend.authenticate_credentials(self.raw_key)
        assert str(err.value.detail) == 'User inactive or deleted.'

    def test_revocation_and_reissue_behaviour(self):
        Token.objects.filter(user=self.user).delete()
        with pytest.raises(AuthenticationFailed):
            self.backend.authenticate_credentials(self.raw_key)

        new_token = Token.objects.create(user=self.user)
        assert new_token.raw_key != self.raw_key
        assert new_token.key != hash_key(self.raw_key)
        user, _ = self.backend.authenticate_credentials(new_token.raw_key)
        assert user == self.user


@override_settings(REST_FRAMEWORK=DIGEST_SETTING)
class LegacyKeyMigrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='legacy_user')
        self.token, self.raw_key = make_legacy_token(self.user)
        assert not is_digest(self.token.key)

    def test_legacy_plaintext_row_still_authenticates_in_digest_mode(self):
        user, auth = TokenAuthentication().authenticate_credentials(self.raw_key)
        assert user == self.user
        assert auth == self.token

    def test_migration_converts_rows_but_keeps_clients_working(self):
        out = io.StringIO()
        call_command('drf_migrate_token_digests', stdout=out)

        migrated = Token.objects.get(user=self.user)
        assert is_digest(migrated.key)
        assert migrated.key == hash_key(self.raw_key)
        user, _ = TokenAuthentication().authenticate_credentials(self.raw_key)
        assert user == self.user
        with pytest.raises(AuthenticationFailed):
            TokenAuthentication().authenticate_credentials(migrated.key)
        assert 'Migrated 1 token' in out.getvalue()

    def test_migration_is_idempotent(self):
        call_command('drf_migrate_token_digests')
        out = io.StringIO()
        call_command('drf_migrate_token_digests', stdout=out)
        assert 'No plaintext tokens to migrate' in out.getvalue()

    def test_dry_run_writes_nothing(self):
        out = io.StringIO()
        call_command('drf_migrate_token_digests', '--dry-run', stdout=out)
        assert Token.objects.get(user=self.user).key == self.raw_key
        assert 'would be migrated' in out.getvalue()

    def test_failed_migration_rolls_back_every_row_atomically(self):
        other_user = User.objects.create_user(username='legacy_user_2')
        _, other_raw = make_legacy_token(other_user)

        target = (
            'rest_framework.authtoken.management.commands.'
            'drf_migrate_token_digests.hash_key'
        )
        calls = {'n': 0}

        def flaky_hash(key):
            calls['n'] += 1
            if calls['n'] == 2:
                raise RuntimeError('simulated migration failure')
            return hash_key(key)

        with mock.patch(target, side_effect=flaky_hash):
            with pytest.raises(RuntimeError):
                call_command('drf_migrate_token_digests')

        # No half-migrated state: every row keeps its original key.
        assert Token.objects.get(user=self.user).key == self.raw_key
        assert Token.objects.get(user=other_user).key == other_raw
        user, _ = TokenAuthentication().authenticate_credentials(self.raw_key)
        assert user == self.user

    def test_migration_command_requires_digest_mode(self):
        with override_settings(REST_FRAMEWORK={}):
            with pytest.raises(Exception):
                call_command('drf_migrate_token_digests')


class LegacyModeUnchangedTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='plain_user')

    def test_legacy_mode_stores_and_shows_plaintext(self):
        token = Token.objects.create(user=self.user)

        assert not is_digest(token.key)
        assert token.raw_key is None
        assert str(token) == token.key
        assert len(token.key) == 40

    def test_legacy_mode_authenticates_directly(self):
        token = Token.objects.create(user=self.user)
        user, auth = TokenAuthentication().authenticate_credentials(token.key)
        assert user == self.user and auth == token

    def test_legacy_rows_remain_queryable_after_mode_switch(self):
        token, raw = make_legacy_token(self.user)
        with override_settings(REST_FRAMEWORK=DIGEST_SETTING):
            user, _ = TokenAuthentication().authenticate_credentials(raw)
            assert user == self.user
            assert Token.objects.get_by_raw_key(raw).key == raw


@override_settings(ROOT_URLCONF='tests.test_authtoken_digest',
                   REST_FRAMEWORK=DIGEST_SETTING)
class ObtainTokenViewDigestTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='view_user', password='secret'
        )

    def test_view_returns_plaintext_once_then_conflicts(self):
        response = self.client.post(
            '/api-token-auth/',
            {'username': 'view_user', 'password': 'secret'},
        )
        assert response.status_code == status.HTTP_200_OK
        raw = response.data['token']
        assert len(raw) == 40

        stored = Token.objects.get(user=self.user)
        assert stored.key == hash_key(raw)

        second = self.client.post(
            '/api-token-auth/',
            {'username': 'view_user', 'password': 'secret'},
        )
        assert second.status_code == status.HTTP_409_CONFLICT
        assert stored.key not in str(second.content)
        assert raw not in str(second.content)

        user, _ = TokenAuthentication().authenticate_credentials(raw)
        assert user == self.user

    def test_bad_credentials_behave_as_before(self):
        response = self.client.post(
            '/api-token-auth/',
            {'username': 'view_user', 'password': 'wrong'},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST


@override_settings(REST_FRAMEWORK=DIGEST_SETTING)
class DigestAdminTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='admin_user')
        self.token = Token.objects.create(user=self.user)
        self.admin = TokenAdmin(TokenProxy, site)

    def test_changelist_redacts_digests_and_shows_legacy_keys(self):
        assert self.admin.key_display(self.token) == REDACTED_TOKEN

        legacy_user = User.objects.create_user(username='legacy_admin_user')
        legacy, raw = make_legacy_token(legacy_user)
        assert self.admin.key_display(legacy) == raw

    def test_add_form_delivers_plaintext_once_via_message(self):
        class FakeMessages:
            def __init__(self):
                self.items = []

            def add(self, level, message, extra_tags=''):
                self.items.append(message)

            def __iter__(self):
                return iter(self.items)

        request = RequestFactory().post('/')
        request._messages = FakeMessages()
        new_user = User.objects.create_user(username='new_admin_user')
        obj = TokenProxy(user=new_user)

        self.admin.save_model(request, obj, form=None, change=False)
        raw = obj.raw_key
        assert is_digest(obj.key)

        shown = list(request._messages)
        assert any(raw in message for message in shown)
        assert all(obj.key not in message for message in shown)


@override_settings(REST_FRAMEWORK=DIGEST_SETTING)
class CustomTokenModelFallbackTests(TestCase):
    def test_custom_model_without_digest_lookup_keeps_direct_match(self):
        from tests.authentication.models import CustomToken

        class CustomTokenAuthentication(TokenAuthentication):
            model = CustomToken

        user = User.objects.create_user(username='custom_user')
        token = CustomToken.objects.create(key='c' * 40, user=user)
        backend = CustomTokenAuthentication()

        found_user, found_token = backend.authenticate_credentials(token.key)
        assert found_user == user
        assert found_token == token
        with pytest.raises(AuthenticationFailed):
            backend.authenticate_credentials('z' * 40)


@override_settings(REST_FRAMEWORK=DIGEST_SETTING)
class DigestCommandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='cmd_user')

    def test_create_command_prints_plaintext_but_never_the_digest(self):
        out = io.StringIO()
        call_command('drf_create_token', 'cmd_user', stdout=out)
        stored = Token.objects.get(user=self.user)
        assert is_digest(stored.key)
        assert stored.key not in out.getvalue()
        assert DIGEST_PREFIX not in out.getvalue()

    def test_second_create_hints_at_reset_instead_of_returning_digest(self):
        first_out = io.StringIO()
        call_command('drf_create_token', 'cmd_user', stdout=first_out)
        stored = Token.objects.get(user=self.user)

        out = io.StringIO()
        call_command('drf_create_token', 'cmd_user', stdout=out)
        assert '--reset' in out.getvalue()
        assert stored.key not in out.getvalue()

    def test_reset_generates_new_random_credential(self):
        call_command('drf_create_token', 'cmd_user')
        first_digest = Token.objects.get(user=self.user).key

        out = io.StringIO()
        call_command('drf_create_token', 'cmd_user', '--reset', stdout=out)
        second = Token.objects.get(user=self.user)
        assert second.key != first_digest
        assert second.key not in out.getvalue()
        assert is_digest(second.key)
