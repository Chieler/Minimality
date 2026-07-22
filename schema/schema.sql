-- =====================================================================
-- Minimality — v1 (MVP) schema. 11 tables + 2 vector indexes.
-- Simplified from the full design (see docs/DB_SCHEMA.md history):
--   * providers table          -> LiteLLM Router config (config.yaml)
--   * meter views              -> LiteLLM rpm/tpm limits + cooldowns
--   * decisions/open_questions -> merged into `notes`
--   * tool_tests/tool_calls    -> JSON on `tools` / detail on `runs`
--   * artifacts                -> recorded in runs.detail JSON
--   * skills                   -> deferred (nodes.kind reserves 'skill')
--   * graphqlite mirror        -> dropped; edges queried with SQL
--   * schema_migrations        -> system_meta key 'schema_version'
--
-- Rules kept from the full design:
--   * Relational tables are the source of truth; vec tables rebuildable.
--   * STRICT, ISO-8601 UTC text timestamps, CHECK enums, json_valid.
--   * No secrets in the DB (API keys live in .env, read by LiteLLM).
-- =====================================================================

PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

-- 'schema_version', 'embedding_model', 'embedding_dim'
CREATE TABLE IF NOT EXISTS system_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
) STRICT;

-- ------------------------------------------------------------------ --
-- Conversation memory
-- ------------------------------------------------------------------ --

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    title       TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY,
    session_id   INTEGER NOT NULL REFERENCES sessions(id),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    role         TEXT NOT NULL CHECK (role IN ('user','assistant','system','tool')),
    content      TEXT NOT NULL,
    sensitivity  TEXT NOT NULL DEFAULT 'local_only'
                 CHECK (sensitivity IN ('local_only','shareable')),
    provider     TEXT,   -- LiteLLM model string of the author, NULL for user
    metadata     TEXT CHECK (metadata IS NULL OR json_valid(metadata))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

-- ------------------------------------------------------------------ --
-- Knowledge: universal node table + edges (SQL-queried, no graph lib)
-- ------------------------------------------------------------------ --

CREATE TABLE IF NOT EXISTS nodes (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'entity'
                 CHECK (kind IN ('entity','tool','skill')),
    entity_type  TEXT,
    description  TEXT,                 -- the embedded field
    meta         TEXT CHECK (meta IS NULL OR json_valid(meta)),  -- aliases, confidence, ...
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (name, kind)
) STRICT;

CREATE TABLE IF NOT EXISTS edges (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    target_id   INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    rel_type    TEXT NOT NULL,
    evidence    TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (source_id, target_id, rel_type)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);

-- ------------------------------------------------------------------ --
-- Task ledger (briefing-pack source)
-- ------------------------------------------------------------------ --

CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY,
    session_id   INTEGER REFERENCES sessions(id),
    goal         TEXT NOT NULL,
    constraints  TEXT CHECK (constraints IS NULL OR json_valid(constraints)),
    task_type    TEXT,
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','done','failed','paused')),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

CREATE TABLE IF NOT EXISTS plan_steps (
    id              INTEGER PRIMARY KEY,
    task_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL,
    description     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'todo'
                    CHECK (status IN ('todo','doing','done','blocked','skipped')),
    result_summary  TEXT,
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (task_id, position)
) STRICT;

-- Decisions and open questions, one table.
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('decision','question')),
    content     TEXT NOT NULL,          -- the decision, or the question
    resolution  TEXT,                   -- rationale / answer; NULL = open question
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

-- ------------------------------------------------------------------ --
-- Execution
-- ------------------------------------------------------------------ --

-- One orchestrator dispatch: worker turn + sandbox execution + verify.
-- detail JSON carries: verification checks, tool invocations, artifact
-- paths + hashes. Promote to columns later only if queried often.
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY,
    task_id      INTEGER NOT NULL REFERENCES tasks(id),
    step_id      INTEGER REFERENCES plan_steps(id),
    provider     TEXT,     -- LiteLLM model string; NULL = handled locally
    task_type    TEXT,
    started_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at  TEXT,
    outcome      TEXT CHECK (outcome IN ('verified','failed','unverified','aborted')),
    detail       TEXT CHECK (detail IS NULL OR json_valid(detail))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_runs_provider ON runs(provider, task_type, finished_at);

-- One row per API request, written by the LiteLLM success/failure
-- callback. Observability + future bandit input; LiteLLM itself handles
-- rate limits and cooldowns, so no meter views needed.
CREATE TABLE IF NOT EXISTS llm_calls (
    id                 INTEGER PRIMARY KEY,
    run_id             INTEGER REFERENCES runs(id),
    provider           TEXT NOT NULL,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    prompt_tokens      INTEGER,
    completion_tokens  INTEGER,
    latency_ms         INTEGER,
    status             TEXT NOT NULL
                       CHECK (status IN ('ok','rate_limited','error','timeout','parse_error')),
    error              TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS idx_llm_calls_provider ON llm_calls(provider, created_at);

-- ------------------------------------------------------------------ --
-- Tool forge (minimal): tool = node(kind='tool') + this row.
-- tests JSON: [{"input": {...}, "expected": "...", "passed": true}]
-- Promotion gate (all tests passed) enforced in code.
-- ------------------------------------------------------------------ --

CREATE TABLE IF NOT EXISTS tools (
    node_id      INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    code         TEXT NOT NULL,
    signature    TEXT NOT NULL CHECK (json_valid(signature)),
    tests        TEXT CHECK (tests IS NULL OR json_valid(tests)),
    status       TEXT NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft','promoted','deprecated')),
    version      INTEGER NOT NULL DEFAULT 1,
    created_by   TEXT,     -- LiteLLM model string of the authoring worker
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    promoted_at  TEXT
) STRICT;

-- ------------------------------------------------------------------ --
-- Vector indexes (sqlite-vec >= 0.1.6; rebuildable from messages/nodes)
-- ------------------------------------------------------------------ --

CREATE VIRTUAL TABLE IF NOT EXISTS vec_messages USING vec0(
    message_id  INTEGER PRIMARY KEY,
    embedding   float[256]
);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_nodes USING vec0(
    node_id     INTEGER PRIMARY KEY,
    embedding   float[256],
    kind        TEXT
);

INSERT OR IGNORE INTO system_meta(key, value) VALUES ('schema_version', '1');
