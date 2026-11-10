import secrets

from django.conf import settings
from django.db import models, router
from django.utils.translation import gettext_lazy as _

from rest_framework.authtoken.digests import (
    hash_key, is_digest, KEY_FIELD_MAX_LENGTH
)
from rest_framework.settings import api_settings

REDACTED_TOKEN = '[redacted]'


class TokenQuerySet(models.QuerySet):
    def get_by_raw_key(self, key):
        """
        Return the token matching a plaintext key supplied by a client,
        looking it up through its stored digest in digest mode and by
        direct comparison in legacy mode.
        """
        if api_settings.AUTHTOKEN_DIGEST_ENABLED:
            try:
                token = self.get(key=hash_key(key))
            except self.model.DoesNotExist:
                # Tokens created before digest mode was enabled may still
                # be stored as plaintext. Compare in constant time so a
                # fallback row cannot be distinguished by timing.
                token = self._get_by_legacy_key(key)
            return token
        return self.get(key=key)

    def _get_by_legacy_key(self, key):
        for token in self.iterator():
            if not is_digest(token.key) and secrets.compare_digest(token.key, key):
                return token
        raise self.model.DoesNotExist


class Token(models.Model):
    """
    The default authorization token model.

    Two storage modes are supported, selected by the
    ``AUTHTOKEN_DIGEST_ENABLED`` API setting:

    * Legacy mode (default): the plaintext key is stored in `key`, as in
      earlier releases.
    * Digest mode: `key` holds a prefixed SHA-256 digest and the
      plaintext key is only available on the in-memory instance, via
      `raw_key`, immediately after creation. It can never be read back
      from the database.
    """
    objects = TokenQuerySet.as_manager()

    key = models.CharField(
        _("Key"), max_length=KEY_FIELD_MAX_LENGTH, primary_key=True
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, related_name='auth_token',
        on_delete=models.CASCADE, verbose_name=_("User")
    )
    created = models.DateTimeField(_("Created"), auto_now_add=True)

    class Meta:
        # Work around for a bug in Django:
        # https://code.djangoproject.com/ticket/19422
        #
        # Also see corresponding ticket:
        # https://github.com/encode/django-rest-framework/issues/705
        abstract = 'rest_framework.authtoken' not in settings.INSTALLED_APPS
        verbose_name = _("Token")
        verbose_name_plural = _("Tokens")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Holds the plaintext key between generation/assignment and the
        # moment the row is written. Never persisted, never reloaded.
        self._raw_key = None

    def save(self, *args, **kwargs):
        """
        Save the token instance.

        If no key is provided, generates a cryptographically secure key.
        In digest mode the plaintext key is retained on the instance and
        only its digest is written to the database. For new tokens, an
        INSERT is forced so existing tokens can never be overwritten.
        """
        if not self.key:
            self.key = self.generate_key()
            if self._state.adding:
                kwargs['force_insert'] = True
        if api_settings.AUTHTOKEN_DIGEST_ENABLED and not self.is_hashed:
            self._store_digest()
        return super().save(*args, **kwargs)

    def _store_digest(self):
        self._raw_key = self.key
        self.key = hash_key(self.key)

    @classmethod
    def generate_key(cls):
        return secrets.token_hex(20)

    @property
    def is_hashed(self):
        """
        True when this token's database row holds a digest rather than a
        plaintext key.
        """
        return is_digest(self.key)

    @property
    def raw_key(self):
        """
        The plaintext key, available once at creation in digest mode.
        Returns None once the instance has been reloaded from the
        database, or for tokens that were never given a plaintext key.
        """
        return self._raw_key

    def refresh_from_db(self, *args, **kwargs):
        super().refresh_from_db(*args, **kwargs)
        # The database only contains the digest after a reload; the
        # one-time plaintext must not survive it.
        self._raw_key = None

    def __str__(self):
        if is_digest(self.key):
            return REDACTED_TOKEN
        return self.key


class TokenProxyQuerySet(TokenQuerySet):
    """
    Deletes through the concrete `Token`: the collector deletes by `pk`,
    which the proxy maps to the user id.
    """
    def delete(self):
        self._not_support_combined_queries('delete')
        if self.query.is_sliced:
            raise TypeError("Cannot use 'limit' or 'offset' with delete().")
        if self.query.distinct or self.query.distinct_fields:
            raise TypeError("Cannot call delete() after .distinct().")
        if self._fields is not None:
            raise TypeError(
                "Cannot call delete() after .values() or .values_list()"
            )
        # Ensure `self.db` resolves with the write routers, like the `QuerySet.delete()`
        self._for_write = True
        keys = list(self.values_list('pk', flat=True))
        return Token.objects.using(self.db).filter(pk__in=keys).delete()
    delete.alters_data = True
    delete.queryset_only = True


class TokenProxy(Token):
    """
    Proxy mapping pk to user pk for use in admin.
    """
    objects = TokenProxyQuerySet.as_manager()

    @property
    def pk(self):
        return self.user_id

    def delete(self, using=None, keep_parents=False):
        """
        Deletes through the concrete `Token`. The inherited delete matches
        the `key` column against `pk` -- a user id here -- and silently
        deletes nothing.
        """
        if self.key is None:
            raise ValueError(
                "%s object can't be deleted because its %s attribute is set "
                "to None." % (self._meta.object_name, self._meta.pk.attname)
            )
        # Resolve `using` in same order as in the base `Model.delete()`
        using = using or router.db_for_write(self.__class__, instance=self)
        deleted = type(self).objects.using(using).filter(key=self.key).delete()
        # `Model.delete()` clears the pk of the in-memory instance
        self.key = None
        return deleted

    class Meta:
        proxy = 'rest_framework.authtoken' in settings.INSTALLED_APPS
        abstract = 'rest_framework.authtoken' not in settings.INSTALLED_APPS
        verbose_name = _("Token")
        verbose_name_plural = _("Tokens")
