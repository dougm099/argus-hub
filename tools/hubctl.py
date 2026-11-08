#!/usr/bin/env python3
"""hubctl: bootstrap and admin CLI for Argus Hub.

Writes straight to the DB; safe to run while the server is up because the
db runs in WAL mode.

usage:
  hubctl init-db --db PATH
  hubctl add-user --db PATH --handle doug          # prints bootstrap token once
  hubctl token   --db PATH --handle doug           # new token for existing user
  hubctl agent-token --db PATH --name doofy
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from hub import auth  # noqa: E402
from hub.migrations import connect, migrate  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path.home() / ".hermes/argus/hub/hub.db"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    p = sub.add_parser("add-user")
    p.add_argument("--handle", required=True)
    p = sub.add_parser("token")
    p.add_argument("--handle", required=True)
    p = sub.add_parser("agent-token")
    p.add_argument("--name", required=True)
    a = ap.parse_args()
    con = connect(a.db)
    migrate(con)
    if a.cmd == "init-db":
        print("ok:", a.db)
    elif a.cmd == "add-user":
        uid = auth.create_user(con, a.handle)
        tok = auth.issue_token(con, "user", uid, "cli-bootstrap")
        print(f"user {a.handle} created; token (one-time): {tok}")
    elif a.cmd == "token":
        row = con.execute("SELECT id FROM users WHERE handle=?", (a.handle,)).fetchone()
        if not row:
            sys.exit("no such user")
        print("token (one-time):", auth.issue_token(con, "user", row["id"], "cli"))
    elif a.cmd == "agent-token":
        ag = auth.get_or_create_agent(con, a.name)
        con.commit()
        print("token (one-time):", auth.issue_token(con, "agent", ag["id"], "cli"))


if __name__ == "__main__":
    main()
