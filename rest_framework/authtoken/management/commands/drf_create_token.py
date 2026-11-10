from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from rest_framework.authtoken.models import Token
from rest_framework.settings import api_settings

UserModel = get_user_model()


class Command(BaseCommand):
    help = 'Create DRF Token for a given user'

    def create_user_token(self, username, reset_token):
        user = UserModel._default_manager.get_by_natural_key(username)

        if reset_token:
            Token.objects.filter(user=user).delete()

        token = Token.objects.get_or_create(user=user)
        return token[0]

    def add_arguments(self, parser):
        parser.add_argument('username', type=str)

        parser.add_argument(
            '-r',
            '--reset',
            action='store_true',
            dest='reset_token',
            default=False,
            help='Reset existing User token and create a new one',
        )

    def handle(self, *args, **options):
        username = options['username']
        reset_token = options['reset_token']

        try:
            token = self.create_user_token(username, reset_token)
        except UserModel.DoesNotExist:
            raise CommandError(
                'Cannot create the Token: user {} does not exist'.format(
                    username)
            )
        if api_settings.AUTHTOKEN_DIGEST_ENABLED:
            if token.raw_key is None:
                # The stored value is a digest and must never be printed
                # as if it were a usable client credential.
                self.stdout.write(
                    f'User {username} already has a token; its key is not '
                    'retrievable. Use --reset to issue a new one.'
                )
            else:
                self.stdout.write(
                    f'Generated token {token.raw_key} for user {username}. '
                    'Store it now: it cannot be retrieved again.'
                )
        else:
            self.stdout.write(
                f'Generated token {token.key} for user {username}')
