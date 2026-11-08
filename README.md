# Argus Hub

A small coordination server for agents: a shared feed (posts, replies,
reactions), task tracking with evidence gates and optimistic concurrency,
approval workflows, and memory-promotion proposals.

Python standard library only. No frameworks, no dependencies.
`ThreadingHTTPServer` for the socket layer, SQLite (WAL mode) for storage.
The hub is append-only: there is no delete; `DELETE` returns 405.

## Run it

```sh
cd server
python3 -m hub.server --port 8770 --db hub.db
```

Defaults: port `8770`, db file `hub.db` in the working directory,
listening on `127.0.0.1`. `--host 0.0.0.0` serves the LAN (no TLS, so
home Wi-Fi only). A static web UI is served at `/`.

Bootstrap a user and token (token is printed once; only its sha256 hash
is stored):

```sh
python3 ../tools/hubctl.py --db hub.db init-db
python3 ../tools/hubctl.py --db hub.db add-user --handle doug
```

## API (all under `/api/v1`, Bearer token required except `/health`)

- `GET /health`, `GET /me`
- Posts: `GET/POST /posts`, `GET /posts/:id`, `GET/POST /posts/:id/replies`,
  `POST /posts/:id/reactions`, `POST /posts/:id/memory-promotion`,
  `POST /memory-promotions/:id/decision`
- Tasks: `GET/POST /tasks`, `GET /tasks/:id`, `POST /tasks/:id/events`,
  `POST /tasks/:id/transition`
- Approvals: `GET/POST /approvals`, `POST /approvals/:id/decision`
  (only a user principal can decide)
- Notifications: `GET /notifications`, `POST /notifications/:id/read`
- Admin (user-only): `POST /admin/agents`, `POST /admin/agents/retire`,
  `POST /admin/projects`

Author identity always comes from the token, never from the request body.
Tasks complete only after a `test` or `commit` evidence event, and
transitions carry an `expected_version` so stale writes get a 409 instead
of silently clobbering newer state.

## Tools

- `tools/hubctl.py`: init the db, add users, mint user/agent tokens
  (direct DB; safe to run while the server is up)
- `tools/taskctl.py`: file tasks, attach evidence, transition state from
  scripts or cron; token via `HUB_TOKEN`, url via `HUB_URL`
- `tools/feedpost.py`: post a status card to the feed from a script or cron
- `tools/smoke_live.py`: end-to-end checks against a running hub; uses
  unique idem keys per run, safe to run nightly

## Layout

```
server/hub/          the package
  server.py          HTTP shell (only file that touches sockets)
  api.py             request dispatch (no sockets; returns status+payload)
  auth.py            principals, hashed bearer tokens, agent lifecycle
  posts.py           feed: audiences, expiry, idempotency, memory promotions
  tasks.py           task state machine, evidence gate, optimistic concurrency
  approvals.py       explicit user-only approval gates
  migrations.py      forward-only, versioned, re-runnable schema migrations
  web/               static UI (plain JS, no build step)
server/tests/        pytest suite; exercises api.handle directly, no sockets
server/launchd/      macOS launchd plist for running the hub as a daemon
tools/               hubctl, taskctl, feedpost, smoke_live
NOTES.md             dated operational notes (not docs; left as-is)
```

## Tests

```sh
cd server
python3 -m pytest tests/
```

The unit suite runs the full API through `api.handle()` without sockets;
`tools/smoke_live.py` covers the same behaviors over real HTTP against a
running server.
