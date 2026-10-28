# Maple Baby Publisher

A private Instagram publishing API for Maple Baby. Upload media, create a draft,
review its exact revision, then approve it for immediate or scheduled publishing.
The durable worker handles Meta's container workflow and records the outcome.

**Starts in demo mode.** Approved demo jobs become `DEMO_PUBLISHED` without calling
Meta's publishing API. A real account connection is optional for this local workflow.
OAuth connection actions, when configured, still contact Meta.

## What's included

- Instagram Login, encrypted account tokens, automatic token refresh, and verified
  deauthorization/data-deletion callbacks.
- Single-image feed posts, image carousels of 2–10 items, Reels with optional covers,
  and image/video Stories. Single-image posts support alt text.
- JPEG/PNG/WebP image preparation into metadata-stripped JPEGs. MP4 validation requires
  H.264 video, AAC audio when present, fast-start layout, and at most 100 MB; videos
  are validated rather than silently transcoded.
- Revision-based review/approval, durable scheduling, cancellation, audit events, and
  conservative recovery. Uncertain publish results require reconciliation and are
  never automatically submitted again.
- Bearer-key administration, expiring signed media URLs, an interactive `/docs` API
  reference, a command-line client, Docker packaging, and offline tests.

This is an API service with interactive documentation, not a separate social-media
dashboard. Profile-picture changes and interactive Story stickers are outside its
scope. A Reel requires a video; a cover image alone cannot become a Reel.

## Run locally

Use Python 3.12 and install FFmpeg/`ffprobe` if you will upload videos. Run commands
from the repository root:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -c requirements.lock -e '.[dev]'
python scripts/bootstrap-env.py
uvicorn maple_publisher.app:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

The bootstrap creates a private, ignored `.env` with random keys and demo mode enabled.
It refuses to overwrite an existing file. Keep `--no-access-log`: default request logs
can expose OAuth codes and signed-media credentials in query strings.

In a second terminal:

```sh
. .venv/bin/activate
python -m maple_publisher.worker
```

Open <http://127.0.0.1:8000/docs>. Read `ADMIN_API_KEY` privately in your editor and use
**Authorize** to enter it as the Bearer credential. Do not print the environment file
into logs or copy its secrets into an issue. The CLI reads the key from local settings.

## Try the small-wins post

With the API running, this example uploads the supplied artwork and creates a
**draft only**. It does not approve, schedule, or publish:

```sh
python examples/demo.py --base-url http://localhost:8000
```

Review the resulting post in `/docs`, then approve its current revision to try the
demo worker. Confirm it finishes as `DEMO_PUBLISHED`. The examples include the feed
and Story artwork under `examples/assets/`, plus `examples/small-wins-caption.txt`.

The CLI can perform the same steps. Replace `POST_ID` and `N` with the reviewed values:

```sh
python -m maple_publisher.cli status
python -m maple_publisher.cli upload examples/assets/small-wins-feed.png --kind image
python -m maple_publisher.cli draft path/to/draft.json
python -m maple_publisher.cli review POST_ID
python -m maple_publisher.cli approve POST_ID --revision N
python -m maple_publisher.cli posts
```

Other commands include `accounts`, `connect`, `cancel`, `retry`, `refresh`, and `events`.
Use `--help` for arguments. `--base-url` overrides the configured `PUBLIC_BASE_URL`.
Remote CLI clients only need `ADMIN_API_KEY` and `PUBLIC_BASE_URL`; keep Meta secrets
and the token encryption key on the server.
Upload kinds are `image`, `story`, `cover`, and `reel`. Draft request bodies follow the
`PostDraft` schema in `/docs`; scheduled timestamps must include a timezone.

## Connect Instagram for real publishing

The implementation is ready for account setup; building it does not create a Meta app,
grant permissions, or complete Instagram login. To go live you still need:

1. An Instagram professional account; use **Business** for this release's Story flow.
2. A Meta app configured for Instagram Login with the basic and publishing permissions.
3. Public HTTPS hosting with persistent storage and a continuously running worker.
4. Your app ID/secret, configured OAuth/lifecycle callbacks, and the account owner's login.

Follow [Meta setup](docs/meta-setup.md) for exact permissions, callback URLs, access
levels, and token handling. Start the live deployment with its own database/volume,
then explicitly review and approve its first post. Approvals are bound to both the
draft revision and demo/live mode, so changing mode does not reuse a demo approval.

## Docker and deployment

```sh
# Create .env first if it does not already exist.
python scripts/bootstrap-env.py
docker compose up --build -d
python scripts/smoke.py
```

Compose runs the API and worker as non-root processes with shared persistent `/data`
storage. It binds to `127.0.0.1:8000`; configure your own HTTPS reverse proxy for a live
deployment. See [deployment](docs/deployment.md) for proxy, secrets, and storage setup.
SQLite is for a single host and one worker. This initial release creates its schema
on startup and does not yet provide versioned database migrations.

## Verification and operations

```sh
python -m ruff check .
python -m pytest
python scripts/smoke.py --url http://127.0.0.1:8000
```

Tests mock Meta; CI does not connect an account or publish. The smoke check is read-only.
Read [operations](docs/operations.md) for backups, token maintenance, graceful shutdown,
and uncertain outcomes. The authenticated remote-status and reconciliation endpoints
check an existing container; reconciliation never repeats a publish request.
