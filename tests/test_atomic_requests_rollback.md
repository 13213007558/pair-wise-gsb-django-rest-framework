# Request transaction rollback after DRF converts an exception

Evidence module: `tests/test_atomic_requests_rollback.py`
Run: `python -m pytest tests/test_atomic_requests_rollback.py -v`

## Setup

Two SQLite databases are hit by the same view code in every request:

| alias | `ATOMIC_REQUESTS` | per-request behaviour |
| --- | --- | --- |
| `default` | `True` | Django wraps the view in an outer `atomic()` block |
| `secondary` | `False` | autocommit; no request transaction is ever opened |

Persistence is always re-queried **outside** of any atomic block
(`persisted_count()` asserts `connection.in_atomic_block is False` first), so the
numbers below are committed rows, not a snapshot or a connection flag. Status
codes are reported but are not treated as rollback evidence.

## The chain under test

1. The view writes a row, then raises (e.g. `rest_framework.exceptions.PermissionDenied`).
2. `APIView.dispatch` catches it and calls `handle_exception`.
3. The default `rest_framework.views.exception_handler` converts an
   `APIException` (Django's `PermissionDenied`/`Http404` are coerced first) into
   a `Response` and calls `set_rollback()` before returning it.
4. `set_rollback()` only flags connections that have `ATOMIC_REQUESTS=True`
   **and** are currently inside an atomic block; everything else is left alone.
5. On response exit the request's atomic block rolls back a flagged connection
   (or rolls back to the savepoint for nested blocks) and commits otherwise.
6. Exceptions the handler does not recognise return `None`; DRF re-raises them
   and Django's outer request block performs the rollback itself.

## Scenario results

| # | Scenario (short) | Response / flow | `default` (atomic) | `secondary` (non-atomic) | Who owns the rollback |
| --- | --- | --- | --- | --- | --- |
| 1 | DRF `PermissionDenied` | HTTP 403 | row rolled back (0) | row kept (1) | framework, via `set_rollback()` |
| 2 | Django core `PermissionDenied` | coerced, HTTP 403 | row rolled back (0) | row kept (1) | framework, via `set_rollback()` |
| 3 | `APIException` | HTTP 500 | row rolled back (0) | row kept (1) | framework, via `set_rollback()` |
| 4 | plain `RuntimeError`, not an `APIException` | re-raised | row rolled back (0) | row kept (1) | Django outer request block |
| 5 | nested `atomic()`, inner `PermissionDenied` caught in view | HTTP 200 | savepoint rolled back; outer rows kept (2) | not written | inner `atomic()` block only |
| 6 | nested `atomic()` on both DBs, inner `APIException` caught | HTTP 200 | inner row rolled back, `before/after` kept (2) | same block semantics (2) | inner `atomic()` blocks |
| 7 | custom exception handler returns a `Response` | HTTP 200 | row kept (1) | row kept (1) | nobody — application chose to answer |
| 8 | custom handler returns `None` (unknown error) | re-raised | row rolled back (0) | row kept (1) | Django outer request block |
| 9 | `@non_atomic_requests` view raising `PermissionDenied` | HTTP 403 | no transaction opened; row kept (1) | not written | nobody; non-atomic semantics preserved |
| 10 | handled 403 followed by a normal request | 403 then 200 | second request commits cleanly (1) | second request commits (2) | rollback flag is per-request, does not leak |
| 11 | `set_rollback()` with both DBs in a block | — | only the atomic DB is flagged | never flagged | scoping guard |
| 12 | `set_rollback()` outside any block | — | no-op, no error | no-op, no error | non-atomic path untouched |
| 13 | default `exception_handler` on `PermissionDenied` | HTTP 403 | rollback flagged | not flagged | shared entry `set_rollback()` |
| 14 | default `exception_handler` on unknown `RuntimeError` | returns `None`, re-raised | not flagged here | not flagged | Django outer request block |

Rows counts are per test; each `TransactionTestCase` starts from empty tables.

## Conclusions

- **Framework promise vs. application choice are distinct.** A converted
  `APIException`/`PermissionDenied` rolls back exactly the databases that both
  opted into `ATOMIC_REQUESTS` and hold an open request transaction. A custom
  handler that deliberately answers with a normal `Response` without calling
  `set_rollback()` keeps the changes — the framework does not second-guess it.
- **There is no unconditional rollback.** `set_rollback()`
  (`rest_framework/views.py`) iterates initialised connections and skips any
  that are non-atomic or not in a block; non-atomic requests are never wrapped
  in a transaction.
- **The default processing chain has no missing rollback on this baseline.**
  Handled `APIException`s are rolled back by `set_rollback()`; unhandled
  exceptions are re-raised so Django's request block rolls them back. No source
  change was required, so the shared exception entry
  (`rest_framework/views.exception_handler`) and existing non-atomic request
  semantics were left unchanged.
- **No cross-request pollution.** The rollback marker lives on the connection
  only for the duration of the failing request's atomic block; a later normal
  request starts in autocommit state and commits normally (scenario 10).

## Mutation check

Making `set_rollback()` a no-op fails precisely scenarios 1, 2, 3, 10 and 11 —
the framework-promised rollback paths — while scenarios 4, 7 and 8 (Django-owned
or application-owned behaviour) remain green. This demonstrates the tests
actually pin the rollback responsibility rather than the status codes.
