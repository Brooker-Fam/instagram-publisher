#!/usr/bin/env python3
"""Create a private local .env without displaying any credential values."""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path(".env"), help="new file path (default: .env)"
    )
    args = parser.parse_args()
    target = args.output.absolute()
    if target.exists() or target.is_symlink():
        parser.error(f"Refusing to replace existing {target.name}; edit it explicitly instead.")
    if not target.parent.is_dir():
        parser.error("The output directory does not exist.")

    encryption_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    content = (
        "# Local settings. Never commit this file.\n"
        "PUBLISHER_ENV=development\n"
        "PUBLISHER_MODE=demo\n"
        "PUBLIC_BASE_URL=http://localhost:8000\n"
        "DATABASE_URL=sqlite:///./data/publisher.db\n"
        "MEDIA_ROOT=./data/uploads\n"
        f"ADMIN_API_KEY={secrets.token_urlsafe(48)}\n"
        f"TOKEN_ENCRYPTION_KEY={encryption_key}\n"
        f"MEDIA_SIGNING_KEY={secrets.token_urlsafe(48)}\n"
        "META_APP_ID=\n"
        "META_APP_SECRET=\n"
        "META_API_VERSION=v26.0\n"
    )

    # A same-directory hard link atomically creates the destination without
    # replacing a file that another process may have created in the meantime.
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".env-bootstrap-", dir=target.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.chmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
    except FileExistsError:
        print("The output file already exists; no file was replaced.", file=sys.stderr)
        return 1
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)

    print(f"Created {target.name} with owner-only permissions. Secret values were not printed.")
    print("Demo mode is enabled. Complete docs/meta-setup.md before switching to live mode.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
