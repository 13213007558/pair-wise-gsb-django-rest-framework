"""
End-to-end evidence for what happens to a request's database changes once DRF
turns an exception into a response.

Two SQLite databases are exercised together so the framework's promise and the
application's own choices can be told apart:

* ``default``   runs with ``ATOMIC_REQUESTS=True``  -> Django wraps each view
  request in an outer ``atomic`` block.
* ``secondary`` runs with ``ATOMIC_REQUESTS=False`` -> the very same view code
  runs there without any request transaction.

Every scenario re-queries the *persisted* rows from outside of any atomic block
(autocommit state). Status codes and connection flags alone are not accepted as
proof. The expected outcomes are summarised at the bottom of this module in
``EVIDENCE``.
"""
import unittest

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.db import connection, connections, transaction
from django.test import TransactionTestCase, override_settings
from django.urls import path

from rest_framework import status
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from tests.models import BasicModel

ATOMIC_DB = 'default'
NON_ATOMIC_DB = 'secondary'


def write_row(alias, tag):
    BasicModel.objects.using(alias).create(text=tag)


def write_both(tag):
    write_row(ATOMIC_DB, tag)
    write_row(NON_ATOMIC_DB, tag)


def persisted_count(alias):
    """
    Count rows through autocommit, after the request's atomic blocks have all
    closed. Asserting ``in_atomic_block is False`` first guarantees we are
    reading committed state rather than a snapshot held open by a transaction.
    """
    assert connections[alias].in_atomic_block is False
    return BasicModel.objects.using(alias).count()


class AtomicRequestConfigMixin:
    """
    Flip ``ATOMIC_REQUESTS`` on the live connection settings for one test only.

    ``override_settings(DATABASES=...)`` is not used because the connection
    handler caches the resolved settings; mutating the resolved dict is the same
    mechanism Django itself reads in ``Handler.make_view_atomic``. The flags are
    restored in ``tearDown`` so the non-atomic request semantics used elsewhere
    in the test suite are left untouched.
    """

    def setUp(self):
        super().setUp()
        connections.databases[ATOMIC_DB]['ATOMIC_REQUESTS'] = True
        connections.databases[NON_ATOMIC_DB]['ATOMIC_REQUESTS'] = False

    def tearDown(self):
        connections.databases[ATOMIC_DB]['ATOMIC_REQUESTS'] = False
        connections.databases[NON_ATOMIC_DB]['ATOMIC_REQUESTS'] = False
        super().tearDown()


# --- Views: each one performs a write before raising / returning -----------


class PermissionDeniedView(APIView):
    permission_classes = []

    def get(self, request):
        write_both('permission-denied')
        # Mirrors a protected endpoint that records a tentative change before
        # its permission check fails.
        raise PermissionDenied('you shall not pass')


class DjangoPermissionDeniedView(APIView):
    permission_classes = []

    def get(self, request):
        write_both('django-permission-denied')
        raise DjangoPermissionDenied('core denied')


class APIExceptionView(APIView):
    permission_classes = []

    def get(self, request):
        write_both('api-exception')
        raise APIException('api boom')


class GenericExceptionView(APIView):
    permission_classes = []

    def get(self, request):
        write_both('generic-exception')
        raise RuntimeError('plain boom')


class NestedAtomicCaughtView(APIView):
    """An inner savepoint rolls back while outer request writes survive."""

    permission_classes = []

    def get(self, request):
        write_row(ATOMIC_DB, 'outer-before')
        try:
            with transaction.atomic(using=ATOMIC_DB):
                write_row(ATOMIC_DB, 'inner')
                raise PermissionDenied('inner denied')
        except PermissionDenied:
            # Application swallows the error and returns a normal response.
            pass
        write_row(ATOMIC_DB, 'outer-after')
        return Response({'handled': True})


class NestedAtomicBothDatabasesView(APIView):
    permission_classes = []

    def get(self, request):
        write_both('before')
        try:
            with (
                transaction.atomic(using=ATOMIC_DB),
                transaction.atomic(using=NON_ATOMIC_DB),
            ):
                write_both('inner')
                raise APIException('inner api error')
        except APIException:
            pass
        write_both('after')
        return Response({'handled': True})


def swallowing_exception_handler(exc, context):
    # Deliberately does NOT call ``set_rollback``: the application takes full
    # ownership of the exception and answers with a regular response.
    if isinstance(exc, RuntimeError):
        return Response({'detail': 'swallowed'}, status=status.HTTP_200_OK)
    return None


class CustomHandlerResponseView(APIView):
    permission_classes = []

    def get_exception_handler(self):
        return swallowing_exception_handler

    def get(self, request):
        write_both('custom-handled')
        raise RuntimeError('swallowed by custom handler')


class CustomHandlerPropagateView(APIView):
    permission_classes = []

    def get_exception_handler(self):
        return swallowing_exception_handler

    def get(self, request):
        write_both('custom-propagated')
        # The custom handler only knows RuntimeError, so it returns None and
        # DRF re-raises.
        raise ValueError('unknown to custom handler')


class NonAtomicDispatchView(APIView):
    """Opt the atomic database out of ATOMIC_REQUESTS for this view."""

    permission_classes = []

    @transaction.non_atomic_requests(using=ATOMIC_DB)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def get(self, request):
        write_row(ATOMIC_DB, 'non-atomic-view')
        raise PermissionDenied('denied without request transaction')


class OkView(APIView):
    permission_classes = []

    def get(self, request):
        write_both('ok')
        return Response({'ok': True})


urlpatterns = (
    path('permission-denied/', PermissionDeniedView.as_view()),
    path('django-permission-denied/', DjangoPermissionDeniedView.as_view()),
    path('api-exception/', APIExceptionView.as_view()),
    path('generic-exception/', GenericExceptionView.as_view()),
    path('nested-caught/', NestedAtomicCaughtView.as_view()),
    path('nested-both/', NestedAtomicBothDatabasesView.as_view()),
    path('custom-handled/', CustomHandlerResponseView.as_view()),
    path('custom-propagated/', CustomHandlerPropagateView.as_view()),
    path('non-atomic-dispatch/', NonAtomicDispatchView.as_view()),
    path('ok/', OkView.as_view()),
)


@unittest.skipUnless(
    connection.features.uses_savepoints,
    "'atomic' requires transactions and savepoints.",
)
@override_settings(ROOT_URLCONF='tests.test_atomic_requests_rollback')
class RequestTransactionRollbackTests(AtomicRequestConfigMixin, TransactionTestCase):
    databases = '__all__'

    def test_drf_permission_denied_rolls_back_atomic_db_only(self):
        response = self.client.get('/permission-denied/')

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        # Framework promise: set_rollback() inside the default exception
        # handler rolls the request transaction back on the atomic database.
        self.assertEqual(persisted_count(ATOMIC_DB), 0)
        # Non-atomic database has no request transaction to roll back.
        self.assertEqual(persisted_count(NON_ATOMIC_DB), 1)

    def test_django_permission_denied_is_coerced_and_rolled_back(self):
        response = self.client.get('/django-permission-denied/')

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(persisted_count(ATOMIC_DB), 0)
        self.assertEqual(persisted_count(NON_ATOMIC_DB), 1)

    def test_api_exception_rolls_back_atomic_db_only(self):
        response = self.client.get('/api-exception/')

        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(persisted_count(ATOMIC_DB), 0)
        self.assertEqual(persisted_count(NON_ATOMIC_DB), 1)

    def test_generic_exception_is_rolled_back_by_django_outer_block(self):
        # DRF does not convert this into a response; it re-raises and Django's
        # request atomic block performs the rollback.
        with self.assertRaises(RuntimeError):
            self.client.get('/generic-exception/')

        self.assertEqual(persisted_count(ATOMIC_DB), 0)
        # Autocommit connection: the write was committed before the raise.
        self.assertEqual(persisted_count(NON_ATOMIC_DB), 1)

    def test_nested_atomic_rolls_back_savepoint_but_keeps_outer_writes(self):
        response = self.client.get('/nested-caught/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # 'outer-before' and 'outer-after' survive; 'inner' is rolled back to
        # the savepoint, and the swallowed exception must not poison the outer
        # request transaction (no stray needs_rollback flag).
        self.assertEqual(persisted_count(ATOMIC_DB), 2)
        self.assertEqual(
            set(BasicModel.objects.using(ATOMIC_DB).values_list('text', flat=True)),
            {'outer-before', 'outer-after'},
        )

    def test_nested_atomic_on_both_databases_is_scoped_to_savepoints(self):
        response = self.client.get('/nested-both/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # On each database the 'inner' write is rolled back; 'before' and
        # 'after' remain. On the non-atomic database the application's explicit
        # atomic() is still a real (savepoint-less) nested block.
        self.assertEqual(persisted_count(ATOMIC_DB), 2)
        self.assertEqual(persisted_count(NON_ATOMIC_DB), 2)
        for alias in (ATOMIC_DB, NON_ATOMIC_DB):
            self.assertEqual(
                set(BasicModel.objects.using(alias).values_list('text', flat=True)),
                {'before', 'after'},
            )

    def test_custom_handler_response_does_not_trigger_framework_rollback(self):
        response = self.client.get('/custom-handled/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {'detail': 'swallowed'})
        # The application chose to answer normally and never asked for a
        # rollback: both databases keep their changes.
        self.assertEqual(persisted_count(ATOMIC_DB), 1)
        self.assertEqual(persisted_count(NON_ATOMIC_DB), 1)

    def test_custom_handler_returning_none_falls_back_to_framework_rollback(self):
        with self.assertRaises(ValueError):
            self.client.get('/custom-propagated/')

        self.assertEqual(persisted_count(ATOMIC_DB), 0)
        self.assertEqual(persisted_count(NON_ATOMIC_DB), 1)

    def test_non_atomic_requests_view_is_not_wrapped_in_transaction(self):
        # The view opts out on the atomic database; DRF must not force a
        # transaction around it. The write therefore survives the 403.
        response = self.client.get('/non-atomic-dispatch/')

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(connections[ATOMIC_DB].in_atomic_block)
        self.assertEqual(persisted_count(ATOMIC_DB), 1)

    def test_handled_error_does_not_pollute_later_normal_request(self):
        # A converted 403 marks rollback only for its own request. The next,
        # successful request must start with clean transaction state and commit.
        first = self.client.get('/permission-denied/')
        self.assertEqual(first.status_code, status.HTTP_403_FORBIDDEN)
        # The request block has fully closed and its rollback flag must not
        # leak onto the connection for subsequent requests.
        self.assertFalse(connections[ATOMIC_DB].in_atomic_block)
        self.assertFalse(connections[ATOMIC_DB].needs_rollback)

        second = self.client.get('/ok/')
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertFalse(connections[ATOMIC_DB].in_atomic_block)
        self.assertEqual(persisted_count(ATOMIC_DB), 1)
        self.assertEqual(persisted_count(NON_ATOMIC_DB), 2)


@unittest.skipUnless(
    connection.features.uses_savepoints,
    "'atomic' requires transactions and savepoints.",
)
class SetRollbackScopingTests(AtomicRequestConfigMixin, TransactionTestCase):
    databases = '__all__'

    def test_set_rollback_only_marks_atomic_databases_in_a_block(self):
        from rest_framework.views import set_rollback

        # Both connections are inside an atomic block (e.g. nested application
        # blocks), but only one database has ATOMIC_REQUESTS enabled.
        with (
            transaction.atomic(using=ATOMIC_DB),
            transaction.atomic(using=NON_ATOMIC_DB),
        ):
            set_rollback()
            self.assertTrue(transaction.get_rollback(using=ATOMIC_DB))
            # The non-atomic database must never be marked for rollback.
            self.assertFalse(transaction.get_rollback(using=NON_ATOMIC_DB))

        self.assertFalse(connections[ATOMIC_DB].in_atomic_block)
        self.assertFalse(connections[NON_ATOMIC_DB].in_atomic_block)

    def test_set_rollback_is_a_noop_without_atomic_block(self):
        from rest_framework.views import set_rollback

        # Outside of any atomic block there is nothing to flag and no error may
        # be raised: non-atomic requests keep their autocommit semantics.
        set_rollback()
        self.assertFalse(connections[ATOMIC_DB].in_atomic_block)
        self.assertFalse(connections[NON_ATOMIC_DB].in_atomic_block)


@unittest.skipUnless(
    connection.features.uses_savepoints,
    "'atomic' requires transactions and savepoints.",
)
class SharedExceptionEntryRollbackTests(AtomicRequestConfigMixin, TransactionTestCase):
    databases = '__all__'

    def test_default_handler_marks_rollback_at_the_shared_entry(self):
        # The single shared entry point every DRF API error flows through. If a
        # future change drops set_rollback() from it, handled API exceptions
        # would silently commit on atomic databases.
        from rest_framework.views import exception_handler

        with (
            transaction.atomic(using=ATOMIC_DB),
            transaction.atomic(using=NON_ATOMIC_DB),
        ):
            response = exception_handler(PermissionDenied('x'), {})
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            self.assertTrue(transaction.get_rollback(using=ATOMIC_DB))
            self.assertFalse(transaction.get_rollback(using=NON_ATOMIC_DB))

    def test_default_handler_returns_none_for_unknown_exception(self):
        from rest_framework.views import exception_handler

        # Non-API exceptions must not be converted: DRF re-raises so Django's
        # request block owns the rollback.
        with transaction.atomic(using=ATOMIC_DB):
            self.assertIsNone(exception_handler(RuntimeError('x'), {}))
            self.assertFalse(transaction.get_rollback(using=ATOMIC_DB))


# Human-readable summary of the behaviour proven above. The assertions in the
# tests are the source of truth; this table documents their combined result.
EVIDENCE = (
    ('scenario', 'atomic DB (ATOMIC_REQUESTS=True)', 'non-atomic DB (False)'),
    ('APIException / PermissionDenied -> DRF Response', 'rolled back', 'kept'),
    ('generic exception re-raised by DRF', 'rolled back by Django outer block', 'kept'),
    (
        'nested atomic(), exception caught in view',
        'savepoint rolled back, outer kept', 'same block semantics apply',
    ),
    ('custom handler returns a Response', 'kept (application owns the choice)', 'kept'),
    ('custom handler returns None', 'rolled back via framework path', 'kept'),
    ('non_atomic_requests view', 'no transaction; kept', 'kept'),
    ('later normal request after a handled 403', 'commits cleanly', 'commits cleanly'),
)
