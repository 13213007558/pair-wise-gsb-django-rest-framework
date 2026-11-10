from rest_framework import parsers, renderers
from rest_framework.authtoken.exceptions import TokenAlreadyIssued
from rest_framework.authtoken.models import Token
from rest_framework.authtoken.serializers import AuthTokenSerializer
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.views import APIView


class ObtainAuthToken(APIView):
    throttle_classes = ()
    permission_classes = ()
    parser_classes = (parsers.FormParser, parsers.MultiPartParser, parsers.JSONParser,)
    renderer_classes = (renderers.JSONRenderer,)
    serializer_class = AuthTokenSerializer

    def get_serializer_context(self):
        return {
            'request': self.request,
            'format': self.format_kwarg,
            'view': self
        }

    def get_serializer(self, *args, **kwargs):
        kwargs['context'] = self.get_serializer_context()
        return self.serializer_class(*args, **kwargs)

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data['user']
        if api_settings.AUTHTOKEN_DIGEST_ENABLED:
            return Response({'token': self._issue_digest_token(user)})
        token, created = Token.objects.get_or_create(user=user)
        return Response({'token': token.key})

    def _issue_digest_token(self, user):
        existing = Token.objects.filter(user=user).first()
        if existing is not None:
            # Never hand back a stored digest in place of a client key,
            # and never silently invalidate a token the client may still
            # hold: reissue is an explicit, separate action.
            raise TokenAlreadyIssued()
        token = Token.objects.create(user=user)
        return token.raw_key


obtain_auth_token = ObtainAuthToken.as_view()
