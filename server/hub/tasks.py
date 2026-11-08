"""Hub tasks domain: lifecycle state machine with optimistic concurrency.

States: created -> assigned -> active -> validating -> completed|failed
        any open state -> blocked -> active; failed -> active (retry)
Invariants:
- completed REQUIRES at least one validation-evidence event (test|commit)
- agents may act ONLY on tasks bound to them; users may act on any
- transition() carries expected_version (optimistic concurrency): stale
  writers get a 409 instead of silently clobbering newer state
"""
import json

from .auth import utcnow
from .posts import HubError

LEGAL = {
    "created": {"assigned"},
    "assigned": {"active", "blocked"},
    "active": {"validating", "blocked", "failed"},
    "validating": {"completed", "failed", "blocked", "active"},
    "blocked": {"active"},
    "failed": {"active"},
    "completed": set(),
}
EVIDENCE = ("test", "commit")


def create_task(con, principal, key, title, agent=None, project=None,
                notification_note=None):
    # Any principal may file a task. Agents file for themselves without being
    # asked (decided 2026-09-15); completion stays evidence-gated and
    # approvals stay human-only, so the autonomy stops at filing.
    if principal.ptype == "agent":
        if agent and agent != principal.name:
            raise HubError(403, "forbidden", "agents may only file tasks for themselves")
        agent = principal.name
    if not key.strip() or not title.strip():
        raise HubError(400, "invalid_task", "key and title required")
    aid = pid = None
    if agent:
        row = con.execute("SELECT id FROM agents WHERE name=?", (agent,)).fetchone()
        if not row:
            raise HubError(400, "unknown_agent", agent)
        aid = row["id"]
    if project:
        row = con.execute("SELECT id FROM projects WHERE slug=?", (project,)).fetchone()
        if not row:
            raise HubError(400, "unknown_project", project)
        pid = row["id"]
    status = "assigned" if aid else "created"
    with con:
        cur = con.execute(
            "INSERT INTO tasks (key,title,status,agent_id,project_id,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?)",
            (key.strip(), title.strip(), status, aid, pid, utcnow(), utcnow()))
        tid = cur.lastrowid
        _event(con, principal, tid, "assignment",
               {"agent": agent, "note": notification_note})
        if aid and aid != principal.pid:  # no self-notification
            con.execute("INSERT INTO notifications (recipient_type,recipient_id,"
                        "kind,severity,message,task_id,created_at)"
                        " VALUES ('agent',?,'assignment','action_required',?,?,?)",
                        (aid, f"assigned: {title}", tid, utcnow()))
    return get_task(con, principal, tid)


def _event(con, actor, task_id, type_, payload):
    if type_ not in ("assignment", "attempt", "test", "commit", "error",
                     "handoff", "cost", "note"):
        raise HubError(400, "invalid_event", type_)
    con.execute(
        "INSERT INTO task_events (task_id,type,payload,actor_type,actor_id,"
        "created_at) VALUES (?,?,?,?,?,?)",
        (task_id, type_, json.dumps(payload), actor.ptype, actor.pid, utcnow()))
    # A test/commit event counts as evidence when it names something concrete.
    # Accept either an explicit ref or the event's own name field.
    ref = payload.get("ref") or payload.get("name")
    if type_ in EVIDENCE and ref:
        con.execute("INSERT INTO references_link (task_id,ref_type,ref_value,"
                    "created_at) VALUES (?,?,?,?)", (task_id, type_, ref, utcnow()))


def add_event(con, principal, task_id, type_, payload=None):
    task = _load(con, task_id)
    _check_writer(principal, task)
    with con:
        _event(con, principal, task_id, type_, payload or {})
    return {"ok": True}


def transition(con, principal, task_id, to, expected_version):
    task = _load(con, task_id)
    _check_writer(principal, task)
    if to not in LEGAL.get(task["status"], set()):
        raise HubError(409, "illegal_transition",
                       f"{task['status']} -> {to}")
    if to == "completed":
        ev = con.execute("SELECT COUNT(*) n FROM task_events WHERE task_id=?"
                         " AND type IN ('test','commit')", (task_id,)).fetchone()["n"]
        if not ev:
            raise HubError(422, "completion_without_evidence",
                           "link a test or commit before completing")
    with con:
        cur = con.execute(
            "UPDATE tasks SET status=?, version=version+1, updated_at=?"
            " WHERE id=? AND version=?", (to, utcnow(), task_id, expected_version))
        if cur.rowcount != 1:
            raise HubError(409, "stale_write",
                           "task changed; re-read and retry")
        _event(con, principal, task_id, "note", {"transition": to})
        if to in ("blocked", "completed", "failed"):
            con.execute("INSERT INTO notifications (recipient_type,recipient_id,"
                        "kind,severity,message,task_id,created_at)"
                        " SELECT 'user', id, ?, ?, ?, ?, ? FROM users",
                        (to, "action_required" if to != "failed" else "notice",
                         f"task {task['key']} {to}", task_id, utcnow()))
    return get_task(con, principal, task_id)


def _load(con, task_id):
    row = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise HubError(404, "not_found", f"task {task_id}")
    return dict(row)


def _check_writer(principal, task):
    if principal.ptype == "user":
        return
    if task["agent_id"] != principal.pid:
        raise HubError(403, "forbidden", "task not assigned to this agent")


def get_task(con, principal, task_id):
    t = _load(con, task_id)
    t["events"] = [dict(r) for r in con.execute(
        "SELECT type,payload,actor_type,actor_id,created_at FROM task_events"
        " WHERE task_id=? ORDER BY id", (task_id,))]
    t["references"] = [dict(r) for r in con.execute(
        "SELECT ref_type,ref_value FROM references_link WHERE task_id=?", (task_id,))]
    return t


def list_tasks(con, principal, status=None, agent=None):
    sql = ("SELECT t.*, a.name agent_name, p.slug project_slug FROM tasks t"
           " LEFT JOIN agents a ON a.id=t.agent_id"
           " LEFT JOIN projects p ON p.id=t.project_id WHERE 1=1")
    args = []
    if principal.ptype == "agent":
        sql += " AND t.agent_id=?"
        args.append(principal.pid)
    if status:
        sql += " AND t.status=?"
        args.append(status)
    if agent:
        sql += " AND a.name=?"
        args.append(agent)
    return [dict(r) for r in con.execute(sql + " ORDER BY t.updated_at DESC", args)]
