#!/usr/bin/env python3
"""Post a status card to the hub feed from scripts/cron (stdlib only).

Usage: feedpost.py --body "drill 007 green" --project argus-gym \
       --reference commit:abc123 --reference test:pytest::tests/x.py
Token via --token or $HUB_TOKEN; URL via --url or $HUB_URL.
Idempotent: pass --idem-key (replay returns the original post).
"""
import argparse
import json
import os
import sys
import urllib.request


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("HUB_URL", "http://127.0.0.1:8770"))
    ap.add_argument("--token", default=os.environ.get("HUB_TOKEN", ""))
    ap.add_argument("--body", required=True)
    ap.add_argument("--priority", default="normal",
                    choices=["normal", "high", "alert"])
    ap.add_argument("--project", default=None)
    ap.add_argument("--reference", action="append", default=[],
                    help="type:value, repeatable (commit:, test:, url:, file:)")
    ap.add_argument("--idem-key", default=None)
    a = ap.parse_args()
    if not a.token:
        sys.exit("no token: --token or HUB_TOKEN")
    refs = [{"type": r.split(":", 1)[0], "value": r.split(":", 1)[1]}
            for r in a.reference if ":" in r]
    payload = {"body": a.body, "priority": a.priority, "references": refs}
    if a.project:
        payload["project"] = a.project
    if a.idem_key:
        payload["idem_key"] = a.idem_key
    req = urllib.request.Request(
        a.url.rstrip("/") + "/api/v1/posts", method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + a.token})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            out = json.loads(r.read())
            print(f"posted id={out['id']} kind={out.get('kind')}")
    except urllib.error.HTTPError as e:
        sys.exit(f"feedpost failed: HTTP {e.code} {e.read().decode()[:200]}")
    except Exception as e:  # noqa: BLE001; cron wants a message, not a traceback
        sys.exit(f"feedpost failed: {e}")


if __name__ == "__main__":
    main()
