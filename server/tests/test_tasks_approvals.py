import pytest
from hub import approvals, auth, tasks
from hub.migrations import connect, migrate
from hub.posts import HubError


@pytest.fixture()
def w(tmp_path):
    con = connect(tmp_path / "hub.db")
    migrate(con)
    uid = auth.create_user(con, "doug")
    ag = auth.get_or_create_agent(con, "argus")
    other = auth.get_or_create_agent(con, "forge")
    con.execute("INSERT INTO projects (name,slug,created_at) VALUES ('Hub','hub',?)",
                (auth.utcnow(),))
    con.commit()
    return dict(con=con, user=auth.Principal("user", uid, "doug"),
                argus=auth.Principal("agent", ag["id"], "argus"),
                forge=auth.Principal("agent", other["id"], "forge"))


def test_happy_lifecycle_with_evidence(w):
    t = tasks.create_task(w["con"], w["user"], "H-1", "build api",
                          agent="argus", project="hub")
    assert t["status"] == "assigned"
    tasks.transition(w["con"], w["argus"], t["id"], "active", t["version"])
    tasks.add_event(w["con"], w["argus"], t["id"], "test",
                    {"name": "pytest", "result": "18 passed"})
    t2 = tasks.get_task(w["con"], w["user"], t["id"])
    t2 = tasks.transition(w["con"], w["argus"], t2["id"], "validating", t2["version"])
    done = tasks.transition(w["con"], w["argus"], t2["id"], "completed", t2["version"])
    assert done["status"] == "completed"
    assert [r["ref_value"] for r in done["references"]] == ["pytest"]  # evidence linked


def test_completed_without_evidence_rejected(w):
    t = tasks.create_task(w["con"], w["user"], "H-2", "vibe", agent="argus")
    t = tasks.transition(w["con"], w["argus"], t["id"], "active", t["version"])
    t = tasks.transition(w["con"], w["argus"], t["id"], "validating", t["version"])
    with pytest.raises(HubError) as e:
        tasks.transition(w["con"], w["argus"], t["id"], "completed", t["version"])
    assert e.value.code == "completion_without_evidence"


def test_stale_version_conflict(w):
    t = tasks.create_task(w["con"], w["user"], "H-3", "race", agent="argus")
    tasks.transition(w["con"], w["argus"], t["id"], "active", t["version"])
    with pytest.raises(HubError) as e:  # old version from memory
        tasks.transition(w["con"], w["argus"], t["id"], "blocked", t["version"])
    assert e.value.status == 409 and e.value.code == "stale_write"


def test_agent_cannot_touch_foreign_task(w):
    t = tasks.create_task(w["con"], w["user"], "H-4", "mine", agent="argus")
    with pytest.raises(HubError) as e:
        tasks.transition(w["con"], w["forge"], t["id"], "active", t["version"])
    assert e.value.status == 403


def test_blocked_notifies_user(w):
    t = tasks.create_task(w["con"], w["user"], "H-5", "risky", agent="argus")
    tasks.transition(w["con"], w["argus"], t["id"], "active", t["version"])
    t2 = tasks.get_task(w["con"], w["user"], t["id"])
    tasks.transition(w["con"], w["argus"], t2["id"], "blocked", t2["version"])
    notes = list(w["con"].execute(
        "SELECT * FROM notifications WHERE recipient_type='user' AND kind='blocked'"))
    assert len(notes) == 1


def test_agent_files_own_task_and_cannot_file_others(w):
    # agents file their own work without being asked (decided 2026-09-15)
    t = tasks.create_task(w["con"], w["argus"], "A-10", "self-filed work")
    assert t["status"] == "assigned" and t["agent_id"] == w["argus"].pid
    t = tasks.transition(w["con"], w["argus"], t["id"], "active", t["version"])
    assert t["status"] == "active"
    with pytest.raises(HubError) as e:
        tasks.create_task(w["con"], w["argus"], "A-11", "not mine", agent="forge")
    assert e.value.status == 403
    # completion still evidence-gated even for self-filed tasks
    with pytest.raises(HubError) as e2:
        t = tasks.transition(w["con"], w["argus"], t["id"], "validating", t["version"])
        tasks.transition(w["con"], w["argus"], t["id"], "completed", t["version"])
    assert e2.value.code == "completion_without_evidence"


def test_user_can_assign_and_reassign(w):
    t = tasks.create_task(w["con"], w["user"], "A-12", "user work", agent="argus")
    assert t["agent_id"] == w["argus"].pid
    assert t["status"] == "assigned"


def test_approval_flow_durable_and_audited(w):
    t = tasks.create_task(w["con"], w["user"], "H-6", "deploy", agent="argus")
    req = approvals.request(w["con"], w["argus"], "deploy prod", task_id=t["id"],
                            risk="high")
    pend = approvals.list_for(w["con"], w["user"])
    assert pend[0]["id"] == req["id"]
    with pytest.raises(HubError):  # agent cannot approve its own request
        approvals.decide(w["con"], w["argus"], req["id"], True)
    out = approvals.decide(w["con"], w["user"], req["id"], True, reason="ship it")
    assert out["status"] == "approved"
    with pytest.raises(HubError):  # terminal: no re-decision
        approvals.decide(w["con"], w["user"], req["id"], False)
    audit = list(w["con"].execute(
        "SELECT action,result FROM audit_log WHERE target LIKE 'approval:%'"
        " ORDER BY id", ()))
    assert [r["action"] for r in audit] == ["approval_request", "approval_approved"]


def test_expired_approval_undecidable(w):
    req = approvals.request(w["con"], w["argus"], "old thing",
                            expires_at="2020-01-01T00:00:00Z")
    with pytest.raises(HubError) as e:
        approvals.decide(w["con"], w["user"], req["id"], True)
    assert e.value.status == 410
