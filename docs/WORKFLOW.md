# Minimality — Workflow Graph & Data Lineage

> **v1 note:** the 3-week build implements a simplified version of these
> flows — see [`MVP.md`](MVP.md) §3 for the v1 workflow and what each stage
> shrank to. This doc remains the full-design reference.

Companion to [`DB_SCHEMA.md`](DB_SCHEMA.md) (what the tables are) and
[`DIRECTION.md`](DIRECTION.md) (why). This doc shows **when each table is
touched during a turn, and what data feeds which column**.

Legend used in every diagram:

```mermaid
flowchart LR
    P[process step] --> T[(table)]
    V{{view / derived}} --> D{decision}
    M([system_meta key]) -.->|config read| P
    T -->|"edge label = columns read/written"| P
```

- `[(cylinder)]` = relational table (source of truth)
- `{{hexagon}}` = derived view or rebuildable index (vec / graph layers)
- `([stadium])` = a single `system_meta` **key as its own node** — meta values
  are configuration inputs to processes, not part of the data flow, so each
  key gets its own node and dotted edges
- solid edge = data write/read on the hot path; dotted edge = config read
- edge labels name the exact columns involved

---

## 0. One turn, end to end

Every numbered stage has its own detailed graph below.

```mermaid
flowchart LR
    U([user message]) --> S1[1 · Ingest<br/>& classify]
    S1 --> S2[2 · Ledger update<br/>local Qwen]
    S2 --> S3{3 · Skill-first<br/>check}
    S3 -->|skill hit| S7
    S3 -->|miss| S4[4 · Route<br/>provider]
    S4 --> S5[5 · Render<br/>briefing pack]
    S5 --> S6[6 · Worker loop<br/>generate + tool calls]
    S6 --> S7[7 · Sandbox<br/>execute & verify]
    S7 --> S8[8 · Synthesize, extract,<br/>distill, reply]
    S8 --> R([reply to user])
    S6 -.->|quota hit /<br/>provider dies| S4
```

The dotted loop `6 → 4` is the failover path: because state lives in the
ledger and memory (not the provider's context), re-routing mid-task re-enters
at stage 4 and costs one briefing-pack render.

---

## 1. Ingest & classify

First touch of the turn. Note the two `system_meta` keys as standalone nodes —
the embedder consults them on startup; a mismatch with the loaded model routes
to the rebuild flow (§9) instead of silently writing incompatible vectors.

```mermaid
flowchart TD
    U([user message]) -->|"session_id ← current session<br/>role='user', content=text"| MSG[(messages)]
    SES[(sessions)] -->|id| U

    QC[Qwen sensitivity classifier] -->|"sensitivity:<br/>'local_only' → 'shareable'<br/>(upgrade only, fails closed)"| MSG

    MSG -->|content| EMB[embedder]
    MK([system_meta:<br/>embedding_model]) -.->|must match loaded model| EMB
    DK([system_meta:<br/>embedding_dim]) -.->|vector length 256| EMB
    EMB -->|"message_id = messages.id<br/>embedding = float[256]"| VMSG{{vec_messages}}
```

## 2. Ledger update (local Qwen)

Qwen reads the new message plus current ledger and makes *small targeted
writes* — never a full rewrite. This is the continuously-maintained state that
makes handoff O(1).

```mermaid
flowchart TD
    QW[Qwen orchestrator]

    QW -->|"new task: goal, constraints (JSON),<br/>task_type, session_id, status='active'"| TAS[(tasks)]
    QW -->|"task_id, position, description<br/>status: todo→doing→done/blocked"| PS[(plan_steps)]
    QW -->|"task_id, decision, rationale"| DEC[(decisions)]
    QW -->|"task_id, question<br/>status: open→answered, answer"| OQ[(open_questions)]

    MSG[(messages)] -->|latest turn content| QW
    TAS -->|current goal/status| QW
    PS -->|current plan state| QW
```

## 3. Skill-first check

Before spending cloud quota: has the system distilled a skill for this shape
of task?

```mermaid
flowchart TD
    TV[task embedding] -->|"KNN: k=3, kind='skill'"| VN{{vec_nodes}}
    VN -->|node_id| N[(nodes)]
    N -->|node_id| SK[(skills)]
    SK -->|"procedure, success_count,<br/>failure_count, task_type match"| D{confident<br/>skill hit?}

    D -->|yes| LR[local attempt<br/>Qwen + promoted tools]
    LR -->|"task_id, step_id, task_type<br/>provider_id=NULL ← marks local run"| RUN[(runs)]
    LR --> V7[→ stage 7 verify]

    D -->|no| RT[→ stage 4 route]

    V7 -->|"on outcome: success_count++<br/>or failure_count++, last_used_at"| SK
```

## 4. Route provider (bandit + meters)

Everything the router reads is **derived** — the only write is the new `runs`
row. Meters are views over `llm_calls`, never stored counters.

```mermaid
flowchart TD
    P[(providers)] -->|"enabled=1, rpm/rpd/tpm/tpd limits,<br/>context_window"| RT[router]
    LC[(llm_calls)] --> MU{{v_provider_minute_usage}}
    LC --> DU{{v_provider_day_usage}}
    RUN0[(runs)] --> BS{{v_bandit_scores}}

    MU -->|"requests_1m, tokens_1m<br/>vs rpm/tpm_limit"| RT
    DU -->|"requests_24h, rate_limited_24h,<br/>parse_errors_24h vs rpd/tpd_limit"| RT
    BS -->|"success_rate, avg_retries<br/>per (provider, task_type)"| RT

    RT --> EG{ε-greedy:<br/>exploit best or<br/>explore random}
    EG -->|"task_id, step_id, provider_id,<br/>task_type, started_at"| RUN[(runs)]
    P -.->|"api_key_env → key read<br/>from environment, never DB"| WK[worker client]
```

## 5. Render briefing pack

The handoff artifact. **Every read that leaves the machine passes the
sensitivity filter** — the renderer is the single choke point for the privacy
gate.

```mermaid
flowchart TD
    TAS[(tasks)] -->|goal, constraints| BP[briefing-pack renderer]
    PS[(plan_steps)] -->|"position, description,<br/>status, result_summary"| BP
    DEC[(decisions)] -->|last 10: decision, rationale| BP
    OQ[(open_questions)] -->|status='open': question| BP

    VMSG{{vec_messages}} -->|"KNN k=5 on task embedding"| MSG[(messages)]
    MSG -->|"content WHERE<br/>sensitivity='shareable' ONLY"| BP

    VN{{vec_nodes}} -->|"KNN k=5, kind='tool'"| N[(nodes)]
    N -->|name, description| TOOL[(tools)]
    TOOL -->|"signature WHERE<br/>status='promoted' ONLY"| BP

    BP -->|system prompt:<br/>ledger + memory + tools +<br/>tool-call convention| WK[cloud worker]
```

## 6. Worker loop (generate + plain-text tool calls)

One `llm_calls` row **per API request** — this append-only log is what the
meter views aggregate. Parse failures are first-class telemetry.

```mermaid
flowchart TD
    WK[cloud worker] -->|response text| PAR[tool-fence parser]

    WK -->|"provider_id, run_id,<br/>prompt_tokens, completion_tokens,<br/>latency_ms, status='ok'"| LC[(llm_calls)]
    WK -->|"on 429: status='rate_limited'"| LC
    PAR -->|"malformed fence:<br/>status='parse_error'"| LC
    LC -.->|feeds meters + parse-error<br/>rate → bandit scores| RT4[→ stage 4 re-route<br/>on quota exhaustion]

    PAR -->|"valid tool fence:<br/>name + args"| SBX[sandbox executor]
    SBX -->|"run_id, tool_id, args (JSON),<br/>result, ok (0/1), duration_ms"| TC[(tool_calls)]
    SBX -->|result as role='tool' message| WK

    SBX -->|"files created/changed:<br/>task_id, run_id, path,<br/>kind, sha256"| ART[(artifacts)]
    PAR -->|"tool definition emitted<br/>(new tool draft)"| TF[→ tool forge §8]
```

## 7. Sandbox verify → close the run

The worker stated its expected outcome *before* execution; the sandbox checks
the claim. This single `UPDATE` is what the bandit learns from.

```mermaid
flowchart TD
    SBX[sandbox: run code,<br/>run tests, diff outputs] --> CHK{claim<br/>holds?}
    CHK -->|yes| OK["outcome='verified'"]
    CHK -->|no| KO["outcome='failed'"]
    CHK -->|could not check| UV["outcome='unverified'"]

    OK & KO & UV -->|"finished_at, outcome,<br/>verification_detail (JSON),<br/>retries"| RUN[(runs)]
    RUN --> BS{{v_bandit_scores<br/>recomputes on read}}
    KO -->|failure detail back to worker,<br/>retries++| L6[→ stage 6 retry]
    OK --> S8[→ stage 8]
```

## 8. Synthesize, extract, distill (local Qwen, after reply streams)

All writes here are async — off the reply's critical path. This stage is the
only writer of `nodes`/`edges`, and the graph mirror is updated *last*, from
the relational rows just written (mirror never leads, only follows).

```mermaid
flowchart TD
    WR[worker result] -->|"session_id, role='assistant',<br/>content, provider_id ← who wrote it"| MSG[(messages)]
    MSG -->|content| EMB[embedder] -->|message_id, embedding| VMSG{{vec_messages}}

    WR --> QW[Qwen synthesizer]
    QW -->|"step: status='done',<br/>result_summary"| PS[(plan_steps)]
    QW -->|"new decision, rationale"| DEC[(decisions)]
    QW -->|"answered: status, answer"| OQ[(open_questions)]

    QW --> EX[structured extractor<br/>instructor + pydantic]
    EX -->|"name (casefolded), kind='entity',<br/>entity_type, description,<br/>aliases (JSON), confidence"| N[(nodes)]
    EX -->|"source_id, target_id, rel_type,<br/>evidence (exact span), confidence"| E[(edges)]
    N -->|"node_id, embedding,<br/>kind='entity'"| VN{{vec_nodes}}
    N & E -->|"upsert mirror: label=kind,<br/>key=nodes.id, type=rel_type"| G{{graphqlite mirror}}

    RUN[(runs)] -->|"outcome='verified'?"| DIS{worth<br/>distilling?}
    DIS -->|yes| SKN["skill card: name, kind='skill',<br/>description → nodes;<br/>procedure, task_type,<br/>source_run_id → skills"]
    SKN --> N2[(nodes)] & SK[(skills)]
    N2 -->|"node_id, embedding, kind='skill'"| VN
```

## 9. Tool Forge lifecycle (spans turns)

Status transitions on `tools.status`; the promotion gate (all tests pass) is
enforced in the promotion code path, not the schema.

```mermaid
flowchart TD
    WK[worker emits tool draft] -->|"name, kind='tool',<br/>description ← docstring"| N[(nodes)]
    WK -->|"node_id, code, signature (JSON schema),<br/>version=1, status='draft',<br/>created_by_provider, created_in_run"| T[(tools)]
    WK -->|"tool_id, input (example args JSON),<br/>expected (output/assertion)"| TT[(tool_tests)]

    TT --> SBX[sandbox runs<br/>every example]
    SBX -->|"passed (0/1), last_result,<br/>last_run_at"| TT
    SBX --> GATE{ALL tests<br/>passed?}

    GATE -->|no| FB[failure → back to worker<br/>status stays 'draft'] --> WK
    GATE -->|yes| PRO[promote]
    PRO -->|"status='promoted', promoted_at"| T
    PRO -->|"node_id, embedding of description,<br/>kind='tool'"| VN{{vec_nodes}}
    PRO -->|"tool_node -[depends_on]→ library/tool nodes"| E[(edges)]
    E --> G{{graphqlite mirror}}

    TC[(tool_calls)] -->|"rolling ok-rate per tool"| QG{failing in<br/>the wild?}
    QG -->|yes| QUA["status='quarantined'"] --> T
```

## 10. Meta & rebuild flows (each `system_meta` key its own node)

The rebuild contract from `DB_SCHEMA.md` §1, as a graph. Derived layers are
consumers only; every arrow into them originates in a relational table.

```mermaid
flowchart TD
    MK([system_meta:<br/>embedding_model]) & DK([system_meta:<br/>embedding_dim]) -.-> CHKV{match the<br/>loaded model?}
    CHKV -->|yes| HOT[normal operation]
    CHKV -->|no| RB[rebuild vectors:<br/>drop, recreate, re-embed]
    MSG[(messages)] -->|id, content| RB
    N[(nodes)] -->|id, name + description, kind| RB
    RB --> VMSG{{vec_messages}} & VN{{vec_nodes}}
    RB -->|"update key values<br/>to new model/dim"| MK2([system_meta<br/>updated])

    N2[(nodes)] -->|"label=kind, key=id, name"| RG[rebuild_graph:<br/>truncate + re-upsert]
    E[(edges)] -->|"source_id, target_id, rel_type"| RG
    RG --> G{{graphqlite mirror}}

    SM([schema_migrations:<br/>version]) -.->|highest applied| MIG[migration runner]
    MIG -->|"apply pending DDL,<br/>then INSERT version"| SM2([schema_migrations<br/>+= new version])
    MIG -->|after any schema change<br/>touching indexed content| RB & RG
```

---

## Appendix — column lineage matrix

Who writes every meaningful column, at which stage, from what source.
(`id` PKs are auto-assigned; `created_at`/`started_at` are `DEFAULT` server
timestamps — omitted below unless set explicitly.)

### `sessions`
| Column | Stage | Fed by |
|---|---|---|
| `title` | 2 | Qwen, summarized from first user message |
| `metadata` | 1 | client context (UI origin, etc.), JSON |

### `messages`
| Column | Stage | Fed by |
|---|---|---|
| `session_id` | 1, 8 | current session |
| `role` | 1, 6, 8 | `'user'` (ingest) / `'tool'` (sandbox result) / `'assistant'` (worker reply) |
| `content` | 1, 6, 8 | raw text of the turn |
| `sensitivity` | 1 | default `'local_only'`; Qwen classifier upgrades to `'shareable'` |
| `provider_id` | 8 | `providers.id` of the worker that wrote the reply; NULL for user/system |
| `metadata` | any | free-form JSON |

### `nodes`
| Column | Stage | Fed by |
|---|---|---|
| `name` | 8, 9 | extractor entity name / tool name / skill name (casefolded, trimmed) |
| `kind` | 8, 9 | `'entity'` (extractor) / `'tool'` (forge draft) / `'skill'` (distillation) |
| `entity_type` | 8 | extractor `Entity.type` (Library, Framework, …) |
| `description` | 8, 9 | extractor `Entity.description` / tool docstring / skill summary — **this is what gets embedded** |
| `aliases` | 8 | extractor `Entity.aliases`, JSON array |
| `confidence` | 8 | extractor `Entity.confidence` |
| `sensitivity` | 8 | default `'shareable'`; classifier may demote |
| `last_accessed` | 3, 5 | touched on retrieval |

### `edges`
| Column | Stage | Fed by |
|---|---|---|
| `source_id`, `target_id` | 8, 9 | `nodes.id` of extracted pair / tool + its dependency |
| `rel_type` | 8, 9 | extractor `relationship_type` / `'depends_on'` (forge) |
| `evidence` | 8 | extractor `Relationship.evidence` (exact text span) |
| `confidence` | 8 | extractor `Relationship.confidence` |

### `tasks`
| Column | Stage | Fed by |
|---|---|---|
| `session_id` | 2 | current session |
| `goal` | 2 | Qwen, from user intent |
| `constraints` | 2 | Qwen, JSON array of hard requirements |
| `task_type` | 2 | Qwen classification — **routing key for bandit** |
| `status` | 2, 8 | Qwen lifecycle updates |
| `updated_at` | 2, 8 | set on every ledger write |

### `plan_steps`
| Column | Stage | Fed by |
|---|---|---|
| `task_id`, `position`, `description` | 2 | Qwen planning |
| `status` | 2, 8 | Qwen: `todo→doing` (dispatch), `→done/blocked` (synthesis) |
| `result_summary` | 8 | Qwen, one-liner from verified run output |

### `decisions` / `open_questions`
| Column | Stage | Fed by |
|---|---|---|
| `decision`, `rationale` | 2, 8 | Qwen, from user turns and run outcomes |
| `question` | 2 | Qwen, on ambiguity |
| `status`, `answer` | 8 | Qwen, when a run or user turn resolves it |

### `artifacts`
| Column | Stage | Fed by |
|---|---|---|
| `task_id`, `run_id` | 6 | active task/run |
| `path` | 6 | sandbox: file created/modified, relative to workspace |
| `kind` | 6 | sandbox classification (`file`/`tool`/`report`/…) |
| `sha256` | 6 | sandbox content hash at registration |

### `providers`
| Column | Stage | Fed by |
|---|---|---|
| all | manual/seed | operator config: name, base_url, model, `api_key_env` (env-var **name**), declared limits, enabled |

### `llm_calls`
| Column | Stage | Fed by |
|---|---|---|
| `provider_id`, `run_id` | 6 | active dispatch |
| `prompt_tokens`, `completion_tokens` | 6 | API response `usage` object |
| `latency_ms` | 6 | client-measured wall time |
| `status` | 6 | `'ok'` / `'rate_limited'` (429) / `'timeout'` / `'error'` / `'parse_error'` (bad tool fence) |
| `error` | 6 | raw error body, truncated |

### `runs`
| Column | Stage | Fed by |
|---|---|---|
| `task_id`, `step_id` | 3, 4 | active ledger position |
| `provider_id` | 4 | router pick; **NULL = local skill-card run** |
| `task_type` | 4 | copied from `tasks.task_type` at dispatch (denormalized for the bandit view) |
| `finished_at`, `outcome` | 7 | sandbox verification: `verified`/`failed`/`unverified`/`aborted` |
| `verification_detail` | 7 | JSON: claim, checks run, diffs |
| `retries` | 7 | incremented per stage-6 retry |

### `tools`
| Column | Stage | Fed by |
|---|---|---|
| `node_id` | 9 | the tool's `nodes` row |
| `signature` | 9 | worker-emitted params JSON schema |
| `code` | 9 | worker-emitted source; `version++` on each accepted revision |
| `status` | 9 | `draft` (emit) → `promoted` (all tests pass) → `quarantined` (in-the-wild failures) / `deprecated` (manual) |
| `created_by_provider`, `created_in_run` | 9 | provenance of the authoring dispatch |
| `promoted_at` | 9 | promotion timestamp |

### `tool_tests`
| Column | Stage | Fed by |
|---|---|---|
| `tool_id`, `input`, `expected` | 9 | authoring worker's example invocations |
| `passed`, `last_result`, `last_run_at` | 9 | sandbox test run |

### `tool_calls`
| Column | Stage | Fed by |
|---|---|---|
| `run_id`, `tool_id`, `args` | 6 | parsed tool fence |
| `result`, `ok`, `duration_ms` | 6 | sandbox execution |

### `skills`
| Column | Stage | Fed by |
|---|---|---|
| `node_id` | 8 | the skill's `nodes` row |
| `procedure` | 8 | Qwen distillation: steps + tools used + pitfalls (markdown) |
| `task_type` | 8 | from the source run |
| `source_run_id` | 8 | the verified cloud run it was learned from |
| `success_count`, `failure_count`, `last_used_at` | 3/7 | incremented when a local skill-card run verifies/fails |

### `system_meta` (each key an independent node)
| Key | Stage | Fed by |
|---|---|---|
| `embedding_model` | seed / rebuild (10) | operator config; updated only after a successful vector rebuild |
| `embedding_dim` | seed / rebuild (10) | ditto (currently `256`) |

### `schema_migrations`
| Column | Stage | Fed by |
|---|---|---|
| `version` | migration (10) | migration runner, after applying DDL |

### Derived layers (never written directly by business logic)
| Object | Rebuilt from | Trigger |
|---|---|---|
| `vec_messages` | `messages(id, content)` | embedding model/dim change |
| `vec_nodes` | `nodes(id, name, description, kind)` | embedding model/dim change |
| graphqlite mirror | `nodes` + `edges` | any mirror doubt → `rebuild_graph()` |
| `v_provider_*_usage` | `llm_calls` | view — always live |
| `v_bandit_scores` | `runs` | view — always live |
