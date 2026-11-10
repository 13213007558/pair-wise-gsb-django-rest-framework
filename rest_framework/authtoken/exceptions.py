from django.utils.translation import gettext_lazy as _

from rest_framework import status
from rest_framework.exceptions import APIException


class TokenAlreadyIssued(APIException):
    """
    Raised when a token is requested in digest mode for a user that
    already has one. The existing plaintext key can only have been
    delivered once and cannot be recovered, so the caller must
    explicitly revoke and reissue the token instead.
    """
    status_code = status.HTTP_409_CONFLICT
    default_detail = _(
        'A token already exists for this user and its key cannot be '
        'retrieved. Revoke or reset the token to be issued a new one.'
    )
    default_code = 'token_already_issued'
