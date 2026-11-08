# Hub — Operational Notes

## 2026-09-16 00:30 — first live smoke run + one real bug

**Smoke:** `tools/smoke_live.py` against running server on :8770 — all checks PASS, exit 0
(health, 401 rejection, posts/audiences/expiry/idempotency, task evidence-gate + optimistic
concurrency, approvals user-only, memory-promotion boundary, notifications, timeline
consistency, agent retirement revoke-in-tx). The 27 dispatch tests never exercised sockets;
this closes the DoD gap.

**BUG-1 (found by symptom tonight; root-caused + fixed BEFORE this session, fix verified here).**
Symptom: the long-running server (pid 13755, up 1h21m) blocked every `hubctl` write with
`database is locked` across retries. Root cause, per the already-in-working-tree fix this
session discovered and committed: `audit()` left its INSERT uncommitted (open write txn
forever on a thread-local connection) and connections crossed threads unsafely.
Prior session's fix: `auth.py` commits after audit; `server.py` uses thread-local
connections + WAL. This session's contribution: diagnosed the symptom, restarted the
server (which loaded the fix), and verified live — token issuance + full smoke then passed
immediately. Remaining hardening: (a) `busy_timeout` in `_conn()`; (b) concurrency test:
writer loop while server idle.

**Ops state:** hub server runs as Hermes-tracked background session
`proc_41f2ce66c664` (pid 21968), :8770, db `~/.hermes/argus/hub/hub.db`.
User token: `~/.hermes/argus/hub/doug_token` (chmod 600, never in git).
Legacy BotFeed M0 still on :8765 — retire once hub is adopted as the feed.
