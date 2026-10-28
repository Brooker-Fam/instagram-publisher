#!/usr/bin/env python3
"""Read-only HTTP checks. This script never uploads, schedules, or publishes."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    url = args.url.rstrip("/")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        parser.error("Use an HTTP(S) base URL without embedded credentials.")
    try:
        with urllib.request.urlopen(f"{url}/healthz", timeout=10) as response:
            if response.status != 200:
                raise ValueError("Health endpoint did not return HTTP 200.")
            payload = json.load(response)
            if not isinstance(payload, dict):
                raise ValueError("Health endpoint did not return a JSON object.")
        print("PASS: health endpoint is responding.")
        print("No posting action was attempted. Run pytest for the full offline test suite.")
        return 0
    except (urllib.error.URLError, ValueError, json.JSONDecodeError):
        print("FAIL: health check did not succeed. Check the API process and URL.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
