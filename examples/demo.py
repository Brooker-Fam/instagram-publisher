"""Upload the supplied artwork and create a DEMO draft. Never approves or publishes."""

import argparse
import json
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

from maple_publisher.config import ClientSettings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()
    url = urlparse(args.base_url)
    if url.scheme not in {"https", "http"} or (url.scheme == "http" and url.hostname not in {"127.0.0.1", "localhost", "::1"}):
        parser.error("Use HTTPS or a local HTTP server")
    if url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
        parser.error("The server URL must be a plain origin")
    settings = ClientSettings()
    root = Path(__file__).resolve().parent
    with httpx.Client(base_url=args.base_url, timeout=60, follow_redirects=False,
                      headers={"Authorization": "Bearer " + settings.admin_api_key.get_secret_value()}) as client:
        status = client.get("/api/status")
        status.raise_for_status()
        if status.json()["mode"] != "demo":
            raise SystemExit("The example only runs against a server in demo mode.")
        with (root / "assets/small-wins-feed.png").open("rb") as stream:
            uploaded = client.post("/api/assets", data={"kind": "image"}, files={"file": ("small-wins.png", stream)})
        uploaded.raise_for_status()
        draft = json.loads((root / "draft.json").read_text())
        draft["asset_ids"] = [uploaded.json()["id"]]
        response = client.post("/api/posts", json=draft, headers={"Idempotency-Key": str(uuid.uuid4())})
        response.raise_for_status()
        post = response.json()
        print(f"Draft created: {post['id']} (revision {post['revision']}). Nothing was approved or published.")
        print(f"Review: python -m maple_publisher.cli --base-url {args.base_url} review {post['id']}")
        print("Or open /docs, Authorize, and use GET /api/posts/{post_id}/review.")


if __name__ == "__main__":
    main()
