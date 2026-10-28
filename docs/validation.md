# Validation of the initial release

Checked on 2026-10-28 in the development workspace:

- 235 automated tests pass. They cover the Meta HTTP contract using mocked responses,
  image/video preparation, signed delivery, authentication before upload parsing,
  OAuth state, encrypted credentials, scoped lifecycle callbacks, draft revisions,
  queue claims, token refresh, shutdown, and uncertain publication recovery.
- Ruff checks pass and `pip check` reports no broken requirements.
- The Docker image builds with the committed dependency constraints.
- Docker Compose starts the API as healthy and the worker reports a current heartbeat.
- A local end-to-end run uploaded the supplied small-wins PNG, retrieved the normalized
  JPEG through its signed preview, created/reviewed/approved a draft, and completed it
  as `DEMO_PUBLISHED` with no Instagram media ID.
- FFmpeg/FFprobe validation was checked with an actual 3-second H.264 fast-start MP4.

No live Instagram account was connected, no Meta credentials were supplied, and no
post was sent to Instagram. Successful tests establish local behavior and request
construction; they do not establish approval or availability for a particular Meta
app/account. Complete the setup in [Meta setup](meta-setup.md), then exercise one
explicitly reviewed post on the intended account before relying on scheduled publishing.

The current test environment emits one dependency deprecation warning about
Starlette's HTTPX test transport. It does not fail the tests or affect the production
HTTP client. Versions are pinned in `requirements.lock` for reproducibility.

This release targets one owner and one persistent host. Schema migrations and a
multi-user dashboard are not included. Backups must retain the encryption key and
database together; see [operations](operations.md).
