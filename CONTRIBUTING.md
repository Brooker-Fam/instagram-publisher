# Contributing to Instagram Publisher

Follow the local setup in [README.md](README.md). Keep development and tests in demo
mode, and use a separate test account for any explicitly authorized live verification.
Never include `.env` files, access tokens, uploaded private media, or database contents
in a commit or issue. Report errors with credentials and signed URLs removed.

Before opening a pull request, run:

```sh
python -m ruff check .
python -m pytest
```

Add focused tests for behavior changes. Changes to approvals, retries, account
identifiers, callbacks, or media delivery should preserve the service's existing
authentication and duplicate-prevention guarantees. Document database changes and
test the offline migration with existing account and post records. Startup must not
partially upgrade an older schema while the API and worker share it.

Explain the problem, the resulting behavior, and relevant validation in your pull
request. Contributions are made under this project's MIT license.
