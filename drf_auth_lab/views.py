from django.http import JsonResponse
from django.middleware.csrf import get_token
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.response import Response
from rest_framework.views import APIView

BUSINESS_CALLS = {"count": 0}


def csrf_view(request):
    # Plain Django view: forces a CSRF cookie to be issued and returns the token.
    return JsonResponse({"csrfToken": get_token(request)})


class EchoView(APIView):
    """Reports which authenticator won; increments a business counter."""

    def _report(self, request):
        BUSINESS_CALLS["count"] += 1
        authenticator = request.successful_authenticator
        return Response({
            "user": str(request.user),
            "auth": repr(request.auth),
            "authenticator": type(authenticator).__name__ if authenticator else None,
            "business_calls": BUSINESS_CALLS["count"],
        })

    def get(self, request):
        return self._report(request)

    def post(self, request):
        return self._report(request)


class SessionFirstView(EchoView):
    authentication_classes = [SessionAuthentication, TokenAuthentication]


class TokenFirstView(EchoView):
    authentication_classes = [TokenAuthentication, SessionAuthentication]
