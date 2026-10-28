# Run the publisher

The default configuration is a single-owner service: one API process, one worker, and
a shared SQLite database and media directory. It is intended for Maple Baby's own
account. It is not a multi-tenant SaaS or an infinitely scalable queue.

## Local development

Requirements: Python 3.12, `ffprobe` from FFmpeg for video validation, and a virtual
environment. Tests use mocked Meta responses and need no real Instagram credentials.

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -c requirements.lock -e '.[dev]'
python scripts/bootstrap-env.py
uvicorn maple_publisher.app:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

In a second terminal with the same virtual environment and working directory:

```sh
python -m maple_publisher.worker
```

The API and worker must use the same environment file, database, media directory, and
encryption/signing keys. Run these commands from the repository root. The bootstrap
sets demo mode and private random keys; it never prints those keys and refuses to
overwrite an existing file.

Keep `--no-access-log` enabled: Uvicorn's default request log includes query strings,
which can contain OAuth authorization codes and signed-media credentials. Application
errors and worker events are still logged without enabling HTTP access logs.

Open `http://127.0.0.1:8000/docs` for the interactive API documentation. Authenticate
with the generated admin API key using **Authorize**. `GET /api/status` reports the
protected service status, while `GET /healthz` is a minimal public health check.
For a single queue pass instead of a persistent worker, run:

```sh
python -m maple_publisher.worker --once
```

## Docker Compose

Requirements: Docker Engine with the Compose plugin. The image installs FFmpeg and
runs as the unprivileged UID/GID `10001`.

```sh
python scripts/bootstrap-env.py
docker compose up --build -d
docker compose ps
python scripts/smoke.py
```

Skip the bootstrap command if `.env` already exists. The Compose file mounts a shared
named volume at `/data`, where both processes use `publisher.db` and `uploads/`.
It binds the API to `127.0.0.1:8000`; placing an HTTPS reverse proxy in front is a
separate production deployment step.

```sh
docker compose logs --tail=100 api worker
docker compose down
```

`docker compose down` keeps the persistent volume. **Do not use `down --volumes`**
unless you intend to delete the account connection, job history, drafts, and uploads.

The application image is read-only except `/data` and temporary storage. If using a
host bind mount instead of the provided named volume, create it with ownership and
permissions that let UID `10001` write, and keep it out of the Git checkout.

## Production HTTPS

Use a host or container platform that supports a persistent disk and two long-running
processes. Run a single worker for this SQLite deployment. The worker must stay awake
for scheduled posts and account-token refresh; a process that sleeps between web requests
is not a reliable scheduler.

Example Caddy configuration on the same host:

```caddyfile
publisher.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Point DNS at the host, allow HTTPS, and set `PUBLIC_BASE_URL` to the same origin.
Apply the settings in [Meta setup](meta-setup.md), restart the API and worker, and
finish OAuth. Review proxy timeouts and upload limits so they allow the permitted
media size. Keep reverse-proxy HTTP access logging disabled, as in the Caddy example,
or explicitly redact query strings before enabling it. OAuth callback parameters
contain authorization credentials, and signed-media query parameters grant file access.
Do not enable a default access-log format that records complete request URLs.

Do not front every route with a separate browser login: Meta must fetch signed media,
redirect the account owner to the OAuth callback, and deliver its signed lifecycle
callbacks. The deletion-confirmation status route must also remain reachable. The
publisher's own admin endpoints retain authentication. A reverse proxy can add stricter
protection to admin routes while leaving the required media and callback routes reachable.

Keep demo and live deployments on separate databases/volumes. Read the job states
and recovery guidance in [operations](operations.md) before scheduling production
content.

## Secret storage

The generated `.env` has owner-only filesystem permissions. In a hosted environment,
prefer its secret manager and inject the values into both processes. Do not put keys
in Docker build arguments, baked image layers, a repository, a URL, or screenshots.

Keep the token encryption key when upgrading or restoring the database. Losing it
means stored Instagram tokens cannot be decrypted. Keep the media signing key stable
while Meta may still be retrieving pending uploads. Rotate the admin API key if it is
exposed, then update the API clients that use it.

## Managed development environments with an HTTPS proxy

The Dockerfile accepts an optional BuildKit secret named `proxy_ca`. When a managed
environment supplies a combined CA bundle, mount it only for the build:

```sh
docker build --secret id=proxy_ca,src=/etc/ssl/certs/ca-certificates.crt \
  --tag maple-baby-publisher:local .
docker compose up --no-build -d
```

Preserve that environment's Docker proxy configuration and registry credentials.
Runtime HTTPS clients may also require a read-only combined CA-bundle mount and
`SSL_CERT_FILE` configured by the environment administrator. Do not turn off TLS
verification or commit session certificates into the image.

## Verification

```sh
python -m ruff check .
python -m pytest
python scripts/smoke.py --url http://127.0.0.1:8000
```

The smoke script only reads the health endpoint. It never uploads or publishes.
CI checks code, runs the offline tests, and builds the runtime image. It does not
deploy, log in to Instagram, or make live posts.

This initial release creates the database schema on startup. It does not include
versioned schema migrations yet. Back up `/data` before an upgrade and examine any
model changes; do not assume startup will migrate an older database automatically.
