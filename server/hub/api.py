"""Hub API v1: pure dispatch, no sockets (server.py is the only HTTP shell).

handle(con, principal, method, path, body) -> (status, payload)
- principal: auth.Principal or None (None => only /health allowed)
- every input crosses validation; errors are structured {error:{code,message}}
- author identity is ALWAYS the principal; bodies have no author fields
"""
import json
import secrets

from . import approvals, auth, posts, tasks
from .migrations import connect as _connect  # noqa: F401 (symmetry)
from .posts import HubError

VERSION = "1"


def handle(con, principal, method, path, body=None):
    try:
        return _route(con, principal, method.upper(), path, body)
    except HubError as e:
        return e.status, {"error": {"code": e.code, "message": e.message}}
    except Exception as e:  # anything unplanned is a 500; never pretend it worked
        return 500, {"error": {"code": "internal", "message": str(e)[:200]}}


def _pbody(body):
    if isinstance(body, (str, bytes)):
        try:
            body = json.loads(body)
        except ValueError:
            raise HubError(400, "invalid_json", "body is not JSON")
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise HubError(400, "invalid_json", "body must be an object")
    return body


def _q(path, name):
    from urllib.parse import parse_qs, urlsplit
    vals = parse_qs(urlsplit(path).query).get(name)
    return vals[0] if vals else None


def _tail(path, prefix):
    """'/api/v1/posts/7/replies' -> ['posts', '7', 'replies'].
    The /api/v1 prefix is stripped so each section matches plain segments."""
    from urllib.parse import urlsplit
    parts = [p for p in urlsplit(path).path.split("/") if p]
    # strip /api/v1
    if parts[:2] == ["api", "v1"]:
        parts = parts[2:]
    return parts


def _route(con, principal, m, path, body):
    # route map: health, me, admin/*, posts/*, memory-promotions, tasks/*,
    # approvals/*, notifications/*. Anything unrecognized falls through to 404.
    parts = _tail(path, "")
    if parts == ["health"]:
        return 200, {"app": "argus-hub", "version": VERSION}
    if principal is None:
        return 401, {"error": {"code": "unauthorized", "message": "token required"}}
    if parts == ["me"]:
        return 200, {"type": principal.ptype, "name": principal.name}

    # ---------- admin (user only) ----------
    if parts and parts[0] == "admin":
        if principal.ptype != "user":
            return 403, {"error": {"code": "forbidden", "message": "admin is user-only"}}
        if parts[1:] == ["agents"] and m == "POST":
            b = _pbody(body)
            name = str(b.get("name", "")).strip()
            if not name:
                return 400, {"error": {"code": "invalid_agent", "message": "name required"}}
            ag = auth.get_or_create_agent(con, name, b.get("capabilities", ()))
            token = auth.issue_token(con, "agent", ag["id"], b.get("label"))
            auth.audit(con, principal, "agent_register", f"agent:{name}", "ok")
            return 201, {"agent": name, "id": ag["id"], "token": token,
                         "note": "shown once; store securely"}
        if parts[1:] == ["agents", "retire"] and m == "POST":
            b = _pbody(body)
            row = con.execute("SELECT id,status FROM agents WHERE name=?",
                              (str(b.get("name", "")),)).fetchone()
            if not row:
                return 404, {"error": {"code": "unknown_agent", "message": "name"}}
            auth.retire_agent(con, row["id"])
            auth.audit(con, principal, "agent_retire",
                       f"agent:{b.get('name')}", "ok")
            return 200, {"retired": b.get("name")}
        if parts[1:] == ["projects"] and m == "POST":
            b = _pbody(body)
            slug = (b.get("slug") or b.get("name", "")).lower().replace(" ", "-")
            with con:
                cur = con.execute("INSERT INTO projects (name,slug,created_at)"
                                  " VALUES (?,?,?)", (b.get("name", slug), slug,
                                                      auth.utcnow()))
            return 201, {"id": cur.lastrowid, "slug": slug}
        return 404, {"error": {"code": "not_found", "path": "/".join(parts)}}

    # ---------- posts ----------
    if parts[:1] == ["posts"]:
        b = _pbody(body) if m == "POST" else None
        if parts == ["posts"] and m == "POST":
            p = posts.create_post(
                con, principal, body=b.get("body", ""),
                priority=b.get("priority", "normal"),
                persistence=b.get("persistence", "session"),
                memory_eligible=bool(b.get("memory_eligible", False)),
                audiences=b.get("audiences"),
                references=b.get("references", ()),
                task_id=b.get("task_id"),
                project_slug=b.get("project"),
                expires_at=b.get("expires_at"),
                idem_key=b.get("idem_key"))
            return (200 if p.pop("replayed", False) else 201), p
        if parts == ["posts"] and m == "GET":
            return 200, {"posts": posts.timeline(
                con, principal, kind=_q(path, "kind"), priority=_q(path, "priority"),
                project=_q(path, "project"), agent=_q(path, "agent"),
                since=_q(path, "since"), limit=int(_q(path, "limit") or 50),
                include_expired=_q(path, "include_expired") == "1")}
        if len(parts) == 2 and m == "GET":
            return 200, posts.get_post(con, principal, int(parts[1]))
        if len(parts) == 3 and parts[2] == "replies" and m == "GET":
            return 200, {"replies": posts.list_replies(con, principal, int(parts[1]))}
        if len(parts) == 3 and parts[2] == "replies" and m == "POST":
            r = posts.reply(con, principal, int(parts[1]), b.get("body", ""),
                            b.get("idem_key"))
            return (200 if r["replayed"] else 201), r
        if len(parts) == 3 and parts[2] == "reactions" and m == "POST":
            return 201, posts.react(con, principal, int(parts[1]), b.get("kind", ""))
        if len(parts) == 3 and parts[2] == "memory-promotion" and m == "POST":
            return 201, posts.propose_memory(con, principal, int(parts[1]),
                                             b.get("target"))
        return 404, {"error": {"code": "not_found", "path": "/".join(parts)}}

    # ---------- memory decisions ----------
    if len(parts) == 3 and parts[0] == "memory-promotions" and parts[2] == "decision" \
            and m == "POST":
        b = _pbody(body)
        return 200, posts.decide_memory(con, principal, int(parts[1]),
                                        bool(b.get("approve")))

    # ---------- tasks ----------
    if parts[:1] == ["tasks"]:
        b = _pbody(body) if m == "POST" else None
        if parts == ["tasks"] and m == "POST":
            return 201, tasks.create_task(con, principal, b.get("key", ""),
                                          b.get("title", ""), agent=b.get("agent"),
                                          project=b.get("project"))
        if parts == ["tasks"] and m == "GET":
            return 200, {"tasks": tasks.list_tasks(con, principal,
                                                   status=_q(path, "status"))}
        if len(parts) == 2 and m == "GET":
            return 200, tasks.get_task(con, principal, int(parts[1]))
        if len(parts) == 3 and parts[2] == "events" and m == "POST":
            return 201, tasks.add_event(con, principal, int(parts[1]),
                                        b.get("type", ""), b.get("payload", {}))
        if len(parts) == 3 and parts[2] == "transition" and m == "POST":
            return 200, tasks.transition(con, principal, int(parts[1]),
                                         b.get("to", ""),
                                         int(b.get("expected_version", -1)))
        return 404, {"error": {"code": "not_found", "path": "/".join(parts)}}

    # ---------- approvals ----------
    if parts[:1] == ["approvals"]:
        b = _pbody(body) if m == "POST" else None
        if parts == ["approvals"] and m == "POST":
            return 201, approvals.request(con, principal, b.get("action", ""),
                                          b.get("task_id"), b.get("risk", "medium"),
                                          b.get("expires_at"))
        if parts == ["approvals"] and m == "GET":
            return 200, {"approvals": approvals.list_for(
                con, principal, _q(path, "status") or "pending")}
        if len(parts) == 3 and parts[2] == "decision" and m == "POST":
            return 200, approvals.decide(con, principal, int(parts[1]),
                                         bool(b.get("approve")), b.get("reason"))
        return 404, {"error": {"code": "not_found", "path": "/".join(parts)}}

    # ---------- notifications ----------
    if parts[:1] == ["notifications"]:
        if parts == ["notifications"] and m == "GET":
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM notifications WHERE recipient_type=? AND"
                " recipient_id=? ORDER BY id DESC LIMIT ?",
                (principal.ptype, principal.pid,
                 int(_q(path, "limit") or 50)))]
            return 200, {"notifications": rows}
        if len(parts) == 3 and parts[2] == "read" and m == "POST":
            with con:
                cur = con.execute(
                    "UPDATE notifications SET read_at=? WHERE id=? AND"
                    " recipient_type=? AND recipient_id=? AND read_at IS NULL",
                    (auth.utcnow(), int(parts[1]), principal.ptype, principal.pid))
            if cur.rowcount != 1:
                return 404, {"error": {"code": "not_found", "message": "notification"}}
            return 200, {"ok": True}
        return 404, {"error": {"code": "not_found", "path": "/".join(parts)}}

    return 404, {"error": {"code": "not_found", "path": "/".join(parts) or "/"}}
