import pytest
from hub import auth
from hub.migrations import connect, migrate


@pytest.fixture()
def db(tmp_path):
    con = connect(tmp_path / "hub.db")
    assert migrate(con) == [1]
    return con


def test_migration_idempotent(db):
    assert migrate(db) == []  # nothing pending
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("posts", "task_events", "approvals", "notifications",
              "memory_promotions", "audit_log", "references_link"):
        assert t in tables


def test_token_flow_and_revocation(db):
    uid = auth.create_user(db, "doug")
    tok = auth.issue_token(db, "user", uid)
    p = auth.authenticate(db, tok)
    assert p == auth.Principal("user", uid, "doug")
    with db:
        db.execute("UPDATE tokens SET revoked_at=? WHERE token_hash IS NOT NULL",
                   (auth.utcnow(),))
    assert auth.authenticate(db, tok) is None


def test_agent_retire_kills_tokens(db):
    ag = auth.get_or_create_agent(db, "argus", ("se", "hub-post"))
    tok = auth.issue_token(db, "agent", ag["id"], "gym")
    assert auth.authenticate(db, tok).name == "argus"
    auth.retire_agent(db, ag["id"])
    assert auth.authenticate(db, tok) is None


def test_no_user_named_lookup_bypass(db):
    assert auth.authenticate(db, "garbage") is None
    assert auth.authenticate(db, "") is None


def test_forged_authority_columns(db):
    """posts.kind is server-derived: an agent cannot write user_update."""
    ag = auth.get_or_create_agent(db, "argus")
    with pytest.raises(db.IntegrityError):
        db.execute("INSERT INTO posts (author_type,author_id,kind,body,created_at)"
                   " VALUES ('agent',?,'user_update','x',?)", (ag["id"], auth.utcnow()))
