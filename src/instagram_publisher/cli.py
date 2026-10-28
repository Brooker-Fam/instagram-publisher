"""Admin CLI. Credentials come from environment/.env, never command arguments."""

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

from instagram_publisher.config import ClientSettings


def main():
    parser = argparse.ArgumentParser(description="Manage Instagram publishing drafts and schedules")
    parser.add_argument("--base-url", help="API origin (default: PUBLIC_BASE_URL)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "accounts", "connect", "posts", "events"):
        sub.add_parser(name)
    upload = sub.add_parser("upload")
    upload.add_argument("path", type=Path)
    upload.add_argument("--kind", choices=["image", "story", "cover", "reel"], default="image")
    draft = sub.add_parser("draft")
    draft.add_argument("path", type=Path, help="Draft JSON file (see examples/draft.json)")
    draft.add_argument("--idempotency-key", help="Reuse the same key when retrying an identical draft creation")
    for name in ("review", "cancel", "retry", "remote-status", "reconcile"):
        sub.add_parser(name).add_argument("post_id")
    approve = sub.add_parser("approve")
    approve.add_argument("post_id")
    approve.add_argument("--revision", type=int, required=True, help="Revision you reviewed")
    sub.add_parser("refresh").add_argument("account_id")
    args = parser.parse_args()
    settings = ClientSettings()
    base = (args.base_url or settings.public_base_url).rstrip("/")
    origin = urlparse(base)
    if origin.scheme != "https" and not (origin.scheme == "http" and origin.hostname in {"localhost", "127.0.0.1", "::1"}):
        parser.error("Use HTTPS for remote servers; HTTP is allowed only for localhost")
    if origin.username or origin.password or origin.query or origin.fragment or origin.path not in {"", "/"}:
        parser.error("--base-url must be a plain origin")
    headers = {"Authorization": "Bearer " + settings.admin_api_key.get_secret_value()}
    try:
        with httpx.Client(base_url=base, headers=headers, timeout=120, follow_redirects=False) as client:
            if args.command == "upload":
                with args.path.open("rb") as stream:
                    response = client.post("/api/assets", data={"kind": args.kind}, files={"file": (args.path.name, stream)})
            elif args.command == "draft":
                payload = json.loads(args.path.read_text())
                extra = {"Idempotency-Key": args.idempotency_key} if args.idempotency_key else {}
                response = client.post("/api/posts", json=payload, headers=extra)
            elif args.command == "connect":
                response = client.post("/api/accounts/connect")
            elif args.command == "refresh":
                response = client.post(f"/api/accounts/{args.account_id}/refresh")
            elif args.command == "approve":
                response = client.post(f"/api/posts/{args.post_id}/approve", json={"revision": args.revision})
            elif args.command in {"review", "remote-status"}:
                response = client.get(f"/api/posts/{args.post_id}/{args.command}")
            elif args.command in {"cancel", "retry", "reconcile"}:
                response = client.post(f"/api/posts/{args.post_id}/{args.command}")
            else:
                response = client.get(f"/api/{args.command}")
            try:
                result = response.json()
            except ValueError:
                print(f"Server returned HTTP {response.status_code} without a JSON response.", file=sys.stderr)
                raise SystemExit(1) from None
            print(json.dumps(result, indent=2, ensure_ascii=False))
            if response.is_error:
                raise SystemExit(1)
    except (httpx.HTTPError, OSError, ValueError):
        print("The request could not be completed. Check the server address, file, and connection.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
