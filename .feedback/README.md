# Feedback task starting environment

Upstream: https://github.com/encode/django-rest-framework
Upstream commit: `9aba6856f6d2174098d5adcf52d0e20a9a16bf56`
Prepared on macOS with CPython 3.12.11. No external database, queue, or cloud account is required.
The dependency file pins the installed runtime and test packages for the selected baseline. It is not a claim that all optional upstream test suites are installed.

From the repository root:

```sh
export NO_INTERNET=1 TZ=UTC
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r .feedback/requirements.txt -e .
.venv/bin/python -m pytest -q -o addopts= tests/authentication/test_authentication.py tests/test_permissions.py tests/test_request.py tests/test_parsers.py
```

Scope note: The scope is the command below, not all optional upstream integrations.

Observed baseline (2026-11-10): `160 passed, 10 skipped, 37 subtests passed in 0.58s`
Whole-command wall time: 0.869 seconds, exit code 0. This is one local measurement, not a runtime guarantee. The first install is excluded.

This seed adds preparation metadata only. Application sources and upstream tests are unchanged; there is no task solution in this commit.
