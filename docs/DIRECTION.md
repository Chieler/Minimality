# Minimality — New Direction: Local Orchestrator + Free Cloud Workers

**The pitch:** A local Qwen model orchestrates — synthesizing, summarizing, tracking
progress — while the heavy reasoning is done by free-tier cloud LLMs (DeepSeek,
Mistral, Groq-hosted models, etc.). An execution namespace runs the code these
models produce, letting them build persistent tools for themselves. When a free
API exhausts its quota, the system rotates to another provider and the local
model briefs the newcomer.

---

## 1. Evaluation

### What's genuinely strong about this idea

**The economics are real.** Free tiers across Groq, Google AI Studio, Mistral
La Plateforme, OpenRouter `:free` models, Cerebras, and GitHub Models add up to
a meaningful daily token budget *if* you rotate intelligently. Nobody's free
tier alone is enough; the union of them is. Rotation-as-architecture (not
rotation-as-hack) is an underexplored design point.

**Your memory layer is the moat.** The existing work — episodic logs with
vector retrieval, entity graph with typed relationships — is exactly what makes
provider-swapping viable. If all durable state lives in *your* system rather
than in any provider's context window, then workers become stateless and
interchangeable by construction. Most multi-provider projects treat the
provider's chat history as the state and suffer for it. You already built the
alternative.

**The execution namespace closes the loop.** Cloud models that can only talk
are fungible; cloud models whose code you *run* and whose tools *persist*
compound. A tool built during a DeepSeek session is still there when Mistral
takes over. The system's capability grows monotonically even as workers churn.

### The hard problems (be honest about these)

1. **"Catch-up" via summarization is the weakest link as stated.** If handoff
   means "Qwen writes a summary of the transcript for the new model," every
   provider switch is a lossy compression event, and errors compound across
   switches. See §2.1 for the fix: make handoff a *rendering of structured
   state*, not a summarization of a transcript.

2. **Running cloud-LLM code is arbitrary code execution.** With persistent
   tools it's worse: a subtly wrong (or prompt-injected) tool persists and gets
   reused. The namespace needs real isolation (subprocess with resource
   limits at minimum; container later) and tools need a promotion gate, not
   direct write access to the registry.

3. **Free tiers have teeth.** Per-minute *and* per-day limits, ToS that often
   permit training on your inputs, silent model deprecations, and quality
   variance. Two consequences: (a) you need a provider registry with declared
   limits and live meters, not just try/except-and-rotate; (b) your memory
   contains personal context — you need a sensitivity gate deciding what is
   allowed to leave the machine.

4. **Heterogeneous tool-calling.** Native function-calling APIs differ per
   provider and are flaky on free tiers. Don't depend on them: define one
   plain-text tool-call convention (e.g. fenced ` ```tool ` JSON blocks),
   parse it yourself, and use the OpenAI-compatible endpoint every listed
   provider already exposes. One client, one parser, N providers.

5. **A 9B local model is a fine librarian and a poor judge.** Qwen can
   synthesize, index, route, and track. It cannot reliably *evaluate* whether
   a cloud model's 400-line diff is correct. Lean on the execution namespace
   for judgment: run the tests. Verification by execution is cheap and
   trustworthy; verification by small-model vibes is neither. (§2.4)

### Current codebase notes (pre-work the roadmap depends on)

- `mem_class.py:15` — hardcoded absolute macOS path for `DB_URL`; move to config/env.
- `main.py` — `insert_turn_log` is never called, so conversations are retrieved
  from memory but never written to it; the loop is open.
- Two competing extractors (`Memory_Manager.get_entities` regex-parsing
  llama3.2:1b vs. `Entity_Extractor` with instructor/pydantic). Keep the
  structured one; delete the regex one.
- No `requirements.txt`/`pyproject.toml`, no tests, `check_same_thread=False`
  with a single shared cursor will bite once anything is async.

---

## 2. Suggestions & Improvements (originality-ranked)

### 2.1 Replace "catch-up" with a **Briefing Pack** — resumable by construction

Don't summarize transcripts at switch time. Instead, Qwen continuously
maintains a structured **Task Ledger**:

```
goal            what we're ultimately doing
constraints     hard requirements, user preferences
decisions[]     choices made + one-line rationale
plan[]          steps with status (todo/doing/done/blocked)
open_questions[]
artifacts[]     files/tools produced, with paths
last_worker     provider, what it was mid-way through
```

Any new worker is initialized by *rendering* the ledger + top-k memory
retrievals into a system prompt. Handoff becomes O(1) and lossless-enough at
any moment — even mid-task, even after a crash. The provider switch stops
being an event and becomes a non-event. This also gives you free
crash-recovery and a natural UI (the ledger *is* the progress display).

### 2.2 The **Tool Forge**: draft → tested → promoted, indexed in your existing memory

Models don't get to write into the tool registry directly. Lifecycle:

1. **Draft** — worker emits a tool (Python function + docstring + 2–3 example
   invocations with expected outputs).
2. **Test** — the examples run in the sandbox as acceptance tests. Fail → the
   failure goes back to the worker, tool stays a draft.
3. **Promote** — passing tools get registered: docstring embedded into
   `sqlite-vec` (you already have the table pattern), tool node added to the
   entity graph with `uses`/`depends_on` edges to libraries and other tools.

Tool *retrieval* is then just your existing vector search: given a new task,
fetch the 5 most relevant promoted tools and inject only those into the
worker's prompt. The graph edges give you dependency-aware loading and impact
analysis ("what breaks if this tool is bad?") for free. **This reuses both of
your memory subsystems for a new purpose — that's the original part.**

### 2.3 **Skill distillation**: the system gets cheaper as it runs

Frame the free APIs as *teachers* and the local model as the *student*. When a
cloud worker solves a task well, Qwen extracts a **skill card** — the
generalized procedure, the tools used, the pitfalls hit — into memory. Next
time a similar task arrives (vector match on skill cards), Qwen attempts it
locally with the recorded procedure + promoted tools before spending cloud
quota. Over time the cloud-call rate for *recurring* task shapes trends toward
zero. Nobody designs free-tier systems to need the free tier less over time;
this makes rotation a bootstrapping mechanism rather than a permanent crutch.

### 2.4 **Verification asymmetry**: local model gates, execution judges

Division of labor by trust, not just by size:

- Cloud workers **generate** (code, plans, analyses).
- The **sandbox verifies** (run it, run the tests, check outputs against the
  worker's own stated expectations).
- **Qwen adjudicates** only the cheap judgments (does the output match the
  ledger's goal? did the tests pass? route accordingly).

Never let generation and verification happen in the same place. A worker must
state its expected outcome *before* execution; the sandbox checks the claim.
This turns "is this free model any good?" from a vibes question into a
measured pass-rate.

### 2.5 **Bandit router** over a provider registry — not just failover

Keep a registry per provider: declared limits (RPM/RPD/TPM), context window,
live meter (tokens/requests consumed, reset times), and a rolling **outcome
score per task-type** (did the sandbox verify its output? how many retries?).
Route new tasks by requirement (long-context → the big-window provider; quick
code → the fast one; hard reasoning → the strongest free reasoner), tie-break
by an epsilon-greedy bandit on outcome scores. Quota exhaustion is then just
one more input to routing rather than an exception to handle. You end up
*learning* which free model is actually good at what — data almost nobody has.

### 2.6 **Sensitivity tiering** on memory

Tag memory rows `local_only | shareable` at write time (Qwen classifies —
this is squarely within a 9B model's ability). The briefing-pack renderer
filters by tier before anything is sent to a cloud provider. Free tiers often
reserve the right to train on inputs; your episodic memory of *you* should not
be part of that. This is a two-line schema change now and a nightmare retrofit
later.

### 2.7 Smaller improvements

- One OpenAI-compatible client for all providers; provider = base_url + key + limits row.
- Structured outputs everywhere via `instructor` (you already have it) — delete the regex JSON scraping.
- Async writes: memory inserts after the response streams, not before.
- Ledger + registries live in the same SQLite file — one `cp` backs up the entire system state.

---

## 3. Roadmap

Each phase is independently useful; stop anywhere and you still have a working system.

### Phase 0 — Foundations (small, do first)
- `pyproject.toml` + pinned deps; config file/env for paths & model names (kill `mem_class.py:15`).
- Close the memory loop in `main.py`: actually call `insert_turn_log` for both roles.
- Consolidate on the instructor-based extractor; delete the regex one.
- A handful of pytest tests for `Memory_Manager` (insert/retrieve round-trips).

### Phase 1 — Provider layer
- `providers.py`: OpenAI-compatible client wrapper; registry table (name, base_url, model, RPM/RPD/TPM, context, meter state).
- Rotation on 429/quota with exponential backoff; meters persisted in SQLite.
- Verify current free-tier terms for each provider you enlist (they shift quarterly).

### Phase 2 — Execution namespace (v1: honest sandbox, not perfect sandbox)
- Workspace dir per session; runner = `subprocess` with timeout, memory/CPU rlimits, no network by default, stdout/stderr captured.
- Artifact tracking: files created/modified per run recorded in the ledger.
- Document the threat model; plan the container upgrade (Docker/gVisor) as v2.

### Phase 3 — Orchestrator + Task Ledger (§2.1)
- Ledger schema in SQLite; Qwen updates it after every worker turn.
- Briefing-pack renderer (ledger + top-k memory + top-k tools → system prompt).
- The core loop: user goal → Qwen plans into ledger → dispatch worker → execute/verify → Qwen synthesizes into ledger + memory → repeat.
- Provider switch = kill worker, render briefing, dispatch elsewhere. Demo it mid-task.

### Phase 4 — Tool Forge (§2.2)
- Plain-text tool-call convention + parser (no reliance on native function calling).
- `tools` table + embedding index + graph nodes/edges; draft→test→promote pipeline.
- Retrieval-based tool injection (top-5 relevant tools per task, never the whole library).

### Phase 5 — Routing intelligence (§2.4, §2.5, §2.3)
- Outcome scoring from sandbox verification; per-provider, per-task-type stats.
- Bandit routing; quota-aware scheduling (defer non-urgent work to quota resets).
- Skill cards: extract on success, retrieve on similar tasks, attempt locally first.

### Phase 6 — Observability & UI
- Streamlit dashboard: live ledger, provider meters/scores, tool library browser, memory-graph view (you have `audit.py` as a seed).
- Trace log per task: which provider, which tools, verification results — the debugging surface for everything above.

### Sequencing rationale
0→1→2 are prerequisites and mostly plumbing. **Phase 3 is the identity of the
project** — do it before the tool forge, because a mid-task provider handoff
with no quality loss is the demo that proves the thesis. 4 and 5 are where the
original compounding value (tools, skills, routing data) accrues.

---

## 4. Risks to keep on the wall

| Risk | Mitigation |
|---|---|
| Free tier ToS / training on inputs | Sensitivity tiering (§2.6); read terms per provider |
| Provider deprecates model silently | Registry health-check ping; bandit degrades it naturally |
| Prompt-injected or buggy persistent tool | Promotion gate + sandbox-only execution + graph impact analysis |
| Small-model orchestration errors | Ledger is human-readable/editable; execution-based verification, never Qwen-as-judge for code |
| Handoff quality loss | Briefing pack from structured state, not transcript summarization |
