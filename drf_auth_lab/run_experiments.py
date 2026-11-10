import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "drf_auth_lab.settings")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import django

django.setup()

from django.core.management import call_command
from django.test import Client

call_command("migrate", run_syncdb=True, verbosity=0)

from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token

from drf_auth_lab.views import BUSINESS_CALLS

User = get_user_model()
User.objects.all().delete()
alice = User.objects.create_user("alice", password="pw")
token = Token.objects.create(user=alice)
TOKEN_KEY = token.key
BAD_KEY = "0" * 40


def fresh_client(login=False, bad_session=False):
    client = Client(enforce_csrf_checks=True)
    if login:
        client.force_login(alice)
    if bad_session:
        client.cookies["sessionid"] = "bogus-session-id"
    return client


def get_csrf(client):
    resp = client.get("/csrf/")
    return resp.json()["csrfToken"]


def run(label, method, path, login=False, bad_session=False,
        token_key=None, with_csrf=False, bearer=False):
    client = fresh_client(login=login, bad_session=bad_session)
    headers = {}
    if token_key is not None:
        headers["HTTP_AUTHORIZATION"] = f"Token {token_key}"
    if bearer:
        headers["HTTP_AUTHORIZATION"] = "Bearer some-opaque-string"
    if with_csrf:
        headers["HTTP_X_CSRFTOKEN"] = get_csrf(client)
    before = BUSINESS_CALLS["count"]
    resp = getattr(client, method)(path, **headers)
    after = BUSINESS_CALLS["count"]
    www = resp.headers.get("WWW-Authenticate", "-")
    try:
        body = resp.json()
    except Exception:
        body = {}
    if resp.status_code == 200:
        detail = (f"user={body.get('user')} auth={body.get('auth')} "
                  f"authenticator={body.get('authenticator')}")
    else:
        detail = f"detail={body.get('detail', resp.content[:60])}"
    print(f"  {label:<38} -> {resp.status_code}  "
          f"WWW-Authenticate={www:<12} biz_calls=+{after - before}  {detail}")


SCENARIOS = [
    # label, method, kwargs
    ("anon GET",                "get",  {}),
    ("valid session GET",       "get",  {"login": True}),
    ("invalid session GET",     "get",  {"bad_session": True}),
    ("valid token GET",         "get",  {"token_key": TOKEN_KEY}),
    ("wrong token GET",         "get",  {"token_key": BAD_KEY}),
    ("session + wrong token GET", "get", {"login": True, "token_key": BAD_KEY}),
    ("session+token POST no CSRF", "post", {"login": True, "token_key": TOKEN_KEY}),
    ("session+token POST +CSRF",   "post", {"login": True, "token_key": TOKEN_KEY, "with_csrf": True}),
    ("token only POST no CSRF",    "post", {"token_key": TOKEN_KEY}),
    ("session only POST no CSRF",  "post", {"login": True}),
    ("session+badtoken POST noCSRF", "post", {"login": True, "token_key": BAD_KEY}),
    ("Bearer-scheme GET (no session)", "get", {"bearer": True}),
    ("session + Bearer-scheme GET", "get", {"login": True, "bearer": True}),
]

for path, title in [("/session-first/", "SessionAuthentication, TokenAuthentication"),
                    ("/token-first/", "TokenAuthentication, SessionAuthentication")]:
    print(f"\n=== authentication_classes = [{title}]  ({path}) ===")
    for label, method, kwargs in SCENARIOS:
        run(label, method, path, **kwargs)
