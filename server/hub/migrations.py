"""Argus Hub migrations. Forward-only, versioned, safe to re-run.

v1 schema: clean two-way comms + control domain (no legacy BotFeed tables)."""
import sqlite3

SCHEMA_VERSION = 1

_M = {
1: """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    handle TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','retired')),
    capabilities TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    retired_at TEXT
);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    principal_type TEXT NOT NULL CHECK (principal_type IN ('user','agent')),
    principal_id INTEGER NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created' CHECK (status IN
        ('created','assigned','active','validating','completed','failed','blocked')),
    agent_id INTEGER REFERENCES agents(id),
    project_id INTEGER REFERENCES projects(id),
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    author_type TEXT NOT NULL CHECK (author_type IN ('user','agent')),
    author_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('user_update','agent_event')),
    body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 2000),
    priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN
        ('low','normal','high','critical')),
    persistence TEXT NOT NULL DEFAULT 'session' CHECK (persistence IN
        ('transient','session','project','durable')),
    memory_eligible INTEGER NOT NULL DEFAULT 0 CHECK (memory_eligible IN (0,1)),
    task_id INTEGER REFERENCES tasks(id),
    project_id INTEGER REFERENCES projects(id),
    expires_at TEXT,
    idem_key TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (author_type, author_id, idem_key),
    CHECK ((author_type='user' AND kind='user_update')
        OR (author_type='agent' AND kind='agent_event'))
);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts (created_at, id);
CREATE INDEX IF NOT EXISTS idx_posts_expires ON posts (expires_at);
CREATE TABLE IF NOT EXISTS post_audiences (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    audience_type TEXT NOT NULL CHECK (audience_type IN ('all','project','agent','selected')),
    ref TEXT NOT NULL DEFAULT '',
    UNIQUE (post_id, audience_type, ref)
);
CREATE INDEX IF NOT EXISTS idx_aud_ref ON post_audiences (audience_type, ref);
CREATE TABLE IF NOT EXISTS references_link (
    id INTEGER PRIMARY KEY,
    post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,
    task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    ref_type TEXT NOT NULL CHECK (ref_type IN
        ('commit','test','artifact','incident','project','url','file')),
    ref_value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (post_id IS NOT NULL OR task_id IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    author_type TEXT NOT NULL CHECK (author_type IN ('user','agent')),
    author_id INTEGER NOT NULL,
    body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 1000),
    idem_key TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (author_type, author_id, post_id, idem_key)
);
CREATE TABLE IF NOT EXISTS reactions (
    post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('user','agent')),
    actor_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('ack','concern','resolved')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (post_id, actor_type, actor_id, kind)
);
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK (type IN
        ('assignment','attempt','test','commit','error','handoff','cost','note')),
    payload TEXT NOT NULL,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('user','agent')),
    actor_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tev_task ON task_events (task_id);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY,
    requested_by_type TEXT NOT NULL CHECK (requested_by_type IN ('user','agent')),
    requested_by_id INTEGER NOT NULL,
    task_id INTEGER REFERENCES tasks(id),
    action TEXT NOT NULL,
    risk TEXT NOT NULL DEFAULT 'medium' CHECK (risk IN ('low','medium','high')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
        ('pending','approved','denied','expired')),
    created_at TEXT NOT NULL,
    decided_by_type TEXT, decided_by_id INTEGER, decided_at TEXT,
    expires_at TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    recipient_type TEXT NOT NULL CHECK (recipient_type IN ('user','agent')),
    recipient_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info' CHECK (severity IN ('info','notice','action_required')),
    message TEXT NOT NULL,
    post_id INTEGER, task_id INTEGER, approval_id INTEGER,
    created_at TEXT NOT NULL,
    read_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notif_recipient ON notifications (recipient_type, recipient_id, read_at);
CREATE TABLE IF NOT EXISTS memory_promotions (
    id INTEGER PRIMARY KEY,
    source_post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    proposed_by_type TEXT NOT NULL CHECK (proposed_by_type IN ('user','agent')),
    proposed_by_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','approved','rejected')),
    target TEXT, provenance TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    principal_type TEXT, principal_id INTEGER,
    action TEXT NOT NULL,
    target TEXT,
    result TEXT NOT NULL,
    details TEXT
);
""",
}


def connect(path):
    con = sqlite3.connect(str(path), timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 5000")
    return con


def current_version(con):
    row = con.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(con):
    """Apply pending migrations in order; each migration is one transaction."""
    v = current_version(con)
    applied = []
    for ver in sorted(_M):
        if ver <= v:
            continue
        with con:  # one transaction per migration: all or nothing
            con.executescript(_M[ver])
            # PRAGMA user_version is not transactional, so set it last and
            # separately; a crash mid-script leaves the version unmoved.
            con.execute("PRAGMA user_version = %d" % ver)
        applied.append(ver)
    return applied
