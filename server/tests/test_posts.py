import pytest
from hub import auth, posts
from hub.migrations import connect, migrate
from hub.posts import HubError


@pytest.fixture()
def w(tmp_path):
    con = connect(tmp_path / "hub.db")
    migrate(con)
    uid = auth.create_user(con, "doug")
    user = auth.Principal("user", uid, "doug")
    a1 = auth.get_or_create_agent(con, "argus")
    ag1 = auth.Principal("agent", a1["id"], "argus")
    a2 = auth.get_or_create_agent(con, "forge")
    ag2 = auth.Principal("agent", a2["id"], "forge")
    con.execute("INSERT INTO projects (name,slug,created_at) VALUES ('Apollo','apollo',?)",
                (auth.utcnow(),))
    con.execute("INSERT INTO tasks (key,title,agent_id,project_id,created_at,updated_at)"
                " VALUES ('AP-1','dsp spike',?,?,?,?)",
                (a1["id"], 1, auth.utcnow(), auth.utcnow()))
    con.commit()
    return dict(con=con, user=user, argus=ag1, forge=ag2)


def pget(w, pid, who):
    return posts.get_post(w["con"], w[who], pid)


def test_user_to_all_agents_and_pagination(w):
    for i in range(3):
        posts.create_post(w["con"], w["user"], body=f"context {i}", idem_key=f"c{i}")
    tl = posts.timeline(w["con"], w["argus"], limit=2)
    assert len(tl) == 2 and tl[0]["body"] == "context 2"  # newest first
    cur = f"{tl[-1]['created_at']}|{tl[-1]['id']}"
    nxt = posts.timeline(w["con"], w["argus"], since=cur, limit=2)
    assert [r["body"] for r in nxt] == ["context 0"]


def test_private_agent_audience_not_visible_to_other_agent(w):
    p = posts.create_post(w["con"], w["user"], body="forge only: use ASan",
                          audiences=[{"type": "agent", "ref": "agent:forge"}])
    assert pget(w, p["id"], "forge")["body"] == "forge only: use ASan"
    with pytest.raises(HubError) as e:
        pget(w, p["id"], "argus")
    assert e.value.status == 403
    assert pget(w, p["id"], "user")  # author always sees own
    assert posts.timeline(w["con"], w["argus"]) == []  # not in wrong eyes


def test_project_audience_visible_to_working_agent(w):
    p = posts.create_post(w["con"], w["user"], body="apollo constraint",
                          audiences=[{"type": "project", "ref": "apollo"}])
    assert pget(w, p["id"], "argus")  # active task on apollo
    with pytest.raises(HubError):
        pget(w, p["id"], "forge")     # no task on apollo


def test_expiry_hidden_then_included(w):
    p = posts.create_post(w["con"], w["user"], body="temp until 2020",
                          expires_at="2020-01-01T00:00:00Z")
    assert posts.timeline(w["con"], w["argus"]) == []
    assert len(posts.timeline(w["con"], w["argus"], include_expired=True)) == 1
    assert pget(w, p["id"], "argus")  # expiry only hides timeline rows; direct fetch still works


def test_idempotent_replay(w):
    a = posts.create_post(w["con"], w["argus"], body="drill done",
                          priority="normal", idem_key="gym-042")
    b = posts.create_post(w["con"], w["argus"], body="drill done",
                          priority="normal", idem_key="gym-042")
    assert a["id"] == b["id"] and b["replayed"] is True
    assert len(posts.timeline(w["con"], w["user"])) == 1


def test_reply_react_and_fanout_notifications(w):
    p = posts.create_post(w["con"], w["user"], body="who can help?")
    posts.reply(w["con"], w["argus"], p["id"], "I can", idem_key="r1")
    again = posts.reply(w["con"], w["argus"], p["id"], "I can", idem_key="r1")
    assert again["replayed"] is True
    posts.react(w["con"], w["user"], p["id"], "ack")
    got = pget(w, p["id"], "user")
    assert got["reply_count"] == 1 and got["reactions"] == {"ack": 1}
    posts.create_post(w["con"], w["argus"], body="BLOCKED on approvals", priority="critical")
    notes = list(w["con"].execute("SELECT * FROM notifications WHERE recipient_type='user'"))
    assert len(notes) >= 2  # reply + critical


def test_memory_promotion_is_gated_never_auto(w):
    p = posts.create_post(w["con"], w["user"], body="decision: use WAL",
                          memory_eligible=True)
    assert posts.timeline(w["con"], w["user"])[0]["memory_eligible"] == 1
    promo = posts.propose_memory(w["con"], w["argus"], p["id"], target="06-Decisions")
    assert promo["status"] == "proposed"
    with pytest.raises(HubError):  # agent cannot self-approve
        posts.decide_memory(w["con"], w["argus"], promo["id"], True)
    out = posts.decide_memory(w["con"], w["user"], promo["id"], True)
    assert out["status"] == "approved"
    with pytest.raises(HubError):  # double-decide blocked
        posts.decide_memory(w["con"], w["user"], promo["id"], False)


def test_body_validation(w):
    with pytest.raises(HubError):
        posts.create_post(w["con"], w["user"], body="")
    with pytest.raises(HubError):
        posts.create_post(w["con"], w["user"], body="x" * 2001)
    with pytest.raises(HubError):
        posts.create_post(w["con"], w["user"], body="ok", priority="urgent-please")
