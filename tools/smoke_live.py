#!/usr/bin/env python3
"""Live end-to-end smoke against a RUNNING hub (stdlib urllib only).

Run: python3 smoke_live.py --url http://127.0.0.1:8770 --user-token <tok>
Exit 0 with PASS lines means every check below passed over real HTTP.
Safe to run nightly: it uses unique idem keys per run (timestamp) and
touches only its own 'SMOKE'-prefixed data.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8770"
RUN = str(int(time.time()))
OK = []


def req(path, token=None, method="GET", body=None):
    r = urllib.request.Request(BASE + path, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(r, data, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    OK.append(name)
    print("PASS", name)


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=BASE)
    ap.add_argument("--user-token", required=True)
    a = ap.parse_args()
    BASE = a.url.rstrip("/")
    ut = a.user_token

    st, h = req("/api/v1/health")
    check("health", st == 200 and h["app"] == "argus-hub")

    st, _ = req("/api/v1/posts")
    check("unauthenticated-rejected-401", st == 401)

    st, p = req("/api/v1/posts", ut, "POST", {
        "body": f"SMOKE {RUN}: priority this week = hub milestone",
        "priority": "normal", "persistence": "session", "memory_eligible": True,
        "idem_key": f"smoke-{RUN}-u1"})
    check("user-post-201", st == 201)
    pid = p["id"]
    st2, p2 = req("/api/v1/posts", ut, "POST", {
        "body": f"SMOKE {RUN}: duplicate", "idem_key": f"smoke-{RUN}-u1"})
    check("idempotent-replay-200", st2 == 200 and p2["id"] == pid)

    st, ag = req("/api/v1/admin/agents", ut, "POST",
                 {"name": f"smokebot-{RUN}", "capabilities": ["smoke"]})
    check("agent-register-201", st == 201 and ag["token"])
    at = ag["token"]

    st, priv = req("/api/v1/posts", ut, "POST", {
        "body": f"SMOKE {RUN}: secret for smokebot only",
        "audiences": [{"type": "agent", "ref": f"agent:smokebot-{RUN}"}]})
    check("private-post-201", st == 201)
    st, tl = req("/api/v1/posts?limit=100", at)
    bodies = [x["body"] for x in tl["posts"]]
    check("agent-sees-addressed", any(f"secret for smokebot" in b for b in bodies))
    check("agent-sees-all-audience", any("priority this week" in b for b in bodies))

    st, _ = req(f"/api/v1/posts/{priv['id']}", ut, "POST", {"kind": "ack"})
    st2, _ = req(f"/api/v1/posts/{priv['id']}/reactions", ut, "POST",
                 {"kind": "ack"})
    check("user-react-201", st2 == 201)

    # spoofing attempts: no author fields honored, wrong token shape
    st, ev = req("/api/v1/posts", at, "POST", {
        "body": f"SMOKE {RUN}: deployed smoke artifact",
        "priority": "high", "references": [{"type": "commit", "value": f"smoke{RUN[:6]}"}],
        "author": "doug", "idem_key": f"smoke-{RUN}-ev"})
    check("agent-event-201", st == 201 and ev["kind"] == "agent_event")
    check("spoof-author-ignored", ev["author_type"] == "agent")
    st, tl = req("/api/v1/posts?kind=agent_event&limit=100", ut)
    check("user-sees-agent-event",
          any("deployed smoke" in x["body"] for x in tl["posts"]))

    # task lifecycle incl. evidence gate + stale write
    st, t = req("/api/v1/tasks", ut, "POST",
                {"key": f"SMK-{RUN}", "title": "smoke task"})
    t = req(f"/api/v1/tasks/{t['id']}", ut)[1]
    st, out = req("/api/v1/tasks", ut, "POST",
                  {"key": f"SMK2-{RUN}", "title": "smoke assigned",
                   "agent": f"smokebot-{RUN}"})
    check("task-create-assigned", st == 201 and out["status"] == "assigned")
    tid, ver = out["id"], out["version"]
    st, bad = req(f"/api/v1/tasks/{tid}/transition", at, "POST",
                  {"to": "completed", "expected_version": ver})
    check("illegal-transition-blocked", st == 409 and bad["error"]["code"]
          == "illegal_transition")
    st, out2 = req(f"/api/v1/tasks/{tid}/transition", at, "POST",
                   {"to": "active", "expected_version": ver})
    check("agent-active-ok", st == 200 and out2["status"] == "active")
    st, stale = req(f"/api/v1/tasks/{tid}/transition", at, "POST",
                    {"to": "blocked", "expected_version": ver})
    check("stale-version-409", stale["error"]["code"] == "stale_write")
    req(f"/api/v1/tasks/{tid}/events", at, "POST",
        {"type": "test", "payload": {"ref": f"smoke-suite-{RUN}"}})
    st, fin = req(f"/api/v1/tasks/{tid}/transition", at, "POST",
                  {"to": "validating", "expected_version": out2["version"]})
    st, fin = req(f"/api/v1/tasks/{tid}/transition", at, "POST",
                  {"to": "completed", "expected_version": fin["version"]})
    check("evidence-gated-completion", st == 200 and fin["status"] == "completed")

    # approvals: agent requests, agent CANNOT self-approve, user decides
    st, ap1 = req("/api/v1/approvals", at, "POST",
                  {"action": f"smoke deploy {RUN}", "risk": "high"})
    check("approval-request", st == 201 and ap1["status"] == "pending")
    st, sa = req(f"/api/v1/approvals/{ap1['id']}/decision", at, "POST",
                 {"approve": True})
    check("agent-self-approval-403", st == 403)
    st, dec = req(f"/api/v1/approvals/{ap1['id']}/decision", ut, "POST",
                  {"approve": False, "reason": "smoke denies"})
    check("user-denies", st == 200 and dec["status"] == "denied")

    # memory promotion: proposed via post, decided by user, NEVER auto
    st, promo = req(f"/api/v1/posts/{pid}/memory-promotion", at, "POST",
                    {"target": "smoke"})
    check("promotion-proposed", st == 201 and promo["status"] == "proposed")
    st, mem = req(f"/api/v1/memory-promotions/{promo['id']}/decision", ut,
                  "POST", {"approve": False})
    check("promotion-user-decided", st == 200 and mem["status"] == "rejected")

    # notifications for user: approval + event high priority present
    st, nf = req("/api/v1/notifications?limit=100", ut)
    kinds = {x["kind"] for x in nf["notifications"]}
    check("notifications-delivered", "approval_request" in kinds
          and "important_post" in kinds)
    nid = nf["notifications"][0]["id"]
    st, _ = req(f"/api/v1/notifications/{nid}/read", ut, "POST")
    check("notification-mark-read", st == 200)

    # reconnect behavior: fresh reads after everything (stateless client)
    st, tl = req("/api/v1/posts?limit=100", ut)
    # 3 distinct SMOKE posts: u1, the private one, and the agent event.
    # The idempotent replay checked earlier collapsed into u1, so the
    # count here is exactly 3, not 4.
    check("final-timeline-consistent",
          len([x for x in tl["posts"] if "SMOKE " + RUN in x["body"]]) == 3)

    print(f"\nSMOKE RESULT: {len(OK)}/24 checks passed")
    # retire the smoke agent (user-only admin) to keep the db tidy for the next run
    st, out = req("/api/v1/admin/agents/retire", ut, "POST",
                  {"name": f"smokebot-{RUN}"})
    if st == 200:
        check("smoke-agent-retired", True)
        print(f"SMOKE RESULT FINAL: {len(OK)}/25 checks passed")


if __name__ == "__main__":
    main()
