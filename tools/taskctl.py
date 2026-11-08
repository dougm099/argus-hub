#!/usr/bin/env python3
"""File/transition/evidence own tasks on the hub from scripts/cron (stdlib only).

Agent-side task client (policy: agents self-file their work; completion stays
evidence-gated, approvals stay human-only; see argus-hub 72df441).
Cron note: no inline python/execute_code and no compound `VAR=$(...) curl`
chains; the security wrapper blocks both when unattended, so run this as
its own call.

Examples:
  taskctl.py create --key DAILY-2026-09-15 --title "nightly consolidation" \
      --agent argus --project argus
  taskctl.py event --id 14 --type test --ref "report:2026-09-15.md"
  taskctl.py transition --id 14 --to active
  taskctl.py transition --id 14 --to validating
  taskctl.py transition --id 14 --to completed
  taskctl.py list [--status open]

Token via --token or $HUB_TOKEN; URL via --url or $HUB_URL. Version is
fetched server-side when --expected-version is omitted (last-writer-wins is
still refused by the API itself when stale between fetch and write).
"""
import argparse
import json
import os
import sys
import urllib.request

def _req(a, path, method="GET", payload=None):
    req = urllib.request.Request(
        a.url.rstrip("/") + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + a.token})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"taskctl failed: HTTP {e.code} {e.read().decode()[:300]}")
    except Exception as e:  # noqa: BLE001; cron wants a message, not a traceback
        sys.exit(f"taskctl failed: {e}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("HUB_URL", "http://127.0.0.1:8770"))
    ap.add_argument("--token", default=os.environ.get("HUB_TOKEN", ""))
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("list"); s.add_argument("--status", default=None)
    s = sub.add_parser("create")
    s.add_argument("--key", required=True); s.add_argument("--title", required=True)
    s.add_argument("--agent", default=None); s.add_argument("--project", default=None)
    s = sub.add_parser("event")
    s.add_argument("--id", type=int, required=True)
    s.add_argument("--type", default="test"); s.add_argument("--ref", required=True)
    s = sub.add_parser("transition")
    s.add_argument("--id", type=int, required=True)
    s.add_argument("--to", required=True)
    s.add_argument("--expected-version", type=int, default=None)
    a = ap.parse_args()
    if not a.token:
        sys.exit("no token: --token or HUB_TOKEN")
    if a.cmd == "list":
        st, out = _req(a, "/api/v1/tasks" + (f"?status={a.status}" if a.status else ""))
    elif a.cmd == "create":
        p = {"key": a.key, "title": a.title}
        if a.agent: p["agent"] = a.agent
        if a.project: p["project"] = a.project
        st, out = _req(a, "/api/v1/tasks", "POST", p)
    elif a.cmd == "event":
        st, out = _req(a, f"/api/v1/tasks/{a.id}/events", "POST",
                       {"type": a.type, "payload": {"ref": a.ref}})
    else:  # transition
        ver = a.expected_version
        if ver is None:
            _, cur = _req(a, f"/api/v1/tasks/{a.id}")
            ver = cur["version"]
        st, out = _req(a, f"/api/v1/tasks/{a.id}/transition", "POST",
                       {"to": a.to, "expected_version": ver})
    print(json.dumps(out))

if __name__ == "__main__":
    main()
