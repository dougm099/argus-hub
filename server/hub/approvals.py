"""Hub approvals: explicit, durable, auditable gates for risky actions.

Rules:
- only a USER principal may decide; decisions are terminal (409 if not pending)
- expired approvals become 'expired' lazily on read and can never be decided
- no inference: approvals only change via decide(); requests/decisions audit
"""
from .auth import audit, utcnow
from .posts import HubError


def request(con, principal, action, task_id=None, risk="medium", expires_at=None):
    action = (action or "").strip()
    if not action:
        raise HubError(400, "invalid_action", "action required")
    if risk not in ("low", "medium", "high"):
        raise HubError(400, "invalid_risk", risk)
    with con:
        cur = con.execute(
            "INSERT INTO approvals (requested_by_type,requested_by_id,task_id,"
            "action,risk,created_at,expires_at) VALUES (?,?,?,?,?,?,?)",
            (principal.ptype, principal.pid, task_id, action[:200], risk,
             utcnow(), expires_at))
        audit(con, principal, "approval_request", f"approval:{cur.lastrowid}",
              "pending", action[:200])
        con.execute(
            "INSERT INTO notifications (recipient_type,recipient_id,kind,severity,"
            "message,approval_id,task_id,created_at)"
            " SELECT 'user', id, 'approval_request', 'action_required', ?, ?, ?, ? FROM users",
            (f"approval needed [{risk}]: {action[:120]}", cur.lastrowid, task_id,
             utcnow()))
    return {"id": cur.lastrowid, "status": "pending"}


def _expire_lazy(con):
    with con:
        con.execute("UPDATE approvals SET status='expired' WHERE status='pending'"
                    " AND expires_at IS NOT NULL AND expires_at <= ?", (utcnow(),))


def list_for(con, principal, status="pending"):
    _expire_lazy(con)
    if principal.ptype == "user":
        rows = con.execute("SELECT * FROM approvals WHERE status=? ORDER BY id DESC",
                           (status,))
    else:
        rows = con.execute("SELECT * FROM approvals WHERE status=? AND"
                           " requested_by_type='agent' AND requested_by_id=?"
                           " ORDER BY id DESC", (status, principal.pid))
    return [dict(r) for r in rows]


def decide(con, principal, approval_id, approve, reason=None):
    if principal.ptype != "user":
        raise HubError(403, "forbidden", "only the user decides approvals")
    _expire_lazy(con)
    row = con.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
    if not row:
        raise HubError(404, "not_found", f"approval {approval_id}")
    if row["status"] != "pending":
        raise HubError(410 if row["status"] == "expired" else 409,
                       "not_decidable", f"approval is {row['status']}")
    new = "approved" if approve else "denied"
    with con:
        cur = con.execute(
            "UPDATE approvals SET status=?, decided_by_type=?, decided_by_id=?,"
            " decided_at=? WHERE id=? AND status='pending'",
            (new, principal.ptype, principal.pid, utcnow(), approval_id))
        if cur.rowcount != 1:
            raise HubError(409, "stale_decision", "changed under us")
        audit(con, principal, f"approval_{new}", f"approval:{approval_id}", new, reason)
        con.execute(
            "INSERT INTO notifications (recipient_type,recipient_id,kind,severity,"
            "message,approval_id,task_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (row["requested_by_type"], row["requested_by_id"], "approval_" + new,
             "action_required", f"approval {new}: {row['action'][:120]}",
             approval_id, row["task_id"], utcnow()))
    return {"id": approval_id, "status": new}
