-- =====================================================================
-- Minimality — canonical database schema
-- Single SQLite file. Three layers:
--   1. Relational tables (below)         = source of truth
--   2. sqlite-vec virtual tables         = vector index, rebuildable
--   3. graphqlite nodes/edges            = graph index, rebuildable
-- Layers 2 and 3 must never contain data that cannot be regenerated
-- from layer 1.
--
-- Conventions:
--   * STRICT tables everywhere the engine allows (virtual tables can't).
--   * Timestamps: TEXT, ISO-8601 UTC with ms ("2026-07-13T14:02:00.000Z").
--   * Enums enforced with CHECK constraints.
--   * JSON columns validated with json_valid().
--   * Secrets never stored — providers reference an env-var NAME only.
-- =====================================================================

PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

-- ---------------------------------------------------------------------
-- 0. Meta
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

-- Records which embedding model produced the vectors currently in the
-- vec tables. If model/dim changes, every vec table must be rebuilt.
CREATE TABLE IF NOT EXISTS system_meta (
    key    TEXT PRIMARY KEY,   -- 'embedding_model', 'embedding_dim', ...
    value  TEXT NOT NULL
) STRICT;

-- ---------------------------------------------------------------------
-- 1. Sessions & raw transcript (episodic memory)
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    title       TEXT,
    metadata    TEXT CHECK (metadata IS NULL OR json_valid(metadata))
) STRICT;

-- Replaces episodic_logs. Raw, append-only transcript.
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY,
    session_id   INTEGER NOT NULL REFERENCES sessions(id),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    role         TEXT NOT NULL CHECK (role IN ('user','assistant','system','tool')),
    content      TEXT NOT NULL,
    -- Sensitivity gate (§2.6 DIRECTION.md). Default is the SAFE value;
    -- the local classifier upgrades rows to 'shareable'.
    sensitivity  TEXT NOT NULL DEFAULT 'local_only'
                 CHECK (sensitivity IN ('local_only','shareable')),
    provider_id  INTEGER REFERENCES providers(id),  -- NULL for user/system rows
    metadata     TEXT CHECK (metadata IS NULL OR json_valid(metadata))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

-- ---------------------------------------------------------------------
-- 2. Knowledge nodes (universal node table) + edges
--    Entities, tools, and skills all live here; tools/skills carry
--    extra columns in subtype tables (class-table inheritance).
--    This is the ONLY namespace mirrored into graphqlite.
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS nodes (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,                      -- casefolded, trimmed
    kind          TEXT NOT NULL DEFAULT 'entity'
                  CHECK (kind IN ('entity','tool','skill')),
    entity_type   TEXT,          -- 'framework','library','language',... (extractor's Entity.type)
    description   TEXT,          -- embedded into vec_nodes
    aliases       TEXT CHECK (aliases IS NULL OR json_valid(aliases)),  -- JSON array
    confidence    REAL,
    sensitivity   TEXT NOT NULL DEFAULT 'shareable'
                  CHECK (sensitivity IN ('local_only','shareable')),
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_accessed TEXT,
    UNIQUE (name, kind)
) STRICT;

-- Source of truth for ALL relationships. graphqlite is a mirror of
-- exactly this table and nothing else.
CREATE TABLE IF NOT EXISTS edges (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    target_id   INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    rel_type    TEXT NOT NULL,   -- 'uses','depends_on','is_a','part_of','calls','references'
    evidence    TEXT,            -- exact text span that supports the edge
    confidence  REAL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (source_id, target_id, rel_type)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);

-- ---------------------------------------------------------------------
-- 3. Task Ledger (§2.1 DIRECTION.md) — the briefing-pack source
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY,
    session_id   INTEGER REFERENCES sessions(id),
    goal         TEXT NOT NULL,
    constraints  TEXT CHECK (constraints IS NULL OR json_valid(constraints)), -- JSON array
    task_type    TEXT,           -- routing category: 'code_gen','refactor','research','summarize',...
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

CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    decision    TEXT NOT NULL,
    rationale   TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

CREATE TABLE IF NOT EXISTS open_questions (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    question    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','answered','dropped')),
    answer      TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

CREATE TABLE IF NOT EXISTS artifacts (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_id      INTEGER REFERENCES runs(id),
    path        TEXT NOT NULL,   -- relative to the session workspace
    kind        TEXT NOT NULL DEFAULT 'file'
                CHECK (kind IN ('file','tool','report','dataset','other')),
    sha256      TEXT,            -- content hash at time of registration
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

-- ---------------------------------------------------------------------
-- 4. Provider registry + telemetry
--    Meters and bandit scores are NOT stored columns — they are views
--    over the append-only telemetry (rebuildable, audit-friendly).
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS providers (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,   -- 'groq/llama-4-scout', 'openrouter/deepseek-r1:free'
    base_url        TEXT NOT NULL,
    model           TEXT NOT NULL,          -- provider-side model id
    api_key_env     TEXT NOT NULL,          -- NAME of env var holding the key. Never the key.
    context_window  INTEGER,
    -- declared free-tier limits (NULL = unknown/unlimited)
    rpm_limit       INTEGER,
    rpd_limit       INTEGER,
    tpm_limit       INTEGER,
    tpd_limit       INTEGER,
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    notes           TEXT
) STRICT;

-- One row per API request. Meters derive from this.
CREATE TABLE IF NOT EXISTS llm_calls (
    id                 INTEGER PRIMARY KEY,
    provider_id        INTEGER NOT NULL REFERENCES providers(id),
    run_id             INTEGER REFERENCES runs(id),
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    prompt_tokens      INTEGER,
    completion_tokens  INTEGER,
    latency_ms         INTEGER,
    status             TEXT NOT NULL
                       CHECK (status IN ('ok','rate_limited','error','timeout','parse_error')),
    error              TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS idx_llm_calls_provider_time ON llm_calls(provider_id, created_at);

-- ---------------------------------------------------------------------
-- 5. Runs — one orchestrator dispatch (worker turn + execution +
--    verification). The unit the bandit router scores on.
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS runs (
    id                   INTEGER PRIMARY KEY,
    task_id              INTEGER NOT NULL REFERENCES tasks(id),
    step_id              INTEGER REFERENCES plan_steps(id),
    provider_id          INTEGER REFERENCES providers(id),  -- NULL = handled locally (skill hit)
    task_type            TEXT NOT NULL,
    started_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at          TEXT,
    outcome              TEXT CHECK (outcome IN ('verified','failed','unverified','aborted')),
    verification_detail  TEXT CHECK (verification_detail IS NULL OR json_valid(verification_detail)),
    retries              INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE INDEX IF NOT EXISTS idx_runs_bandit ON runs(provider_id, task_type, finished_at);

-- ---------------------------------------------------------------------
-- 6. Tool Forge (§2.2 DIRECTION.md)
--    A tool is a node (kind='tool') plus this subtype row.
--    Retrieval = vector search on nodes.description via vec_nodes.
--    Dependencies = edges (tool node -[depends_on]-> library node).
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tools (
    node_id              INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    signature            TEXT NOT NULL CHECK (json_valid(signature)),  -- params JSON schema
    code                 TEXT NOT NULL,                                -- current source
    version              INTEGER NOT NULL DEFAULT 1,
    status               TEXT NOT NULL DEFAULT 'draft'
                         CHECK (status IN ('draft','tested','promoted','deprecated','quarantined')),
    created_by_provider  INTEGER REFERENCES providers(id),
    created_in_run       INTEGER REFERENCES runs(id),
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    promoted_at          TEXT
) STRICT;

-- Acceptance examples supplied by the authoring model; rerun on every
-- version bump. All must pass before status can become 'promoted'.
CREATE TABLE IF NOT EXISTS tool_tests (
    id           INTEGER PRIMARY KEY,
    tool_id      INTEGER NOT NULL REFERENCES tools(node_id) ON DELETE CASCADE,
    input        TEXT NOT NULL CHECK (json_valid(input)),    -- args JSON
    expected     TEXT NOT NULL,                               -- expected output / assertion expr
    passed       INTEGER CHECK (passed IN (0,1)),
    last_result  TEXT,
    last_run_at  TEXT
) STRICT;

-- Invocation log (feeds tool reliability stats and debugging).
CREATE TABLE IF NOT EXISTS tool_calls (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES runs(id),
    tool_id      INTEGER NOT NULL REFERENCES tools(node_id),
    args         TEXT CHECK (args IS NULL OR json_valid(args)),
    result       TEXT,
    ok           INTEGER NOT NULL CHECK (ok IN (0,1)),
    duration_ms  INTEGER,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_tool_calls_tool ON tool_calls(tool_id, created_at);

-- ---------------------------------------------------------------------
-- 7. Skill cards (§2.3 DIRECTION.md)
--    A skill is a node (kind='skill') plus this subtype row.
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS skills (
    node_id        INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    procedure      TEXT NOT NULL,     -- markdown: steps, tools used, pitfalls
    task_type      TEXT NOT NULL,
    source_run_id  INTEGER REFERENCES runs(id),
    success_count  INTEGER NOT NULL DEFAULT 0,
    failure_count  INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_used_at   TEXT
) STRICT;

-- ---------------------------------------------------------------------
-- 8. Vector index layer (sqlite-vec, rebuildable)
--    Requires sqlite-vec >= 0.1.6 for the metadata column (kind) used
--    in filtered KNN. Rebuild procedure: DROP, recreate, re-embed from
--    messages / nodes.
-- ---------------------------------------------------------------------

CREATE VIRTUAL TABLE IF NOT EXISTS vec_messages USING vec0(
    message_id  INTEGER PRIMARY KEY,       -- = messages.id
    embedding   float[256]
);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_nodes USING vec0(
    node_id     INTEGER PRIMARY KEY,       -- = nodes.id
    embedding   float[256],                -- embeds name + description
    kind        TEXT                       -- metadata col: filtered KNN by 'entity'/'tool'/'skill'
);

-- ---------------------------------------------------------------------
-- 9. Derived views — meters and bandit scores
-- ---------------------------------------------------------------------

-- Live per-minute meter (compare against providers.rpm_limit / tpm_limit).
CREATE VIEW IF NOT EXISTS v_provider_minute_usage AS
SELECT provider_id,
       COUNT(*)                                            AS requests_1m,
       COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens_1m
FROM llm_calls
WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-60 seconds')
GROUP BY provider_id;

-- Rolling-24h meter (compare against rpd_limit / tpd_limit).
CREATE VIEW IF NOT EXISTS v_provider_day_usage AS
SELECT provider_id,
       COUNT(*)                                            AS requests_24h,
       COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens_24h,
       SUM(status = 'rate_limited')                        AS rate_limited_24h,
       SUM(status = 'parse_error')                         AS parse_errors_24h
FROM llm_calls
WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-24 hours')
GROUP BY provider_id;

-- Bandit input: per provider × task_type success over a 14-day window.
CREATE VIEW IF NOT EXISTS v_bandit_scores AS
SELECT provider_id,
       task_type,
       COUNT(*)                    AS n_runs,
       AVG(outcome = 'verified')   AS success_rate,
       AVG(retries)                AS avg_retries
FROM runs
WHERE finished_at IS NOT NULL
  AND finished_at >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-14 days')
  AND provider_id IS NOT NULL
GROUP BY provider_id, task_type;

INSERT OR IGNORE INTO schema_migrations(version) VALUES (1);
