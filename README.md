# Instagram Publisher

A self-hosted, open-source Instagram publishing API for professional accounts.
Each deployment has one administrator who can connect the accounts they manage.
Upload media, create a draft, review its exact revision, then approve it for immediate
or scheduled publishing. The durable worker handles Meta's container workflow and
records the outcome.

**Starts in demo mode.** Approved demo jobs become `DEMO_PUBLISHED` without calling
Meta's publishing API. A real account connection is optional for this local workflow.
OAuth connection actions, when configured, still contact Meta.

## What's included

- Unified Login dashboard-token import for new Meta apps, with encrypted credentials
  and exact-account selection. Existing Instagram Login connections retain automatic
  token refresh and verified deauthorization/data-deletion callbacks.
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
git clone https://github.com/Brooker-Fam/instagram-publisher.git
cd instagram-publisher
python -m venv .venv
. .venv/bin/activate
python -m pip install -c requirements.lock -e '.[dev]'
python scripts/bootstrap-env.py
uvicorn instagram_publisher.app:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

The bootstrap creates a private, ignored `.env` with random keys and demo mode enabled.
It refuses to overwrite an existing file. Keep `--no-access-log`: default request logs
can expose OAuth codes and signed-media credentials in query strings.

In a second terminal:

```sh
. .venv/bin/activate
python -m instagram_publisher.worker
```

Open <http://127.0.0.1:8000/docs>. Read `ADMIN_API_KEY` privately in your editor and use
**Authorize** to enter it as the Bearer credential. Do not print the environment file
into logs or copy its secrets into an issue. The CLI reads the key from local settings.

## Try a sample post

With the API running, this example uploads `examples/assets/sample-feed.jpg` and creates a
**draft only**. It does not approve, schedule, or publish:

```sh
python examples/demo.py --base-url http://localhost:8000
```

Review the resulting post in `/docs`, then approve its current revision to try the
demo worker. Confirm it finishes as `DEMO_PUBLISHED`. The examples include
`examples/assets/sample-feed.jpg`, `examples/assets/sample-story.jpg`,
`examples/caption.txt`, and a request template in `examples/draft.json`.

The CLI can perform the same steps. After uploading, put the returned asset ID into
`examples/draft.json`. Replace `POST_ID` and `N` with the reviewed values:

```sh
python -m instagram_publisher.cli status
python -m instagram_publisher.cli upload examples/assets/sample-feed.jpg --kind image
python -m instagram_publisher.cli draft examples/draft.json
python -m instagram_publisher.cli review POST_ID
python -m instagram_publisher.cli approve POST_ID --revision N
python -m instagram_publisher.cli posts
```

Other commands include `accounts`, `import-unified`, `connect`, `cancel`, `retry`, `refresh`, and `events`.
Use `--help` for arguments. `--base-url` overrides the configured `PUBLIC_BASE_URL`.
Remote CLI clients only need `ADMIN_API_KEY` and `PUBLIC_BASE_URL`; keep Meta secrets
and the token encryption key on the server.
Upload kinds are `image`, `story`, `cover`, and `reel`. Draft request bodies follow the
`PostDraft` schema in `/docs`; scheduled timestamps must include a timezone.

## Connect Instagram for real publishing

The implementation is ready for account setup; building it does not create a Meta app,
grant permissions, or complete Instagram login. To go live you still need:

1. An Instagram professional account. Unified connections support feed posts and Reels;
   Stories remain unavailable when Meta does not provide the account subtype.
2. A Meta app configured for Unified Login, owned by a business portfolio. Existing apps
   that already have Instagram Login can keep that connection method.
3. Public HTTPS hosting with persistent storage and a continuously running worker.
4. The appropriate app ID/secret and an authorized account connection. New apps use a
   dashboard-generated Unified token imported through the CLI's hidden prompt; the
   service does not yet implement Unified browser OAuth or automatic token renewal.

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
SQLite is for a single host and one worker. Fresh databases are initialized on startup.
Existing databases require the offline migration described in [deployment](docs/deployment.md).

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

## Contributing and license

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for development
and testing guidance. This project is available under the [MIT License](LICENSE).
