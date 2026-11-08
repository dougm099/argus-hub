import pytest
from hub import api, auth
from hub.migrations import connect, migrate


@pytest.fixture()
def w(tmp_path):
    con = connect(tmp_path / "hub.db")
    migrate(con)
    uid = auth.create_user(con, "doug")
    utok = auth.issue_token(con, "user", uid)
    user = auth.Principal("user", uid, "doug")
    return dict(con=con, user=user, utok=utok)


def H(w, principal, m, path, body=None):
    return api.handle(w["con"], principal, m, path, body)


def test_health_open_auth_required(w):
    assert H(w, None, "GET", "/api/v1/health")[0] == 200
    assert H(w, None, "GET", "/api/v1/posts")[1]["error"]["code"] == "unauthorized"


def test_agent_registration_end_to_end(w):
    st, out = H(w, w["user"], "POST", "/api/v1/admin/agents",
                {"name": "forge", "capabilities": ["cpp"]})
    assert st == 201 and out["token"]
    # token plaintext is not stored: db holds hash only
    assert "token" not in str(list(w["con"].execute("SELECT * FROM tokens")))
    ap = auth.authenticate(w["con"], out["token"])
    assert ap.name == "forge"
    st, me = H(w, ap, "GET", "/api/v1/me")
    assert me == {"type": "agent", "name": "forge"}


def test_post_roundtrip_scoping_via_api(w):
    st, p = H(w, w["user"], "POST", "/api/v1/posts",
              {"body": "priority this week: apollo", "priority": "high",
               "audiences": [{"type": "agent", "ref": "agent:forge"}]})
    assert st == 201
    st, tl = H(w, w["user"], "GET", "/api/v1/posts")
    assert tl["posts"][0]["id"] == p["id"]
    # other agents can't fetch it
    other = auth.Principal("agent", 99, "nobody")
    st, err = H(w, other, "GET", f"/api/v1/posts/{p['id']}")
    assert st == 403


def test_no_author_field_accepted(w):
    st, p = H(w, w["user"], "POST", "/api/v1/posts",
              {"body": "hi", "author": "someone-else", "author_name": "x"})
    assert st == 201  # stray fields like "author" are ignored, never applied
    assert p["author_type"] == "user" and p["author_id"] == w["user"].pid


def test_invalid_body_is_structured_400(w):
    st, err = H(w, w["user"], "POST", "/api/v1/posts", "not json{{")
    assert st == 400 and err["error"]["code"] == "invalid_json"


def test_task_lifecycle_via_api(w):
    st, out = H(w, w["user"], "POST", "/api/v1/admin/agents", {"name": "argus"})
    ag = auth.authenticate(w["con"], out["token"])
    st, t = H(w, w["user"], "POST", "/api/v1/tasks",
              {"key": "A-1", "title": "sprint", "agent": "argus"})
    assert st == 201 and t["status"] == "assigned"
    st, t2 = H(w, ag, "POST", f"/api/v1/tasks/{t['id']}/transition",
               {"to": "active", "expected_version": t["version"]})
    assert st == 200 and t2["status"] == "active"
    # user can't approve their own way around stale versions:
    st, err = H(w, ag, "POST", f"/api/v1/tasks/{t['id']}/transition",
                {"to": "validating", "expected_version": 0})
    assert st == 409 and err["error"]["code"] == "stale_write"
    H(w, ag, "POST", f"/api/v1/tasks/{t['id']}/events",
      {"type": "test", "payload": {"ref": "pytest 20/20"}})
    st, done = H(w, ag, "POST", f"/api/v1/tasks/{t['id']}/transition",
                 {"to": "validating", "expected_version": 1})
    st, done = H(w, ag, "POST", f"/api/v1/tasks/{t['id']}/transition",
                 {"to": "completed", "expected_version": done["version"]})
    assert st == 200 and done["status"] == "completed"


def test_idempotency_replay_status_200(w):
    st1, p = H(w, w["user"], "POST", "/api/v1/posts",
               {"body": "dup?", "idem_key": "k1"})
    st2, p2 = H(w, w["user"], "POST", "/api/v1/posts",
                {"body": "dup?", "idem_key": "k1"})
    assert st1 == 201 and st2 == 200 and p["id"] == p2["id"]
