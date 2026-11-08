"""Hub auth: principals (user|agent), hashed bearer tokens, agent lifecycle.

Tokens are random 256-bit secrets shown ONCE at issue; only sha256 hashes
are stored in the db. Author identity on every write comes from the
authenticated principal, never from request bodies, so a client cannot
post as someone else. Only a user principal can register new agents."""
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

ADMIN_HANDLE = "argus-admin"


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Principal:
    ptype: str  # 'user' | 'agent'
    pid: int
    name: str

    @property
    def audience_ref(self):
        return f"{self.ptype}:{self.name}"


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_user(con, handle):
    cur = con.execute("INSERT INTO users (handle, created_at) VALUES (?,?)",
                      (handle, utcnow()))
    con.commit()
    return cur.lastrowid


def get_or_create_agent(con, name, capabilities=()):
    row = con.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
    if row:
        return dict(row)
    cur = con.execute(
        "INSERT INTO agents (name, capabilities, created_at) VALUES (?,?,?)",
        (name, json.dumps(list(capabilities)), utcnow()))
    con.commit()
    return dict(con.execute("SELECT * FROM agents WHERE id=?",
                            (cur.lastrowid,)).fetchone())


def retire_agent(con, agent_id):
    with con:
        con.execute("UPDATE agents SET status='retired', retired_at=? WHERE id=?",
                    (utcnow(), agent_id))
        con.execute("UPDATE tokens SET revoked_at=? "
                    "WHERE principal_type='agent' AND principal_id=? AND revoked_at IS NULL",
                    (utcnow(), agent_id))


def issue_token(con, ptype, pid, label=None):
    token = secrets.token_urlsafe(32)
    con.execute("INSERT INTO tokens (token_hash, principal_type, principal_id,"
                " label, created_at) VALUES (?,?,?,?,?)",
                (_hash(token), ptype, pid, label, utcnow()))
    con.commit()
    return token  # the plaintext exists only here and in the caller's hands


def authenticate(con, token):
    if not token:
        return None
    row = con.execute(
        "SELECT t.principal_type ptype, t.principal_id pid, t.revoked_at rev,"
        " u.handle uhandle, a.name aname, a.status astatus"
        " FROM tokens t"
        " LEFT JOIN users u ON t.principal_type='user' AND u.id=t.principal_id"
        " LEFT JOIN agents a ON t.principal_type='agent' AND a.id=t.principal_id"
        " WHERE t.token_hash=?", (_hash(token),)).fetchone()
    if not row or row["rev"] or row["ptype"] is None:
        return None
    if row["ptype"] == "agent" and row["astatus"] != "active":
        return None
    name = row["uhandle"] if row["ptype"] == "user" else row["aname"]
    if name is None:
        return None
    return Principal(row["ptype"], row["pid"], name)


def audit(con, principal, action, target=None, result="ok", details=None):
    con.execute(
        "INSERT INTO audit_log (ts, principal_type, principal_id, action, target,"
        " result, details) VALUES (?,?,?,?,?,?,?)",
        (utcnow(), principal.ptype if principal else None,
         principal.pid if principal else None, action, target, result, details))
    con.commit()
