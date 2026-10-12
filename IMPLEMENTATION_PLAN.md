# Implementation Plan — BotArena

Status legend: pending | in-progress | done | skipped (reason)

Full spec for every phase: [docs/planning/BLUEPRINT.md](docs/planning/BLUEPRINT.md) (v2.0). This
file tracks progress only — if the two ever disagree, this file is the current truth and the
blueprint is the design record.

Checkpoint A = works locally, for one person (Phases 1–4). Checkpoint B = safe to hand a link
to 5–10 people (Phases 5–9). See blueprint §2 for why these are not interchangeable and §13 for
why the hour estimate is split, not blended.

## Phase 1 — Engine proof [done]

Goal: prove the fixed six-turn debate + judge sequence works, in memory, against a fake
provider, with one real-model smoke test. Blueprint §3 (stack), §6 (data model shapes used
here, not yet persisted), §14 (AI behavior/judging), §21 Phase 1.

Tasks:
- [x] `backend/` scaffolded with `uv`, Python 3.13, `package = false` (app, not a library).
  Deps: `pydantic`, `groq`, `python-dotenv`; dev: `pytest`, `pytest-asyncio`.
- [x] `app/engine/models.py` — Side/Agent/RoundName enums, fixed `TURN_ORDER`, `Topic`,
  `Speech` (with stable `message_id`), `CriterionScores`, `Judgment`, `DebateResult`.
- [x] `app/engine/prompts.py` — debater prompt builder (side-locked, round objective, full
  prior transcript with stable IDs) and judge prompt builder (neutral Side A/Side B labels).
- [x] `app/engine/provider.py` — `Provider` protocol, `FakeProvider` (deterministic,
  scriptable, used by all unit tests), `GroqProvider` (real adapter, lazy-imports `groq`,
  requires `GROQ_MODEL`/`GROQ_API_KEY`, not exercised by the test suite).
- [x] `app/engine/validation.py` — speech word-count bounds (reject empty / >250 words);
  `JudgeOutputModel` (Pydantic) enforcing per-criterion score ranges, non-empty explanations,
  and that every `referenced_message_ids` entry exists in the transcript.
- [x] `app/engine/judge.py` — `score_judgment`: backend computes `pro_total`/`con_total` and
  applies the draw rule (diff ≤5 → draw, ≥6 → higher total wins). The model never supplies a
  total or winner.
- [x] `app/engine/orchestrator.py` — `run_debate`: runs the six fixed turns in order (one
  corrective retry per stage on validation/provider failure — the full 3-call stage budget
  and reservation ledger are Phase 2/3 scope, not here), then judging.
- [x] `app/cli.py` + `app/data/topics.json` (3 seed topics) — `uv run python -m app.cli
  --provider fake|groq --topic <id>` runs one full debate end to end.
- [x] `backend/tests/unit/` — 21 tests, all green: six-turn ordering/speakers/sides/rounds,
  stable-ID context growth turn-over-turn, Atlas-opening-has-no-context, fixed sides can't be
  overridden by model text, empty/oversized speech rejected (not silently truncated), draw at
  exactly 5 / win at exactly 6, model has no `total`/`winner` field to supply, judge schema
  rejects out-of-range/non-integer/missing/unknown-reference output.
- [x] Fake-provider CLI run verified end to end (`--provider fake`): six speeches + a
  committed judgment printed correctly.
- [x] **Real-model smoke test done** with `GROQ_MODEL=openai/gpt-oss-20b` (picked from Groq's
  live lineup: only GPT OSS 20B/120B are offered for text; Llama 3.3 70B is gone). Four real
  debates run across two topics; the last one is clean. Findings and fixes:
  - gpt-oss is a reasoning model: hidden thinking counts against the token cap, so the adapter
    sends `max_completion_tokens = cap + 1500` and `reasoning_effort="low"`, and raises
    `ProviderError` if `finish_reason == "length"` instead of accepting a cut-off speech.
  - Judge in `json_object` mode derailed once into garbage (Groq `json_validate_failed`). Tested
    three variants, 3 trials each, all valid; switched the judge to strict `json_schema` mode
    (`validation.JUDGE_JSON_SCHEMA`), which constrains structure. Score ranges and message-id
    existence still enforced by `JudgeOutputModel`.
  - Debaters invented statistics/studies despite the rule → prompt now forbids citing studies,
    programs, years or percentages outright. Re-run: none appeared.
  - **Side flip:** Nova's (Con) closing argued Pro and the judge did not notice. Caused by the
    closing objective + the opponent's closing sitting right above it. Fixed with a closing
    objective that restates the speaker's own case, plus an end-of-prompt FOR/AGAINST reminder.
    One clean re-run; this is a sample of 1, not proof — keep watching it in Phase 9's
    ≥5-debate review.
  - Windows console crashed on the model's non-breaking hyphens → CLI forces UTF-8 stdout.
  - A full debate takes ~17s wall time on the free tier. Firing 3 judge calls in parallel hit a
    free-tier 429 rate limit; the Phase 1 retry has no backoff (Phase 2/3 scope, blueprint §13).

Exit criterion (blueprint §21): "One coherent CLI debate; ordering/context/rubric boundaries
verified by fake-provider tests." — met, with a real model and fake-provider tests.

Open quality notes carried forward (not Phase 1 blockers):
- The judge cites all six message IDs every time, so `referenced_message_ids` is a weak signal.
- The judge's strengths/weaknesses text can describe the wrong side's content (it missed the
  side flip above). Consider a cheap automated side-consistency check, or a judge instruction
  to flag a speech that argues the wrong side. Decide in Phase 9's quality review.
- Speeches sometimes run past the 120–180 word target (hard cap 250 still enforced).

General rules for every phase below: new dependencies go in via `uv add` (backend) or `npm`
(frontend), choosing current versions — the blueprint's version pins are a starting point, not
gospel. Tests use `FakeProvider`/fixtures only; never call Groq from the test suite. Unit tests
for each new behavior ship in the same phase.

## Phase 2 — Persistence and resume [done]

Goal: debates are saved to SQLite after every committed speech, and a failed run can resume
from its checkpoint without regenerating saved turns. Blueprint §5, §6, §13 (retry table),
§16 (SQLite behavior). Est. 10–12h.

Tasks:
- [x] Add deps: `sqlalchemy[asyncio]`, `aiosqlite`, `alembic`.
- [x] `app/config.py`: settings from env (`DATABASE_PATH` default `./data/botarena.db`,
  `DEPLOYMENT_MODE` default `local`). `app/db/base.py`: async engine + session factory; on
  every connection run `PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=5000`,
  `PRAGMA foreign_keys=ON`.
- [x] `app/db/models.py`: `Debate`, `DebateMessage`, `Judgment`, `ExecutionAttempt` exactly as
  blueprint §6 (include `execution_token_hash`, `hidden*`, `revision`, progress/activity
  fields). Create `CreationQuotaRecord` and `ProviderCallReservation` tables now too so later
  phases don't need a schema rewrite, but leave them unused until Phases 5–6.
- [x] Alembic set up under `backend/migrations` (or `app/db/migrations`), first migration
  generated and applied; `uv run alembic upgrade head` creates a working DB.
- [x] Repository layer (e.g. `app/db/repo.py`) with atomic checkpoint writes, each in ONE short
  transaction that also bumps `debates.revision`: commit one speech + progress update; commit
  judgment + totals/winner + attempt completion + debate `completed`; commit failure
  (status `failed`, sanitized `error_code`/`error_message`, `failed_stage`, `failed_turn`,
  attempt finished). Never hold a transaction while waiting on the provider.
- [x] Refactor `engine/orchestrator.py` so it is resumable: start from the committed prefix,
  call a persistence callback/repo after each turn, and enter judging directly when 6 speeches
  exist. Keep `run_debate` working in-memory for the CLI/tests (a no-op persistence
  implementation is fine).
- [x] Checkpoint integrity check before resume: saved turn positions form an uninterrupted
  prefix of 1–6 with the expected round/agent/side per `TURN_ORDER`; otherwise refuse with a
  non-resumable error.
- [x] Stage retry policy per blueprint §13: max 3 provider calls per stage, backoff ~1s then
  ~3s with jitter, honor `Retry-After` on 429 when it fits the 120s stage deadline, 35s
  per-call deadline. Invalid output gets a corrective retry inside the same 3-call limit.
  Do not retry auth/config errors. (Phase 1 only has one blind retry; this replaces it.)
- [x] Per-debate counters now: `accepted_attempts` (max 3 = initial + 2 resumes) and
  `total_calls` (max 30). The global ledger is Phase 6.
- [x] Tests (fake provider + temp SQLite file): failure at turn 4 → resume generates only
  turns 4–6 and judging, turns 1–3 untouched (same IDs/content); judge failure with 6 saved
  speeches → resume calls no debater; stale/duplicate `turn_order` rejected by the unique
  constraint; checkpoint commits are atomic (speech + revision together); two debates writing
  concurrently both finish with no lock errors and no duplicated turn; retry policy honors
  budget and `Retry-After`.

Exit criterion: turn-4 failure resumes at turn 4; judging-only resume works; WAL/busy-timeout
set and tested.

Phase 2 verification (2026-10-11):
- `uv run pytest -q`: **57 passed**. Includes turn-4 resume across an engine reconnect,
  judgment-only resume, UUID preservation, corrupt-prefix refusal, duplicate-turn constraints,
  rollback of speech/progress and judgment/completion, two concurrent debates, a deliberate
  SQLite writer lock, no transaction during provider calls, stale ownership, attempt/call
  limits, cancellation, sanitized failures, retry deadlines/Retry-After, and offline SDK tests.
- `uv run alembic upgrade head`: succeeded against the default local database;
  `uv run alembic current`: `b478c86b9ed8 (head)`;
  `uv run alembic check`: `No new upgrade operations detected.`
  Tests also upgrade/downgrade/re-upgrade a separate temporary database.
- Offline CLI completed all six speeches and judging: Pro 80, Con 75, DRAW.
- Exit criterion met. Remaining: none within Phase 2. Next: Phase 3, after user sign-off.

Implementation handoff:
- `create_database(Settings().database_path)` returns an async engine/session factory;
  apply Alembic migrations before using it. `DebateRepository.create` stores a topic snapshot
  and a caller-supplied token HASH; token generation/validation and HTTP idempotency are Phase 3.
- `DebateRepository.execute` accepts a provider/model ID, opens an attempt, reconstructs the
  saved prefix, and runs checkpointed execution. `begin_attempt` also exposes the claim and
  checkpoint callbacks for Phase 3's task registry. Every callback opens a short session.
- Storage failures propagate immediately without redispatch; active state left by a storage
  outage needs Phase 3 reconciliation. No server/task supervisor/startup reconciliation yet.
- The original CLI remains in-memory. Persistent behavior is exercised through the repository
  tests; no real Groq calls were made in this phase.
- Remaining phase estimates are still provisional; Phase 3 integration is the next point to
  measure lifecycle work before revising the remaining project estimate.

## Phase 3 — Execution lifecycle and API [done]

Goal: a FastAPI server that creates, starts, resumes, and reports debates; accepted work keeps
running with no browser attached. Blueprint §4, §5, §8, §10, §11. Est. 10–12h.

Tasks:
- [x] Add deps: `fastapi`, `uvicorn`; dev: `httpx` (already present via groq) for the ASGI test
  client.
- [x] `app/main.py`: FastAPI app with lifespan. Binds `127.0.0.1` in `local` mode; CORS allows
  only localhost dev origins. Startup order: apply/verify migrations → reconcile → ready.
- [x] `app/core/auth.py`: execution token = 32 random bytes, client-generated, sent in
  `X-Debate-Token`; server stores only SHA-256 hash; constant-time compare. Never log tokens.
- [x] Endpoints per blueprint §11: `GET /api/topics`, `POST /api/debates` (idempotent create),
  `POST /api/debates/{id}/run` (Start/Resume, returns `202`), `GET /api/debates/{id}` (public
  snapshot; valid token adds `controls`), `GET /api/debates?limit=20&cursor=` (newest first,
  stable `(created_at,id)` cursor, excludes hidden), `GET /api/health`. All debate responses
  `Cache-Control: no-store`. Snapshot shape per blueprint §7/§11 (status, revision, progress,
  messages, judgment, error, controls only with valid token).
- [x] Create ordering (blueprint §11 "Atomic create ordering"): look up `client_request_id`
  first; matching replay → `200` with the existing session even if quota would be exhausted;
  changed input → `409`; wrong token → generic `403`; only then validate topic and insert.
  (Quota enforcement itself is Phase 5; keep the ordering correct now.)
- [x] Atomic admission: single conditional `UPDATE` claims the debate (status `created`/eligible
  `failed` → `running`, new `run_id`, `accepted_attempts+1`), not read-then-write. Active/
  completed → `409` with machine-readable reason. Rejected admission consumes no attempt.
  Global active capacity 2 → `503`.
- [x] `app/engine/orchestrator.py` driven by an in-process task registry (strong refs to
  `asyncio.Task`s, exceptions retrieved), worker owns its own DB session, every checkpoint
  re-verifies it still owns `run_id`. Supervisor detects active debates with no live task →
  marks interrupted. 15-minute attempt deadline.
- [x] Startup reconciliation: every `running`/`judging` debate → `failed` with
  `server_interrupted`, attempts → `interrupted`; never auto-resume. Graceful shutdown: stop
  admitting, grace period, cancel, persist interruption, re-raise cancellation.
- [x] Sanitized errors `{code,message,failed_stage,failed_turn}`; no stack traces/secrets.
- [x] Tests (httpx ASGI + fake provider): create/replay/conflict; token required for run;
  public GET has no controls; duplicate concurrent `/run` → exactly one worker; client
  disconnect mid-run doesn't cancel work; restart reconciliation marks failed and preserves
  speeches; hidden/missing → `404`; pagination cursor stable.

Exit criterion: accepted work survives browser disconnect; duplicate requests produce exactly
one worker; restart becomes a resumable failure.

Phase 3 verification (2026-10-11):
- `uv run pytest -q`: **91 passed in 20.24s** (57 existing + 34 new API/auth tests).
  Covers create/replay/conflict and concurrent replay, token/hash exclusion, authorized controls,
  concurrent Start requests, capacity admission including concurrent repository calls without
  the manager lock, disconnect and cancelled HTTP response after admission, restart during
  running/judging with manual resume, orphan supervision, shutdown, attempt deadline,
  saved-prefix API resume, hidden/missing paths, stable pagination with equal timestamps and
  concurrent inserts, ineligible resume, CORS, sanitized API/provider/storage failures.
- `uv run alembic upgrade head`: succeeded; `uv run alembic check`:
  `No new upgrade operations detected.` No schema revision needed for Phase 3.
- Required offline CLI completed six speeches and judging: Pro 80, Con 75, DRAW.
- Real HTTP smoke against `127.0.0.1:8765` using FakeProvider: health 200, create 201,
  replay 200, run 202, polling reached completed with six speeches and DRAW. The smoke server
  was stopped. No real Groq requests were made.
- `git diff --check`: clean. Remaining: none within Phase 3. Next: Phase 4 after sign-off;
  ask for the user's UI design before building it.

Implementation handoff:
- From `backend/`: `uv run python -m app.server --provider fake` starts one worker at
  `127.0.0.1:8000`. `--port` changes only the port. Fake is the default, including
  `uv run uvicorn app.main:app`; `--provider groq` explicitly selects real work and needs
  GROQ_API_KEY/GROQ_MODEL in the process environment. Do not run Groq without user approval.
- Startup applies migrations through the app's database connection (honoring DATABASE_PATH),
  reconciles interrupted attempts, then reports ready. Shared mode currently refuses startup
  until Phases 5–6 implement the required protections; local dev CORS accepts only
  http://localhost:5173 and http://127.0.0.1:5173.
- Browser token contract for Phase 4: generate 32 random bytes, encode as 64 hexadecimal
  characters, keep locally, send only in X-Debate-Token. Server hashes decoded bytes with
  SHA-256 and uses constant-time comparison. Tokens/hashes are absent from all responses.
- Create takes `{topic_id, client_request_id}`. Returns the full snapshot, 201 new/200 replay.
  Run returns 202 `{id,status}`. Topics returns `{topics:[...]}`; history returns
  `{items:[...],next_cursor}` with an opaque cursor. Snapshots include `id`, `topic`, `status`,
  `revision`, timestamps, `progress`, `messages`, `judgment`, and nullable `error`.
  Progress fields: completed_turns, total_turns, current_round, active_speaker, active_turn,
  activity. Messages: id, agent, side, round_number, turn_order, content, created_at.
  Judgment includes pro_scores/con_scores, pro_total/con_total, winner, explanations,
  referenced_message_ids and provenance. Valid tokens add controls with can_start,
  can_resume, remaining_attempts. Capacity is checked on Start; controls express eligibility.
- API errors use `{error:{code,message}}`; saved failure snapshots add failed_stage/failed_turn.
  All /api/debates responses have Cache-Control: no-store. Capacity/storage 503 responses
  include Retry-After: 5. History limits are 1–100, default 20.
- ExecutionManager owns tasks independently of requests, checks orphaned execution every 5s,
  bounds attempts at 900s, stops admission on shutdown, allows 10s grace, then cancels and
  preserves checkpoints. Provider exceptions/cleanup failures never log raw diagnostics.
- Scope carried forward: per-IP/global creation quotas remain Phase 5, reservation/cost
  enforcement Phase 6, admin hide Phase 7. No UI, deployment, commits or pushes in this phase.

## Phase 4 — UI with fixtures [done]

Goal: complete React UI for every lifecycle state, wired to the real API. Blueprint §10, §15.
Est. 12–16h. **ASK THE USER FIRST** whether they have their own designed UI to drop in
(`/ui-drop` workflow in their Claude setup: wire data/logic only, never restyle); if they do,
integrate it as-is instead of building the pages below from scratch.

Tasks (if building from the blueprint):
- [x] `frontend/` via Vite + React + TypeScript + Tailwind; Vitest + React Testing Library.
  Dev server proxies `/api` to `127.0.0.1:8000`.
- [x] Theme tokens: background `#0B1020`, surface `#111827`, violet `#8B5CF6`, Atlas cyan
  `#06B6D4`, Nova coral `#F43F5E`; verify real contrast ratios.
- [x] Pages: Lobby (topics, Start, history link, pending-creation recovery), Arena (round/
  speaker status, transcript, retry/judging state, Jump to Latest, connection status),
  FailureView (saved transcript, failed stage, Resume only with token + eligibility),
  Results (verdict, rubric, strengths/weaknesses, full transcript, New Battle), RecentBattles
  (shared-archive notice, 20/page cursor pagination, empty/error states).
- [x] `storage/pendingCreation.ts`: on Start, generate `client_request_id` + 32-byte token
  (Web Crypto), save `{client_request_id, topic_id, token, created_at}` keyed by UUID BEFORE
  the create call; after success move the token to per-debate storage, THEN clear pending. On
  mount, reconcile pending entries <5 min old by re-sending the same create ONLY — never call
  `/run` from recovery; older entries get Recover/Discard.
- [x] `hooks/usePolling.ts`: ~1.5s, one in-flight request at a time, pause when tab hidden,
  immediate fetch on visible, backoff to 15s on errors, honor `Retry-After`, ignore snapshots
  with an older `revision`, dedupe messages by id and sort by `turn_order`, stop on terminal
  states and on `404`.
- [x] AI text rendered as plain text nodes only — no markdown, no `dangerouslySetInnerHTML`.
  Test literal `<script>`/HTML fixtures render as text.
- [x] Root `ErrorBoundary` with Reload; handle rejected API promises explicitly. Keyboard
  access, visible focus, status not by color alone, reduced-motion, single column on mobile,
  polite live-region announcements. Never put the token in URLs/logs.
- [x] Verify in a real browser: start a battle, leave and return mid-debate, refresh, force a
  failure and Resume, check history, mobile width.

Exit criterion: full local flow works end to end including a forced resume. **Checkpoint A
reached here.**

Phase 4 verification (2026-10-11):
- User confirmed they have no design yet and requested a basic functional reference to help
  design their own later. Built the baseline with the prescribed palette; custom restyling
  remains a future user-directed change.
- `npm test`: **22 passed**, 5 files, 3.45s. Covers header-only tokens, sanitized connection
  failures, pending token preservation/promotion and deduplicated recovery, old-entry manual
  recovery, explicit Start/Resume, no execution on mount/history/recovery, literal HTML text,
  root render-error containment, polling ordering/revisions/visibility/single-flight/backoff,
  Retry-After, terminal/404 stopping, and text contrast.
- `npm run build`: TypeScript and Vite production build succeeded (36 modules).
- `uv run pytest -q`: **91 passed in 21.50s**. Required offline CLI completed six speeches
  and judging: Pro 80, Con 75, DRAW. `git diff --check`: no whitespace errors.
- Real browser against local FastAPI + Vite: initial completion/results, history, leave and
  return while still running, refresh while running, forced turn-4 failure, explicit Resume,
  refresh after Resume, completion with all six speeches. The saved first three speech IDs
  and contents were unchanged. Tested 375px mobile and 1200px desktop; no horizontal overflow,
  stacked mobile cards and three desktop columns. No real Groq calls.
- Contrast ratios on surface: white 17.74:1, cyan 7.31:1, coral 4.83:1; black on violet
  buttons 4.96:1. Violet on background is 4.47:1, used only for large heading text (3:1
  threshold). Nova avatar uses white on its coral tint (15.59:1).
- Exit criterion met for the local fixture flow (Checkpoint A software flow). Remaining:
  none within Phase 4. Real-model quality review remains the Phase 9 task; fake fixtures do
  not establish argument quality. Next: Phase 5 after user sign-off.

Implementation handoff:
- From `frontend/`: `npm run dev` binds 127.0.0.1:5173 and proxies /api to 127.0.0.1:8000;
  `npm test` runs Vitest; `npm run build` checks types and produces frontend/dist.
- From `backend/`: `uv run python -m app.server --provider fake` starts the normal local API.
  Both dev servers were left running for preview; the backend uses the isolated, ignored
  `DATABASE_PATH=./data/phase4-browser.db` containing the browser test debates. Stop with Ctrl+C.
- Reproducible browser fixture: set DATABASE_PATH to a separate test database, then
  `uv run uvicorn tests.fixtures.ui_server:app --host 127.0.0.1 --port 8000`.
  Its second accepted execution fails at turn 4; the third is scripted for the manual resume.
  This fixture server was stopped after QA and replaced with the normal FakeProvider server.
- Execution secrets remain in localStorage; shareable links contain only debate IDs. Pending
  recovery never dispatches execution. Losing browser storage loses Start/Resume control.
- Phases 3 and 4 remain uncommitted on codex/phase-2-persistence-resume; no commit/push was
  requested for these phases. Phase 5 protections and public deployment are still pending.

## Phase 5 — Anti-abuse [done]

Goal: safe to expose to a small group. Blueprint §8 (DEPLOYMENT_MODE), §12. Est. 10–14h.

Tasks:
- [x] `DEPLOYMENT_MODE=shared` startup validation: refuse to become ready unless
  `ADMIN_TOKEN`, `RATE_LIMIT_HMAC_SECRET` (and a real `GROQ_API_KEY`/`GROQ_MODEL`) are set and
  non-trivial. `local` mode still binds `127.0.0.1` only.
- [x] `app/core/identity.py`: client IP only from the configured trusted proxy header (Fly:
  `Fly-Client-IP`); ignore spoofed forwarding headers from untrusted peers;
  `ip_bucket_hash = HMAC-SHA256(RATE_LIMIT_HMAC_SECRET, canonical_ip)` with a `key_version`.
- [x] `slowapi` in-memory buckets keyed by that identity: public GET 600/min, create+run POST
  20/min, admin separate. Honor `Retry-After`.
- [x] Durable DB quotas (rolling 24h, UTC): creations per IP bucket (`CREATE_QUOTA_PER_IP_24H`,
  default 10), global creations (100), active executions per IP (1) and global (2). Record a
  `CreationQuotaRecord` per NEW creation only; idempotent replays consume nothing. Quota
  rejection → `429` with a specific `code` and `Retry-After`.
- [x] Weekly job clears `ip_bucket_hash` from rows older than 7 days (keep the rows).
- [x] Tests: ten simulated same-IP viewers polling stay under the read bucket; POST floods
  rejected; quotas survive restart and midnight; competing last-slot creates can't exceed the
  limit; replay after last slot still returns `200`; spoofed headers ignored.

Exit criterion: normal polling works; floods and over-quota creates are rejected; two real
distinct clients get distinct buckets (verified on the real host in Phase 9).

Phase 5 verification (2026-10-12):
- `uv run pytest -q`: **127 passed in 23.32s** (91 existing + 36 anti-abuse cases). Covers secret and
  configuration validation, IPv4/IPv6 canonicalization and HMAC stability, trusted peers,
  malformed/duplicate headers, HTTP forwarding spoof attempts, 400 polls from ten viewers
  within the combined 600/min bucket, rejection of request 601, combined mutation floods,
  independent admin limits, CORS/Retry-After/no-store on rejections, local-mode bypass,
  ten-creation default, replay/conflict/auth/hidden ordering, simultaneous last-slot creates,
  per-IP and global rolling quotas through restart/midnight/expiry, concurrent per-IP claims,
  global capacity, rejected attempts free, resume from a different network, seven-day cleanup
  boundaries/idempotency/startup catch-up, weekly scheduling/cancellation, loopback runner,
  and shared execution staying blocked before Phase 6.
- Required offline CLI completed six speeches and judging: Pro 80, Con 75, DRAW.
- `uv run alembic check`: `No new upgrade operations detected.` Existing quota/attempt
  columns suffice; no new migration. No real Groq calls, cloud resources, commits or pushes.
- Exit criterion met for local simulated clients and repository admission. Remaining: none
  within Phase 5. Two real client/proxy attribution checks remain Phase 9 as planned.
  Next: Phase 6 after sign-off.

Implementation handoff:
- Anti-abuse guards apply only in shared mode. Local fake-provider preview continues to work
  without shared secrets or quotas. Shared startup validates ADMIN_TOKEN, RATE_LIMIT_HMAC_SECRET,
  GROQ_API_KEY (at least 32 characters, varied and not obvious placeholders), and GROQ_MODEL.
  Settings repr hides secrets; validation errors never include their values.
- Shared read/create endpoints can become ready with valid configuration. All shared HTTP
  Start/Resume requests deliberately return 503 `cost_governance_unavailable` until Phase 6
  installs durable spending reservations; no accepted attempt or provider call is consumed.
  Do not describe this phase alone as public-launch ready.
- Configure TRUSTED_PROXY_NETWORKS as explicit immediate-peer CIDRs and CLIENT_IP_HEADER
  (default Fly-Client-IP). Empty CIDRs trust no forwarding headers and use the socket peer.
  A trusted peer must supply exactly one valid address; missing/malformed headers get 400.
  IPv4-mapped IPv6 normalizes to IPv4. Only HMAC digests persist, with RATE_LIMIT_KEY_VERSION
  on creation records. Keep the HMAC secret stable across restarts; key rotation/migration
  requires a deliberate procedure honoring existing windows (Phase 9 runbook).
- Supported runner keeps one worker on 127.0.0.1, disables automatic proxy-header rewriting
  and raw-IP access logs. Any direct uvicorn launch must also use --no-proxy-headers;
  production binding/proxy setup remains Phase 9.
- RequestLimits uses SlowAPI's public limits backend, per-app in-memory fixed windows:
  600/min reads, 20/min combined create/run POSTs, 10/min admin. Replays remain subject to
  this request guard but consume no durable creation quota. Retry-After accompanies 429s.
- Creation quota check and record insertion share the existing BEGIN IMMEDIATE transaction
  after replay resolution. Defaults: per IP 10/24h, global 100/24h. Rejections add no rows.
- ExecutionManager accepts a ClientIdentity and forwards hash/per-IP capacity to the atomic
  repository claim. Defaults shared per-IP 1, global 2 (configuration cannot exceed 2).
  Phase 6 removes the HTTP execution gate only when reservations are wired; the identity
  forwarding and concurrency error mapping are already in place (429, Retry-After: 5).
- Identity cleanup runs once on shared startup, then every seven days, and is cancelled at
  shutdown. It clears only old creation/attempt hashes, retaining rows and all debate content.
  Failed scheduled cleanup logs a sanitized warning and retries next week.
- Dependencies installed: slowapi 0.1.10 and limits 5.8.0. New configuration is documented
  in backend/.env.example; the real .env was not accessed. Work remains uncommitted on the
  existing codex/phase-2-persistence-resume branch together with Phases 3–4.

## Phase 6 — Cost governance [done]

Goal: no provider call without a prior durable reservation. Blueprint §13. Est. 8–10h.

Tasks:
- [x] Confirm current Groq pricing/limits for `openai/gpt-oss-20b` from Groq's official
  console docs (not third-party trackers); store as a `price_version`-tagged config.
- [x] Reservation function: in ONE transaction check run ownership, per-stage/per-debate/global
  call allowances (global 300 calls/24h) and the cost allowance (`MAX_RESERVED_COST_24H_USD`,
  no default — fail closed in `shared` mode if unset), insert `ProviderCallReservation`
  (integer cents, conservative), bump counters. ALL provider calls (retries, resumes) go
  through it; no SDK-internal retries bypass it (set `max_retries=0` or route them through).
- [x] Reservations stay counted for the full window even after crashes; record actual tokens/
  cost when known without refunding automatically. Mid-match allowance exhaustion → fail at
  checkpoint with `global_usage_limit`, no auto-resume.
- [x] Reject oversized prompts rather than send unbounded; keep token caps (speech 512,
  judgment 2048 visible, plus reasoning headroom — account for it in the reserved maximum).
- [x] `GET /api/admin/stats` (+ `X-Admin-Token` auth, `401` otherwise): active debates,
  creations in window, reserved vs actual spend, remaining allowances, failures by stage/code,
  DB size. No raw IPs.
- [x] Tests: simultaneous last-budget callers can't overspend; crash leaves reservation
  counted; retries/resumes from earlier days count; unset cost cap blocks paid work in
  `shared` mode.

Exit criterion: no dispatch happens without a reservation; stats show reserved vs actual.

Phase 6 verification (2026-10-12):
- Committed the completed Phase 3–5 work before starting: `e27cbfe`, "Implement execution
  API, frontend, and anti-abuse guards", on codex/phase-2-persistence-resume. Not pushed.
- Verified Groq's official [model pricing/limits](https://console.groq.com/docs/model/openai/gpt-oss-20b)
  and [rate limits](https://console.groq.com/docs/rate-limits). Stored source links/date and
  `groq-standard-2026-10-12` in app/data/pricing.json: standard uncached input $0.075/M,
  output $0.30/M, context 131072, max output 65536. Published free-plan base limits are
  30 RPM, 1000 RPD, 8000 TPM, 200000 TPD; account-specific limits may differ.
- `uv run pytest -q`: **154 passed in 33.57s** (127 existing + 27 cost-governance cases).
  Covers a durable reservation visible before every persistent provider dispatch, counters
  and caps, SDK permit matching/single use and a fully mocked seven-call SDK integration,
  simultaneous last-call/last-cent claims, stage limits, crash/reconnect retention, next-day
  retries/resumes, actual usage on success/truncation, oversize input and stale-run refusal,
  mid-match exhaustion preserving speeches, accounting-breach latch, missing/invalid caps,
  unconfirmed-input refusal, authenticated/private admin stats, CLI persistence, no redispatch
  after usage-storage failure, shared FakeProvider flow with per-IP limits, and HTTP budget
  admission rejection without consuming an attempt. Tests never call Groq.
- Required offline CLI completed six persisted speeches and judging: Pro 80, Con 75, DRAW.
- `uv run alembic check`: `No new upgrade operations detected.` `git diff --check`: clean.
- Exit criterion met for the reservation implementation and fake/offline-SDK execution.
  Remaining: none in the software implementation. Paid activation remains deliberately
  gated on verified input accounting and an explicit daily cost cap; see below.
  Next: Phase 7 after sign-off. Phase 6 changes remain uncommitted.

Implementation handoff:
- app/core/costs.py reserves in one short writer transaction: verify run ownership, stage
  allowance (3 per attempt), debate allowance (30), global rolling call count (default 300),
  and reserved cents; insert reservation and increment debate/attempt counters atomically.
  Provider calls happen after commit with no database transaction held. Admission checks
  initial headroom too and rolls back a rejected claim without consuming an attempt.
- Reserve the full 8192 input cap plus 2012 speech / 3548 judgment output tokens (existing
  1500 reasoning headroom included). Round up to whole cents per call: the current real
  profile reserves 1 cent per call, so 300 calls conservatively reserve $3. Fake calls reserve
  zero cents but count toward call allowances. Unknown real-model prices fail closed.
- Global call quota and cost cap apply at reservation time to retries and resumes regardless
  of debate creation date. Failed/abandoned/crashed reservations remain counted until their
  rolling 24h expiry; never refund from actual usage. Mid-run exhaustion persists a resumable
  `global_usage_limit` failure and never restarts automatically.
- Each real SDK dispatch needs a matching one-use request permit created only after the
  durable reservation. Groq SDK retries remain disabled. Pure in-memory FakeProvider engine
  unit tests retain their Phase 1 seam; production API and CLI calls use the durable path.
- Successful responses capture usage before speech validation/commit; valid content marks
  its reservation committed in the content transaction. Invalid retries, provider failures,
  cancellation, and reconciliation mark failed/abandoned without reducing reserved amounts.
  Missing usage remains unknown. Recorded actual cost is an uncached-rate token estimate,
  rounded up per call to cents, not the provider invoice (free credits/cache discounts differ).
- IMPORTANT PAID ACTIVATION NOTE: provider-side strict-schema/chat token framing is not
  confirmed by the public docs. The request-size guard uses UTF-8 serialized bytes plus
  1024 framing allowance; this is a conservative candidate bound, not a verified Groq token
  count. INPUT_ACCOUNTING_CONFIRMED defaults false, and all real-model dispatch/admission
  fails closed while false, including local CLI. Do not set it true merely to get past the
  gate. Verify against Groq usage for the actual payload/template before paid activation;
  real-model runs require user approval under AGENTS.md. Carry this check into Phase 9's
  approved real-model review. Fake execution does not require this flag.
- If reported input/output exceeds a reservation, retain usage and stop with
  `token_accounting_unconfirmed`; future paid admission/dispatch for that model/price version
  stays blocked by the persisted breach. Review the bound/profile and publish a newly
  verified profile version before reactivation; do not erase the audit record.
- MAX_RESERVED_COST_24H_USD has no default; shared startup requires a positive whole-cent
  amount and a supported pricing profile. GLOBAL_CALL_QUOTA_24H defaults 300. Configuration
  is documented only in .env.example; real .env was not read. No real model calls were made.
- `/api/admin/stats` requires X-Admin-Token and returns no-store usage/allowance/failure
  aggregates, known/unknown actual-cost counts, accounting breaches, and database/WAL/SHM
  bytes. No raw IPs, bucket hashes, execution secrets, paths, or transcript text.
- CLI is local-only and now persists debates/reservations to DATABASE_PATH with stable UUID
  speech IDs. It no longer loads .env implicitly; configure process environment explicitly.
  It does not expose the generated execution token or provide browser resume control for
  CLI-created runs. Browser-created debates retain the normal creator token/resume flow.
- Refreshed the idle local FakeProvider preview backend on 127.0.0.1:8000 using the existing
  ignored phase4-browser.db; health reports ready and unauthenticated admin stats returns 401.
  The frontend preview remains at 127.0.0.1:5173. No paid execution was started.

## Phase 7 — Content screening and moderation ops [done]

Goal: basic screening plus an operator kill-switch. Blueprint §5 (hide), §14. Est. 4–6h.

Tasks:
- [x] `better-profanity` (or equivalent offline wordlist) check on every speech and every
  judge free-text field before commit. A flag = corrective retry inside the existing 3-call
  stage budget (no new budget). Exhaustion → `content_flagged`, earlier speeches preserved,
  resumable. Be honest in docs/UI that this is a profanity filter, not moderation.
- [x] `POST /api/admin/debates/{id}/hide`: set `hidden`; if active, bump revision, invalidate
  `run_id` ownership, mark `failed` with `operator_hidden`, cancel the worker; late output must
  not commit. `GET /api/admin/debates/{id}` shows hidden content. Hidden debates `404` on every
  public path (detail, list, replay-create, run).
- [x] Optional side-consistency safeguard (see Phase 1 open notes): a cheap check or judge
  instruction that flags a speech arguing the wrong side. Decide with the user before building.
  **Deferred:** no safeguard or prompt change implemented; carry the choice to Phase 9's
  user-reviewed model-quality work, as already recorded in the Phase 1 quality notes.
- [x] Tests: flagged speech consumes the existing retry budget; acceptable-topic fixtures
  measure false positives; hide mid-run stops further provider calls and blocks late commits.

Exit criterion: flags share the retry budget; hide stops a live debate immediately.

Phase 7 verification (2026-10-12):
- Installed better-profanity 0.7.0 with `uv add`, inspected its real API, and used the bundled
  default offline wordlist without exceptions or new provider calls.
- `uv run pytest -q`: **182 passed in 37.50s** (154 existing + 28 moderation cases).
  Covers speech rejection/corrective retries, all five judge free-text fields, shared three-call
  stage budgets, flagged turn-4 exhaustion and resume with original prefix IDs/content,
  judging-only resume after flagged judgment, mixed validation failures, wordlist baseline,
  admin auth and input validation, idempotent hiding of created/completed/failed debates,
  content retention and secret exclusion, every hidden public path, active speech/judgment
  cancellation, a cancellation-resistant late response, hide between reservation and dispatch,
  final dispatch/hide lock serialization, simultaneous Start/hide, and hide transaction rollback.
- `npm test -- --pool=threads --maxWorkers=1`: **23 passed**, 5 files, 7.75s.
  Includes the new lobby explanation of profanity-filter limitations. `npm run build`:
  TypeScript and Vite build succeeded (36 modules).
- Required offline CLI completed six persisted speeches and judging: Pro 80, Con 75, DRAW.
  `uv run alembic check`: `No new upgrade operations detected.` `git diff --check`: clean.
- Initial combined verification encountered a prolonged machine/test-worker pause: Vitest
  fork workers timed out without running tests, and a wall-clock minute-window test expired
  during the pause (181 backend tests passed, 1 failed). Retried Vitest with a single thread
  worker; gave the two rate-window tests a controlled library clock and explicit expiry checks.
  The final full backend run and frontend thread run above passed. No product rate-limit
  behavior was changed for this test-timing fix.
- Exit criterion met. Remaining: none within Phase 7. Next: Phase 8 after sign-off.
  No real Groq calls, deployment, commits, or pushes were performed; Phases 6–7 are uncommitted.

Implementation handoff:
- app/engine/screening.py owns one offline default Profanity instance. Validation checks each
  speech and pro_strengths/pro_weaknesses/con_strengths/con_weaknesses/overall_explanation
  before scoring or committing. Text is rejected, never censored and saved. Errors and retry
  instructions do not echo offending terms. AI text still renders as plain text.
- ValidationFailure carries a code; RetryPolicy uses its existing three dispatches and
  durable reservations. Content flags get a specific corrective instruction; exhaustion while
  the last failure is a flag persists `content_flagged`. Prior committed speeches remain
  readable, and eligible creators can Resume manually. A later non-content failure keeps
  its own failure category instead of reusing an earlier flag.
- False-positive fixtures: all 22 seeded topic/default-output/benign edge samples pass;
  the intentionally recorded legitimate phrase "Sex education should be age appropriate."
  is flagged by the default list. This limited baseline is not a general accuracy estimate.
  No allowlist changes were made. Lobby copy explicitly says the profanity filter can miss
  harmful content and flag legitimate wording; it is not comprehensive moderation.
- `POST /api/admin/debates/{id}/hide`, X-Admin-Token required, accepts no body or optional
  `{reason: string}` (nonblank, at most 500 characters). First hide stores the reason/time
  and increments revision; repeated hides preserve that metadata and do not bump revision.
  Completed/failed/created status and saved content remain intact when not active.
- Active hide atomically sets hidden, changes status to failed with `operator_hidden`,
  clears run ownership/progress, finishes the active attempt as failed, and marks outstanding
  reservations abandoned without refunds. Only after commit does it cancel the registered
  worker. A failed hide transaction leaves visibility/ownership and the live worker unchanged.
- Final dispatch authorization/task creation shares the manager's hide/admission lock;
  the lock and database transaction are released before awaiting the provider. Cancellation
  propagates to the provider child task. Late responses can record already-spent usage but
  cannot reopen abandoned reservations or commit speech/judgment after ownership is revoked.
  Hiding cannot undo a provider call already in flight.
- `GET /api/admin/debates/{id}`, X-Admin-Token required, returns the full transcript/judgment,
  moderation metadata and attempt/call counts even when hidden; no execution token/hash or
  client identity. Admin endpoints remain no-store and share the separate admin rate bucket.
  Public detail/run/replay return 404 for hidden debates; history omits them.
- Local preview backend was refreshed on 127.0.0.1:8000 with FakeProvider and the existing
  ignored phase4-browser.db after confirming no active debates. Frontend remains at
  127.0.0.1:5173. Admin operations require an explicitly configured process ADMIN_TOKEN;
  the preview does not generate or expose one. Paid-activation gates from Phase 6 remain.

## Phase 8 — Backup/restore [pending]

Goal: verified backups and a proven restore. Blueprint §16. Est. 8–10h.

Tasks:
- [ ] `app/ops/backup.py` + `apscheduler`: nightly 03:00 UTC in-process job. Use
  `sqlite3.Connection.backup()` (never copy the live file with WAL active), off the event loop,
  bounded deadline, no overlapping runs, write to a temp file, `PRAGMA integrity_check` +
  schema/representative-row check, atomic rename to timestamped name, THEN prune verified
  backups older than 7 days — always keep the newest verified-good copy. One catch-up attempt
  if the schedule was missed; never replay every missed run.
- [ ] Log WARN when the DB exceeds 500 MB, free disk is low, WAL grows large, or a backup fails.
- [ ] `app/ops/prune_old_debates.py --older-than-days N [--dry-run]`: MANUAL ONLY, dry-run
  first, lists exact affected rows, excludes active work, keeps quota/reservation evidence for
  its window. Do not schedule it.
- [ ] Restore test into an isolated DB: verify integrity, transcript/judgment rows, and that
  `GET` works against it. Document: after a restore, pause paid work until the cost ledger is
  reconciled (older reservation rows undercount spend).
- [ ] Tests: backup success/failure/full-disk/cleanup paths; last good copy always survives.

Exit criterion: a forced backup restores cleanly into isolation.

## Phase 9 — Deploy and polish [pending]

Goal: Checkpoint B on a real host. Blueprint §7, §17, §18. Est. 6–10h.

Tasks:
- [ ] Fly.io: `fly launch` (no deploy), `fly volumes create botarena_data --size 1`, mount at
  `/data`, `DATABASE_PATH=/data/botarena.db`, exactly one machine/no autoscale,
  `fly secrets set` for `GROQ_API_KEY`, `GROQ_MODEL`, `ADMIN_TOKEN`, `RATE_LIMIT_HMAC_SECRET`.
  Run migrations on release. CORS restricted to the real frontend origin. (Ask the user before
  creating any paid/cloud resource.)
- [ ] Verify attribution with two genuinely different clients; verify restart mid-speech and
  mid-judging then resume; run a backup and restore it; hit `/api/admin/stats`.
- [ ] **Size `CREATE_QUOTA_PER_IP_24H` to the eval group's shared networks before sending the
  link** (blueprint §12).
- [ ] ≥5 varied real debates reviewed for rebuttal grounding, repetition, side flips, judge
  consistency; decide on `gpt-oss-120b` vs `20b`; tune.
- [ ] Accessibility pass (keyboard, contrast, reduced motion, screen-reader announcements).
- [ ] Write `docs/DEPLOYMENT.md` and `docs/RUNBOOK.md` (backup/restore, hide procedure, secret
  rotation, quota tuning, post-restore spend reconciliation) and a real root `README.md`.

Exit criterion: blueprint §17 checklist fully checked. **Checkpoint B reached here.**

## Decisions and deviations log

- Used `uv` (already installed) instead of plain `pip`/`venv` for the backend — faster,
  and `[tool.uv] package = false` avoids needing a `src/` layout for what is an application,
  not a distributable library. Not in the blueprint; doesn't change any behavior it specifies.
- Judge output: Groq strict `json_schema` mode (structure) + Pydantic validation (ranges,
  references). Originally planned prompt-only; changed after a real `json_object` derailment
  once the model was chosen (see Phase 1 findings).
- Model: `openai/gpt-oss-20b` (free tier). Move to `openai/gpt-oss-120b` if debate quality or
  judge consistency disappoints. Blueprint's Llama suggestion is obsolete — it's off Groq's list.
- `GroqProvider` and the `Provider` protocol take `json_schema`, not a boolean JSON-mode flag.
- Seeded only 3 topics in `topics.json` for engine-proof testing, not the full 8–12 — blueprint
  §1 already treats the curated count as "adjust after testing," so the full set is a content
  task for later, not a Phase 1 blocker.

- Phase 2 (2026-10-11): SQLAlchemy 2.1.4, aiosqlite 0.22.1, Alembic 1.20.0 installed
  using `uv add`; initial schema revision `b478c86b9ed8`. SQLite UUIDs are allocated by the
  engine before commit; only committed speeches enter subsequent prompts. In-memory runs
  retain `m1`–`m6` IDs for CLI compatibility.
- Phase 2: disabled SDK-internal retries now (ahead of Phase 6 wiring) because the three-call
  stage allowance must count every dispatch. Cost reservation tables exist but remain unused
  until Phase 6; creation quota tables remain unused until Phase 5.
- Phase 2: enum values are enforced with SQLite CHECK constraints. CreationQuotaRecord's
  `ip_bucket_hash` is nullable to support the already-planned seven-day association cleanup.
- Phase 2: configuration/auth failures stop immediately, but permit a manual resume after
  configuration is fixed within the existing attempt allowance. Corrupt checkpoints are
  permanently non-resumable. Failure text stored in SQLite is fixed and sanitized.
- Phase 3 (2026-10-11): installed FastAPI 0.143.0 and Uvicorn 0.54.0; httpx 0.28.1
  explicitly declared as a dev dependency. Kept execution lifecycle in engine/lifecycle.py
  so the pure orchestrator remains usable without HTTP/task-registry dependencies.
- Phase 3: specified token wire encoding as 64 hex characters (32 random bytes), since the
  blueprint specifies entropy but not serialization. Hash decoded bytes; accept either hex case.
- Phase 3: idempotent create uses a short BEGIN IMMEDIATE transaction before lookup to serialize
  competing replays. Snapshot multi-reads use explicit BEGIN for a consistent SQLite revision.
- Phase 3: global capacity is part of the conditional database claim, with a process lock
  spanning admission/task registration to keep the supervisor from mistaking a fresh claim for
  an orphan. Attempts rejected by capacity or eligibility do not increment counters.
- Phase 3: API defaults to FakeProvider and the supported runner binds only 127.0.0.1 with one
  worker. Shared mode fails closed until the later guard phases, rather than exposing the
  unfinished local checkpoint. Full shared secret/quota validation remains Phase 5.
- Phase 4 (2026-10-11): user requested a basic design reference before their own custom UI.
  Used React 19.3, Router 7.18, Vite 8.3, Tailwind 4.3, TypeScript 7, Vitest 4.1 and RTL.
  Registry access returned HTTP 403; installed compatible packages from the existing npm
  cache offline, with explicit cached transitive overrides recorded in package.json and the
  lockfile. Revisit those overrides when registry access is restored. Windows sandbox blocked
  Python access and Vitest temporary-file renames; the same checks passed outside the sandbox.
- Phase 5 (2026-10-12): guards are shared-mode only; retained a hard HTTP execution gate
  until Phase 6 so valid shared configuration cannot bypass missing spending reservations.
  Selected 10/min for the separate admin bucket. Added explicit trusted-peer CIDRs and
  disabled Uvicorn forwarding rewrites so client headers cannot change the trusted socket
  identity. Weekly privacy cleanup uses an application-owned asyncio task; APScheduler remains
  Phase 8 backup scope. The npm registry issue was confirmed by the user to be their original
  network connection; switching connections restored access. Frontend dependency changes
  were left out of this backend phase.
- Phase 6 (2026-10-12): committed Phases 3–5 as e27cbfe before implementing reservations.
  Chose explicit fail-closed input-accounting confirmation because the provider's strict
  JSON/chat framing is undocumented; the byte-based candidate bound is not represented as
  verified tokenization. Paid activation/real usage verification is still operator work,
  while all reservation behavior is tested offline. Kept conservative whole-cent accounting
  as required by the existing schema, exposed its rounding/billing limits in admin stats,
  and moved the CLI onto the same persistent budget path. No new dependencies or schema
  migration were needed. No push, real Groq run, or deployment was performed.
- Phase 7 (2026-10-12): used better-profanity's default list without exceptions and recorded
  its known benign "sex education" false positive. Added the planned operator hide/inspection
  APIs, preserving content and cost evidence. Closed the authorization-to-dispatch hide race
  with the existing manager lock and a cancellable provider child task, without holding a DB
  transaction or process lock while waiting on the provider. Optional side-consistency checks
  stay deferred to the user-reviewed Phase 9 quality work. No new schema revision or admin UI.
