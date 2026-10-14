# BotArena — MVP Blueprint

**Version:** 2.0 · **Date:** October 11, 2026 · **Status:** Buildable specification

## Lineage and what changed structurally

This supersedes v1.3. It keeps every correctness fix v1.3 made over v1.2 (execution-token ownership, persisted HMAC client identity, split rate-limit buckets, atomic creation ordering, a real provider-call cost ledger, a verified backup pipeline, honest framing of what WAL and a profanity filter actually guarantee) — those were real bugs, not style preferences, and nothing here reverts them.

What this version changes is structural, in response to two problems v1.3 had: its scope had quietly grown from "MVP" into production-grade abuse/billing/DR infrastructure without the hour estimate being re-derived, and several sections described mechanisms without giving the concrete artifact (a folder tree, exact dependency versions, a deployment runbook) needed to actually start typing code.

1. **The build is explicitly split into two checkpoints with separately re-derived estimates** (§13), not one blended 45–70h figure covering work that had grown well past it.
2. **The checkpoint split is enforced by a runtime flag (`DEPLOYMENT_MODE`), not just document ordering** — the app itself refuses to run in `shared` mode without every secret and guard Checkpoint B requires (§8, §11).
3. **The per-IP creation quota is explicitly tied to the eval group size**, so it can't silently suppress the thing you're trying to measure (§12).
4. **Added what a builder needs that no prior version specified**: pinned dependency versions with rationale (§3), external service setup steps (§7), a concrete project structure (§9), and a deployment checklist (§17).
5. Consolidated the four required secrets into one explicit list with a startup validation rule (§8), instead of them being scattered across sections.

## 1. Product definition

BotArena lets a visitor pick a curated debate topic, watch two fixed AI debaters — Atlas (Pro) and Nova (Con) — argue it across three rounds (opening, rebuttal, closing; six speeches total), then read a backend-calculated, explainable verdict. The backend owns all execution; the model never decides scores, order, or sides.

A completed debate has exactly six ordered speeches and one judgment. A failed debate preserves every committed speech and explains what can happen next. Anyone with the link can read a debate's saved state; only its creator (via a locally-held execution token, §8) can start or resume it. The verdict is explicitly labeled an AI assessment of the arguments presented, not verified fact.

### In scope

- 8–12 curated topics (adjust after quality testing).
- Fixed Atlas/Pro vs. Nova/Con, fixed speaking order, three rounds.
- Progressive display of committed speeches, active-speaker and judging state.
- Validated rubric scoring, backend-calculated totals/winner/draw, explanations, transcript references.
- SQLite persistence, checkpointed resume, restart recovery, a public Recent Battles history.
- Responsive, accessible lobby/arena/results/history/failure views with a root error boundary.
- Public read access, token-authorized execution, no user accounts.
- Atomic duplicate-execution prevention, durable creation/call/cost budgets, bounded retries.
- Lexical content screening, operator hide, verified backups, operator stats.

### Out of scope (deferred, revisit only if the eval shows a need)

Accounts, private per-user history, tournaments, multiplayer, leaderboards, custom topics, human debaters, voice, animated replay, export, retrieval/long-term memory, CAPTCHA, a public reporting workflow, multi-instance/multi-worker deployment.

Do not build ahead of evidence: if observed abuse or content issues exceed what §12/§14 cover, restrict access and revise the relevant control — don't pre-build for hypothetical scale.

## 2. Checkpoints

**Checkpoint A — works, locally, for one person.** Fake-provider tests prove the engine; a handful of real matches prove quality; the full create → run → poll → resume → history flow works against a local server. Never bound to a public interface. Token auth exists (it's part of the core API shape) but the hardened abuse/cost/backup machinery around it does not need to exist yet, because nobody but you can reach it.

**Checkpoint B — safe to hand a link to 5–10 people.** Everything in §8–§16 is built, wired, and tested: split rate-limit buckets, durable per-IP identity and quotas sized for the actual eval group, a real provider-call cost ledger, content screening, operator hide, verified backups with a tested restore, and a deployment smoke test on the real host.

These are genuinely different amounts of work (§13). Do not let "basically done" at Checkpoint A read as "ready to share" — the `DEPLOYMENT_MODE` flag in §8 makes that distinction load-bearing in code, not just in this document.

## 3. Tech stack

Pinned to what's current and stable as of this plan's writing. Groq's SDK and model lineup move fast enough that the plan itself (and every version before it) explicitly defers that one choice to a proof-of-concept step — that's a deliberate exception, not an oversight; everything else here is pinned now because there's no reason not to.

| Layer | Choice | Why | Rejected alternative |
|---|---|---|---|
| Backend language/runtime | Python 3.12 | Async SQLAlchemy 2.0 and modern FastAPI both target it cleanly. | Python 3.11 — no real benefit to staying back. |
| Web framework | FastAPI 0.115.x | Async-native, Pydantic-integrated, lifespan hooks for startup reconciliation (§10). | Flask — no native async task model, more glue code for this app's shape. |
| Validation | Pydantic 2.9.x | Already FastAPI's native schema layer; fast validation for judge-output scoring checks. | marshmallow — redundant with what FastAPI already needs. |
| ORM / DB access | SQLAlchemy 2.0.x (async) + `aiosqlite` 0.20.x | Async session support needed so a lock wait (§10) doesn't block the event loop. | Raw `sqlite3` — would need manual thread-offloading for every query; SQLAlchemy already handles it. |
| Migrations | Alembic 1.13.x | Standard SQLAlchemy migration tool; versioned schema from day one (required by every prior version of this plan). | Hand-rolled SQL scripts — no rollback story. |
| ASGI server | Uvicorn 0.30.x | Single-process, single-worker deployment (§5) is exactly its default mode. | Gunicorn+workers — actively wrong for the one-process constraint. |
| Rate limiting | `slowapi` 0.1.9 | In-memory, FastAPI-native, no Redis needed for a single-process deployment. | `fastapi-limiter` (Redis-backed) — adds infra for no benefit at this scale. |
| Content screening | `better-profanity` 0.7.0 | Offline lexical check, zero added latency/cost versus an LLM moderation call. | A second LLM moderation call — roughly doubles provider spend for a low-probability risk on curated topics. |
| Scheduled jobs | `APScheduler` 3.10.4 | Runs in the same single process already decided on; no separate cron container. | OS-level cron — not portable across hosts, and the app already owns its own lifespan. |
| AI provider | Groq SDK (`groq` package) — version TBD at POC | Pin a version-floor by Phase 1 after confirming model availability and structured-output support, per the §14 verification step every prior version of this plan has flagged. | N/A — provider is fixed by prior decision; only the SDK version is deferred. |
| Frontend framework | React 18.3.x + TypeScript 5.5.x | Matches the UI's already-decided component shape (§15). | Vue/Svelte — no reason to switch; React has the larger ecosystem for this solo build. |
| Build tool | Vite 5.4.x | Fast dev server, minimal config for a React+TS SPA. | Create React App — unmaintained. |
| Styling | Tailwind CSS 3.4.x | Matches the already-fixed dark theme token set (§15). | CSS Modules — more boilerplate for the same outcome. |
| Backend tests | pytest 8.3.x + `pytest-asyncio` 0.24.x + `httpx` 0.27.x (ASGI test client) | Standard async FastAPI test stack. | unittest — more verbose for async fixtures. |
| Frontend tests | Vitest 2.1.x + React Testing Library 16.x | Vite-native test runner, avoids a second bundler config (Jest). | Jest — redundant build config alongside Vite. |
| Host | Fly.io, single machine, one persistent volume | Persistent local disk (required for SQLite+WAL, §10), explicit single-instance control, cheap for a small demo. | Render — also viable (persistent disk exists there too); Fly's `fly.toml` makes "exactly one machine, no autoscale" an explicit, auditable config line rather than an implicit setting. Swap freely if you already have a Render habit. |

## 4. Architecture

```text
Browser (React SPA)
  - stores: client_request_id, execution_token (localStorage, per debate)
  - polls GET /api/debates/{id} every ~1.5s while visible and active
  - sends POST create/run with X-Debate-Token header, never a URL param

FastAPI process (single instance, single worker)
  - middleware: CORS (configured origins only), slowapi dual buckets (§12)
  - routers: topics, debates (create/run/get/list), admin (stats/hide/detail), health
  - startup: run pending Alembic migrations, reconcile orphaned running/judging debates,
             validate DEPLOYMENT_MODE=shared secrets if applicable (§8)
  - in-process task registry: one asyncio.Task per active execution attempt, strong refs kept
  - in-process APScheduler: nightly backup job, IP-association cleanup job

Debate orchestrator (runs inside a registered task)
  - prompt builder -> provider-call reservation (§13) -> Groq adapter -> output validation
    (schema + length + content screen, §14) -> atomic checkpoint commit (§10)
  - owns its own async DB session; never shares the request's session or depends on the
    browser connection staying open

SQLite (WAL mode, busy_timeout=5000ms) on the Fly.io persistent volume
  - Debate, DebateMessage, Judgment, ExecutionAttempt, CreationQuotaRecord,
    ProviderCallReservation (§6)
  - nightly: online backup -> integrity check -> atomic rename -> prune backups >7 days,
    always keeping the newest verified-good copy (§16)
```

State lives in exactly one place: the SQLite file. The task registry is in-memory and does not survive a process restart — that's why startup reconciliation (§10) exists, and why background tasks are documented everywhere in this plan as not durable across crashes.

## 5. Lifecycle and execution ownership

```text
created -> running       Start accepted
running -> judging       Sixth speech committed
running -> failed        Error, interruption, or operator hide
judging -> completed     Judgment and completion committed together
judging -> failed        Error, interruption, or operator hide
failed  -> running       Eligible resume with fewer than six speeches
failed  -> judging       Eligible resume with all six speeches
completed                Terminal; New Battle creates a new session
```

`hidden` is a boolean independent of `status` (a `completed` debate can also be `hidden`). Hiding an *active* debate is the one place the two interact: it forces `status -> failed` with reason `operator_hidden` as a side effect of stopping paid work, while the `hidden` flag itself remains the thing that governs public visibility (§14).

**Process boundary.** One backend process, one worker, one instance. `POST /run` validates the execution token, atomically claims an attempt (new `run_id`), registers an `asyncio.Task`, and returns `202` promptly. Never hold a DB transaction across a provider call. Global active-execution capacity is 2; per-current-IP-bucket active capacity is 1 (§12). A rejected Start/Resume changes nothing about the debate's existing checkpoint or attempt count.

**Atomicity.** Every worker checkpoint re-verifies it still owns the current `run_id` before committing. Unique `(debate_id, turn_order)` and one-judgment-per-debate constraints are the final guard beneath the application-level claim. This guarantees one committed record per turn — it does not guarantee exactly one provider charge if a crash happens between generation and commit; that's accepted and documented, not silently assumed away.

**Restart.** Before the app reports ready, mark every previously `running`/`judging` debate `failed` with `server_interrupted`, mark their attempts `interrupted`, and leave checkpoints and usage reservations untouched. Never auto-resume paid work at startup — recovery stays visible and manual.

**Shutdown.** Stop admitting new executions, give active tasks a bounded grace period, cancel what's left, persist interruption state where storage is reachable, re-raise cancellation after cleanup.

**Storage outage.** If a checkpoint or reservation can't be persisted, stop scheduling further provider calls, return `503`, and never display unsaved text as committed. A down database can't guarantee a saved failure record either — reconcile orphaned state on recovery/restart.

## 6. Data model

```text
Debate
  id                      UUID, PK
  client_request_id       UUID, unique
  topic_id                text
  topic_snapshot          JSON   -- topic text frozen at creation time
  execution_token_hash    text   -- SHA-256 of the client-generated token; never the raw token
  status                  enum(created, running, judging, completed, failed)
  hidden                  boolean, default false
  hidden_reason           text, nullable
  hidden_at               timestamp, nullable
  revision                integer, default 0
  current_round           integer, default 0
  active_speaker          text, nullable
  active_turn             integer, nullable
  activity                enum(generating, retrying, evaluating), nullable
  accepted_attempts        integer, default 0
  total_calls              integer, default 0
  active_run_id             UUID, nullable
  failed_stage              text, nullable
  failed_turn                integer, nullable
  error_code                  text, nullable
  error_message                text, nullable
  created_at, updated_at, completed_at   timestamps (UTC)

DebateMessage
  id              UUID, PK
  debate_id       FK -> Debate
  agent           enum(atlas, nova)
  side            enum(pro, con)
  round_number    integer
  turn_order      integer    -- unique with debate_id; 1..6
  content         text
  created_at      timestamp

Judgment
  id                     UUID, PK
  debate_id              FK -> Debate, unique
  pro_logical_reasoning, pro_relevance, pro_rebuttal_quality, pro_clarity   integer
  con_logical_reasoning, con_relevance, con_rebuttal_quality, con_clarity   integer
  pro_total, con_total   integer   -- computed by backend, never by the model
  winner                 enum(pro, con, draw)
  pro_strengths, pro_weaknesses, con_strengths, con_weaknesses   text
  overall_explanation    text
  referenced_message_ids JSON array of DebateMessage.id
  model_id, prompt_version, rubric_version   text
  created_at             timestamp

ExecutionAttempt
  run_id            UUID, PK
  debate_id         FK -> Debate
  attempt_number    integer
  status            enum(active, completed, failed, interrupted)
  started_at, finished_at   timestamp
  starting_checkpoint   integer   -- committed-turn count when this attempt began
  failed_stage, failed_turn   text, integer, nullable
  model_id, prompt_version   text
  call_count        integer
  ip_bucket_hash     text, nullable   -- cleared by the retention job (§12), row kept
  created_at         timestamp

CreationQuotaRecord
  id              UUID, PK
  debate_id       FK -> Debate, unique
  created_at      timestamp
  ip_bucket_hash  text
  key_version     integer   -- which RATE_LIMIT_HMAC_SECRET generation produced this hash

ProviderCallReservation
  id                       UUID, PK
  debate_id, run_id        FK
  stage                    text   -- "speech_turn_3", "judgment", etc.
  reserved_at               timestamp
  model_id, price_version   text
  input_token_cap, output_token_cap   integer
  reserved_max_cost_cents   integer   -- conservative upper bound, integer cents
  dispatch_state            enum(reserved, dispatched, committed, failed, abandoned)
  actual_input_tokens, actual_output_tokens, actual_cost_cents   integer, nullable
  created_at                timestamp
```

Indexes: `Debate(hidden, created_at)` for history pagination; `DebateMessage(debate_id, turn_order)`; `ExecutionAttempt(debate_id, status)`; `CreationQuotaRecord(ip_bucket_hash, created_at)` and `(created_at)` for the §12 quota queries; `ProviderCallReservation(created_at)` for the global call/cost queries. Store everything in UTC; convert to local time only in the browser.

## 7. External services and setup

**Groq (AI provider).**
1. Create an account at console.groq.com; generate a dedicated API key for this project specifically — do not reuse a personal key shared with other tools, so its usage/spend is cleanly attributable (this is also what §13's cost ledger assumes).
2. At Phase 1 (engine proof), list currently available models via the Groq API and pick one that supports structured/JSON output reliably; confirm its context window and current per-token pricing directly from Groq's docs at that time — every version of this plan has flagged that the model lineup and pricing move faster than this document can track, and that remains true.
3. Note the account's current rate limits (requests/minute, tokens/minute) — these bound how aggressive §13's defaults can be regardless of what you set them to.
4. Set `GROQ_API_KEY` as a deployment secret (§8); never commit it.

**Fly.io (host).**
1. Install `flyctl`, run `fly auth login`.
2. `fly launch` in the `backend/` directory (do not deploy yet — just scaffold `fly.toml`).
3. `fly volumes create botarena_data --size 1` (1 GB; raise later if §16's size warning fires) in the same region as the app.
4. In `fly.toml`, mount that volume at `/data`, set `DATABASE_PATH=/data/botarena.db`, and pin `[http_service] min_machines_running = 1` / disable autoscaling — this deployment is explicitly single-instance (§5).
5. `fly secrets set GROQ_API_KEY=... ADMIN_TOKEN=... RATE_LIMIT_HMAC_SECRET=...` (§8) before the first `shared`-mode deploy.

**Independent backup storage (optional, Checkpoint B only).** Any S3-compatible bucket (Backblaze B2 is cheap and sufficient) that the nightly job can push verified backups to, per §16's "local backups aren't disaster recovery" distinction. Not required to reach Checkpoint B's minimum bar — flagged here as the next step if you want it.

## 8. Secrets, configuration, and the deployment-mode gate

Four secrets, no more, consolidated here instead of scattered:

| Secret | Used for | Required for |
|---|---|---|
| `GROQ_API_KEY` | Provider adapter calls | Both checkpoints |
| `ADMIN_TOKEN` | `X-Admin-Token` header on `/api/admin/*` | Checkpoint B (meaningless until the app is reachable by anyone else) |
| `RATE_LIMIT_HMAC_SECRET` | Derives the stable per-IP bucket hash (§12); must stay constant across restarts — regenerating it resets everyone's quota history | Checkpoint B |
| `DATABASE_PATH` | Not a secret, but required config — the SQLite file location on the mounted volume | Both checkpoints |

**`DEPLOYMENT_MODE`** (`local` or `shared`, env var, default `local`) is the mechanism that actually enforces the §2 checkpoint split in code:

- In `local` mode, the app binds to `127.0.0.1` only, CORS allows only `localhost` origins, and startup skips the "all shared-mode secrets present" check. This is Checkpoint A's mode — it still has execution tokens and the full API shape (retrofitting auth later is the real risk to avoid), it just isn't reachable by anyone but you.
- In `shared` mode, startup validates that `ADMIN_TOKEN` and `RATE_LIMIT_HMAC_SECRET` are set and non-trivial (reject empty strings or obvious placeholders), and refuses to reach the ready state otherwise. This turns "missing critical configuration disables paid execution" from a documentation line (as it was in v1.3) into something the process actually checks before it accepts traffic.

This directly answers the risk from the previous review: "basically working" and "safe to share" are different states, and now the app itself — not just this document — knows which one it's in.

## 9. Project structure

```text
botarena/
  backend/
    app/
      main.py                  # FastAPI app, lifespan (migrations, reconciliation, mode check)
      config.py                # pydantic-settings: env vars incl. DEPLOYMENT_MODE, quotas
      db/
        base.py                 # async engine/session, WAL + busy_timeout pragma setup
        models.py                # SQLAlchemy models (§6)
        migrations/               # Alembic
      schemas/
        debate.py                 # request/response Pydantic models
        admin.py
      api/
        topics.py
        debates.py                 # create / run / get / list
        admin.py                     # stats / hide / detail
        health.py
      core/
        auth.py                      # execution-token hash + constant-time verify
        identity.py                    # proxy-trusted IP extraction, HMAC bucket hash
        quotas.py                        # creation / concurrency / call+cost reservation logic
        ratelimit.py                      # slowapi bucket config (read vs. mutation)
      engine/
        orchestrator.py                    # task registry, supervisor, lifecycle transitions
        prompts.py                           # Atlas / Nova / Judge prompt builders
        provider.py                            # Groq adapter + fake provider (test double)
        validation.py                            # schema/length/content-screen checks
        judge.py                                  # score totals, draw/winner rule
      ops/
        backup.py                                  # APScheduler job: backup, verify, prune
        prune_old_debates.py                          # manual, dry-run-first retention script
    tests/
      unit/
      integration/
      fixtures/fake_provider.py
    alembic.ini
    pyproject.toml
    .env.example
  frontend/
    src/
      main.tsx
      App.tsx
      api/client.ts               # fetch wrapper; attaches X-Debate-Token
      storage/pendingCreation.ts   # localStorage: pending creation + per-debate tokens
      pages/
        Lobby.tsx
        Arena.tsx
        Results.tsx
        RecentBattles.tsx
        FailureView.tsx
      components/
        ErrorBoundary.tsx
        TranscriptView.tsx
        SpeakerStatus.tsx
      hooks/
        usePolling.ts
        usePendingCreation.ts
      styles/theme.css
    tests/
    index.html
    package.json
    vite.config.ts
    tailwind.config.js
  docs/
    DEPLOYMENT.md
    RUNBOOK.md                   # backup/restore steps, hide procedure, secret rotation
  README.md
```

## 10. User journeys

**Start a battle.** Lobby loads topics (loading/empty/retryable-error states). On Start: disable the button, generate `client_request_id` (UUID) and a 32-byte random execution token (Web Crypto), and write `{client_request_id, topic_id, execution_token, created_at}` to `localStorage` *before* sending anything — keyed by UUID, not one overwritable slot, so concurrent tabs don't clobber each other. Send the create request with the token in `X-Debate-Token` (never a URL). On success, move the token to permanent per-debate storage, then clear the pending entry — in that order, so a crash between the two can never leave zero copies of the only token. Open the arena and request execution once, within this same original action. Show `202`'s accepted state; never show "running" before the server confirms it.

**Recover an uncertain creation.** On mount, if a pending entry exists and is under 5 minutes old, silently retry the identical create (same UUID/topic/token) and reconcile — this is a pure idempotent replay per §11, so it costs no extra creation-quota slot if the original request actually succeeded server-side. Recovery **only ever re-does the create step.** It never calls `/run` on its own — the user sees the recovered `created` debate with an explicit Start button, exactly as if they'd just finished step one themselves. Older pending entries get an explicit "Recover Pending Battle" vs. "Discard" choice instead of a silent retry.

**Watch the debate.** Six turns (Atlas opening → Nova opening → Atlas rebuttal → Nova rebuttal → Atlas closing → Nova closing), each given every prior committed speech with stable IDs. Show topic, sides, round, active speaker, completed count, transcript, retry status; preserve scroll position with a Jump to Latest action.

**Leave and return.** Fetch a snapshot before anything else. `created`: Start if you hold the token, otherwise "Not Started." `running`/`judging`: resume polling. `completed`: results. `failed`: transcript + explanation, Resume only if token-authorized and eligible. Missing or hidden: Battle Not Found. A connection error never implies failure and never triggers a re-run.

**Resume a failure.** Requires the saved token. Server verifies authorization, checkpoint integrity (positions 1–6 form an uninterrupted prefix with correct speakers/rounds), remaining attempts, usage headroom, and capacity, then atomically claims a new attempt. Continues from the first missing turn, or straight to judging if all six speeches exist. Up to 3 accepted attempts total (initial + 2 resumes); a rejected admission never consumes one. Losing the token loses execution control but not public read access — there's no account-based recovery, and the UI says so plainly.

**Results and history.** Results show winner/draw, totals, four criterion scores per side, strengths/weaknesses, explanation, transcript references, full transcript, New Battle, Recent Battles. Recent Battles is explicitly labeled a shared archive (no accounts); 20 rows/page, newest first, stable `(created_at, id)` cursor, hidden debates excluded. Reading history never triggers a model call.

## 11. API contract

| Method | Endpoint | Behavior |
|---|---|---|
| GET | `/api/topics` | Curated catalogue. |
| POST | `/api/debates` | Idempotent creation; `X-Debate-Token` header required. |
| POST | `/api/debates/{id}/run` | Token-authorized Start/Resume; returns promptly, no held-open stream. |
| GET | `/api/debates/{id}` | Public snapshot; a valid `X-Debate-Token` adds `controls` (can_start/can_resume/remaining attempts). |
| GET | `/api/debates?limit=20&cursor=...` | Non-hidden summaries, stable cursor pagination. |
| GET | `/api/health` | Minimal readiness; no config dump. |
| GET | `/api/admin/stats` | Protected operational aggregates (§14). |
| GET | `/api/admin/debates/{id}` | Protected full inspection, including hidden content. |
| POST | `/api/admin/debates/{id}/hide` | Protected: sets `hidden`, stops active work if any. |

Create body:

```json
{ "topic_id": "ai-replace-teachers", "client_request_id": "client-generated-uuid" }
```

**Atomic create ordering** (fixes the real v1.2 bug where a replay could be rejected after its original request already consumed the last quota slot): within one short transaction — (1) look up `client_request_id`; (2) if found, verify the token hash and that the topic matches, then return the existing session with `200` *regardless of current quota state* (changed input → `409`; wrong token → generic `403` with no session details leaked); (3) if not found, validate the topic, check durable quotas (§12), and insert the debate + quota record atomically; (4) return `201`.

Status codes: creation — `201` new, `200` idempotent replay, `409` UUID reused with different input, `403` missing/incorrect token, `422` invalid shape/topic, `429` quota/rate-limited. Run — `202` accepted, `409` already active/completed/ineligible, `404` missing/hidden, `403` capability failure, `429` concurrency/usage limit, `503` capacity/infra unavailable. Check hidden-or-missing *before* token checks, so a hidden debate's public behavior is indistinguishable whether or not the caller holds a valid token.

All `/api/debates*` responses: `Cache-Control: no-store`. Tokens and token hashes never appear in snapshots, history rows, logs, or URLs — only in the `X-Debate-Token` request header. Admin routes require `X-Admin-Token`; missing/invalid → `401`; in `shared` mode an unset `ADMIN_TOKEN` is a startup failure, not a runtime one (§8).

## 12. Anti-abuse: identity, rate limits, and quotas

Enforced only in `shared` mode (§8); irrelevant and unexercised in `local` mode, which is reachable by nobody but you.

**Identity.** Extract the client IP only from the header your chosen proxy actually sets (Fly.io: `Fly-Client-IP`; configure explicitly, never trust arbitrary forwarding headers from untrusted peers). Compute `ip_bucket_hash = HMAC-SHA256(RATE_LIMIT_HMAC_SECRET, canonical_ip)`. The secret must be stable across restarts — set once via `fly secrets set` and never regenerated casually; regenerating it is a deliberate action documented in the runbook (§17), with the previous key's window honored for in-flight quota records during rotation.

**Fast-path request guards** (`slowapi`, in-memory, resets on restart — the DB-backed quotas below are the actual authority):

| Bucket | Default | Rationale |
|---|---:|---|
| Public GET, combined | 600/min per IP | One polling viewer is ~40/min; supports several viewers behind one shared network comfortably. |
| Create + Run POST, combined | 20/min per IP | Generous enough for idempotent replay storms during a flaky connection, tight enough to block scripted flooding. |
| Admin | separate small limit | Never competes with viewer traffic. |

**Durable quotas** (DB-backed, survive restart):

| Scope | Default | Env var |
|---|---:|---|
| Creations per IP bucket / 24h | **10** | `CREATE_QUOTA_PER_IP_24H` |
| Creations globally / 24h | 100 | `GLOBAL_CREATE_QUOTA_24H` |
| Active executions per IP bucket | 1 | `CONCURRENCY_PER_IP` |
| Active executions globally | 2 | `GLOBAL_CONCURRENCY` |

**This default is set to 10, not v1.3's 5, specifically because of the previous review's finding**: a 5–10-person eval group is plausibly behind one or two shared networks (an office, a household), and a tight per-IP cap can silently suppress exactly the "50% start a second battle" signal §19 is trying to measure. Before actually running the eval: estimate how many testers share a network, and set `CREATE_QUOTA_PER_IP_24H` to at least that count plus a margin of 3–4. Treat this as a required pre-launch step, not a nice-to-have — add it to the §17 checklist, where it also appears.

Creation-quota rejections never insert a debate row or consume an attempt. Replays of hidden debates return `404` and never recreate them. IP associations (`ip_bucket_hash` on `ExecutionAttempt`/`CreationQuotaRecord`) are cleared by a weekly job after 7 days — the debate/message/judgment data itself is never touched by that cleanup.

## 13. Provider-call and spending budgets

Creation quotas (§12) bound how many *new* debates can exist; they do not bound what *older* debates still cost to run. Every dispatch — initial, corrective retry, resumed stage — passes a fresh durable reservation first.

| Setting | Default |
|---|---:|
| Provider deadline per call | 35s |
| Calls per stage per attempt | 3 (incl. first) |
| Retry backoff | ~1s, then 3s, with jitter |
| Stage deadline | 120s |
| Execution-attempt deadline | 15 min |
| Accepted executions per debate | 3 |
| Calls per debate, all attempts | 30 |
| Calls globally / 24h | 300 |
| Input token cap per call | 8,192 or the model's actual limit, whichever is smaller |
| Speech output cap | 512 tokens |
| Judgment output cap | 2,048 tokens |

**The real daily-spend bound is the global call cap, not the creation cap** — this is the specific arithmetic error from v1.2 that v1.3 correctly fixed and that this version keeps: 300 calls/day × (model's per-call price ceiling, computed from its actual input/output token caps above) is the number to actually budget against, independent of how many debates were *created* that day.

**Reservation.** Before every dispatch, atomically check current `run_id` ownership, per-stage/per-debate/global call allowance, and the global cost allowance, then insert a `ProviderCallReservation` row and increment counters — all in one transaction. Compute `MAX_RESERVED_COST_24H_USD` from the table above once you've confirmed real Groq pricing at Phase 1 (§7); there's no universal default to inherit, because it's a function of whichever model you actually pick. If token counting or pricing is unconfirmed, fail closed in `shared` mode rather than guess. Reservations stay counted for their full window even after a crash — conservative, not refunded automatically; actual usage is recorded separately in the same row for the admin stats view (§14) to show the gap between reserved and real spend.

## 14. AI behavior, content screening, and judging

**Debaters.** Atlas: analytical, structured. Nova: skeptical, probing. Both locked to their side, must engage the opponent's actual claims, no fabricated citations, no personal attacks, respect the round's objective. Prompt includes the topic snapshot, side, round objective, every prior committed speech with stable IDs, and length rules. Transcript content is quoted *data* to the model, never an instruction it can act on. Target 120–180 words, hard cap 250; reject empty/excessive output rather than silently truncating it.

**Content screening.** An offline lexical check (`better-profanity`, §3) runs on every committed speech and every judge free-text field before it's saved. A flagged result gets a corrective retry inside the existing 3-call stage budget — not a new budget. Exhausting the budget while still flagged fails that stage as `content_flagged`, preserved and resumable like any other speech failure. This is explicitly a cheap baseline, not comprehensive moderation: it can miss harmful text with no listed words and can false-positive on legitimate phrasing. Inspect real sample output (§18) and document any wordlist exceptions you add. An operator can hide a debate after the fact (`POST /api/admin/debates/{id}/hide`); there's no public report button in this MVP.

**Judge.** Neutral labels (Side A/Pro, Side B/Con) in the prompt, mapped back to Atlas/Nova server-side — reduces some bias, proves nothing about impartiality. Scores: Logical reasoning /35, Relevance /25, Rebuttal quality /25, Clarity /15, per side. The model supplies scores, explanations, and message references only; the backend sums totals and applies the draw rule (≤5-point difference = draw, ≥6 = higher total wins). Validate score bounds/types, explanation length, and that every cited message ID actually exists in the transcript — invalid output never becomes a visible result. Judge free-text passes through the same content filter as debater speech.

## 15. Frontend and accessibility

Dark theme, fixed: background `#0B1020`, surface `#111827`, violet `#8B5CF6`, Atlas cyan `#06B6D4`, Nova coral `#F43F5E`. Verify actual contrast ratios, don't assume.

Screens: Lobby (topics, previews, rules, Start, history, pending-creation recovery), Arena (round/speaker status, transcript, connection feedback, creator-only controls), Failure (transcript, failed stage, plain-language recovery options), Results (verdict, rubric, explanations, full transcript, next actions), Recent Battles (shared-archive notice, pagination, empty/error states).

All AI-generated text renders as plain text nodes — no markdown rendering, no `dangerouslySetInnerHTML`, nowhere in the pipeline. This is the actual XSS boundary (test it directly with literal `<script>` fixtures, §18); content screening (§14) is a separate, unrelated concern. Keyboard-operable, visible focus, status conveyed in text not just color, reduced-motion support, single transcript column on mobile, status changes announced without re-reading the whole transcript on every poll. Root-level React error boundary with a Reload action, wrapping the router; handle rejected API promises explicitly too, not just render-time throws.

The execution token is a powerful local secret: never put it in a copied link, a diagnostic dump, or a console log.

## 16. Persistence, backups, and retention

**SQLite behavior.** WAL mode set and verified at connection open; `busy_timeout=5000`. WAL improves reader/writer overlap but does not eliminate `SQLITE_BUSY` — only one writer commits at a time, and a timeout is still possible under load. Keep every transaction short; on a timeout, return/reconcile a `503` rather than silently retry the whole provider call (never re-dispatch to the model just because the *save* hit a lock). Offload DB and backup work so a lock wait never blocks the event loop.

**Backups (automated — safe to automate because it's non-destructive).** Nightly at 03:00 UTC, in-process (APScheduler): use SQLite's online backup API (never copy the raw file while WAL is active), write to a unique temp file, run off the event loop with a bounded deadline, run an integrity check plus a representative-row check on the result, atomically rename to a timestamped filename only once verified, then prune backups older than 7 days — always keeping the newest *verified-good* copy even if it's older than 7 days. One missed-schedule catch-up attempt on next ready-and-idle; never replay every missed run.

**Retention (manual, deliberately not automated).** Auto-deleting debates during a short 5–10-person eval is the wrong default — it's destructive, and the eval wants to look at completion/resume history (§19). Instead: log a warning once the live DB exceeds 500 MB, and provide `prune_old_debates.py --older-than-days N --dry-run` as a manual, explicit, operator-run step for when storage actually becomes a problem — not before.

**Independent recovery.** Same-volume backups protect against accidental changes and some logical corruption, not loss of that volume. That's an accepted, explicit limitation for this MVP (§7's optional off-host bucket is the upgrade path, not a requirement). After restoring from any backup, pause paid execution until provider usage and the cost ledger (§13) are reconciled against the restored point-in-time, or let the rolling window drain — restoring older reservation rows can undercount real spend.

## 17. Deployment and launch checklist

Checkpoint A (local):
- [ ] `DEPLOYMENT_MODE=local`; server bound to `127.0.0.1`.
- [ ] Fake-provider test suite green; at least 5 real sample debates reviewed for quality, repetition, and content-screen false positives.
- [ ] Full flow exercised locally: create → run → poll → leave/return → resume a forced failure → view in history.
- [ ] Error boundary verified with a forced render error.

Checkpoint B (shared):
- [ ] `fly volumes create`, `fly.toml` mounts it, `DATABASE_PATH` points at it.
- [ ] All four secrets set via `fly secrets set`; confirm the app refuses to reach "ready" with any missing (§8).
- [ ] `DEPLOYMENT_MODE=shared`; CORS restricted to the real frontend origin.
- [ ] Verify IP attribution with two real distinct clients (not two tabs on the same machine) — confirm each gets its own `ip_bucket_hash`.
- [ ] **Estimate the eval group's shared-network exposure and raise `CREATE_QUOTA_PER_IP_24H` accordingly (§12) before sending the link.**
- [ ] Restart the live machine mid-speech and mid-judging; confirm reconciliation and manual resume both work.
- [ ] Force one real backup run; restore it into an isolated DB; confirm transcript/judgment rows and a working `GET` against it.
- [ ] `/api/admin/stats` reachable with the real admin token; confirm it shows reserved vs. actual spend sensibly.
- [ ] Recent Battles correctly labeled as a shared archive in the UI copy itself, not just this document.

## 18. Verification

**Engine/persistence:** six-turn ordering and context correctness; judge rejects invalid/out-of-range/missing scores and unknown message references; draw at 5-point difference, win at 6; each speech/result visible only after its commit transaction; turn-4 failure preserves turns 1–3 and resume only regenerates 4–6; judge failure preserves all six speeches and resume calls no debater; stale `run_id` output can't commit; two concurrent debates both complete under simulated busy-timeout without a duplicated provider call.

**Authorization/quotas:** readers without a token can't Start/Resume; correct token still works after an IP change; wrong token exposes nothing; idempotent replay returns the original session even after the last quota slot is taken by someone else; simultaneous competing creates can't exceed the last available slot; bucket identity survives restart and the midnight boundary; spoofed forwarding headers from untrusted peers are ignored; ten simulated viewers behind one IP stay under the read-bucket limit while mutation floods are still rejected; global call/cost reservations correctly include retries and resumes from earlier days; a forced crash-then-resume can't double-spend a reservation.

**UI/content/admin:** reload mid-create recovers the same UUID/token/session and never auto-runs; literal `<script>`/HTML fixtures render as visible text, never execute; a flagged speech consumes the existing retry budget, not a new one, and an acceptable-topic fixture set measures false-positive rate; hide blocks list/detail/replay/run and stops in-flight work, and late output from a hidden run can't commit; unauthorized admin calls fail, authorized ones show real hidden content.

**Deployment/AI quality:** close/reopen mid-match; restart mid-speech and mid-judging; force backup success/failure/full-disk/cleanup paths and confirm the last good copy always survives; restore into isolation and verify it; run ≥5 varied real debates and read them for rebuttal grounding, repetition, verdict consistency, and content-screen false positives.

Fake-provider tests prove software correctness. Real-model review proves content quality. Neither substitutes for the other.

## 19. Definition of done and evaluation

Checkpoint B is reached when: a user picks a topic, watches six committed context-aware speeches, reads a validated verdict; the creator can leave/return and resume eligible failures without regenerating saved turns; other viewers can read but not spend that debate's remaining attempts; duplicate execution is impossible; restart recovery is automatic and visible; read throttling supports normal polling without false blocks; creation/call/cost budgets are enforced atomically; hidden content can't execute publicly; SQLite contention is handled without data loss; a verified backup has been restored at least once; deployment and limits are documented in `docs/`.

Evaluate with 5–10 people. Targets (provisional, carried from earlier versions): ≥80% debate completion, ≥80% verdict comprehension, ≥50% start a second battle. Track first-attempt completion separately from completion-after-resume. Record: duplicate/corrupt records (should be zero), real call usage vs. reserved, latency, any budget blocks, content-screen false positives, and — specifically — whether anyone hit the per-IP creation quota during normal use. **A normal-use rate-limit hit during the eval is a configuration bug to fix (§12), not evidence the control is working.**

## 20. Known risks

| Risk | Mitigation |
|---|---|
| Proxy/IP attribution misconfigured on the real host | Explicit two-real-client verification step in §17, before any secret-dependent logic is trusted |
| Per-IP quota collides with a shared-network eval group | Default raised to 10 and explicitly tied to a pre-launch sizing step (§12, §17) |
| Groq model/pricing drifts between this plan and build time | Deliberately deferred to a Phase 1 verification step (§3, §7, §13) rather than hardcoded |
| Four secrets to manage is real but small sprawl | Single consolidated list (§8) plus a startup check that fails loudly in `shared` mode instead of silently misbehaving |
| SQLite write contention at this plan's own 2-debate concurrency ceiling | WAL + busy_timeout + short transactions + an explicit concurrency test (§18) |
| Single process/single disk is a single point of failure | Accepted explicitly for this MVP; independent backup storage (§7) is the documented upgrade path, not a surprise gap |
| Scope creep across plan revisions without re-budgeting | This version's entire reason for existing (§13) — re-derived, checkpoint-separated hours instead of an inherited number |

## 21. Build sequence and hour estimate

Re-derived from scratch against this document's actual scope, not carried forward from an earlier version.

**Checkpoint A — 40–50 hours**

| Phase | Work | Exit criterion | Est. |
|---|---|---|---:|
| 1. Engine proof | Fake + real provider adapter, fixed six-turn sequence, prompt/schema validation, judge scoring, model/pricing discovery (§7) | One coherent CLI debate; ordering/context/rubric boundaries verified by fake-provider tests | 8–10h |
| 2. Persistence/resume | Schema, migrations, WAL+busy_timeout, checkpoints, attempts, token-hash field | Turn-4 failure resumes at turn 4; judging-only resume works | 10–12h |
| 3. Execution/API core | Task registry+supervisor, token-authorized create/run/get/list, atomic admission, startup reconciliation | Browser disconnect doesn't cancel accepted work; duplicate requests produce one worker | 10–12h |
| 4. UI + local integration | All screens, pending-creation/token storage, polling, error boundary, local real-provider smoke test | Full flow works locally end to end, incl. a forced resume | 12–16h |

**Checkpoint B — 36–50 hours**

| Phase | Work | Exit criterion | Est. |
|---|---|---|---:|
| 5. Anti-abuse | Split rate buckets, persisted HMAC identity + proxy verification, eval-aware quota defaults (§12) | Ten simulated same-IP viewers poll normally; floods are rejected; two real clients get distinct buckets | 10–14h |
| 6. Cost governance | Reservation ledger, confirmed pricing, fail-closed config, admin stats (§13, §14) | No dispatch happens without a prior reservation; stats show reserved vs. actual | 8–10h |
| 7. Content + moderation ops | Screening wired into retry path, admin hide, false-positive fixture tests | Flag consumes existing retry budget; hide stops a live debate immediately | 4–6h |
| 8. Backup/restore | Verified backup pipeline, restore test, manual pruning script | A forced backup restores cleanly into isolation | 8–10h |
| 9. Deploy + polish | Fly.io deployment, accessibility pass, ≥5 real-debate quality review, `docs/` | Checklist (§17) fully checked; Checkpoint B reached | 6–10h |

**Total: 76–100 focused hours.** Re-estimate after Phase 2 regardless — this is still a planning figure, not a measured one, and nothing about discovering it's wrong mid-build should be treated as the plan having failed; re-cut the remaining phases instead.

## 22. Technical references

- [FastAPI lifespan events](https://fastapi.tiangolo.com/advanced/events/)
- [FastAPI behind a proxy](https://fastapi.tiangolo.com/advanced/behind-a-proxy/)
- [Python asyncio tasks](https://docs.python.org/3/library/asyncio-task.html)
- [SQLite transactions](https://www.sqlite.org/lang_transaction.html)
- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html)
- [SQLite WAL mode](https://www.sqlite.org/wal.html)
- [SQLite busy timeout](https://www.sqlite.org/c3ref/busy_timeout.html)
- [SQLite online backup API](https://www.sqlite.org/backup.html)
- [Python sqlite3 / aiosqlite](https://docs.python.org/3/library/sqlite3.html)
- [slowapi](https://slowapi.readthedocs.io/)
- [APScheduler](https://apscheduler.readthedocs.io/)
- [Fly.io persistent volumes](https://fly.io/docs/volumes/overview/)
