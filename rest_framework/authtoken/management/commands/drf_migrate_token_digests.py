from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from rest_framework.authtoken.digests import hash_key, is_digest
from rest_framework.authtoken.models import Token
from rest_framework.settings import api_settings


class Command(BaseCommand):
    help = (
        'Migrate legacy plaintext token keys to digest storage. '
        'The plaintext keys are deleted from the database and existing '
        'clients keep authenticating with the same keys, which are now '
        'matched against their digests.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report how many tokens would be migrated without writing.',
        )
        parser.add_argument(
            '--database',
            default='default',
            help='Nominates a database to migrate. Defaults to "default".',
        )

    def handle(self, *args, **options):
        if not api_settings.AUTHTOKEN_DIGEST_ENABLED:
            raise CommandError(
                'Digest storage is disabled. Set '
                'REST_FRAMEWORK["AUTHTOKEN_DIGEST_ENABLED"] = True and '
                'deploy the accompanying migration before running this '
                'command.'
            )

        using = options['database']
        manager = Token.objects.using(using)
        pending = [token for token in manager.iterator() if not is_digest(token.key)]
        already = manager.count() - len(pending)

        if options['dry_run']:
            self.stdout.write(
                f'{len(pending)} token(s) would be migrated to digests; '
                f'{already} already stored as digest.'
            )
            return

        if not pending:
            self.stdout.write('No plaintext tokens to migrate.')
            return

        # One atomic batch: any failure rolls every row back, so the
        # table can never be left with an unexplained mix where some
        # clients silently lost their credentials.
        with transaction.atomic(using=using):
            for token in pending:
                digest = hash_key(token.key)
                # `key` is the primary key: Model.save() would treat the
                # changed value as a new row and INSERT, colliding with
                # the existing one. An UPDATE against the old pk rewrites
                # the column in place instead.
                manager.filter(pk=token.pk).update(key=digest)

        self.stdout.write(
            self.style.SUCCESS(
                f'Migrated {len(pending)} token(s) to digest storage. '
                'Plaintext keys can no longer be recovered from the '
                'database.'
            )
        )
