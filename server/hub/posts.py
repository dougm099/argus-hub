"""Hub posts domain: two-way feed with audiences, expiry, idempotency, refs.

Invariants (enforced here + by schema):
- author identity comes from the authenticated Principal, never from input
- kind derives from principal type (user->user_update, agent->agent_event)
- idem_key replays return the original post, never a duplicate
- expired posts are invisible in timelines unless include_expired
- memory_eligible is a RECORD; promotion is a separate proposed/approved flow
"""
import json
from datetime import datetime, timezone

from .auth import utcnow


class HubError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(f"{code}: {message}")


PRIORITIES = ("low", "normal", "high", "critical")
PERSISTENCE = ("transient", "session", "project", "durable")
AUDIENCE_TYPES = ("all", "project", "agent", "selected")
REF_TYPES = ("commit", "test", "artifact", "incident", "project", "url", "file")


def _validate(body, priority, persistence, audiences, references, expires_at):
    if not isinstance(body, str) or not (1 <= len(body.strip()) <= 2000):
        raise HubError(400, "invalid_body", "body must be 1-2000 chars")
    if priority not in PRIORITIES:
        raise HubError(400, "invalid_priority", "priority must be one of " + "|".join(PRIORITIES))
    if persistence not in PERSISTENCE:
        raise HubError(400, "invalid_persistence", "persistence must be one of " + "|".join(PERSISTENCE))
    for a in audiences:
        if a.get("type") not in AUDIENCE_TYPES:
            raise HubError(400, "invalid_audience", "unknown audience type " + str(a.get("type")))
        if a["type"] != "all" and not str(a.get("ref", "")).strip():
            raise HubError(400, "invalid_audience", "audience needs ref")
    for r in references:
        if r.get("type") not in REF_TYPES or not str(r.get("value", "")).strip():
            raise HubError(400, "invalid_reference", "bad reference")
    if expires_at is not None:
        try:
            datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise HubError(400, "invalid_expires_at", "use ISO Z format")


def create_post(con, principal, *, body, priority="normal", persistence="session",
                memory_eligible=False, audiences=None, references=(),
                task_id=None, project_slug=None, expires_at=None, idem_key=None):
    body = body.strip()
    audiences = audiences or [{"type": "all"}]
    _validate(body, priority, persistence, audiences, references, expires_at)

    kind = "user_update" if principal.ptype == "user" else "agent_event"
    if idem_key:
        row = con.execute(
            "SELECT * FROM posts WHERE author_type=? AND author_id=? AND idem_key=?",
            (principal.ptype, principal.pid, idem_key)).fetchone()
        if row:
            existing = get_post(con, principal, row["id"])
            existing["replayed"] = True
            return existing
    proj_id = None
    if project_slug:
        prow = con.execute("SELECT id FROM projects WHERE slug=?", (project_slug,)).fetchone()
        if not prow:
            raise HubError(400, "unknown_project", project_slug)
        proj_id = prow["id"]
    with con:
        cur = con.execute(
            "INSERT INTO posts (author_type,author_id,kind,body,priority,persistence,"
            "memory_eligible,task_id,project_id,expires_at,idem_key,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (principal.ptype, principal.pid, kind, body, priority, persistence,
             1 if memory_eligible else 0, task_id, proj_id, expires_at, idem_key, utcnow()))
        post_id = cur.lastrowid
        for a in audiences:
            ref = "" if a["type"] == "all" else a.get("ref", "")
            con.execute("INSERT INTO post_audiences (post_id,audience_type,ref)"
                        " VALUES (?,?,?)", (post_id, a["type"], ref))
        for r in references:
            con.execute("INSERT INTO references_link (post_id,ref_type,ref_value,created_at)"
                        " VALUES (?,?,?,?)", (post_id, r["type"], r["value"], utcnow()))
        _fanout_notifications(con, principal, post_id, body, priority, kind, audiences)
    out = get_post(con, principal, post_id)
    out["replayed"] = False
    return out


def _fanout_notifications(con, actor, post_id, body, priority, kind, audiences):
    rows = []
    if kind == "agent_event" and priority in ("high", "critical"):
        for u in con.execute("SELECT id FROM users"):
            rows.append(("user", u["id"], "important_post", "action_required",
                         f"[{priority}] {actor.name}: {body[:120]}"))
    if kind == "user_update":
        targets = set()
        include_all = any(a["type"] == "all" for a in audiences)
        for a in audiences:
            if a["type"] in ("agent", "selected"):
                name = a["ref"].split(":", 1)[-1]
                targets.add(name)
        q = "SELECT id,name FROM agents WHERE status='active'"
        for ag in con.execute(q):
            if include_all or ag["name"] in targets:
                rows.append(("agent", ag["id"], "user_context", "notice",
                             f"{actor.name}: {body[:120]}"))
    for rt, rid, kindn, sev, msg in rows:
        con.execute("INSERT INTO notifications (recipient_type,recipient_id,kind,"
                    "severity,message,post_id,created_at) VALUES (?,?,?,?,?,?,?)",
                    (rt, rid, kindn, sev, msg, post_id, utcnow()))


def _visibility(con, principal):
    if principal.ptype == "user":
        return "1=1", []
    return ("(author_type=? AND author_id=?)"
            " OR EXISTS (SELECT 1 FROM post_audiences pa WHERE pa.post_id=posts.id AND ("
            " pa.audience_type='all'"
            " OR (pa.audience_type IN ('agent','selected') AND pa.ref=?)"
            " OR (pa.audience_type='project' AND pa.ref IN"
            "     (SELECT p.slug FROM tasks t JOIN projects p ON p.id=t.project_id"
            "      WHERE t.agent_id=? AND t.status IN"
            "       ('created','assigned','active','validating','blocked')))))"), \
        (principal.ptype, principal.pid, principal.audience_ref, principal.pid)


def timeline(con, principal, *, kind=None, priority=None, project=None,
             agent=None, since=None, limit=50, include_expired=False):
    limit = max(1, min(int(limit), 100))
    where_sql, where_args = _visibility(con, principal)
    where = [where_sql, "1=1"]
    args = list(where_args)
    if not include_expired:
        where.append("(posts.expires_at IS NULL OR posts.expires_at > ?)")
        args.append(utcnow())
    if kind:
        if kind not in ("user_update", "agent_event"):
            raise HubError(400, "invalid_kind", kind)
        where.append("posts.kind=?")
        args.append(kind)
    if priority:
        where.append("posts.priority=?")
        args.append(priority)
    if project:
        where.append("posts.project_id=(SELECT id FROM projects WHERE slug=?)")
        args.append(project)
    if agent:
        where.append("posts.author_type='agent' AND posts.author_id="
                     "(SELECT id FROM agents WHERE name=?)")
        args.append(agent)
    if since:
        ts, _, last_id = since.partition("|")
        # keyset cursor for DESC order: strictly older rows only
        where.append("(posts.created_at < ? OR (posts.created_at = ? AND posts.id < ?))")
        args += [ts, ts, int(last_id or 0)]
    sql = ("SELECT posts.*, COALESCE(u.handle, a.name) AS author_name,"
           " tp.slug AS project_slug"
           " FROM posts"
           " LEFT JOIN users u ON posts.author_type='user' AND u.id=posts.author_id"
           " LEFT JOIN agents a ON posts.author_type='agent' AND a.id=posts.author_id"
           " LEFT JOIN projects tp ON tp.id=posts.project_id"
           f" WHERE {' AND '.join(where)}"
           " ORDER BY posts.created_at DESC, posts.id DESC LIMIT ?")
    rows = [dict(r) for r in con.execute(sql, (*args, limit))]
    return rows


def get_post(con, principal, post_id):
    row = con.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if not row:
        raise HubError(404, "not_found", f"post {post_id}")
    ok, args = _visibility(con, principal)
    probe = con.execute(
        "SELECT 1 FROM posts WHERE id=? AND (" + ok + ")", (post_id, *args))
    if not probe.fetchone():
        raise HubError(403, "forbidden", f"post {post_id}")
    p = dict(row)
    p["audiences"] = [{"type": r["audience_type"], "ref": r["ref"]} for r in con.execute(
        "SELECT audience_type,ref FROM post_audiences WHERE post_id=?", (post_id,))]
    p["references"] = [{"type": r["ref_type"], "value": r["ref_value"]} for r in con.execute(
        "SELECT ref_type,ref_value FROM references_link WHERE post_id=?", (post_id,))]
    p["reactions"] = {r["kind"]: r["n"] for r in con.execute(
        "SELECT kind, COUNT(*) n FROM reactions WHERE post_id=? GROUP BY kind", (post_id,))}
    p["reply_count"] = con.execute(
        "SELECT COUNT(*) n FROM replies WHERE post_id=?", (post_id,)).fetchone()["n"]
    return p


def reply(con, principal, post_id, body, idem_key=None):
    get_post(con, principal, post_id)  # raises 404/403 when not visible
    body = body.strip()
    if not 1 <= len(body) <= 1000:
        raise HubError(400, "invalid_body", "reply must be 1-1000 chars")
    if idem_key:
        row = con.execute("SELECT id FROM replies WHERE author_type=? AND author_id=?"
                          " AND post_id=? AND idem_key=?",
                          (principal.ptype, principal.pid, post_id, idem_key)).fetchone()
        if row:
            return {"id": row["id"], "replayed": True}
    with con:
        cur = con.execute("INSERT INTO replies (post_id,author_type,author_id,body,"
                          "idem_key,created_at) VALUES (?,?,?,?,?,?)",
                          (post_id, principal.ptype, principal.pid, body, idem_key, utcnow()))
        con.execute(
            "INSERT INTO notifications (recipient_type,recipient_id,kind,severity,"
            "message,post_id,created_at) "
            "SELECT author_type, author_id, 'reply', 'notice', ?, ?, ? "
            "FROM posts WHERE id=?",
            (f"{principal.name} replied", post_id, utcnow(), post_id))
    return {"id": cur.lastrowid, "replayed": False}


def react(con, principal, post_id, kind):
    get_post(con, principal, post_id)
    if kind not in ("ack", "concern", "resolved", "like", "dislike"):
        raise HubError(400, "invalid_reaction", kind)
    with con:
        con.execute("INSERT OR IGNORE INTO reactions (post_id,actor_type,actor_id,kind,"
                    "created_at) VALUES (?,?,?,?,?)",
                    (post_id, principal.ptype, principal.pid, kind, utcnow()))
    return {"ok": True}


def propose_memory(con, principal, post_id, target=None):
    get_post(con, principal, post_id)
    with con:
        cur = con.execute(
            "INSERT INTO memory_promotions (source_post_id,proposed_by_type,"
            "proposed_by_id,target,provenance,created_at) VALUES (?,?,?,?,?,?)",
            (post_id, principal.ptype, principal.pid, target,
             json.dumps({"via": "hub", "proposed_by": principal.audience_ref,
                         "post": post_id, "note":
                         "governance: vault write happens only after user approval"}),
             utcnow()))
    return {"id": cur.lastrowid, "status": "proposed"}


def decide_memory(con, principal, promo_id, approve):
    if principal.ptype != "user":
        raise HubError(403, "forbidden", "only the user decides memory promotion")
    with con:
        cur = con.execute("UPDATE memory_promotions SET status=?, decided_at=?"
                          " WHERE id=? AND status='proposed'",
                          ("approved" if approve else "rejected", utcnow(), promo_id))
        if cur.rowcount != 1:
            raise HubError(409, "stale_decision", "not pending")
    # NOTE: approved means queued for vault promotion by the nightly workflow.
    # The hub never writes the vault itself; that boundary is deliberate.
    return {"id": promo_id, "status": "approved" if approve else "rejected"}


def list_replies(con, principal, post_id):
    get_post(con, principal, post_id)  # same visibility gate as the post itself
    return [dict(r) for r in con.execute(
        "SELECT r.id, r.body, r.created_at,"
        " COALESCE(u.handle, a.name) AS author_name, r.author_type"
        " FROM replies r"
        " LEFT JOIN users u ON r.author_type='user' AND u.id=r.author_id"
        " LEFT JOIN agents a ON r.author_type='agent' AND a.id=r.author_id"
        " WHERE r.post_id=? ORDER BY r.id", (post_id,))]
