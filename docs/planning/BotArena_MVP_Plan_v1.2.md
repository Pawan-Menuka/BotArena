# BotArena — Revised MVP Implementation Plan

**Version:** 1.2 · **Date:** October 11, 2026 · **Status:** Proposed plan

This document replaces v1.1 for implementation purposes. It keeps the two-debater, three-round product and v1.1's interruption/recovery/resume/history design, and closes the gaps found reviewing v1.1: abuse and cost exposure from having no accounts, content safety for a shared public archive, data lifecycle for the SQLite store, and a few smaller recovery/observability gaps. Decisions below are proposed defaults, not results of performance or user testing. No application has been built by preparing this plan.

## Changelog from v1.1

| Gap in v1.1 | Fix in v1.2 |
|---|---|
| Only global concurrency/budget caps; one visitor could occupy both active-debate slots | Per-client (hashed-IP) throttle: 1 active debate at a time, 5 creations/24h, plus a global daily creation cap |
| Unlimited `created`-but-never-run debates could bloat the DB | Creation itself is rate-limited (same per-client + global caps apply at `POST /api/debates`, not just at `run`) |
| No global spend ceiling beyond per-debate call budgets | Global daily debate-creation cap bounds worst-case total spend independent of per-debate limits |
| No moderation on AI-generated speech shown in a shared public archive | Lexical filter on every committed speech/explanation, reusing the existing invalid-output retry budget; admin hide endpoint for after-the-fact review |
| No test for AI-generated content escaping as HTML/script | Explicit XSS verification case added; content render path specified as text-only, no markdown/HTML interpretation |
| No retention or backup plan for the single SQLite file | Nightly automated backup (safe, scheduled); pruning is a documented manual script, not automatic (avoids destructive automation during the eval window) |
| SQLite single-writer contention under the plan's own 2-concurrent-debate allowance wasn't addressed | Explicit `WAL` journal mode + `busy_timeout` pragma, set at every connection open |
| Client request UUID only lived in memory; lost on reload mid-creation | Persisted to `localStorage` until the create response is confirmed, so a crash/reload before response fallbacks to idempotent retry |
| No observability for a solo operator in production | Minimal admin-token-gated `/api/admin/stats` endpoint; DB file size logged on a threshold |
| No frontend crash containment | Root-level React error boundary with a reload action |
| 35–55 hour estimate didn't include any of the above | Revised to 45–70 hours |

## 1. Product and completion goal

BotArena lets a user choose a curated debate topic, watch Atlas argue Pro and Nova argue Con across three rounds, and review an explainable AI verdict. The backend controls all execution and calculates the final outcome from validated criterion scores.

A successful match contains exactly six ordered speeches and one judgment. A failed match retains every committed speech and explains the available recovery action. Users can leave the arena, refresh, and revisit saved matches.

The experience should be coherent, entertaining, readable on mobile, and honest about AI limitations. The verdict assesses the arguments presented; it does not establish factual truth or independently verify claims.

## 2. Scope

### Required for this MVP

- Curated topic selection; initially 8–12 topics, adjustable after testing.
- Atlas/Pro and Nova/Con with distinct styles, fixed positions, and fixed speaking order.
- Three rounds: opening, rebuttal, closing.
- Progressive display of completed speeches and explicit current-speaker status.
- Structured judgment, validated scores, backend totals, and a deterministic draw rule.
- Persistent sessions, transcript, judgment, and execution attempts.
- Refresh/reconnect recovery and bounded manual resume of failed sessions.
- A small Recent Battles page so users can find interrupted and completed sessions.
- Responsive, accessible lobby, arena, results, and failure states.
- Duplicate-run prevention, timeouts, bounded retries, usage limits, and relevant automated tests.
- **(v1.2)** Per-client and global anti-abuse limits; a lexical content filter on generated text; automated non-destructive backups.

### Defer until this works

Animated transcript replay, export, custom topics, alternative personalities, human participation, voice, tournaments, accounts, leaderboards, multiplayer, retrieval, long-term agent memory, CAPTCHA/bot challenges, and a public "report content" button.

Recent Battles moves into the MVP because recovery requires a discoverable way back to a saved session. Replay remains optional: reading a stored transcript is sufficient.

CAPTCHA is explicitly deferred: the per-client and global rate limits in §10 are judged sufficient to protect a small shared demo, and a bot-challenge dependency isn't worth adding until real abuse is observed. Revisit if the eval period shows scripted abuse the rate limits don't catch.

## 3. Key decisions that close the original gaps

| Gap | Decision |
|---|---|
| Failed speech | Preserve earlier speeches; a manual Resume continues at the first missing turn. |
| Failed judge | Preserve all six speeches; Resume retries judgment only. |
| Refresh or route navigation | Fetch the saved snapshot; never start execution automatically on page mount. |
| Closing the browser | An already accepted server task continues while the backend process remains alive. |
| Server restart/crash | Before accepting requests, mark unfinished active attempts as interrupted failures. Offer manual resume. |
| Active speaker visibility | Persist current phase, speaker, turn, and retry state before calling the model. |
| History access | Add a paginated debate-list endpoint and Recent Battles page. |
| Duplicate Start/Resume | Atomically claim execution; at most one active attempt per debate. |
| Ambiguous network response | Fetch current state before retrying; preserve the creation request identifier. |
| Runaway usage (per-debate) | Bound active debates, automatic retries, manual resumes, and total model-call attempts. |
| **(v1.2)** Runaway usage (cross-debate / multi-client) | Per-client-fingerprint creation and concurrency limits, plus a global daily creation cap, enforced at creation time, not just at run time. |
| **(v1.2)** Unsafe generated content in a public archive | Lexical filter on every committed speech/explanation; failed filter reuses the existing invalid-output retry budget; admin hide endpoint for post-hoc moderation. |
| **(v1.2)** Unbounded SQLite growth / no backup | Nightly automated backup; documented manual pruning script; DB-size threshold warning in logs. |
| **(v1.2)** SQLite write contention at the plan's own concurrency ceiling | `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` on every connection. |
| **(v1.2)** Lost client request UUID on reload mid-creation | Persist the pending creation (UUID + topic + timestamp) to `localStorage`; retry the same idempotent create on next mount if unresolved. |
| **(v1.2)** No production visibility for a solo operator | Admin-token-gated stats endpoint; DB size logging. |
| **(v1.2)** Unhandled frontend render crash | Root-level error boundary with a reload action. |

**Architecture change:** use a short POST to request execution, followed by GET polling for progress. This replaces the original POST-held-open stream. It still meets the requirement that completed speeches appear progressively. SSE can be added later without changing execution ownership.

## 4. User journeys

### A. Start a new battle

1. Lobby fetches curated topics. Show a loading state, an empty state, or a retryable loading error as appropriate.
2. User selects a topic. Start Battle is disabled until selection is valid.
3. On click, disable duplicate submission, generate a client request UUID, and **(v1.2)** write `{client_request_id, topic_id, created_at}` to `localStorage['botarena.pending_creation']` before sending the request.
4. Create the debate using that UUID. Reuse it if the create response is lost.
5. **(v1.2)** On any definitive response (success or a non-retryable failure), clear the pending-creation key. On next lobby mount, if the key exists and is under 5 minutes old, silently retry the same idempotent create before showing the lobby, so a crash or reload during the original request recovers instead of silently dropping the attempt.
6. On successful creation, open `/debates/{id}` and request execution once.
7. The server accepts execution and returns `202` with a snapshot.
8. The arena begins polling and shows the current speaker and saved transcript.

If creation fails definitively, stay in the lobby. If creation succeeded but execution could not start, show the saved `created` debate with a Start Battle action. Do not show it as running before the server accepts execution. **(v1.2)** If creation is rejected by a rate limit (see §10), show the specific limit message (per-client or global) rather than a generic error, so the user understands it isn't a bug.

### B. Follow the debate

| Turn | Round | Speaker | Objective |
|---:|---|---|---|
| 1 | Opening | Atlas | Establish the Pro case. |
| 2 | Opening | Nova | Establish the Con case. |
| 3 | Rebuttal | Atlas | Address Nova's actual opening claims. |
| 4 | Rebuttal | Nova | Address Atlas's arguments and rebuttal. |
| 5 | Closing | Atlas | Summarize Pro and unresolved objections. |
| 6 | Closing | Nova | Summarize Con and unresolved objections. |

Each turn receives all previously saved speeches with stable IDs. Atlas's opening has no opponent context. Closings should not introduce major new claims.

The arena shows the topic, fixed sides, round, active speaker, completed speech count, and ordered transcript. If a provider request is retrying, show a simple retry status. Preserve the user's reading position; offer a Jump to Latest action instead of forcing scrolling.

### C. Judge and view results

After turn 6 is committed, show Judging. The judge receives the full saved transcript and returns scores and explanations with transcript references. The backend validates the result and calculates totals and outcome. Only a committed, completed judgment may appear as a result.

Results show winner/draw, both totals, four criterion scores, strengths and weaknesses, transcript references, the complete transcript, New Battle, and Recent Battles. Label the verdict as an AI assessment.

### D. Leave, refresh, or reconnect

- Navigating away or closing the browser does not cancel an accepted task.
- Returning to the debate first fetches its snapshot. Route according to the stored status.
- `created`: show the Start action.
- `running` or `judging`: show saved progress and resume polling.
- `completed`: show results.
- `failed`: show saved transcript, failure explanation, and recovery actions.
- `404`: show Battle Not Found with Lobby and Recent Battles links.
- **(v1.2)** `hidden` (moderated): show Battle Not Found, identically to `404`. Do not reveal that content was moderated; this is only reachable by someone who already had the link.

A browser connection error is not proof the debate failed. Keep the transcript visible, show Connection Lost, and retry reading state. Never submit another run simply because polling failed.

### E. Resume a failed match

1. User opens the failure view and clicks Resume if allowed.
2. Server atomically claims a new attempt.
3. Load and verify the saved transcript: turn positions must form an uninterrupted prefix of 1–6 with the expected speakers and rounds.
4. With 0–5 saved speeches, continue at the next missing turn using the committed transcript.
5. With all 6 speeches and no judgment, enter judging directly.
6. Preserve existing speech content and IDs. Do not regenerate saved turns.
7. If another attempt fails, retain the same checkpoint and updated failure information.

Allow at most **three accepted execution attempts per debate**: the initial run plus two manual resumes. Display remaining resumes. The total model-call budget also applies. Corrupt stored state is not resumable; offer a new battle and log the internal problem.

Manual resume may produce a different continuation because AI generation is nondeterministic. It preserves the earlier committed debate, not an unsaved provider response.

### F. Revisit saved battles

Recent Battles lists topic, status, saved speech count, creation time, and outcome if completed. Order newest first; fetch up to 20 per page with a stable cursor. **(v1.2)** Debates flagged `hidden` are excluded from this listing.

Selecting a row loads that debate's saved state. Reading a match never triggers AI calls. Empty history provides a Start Your First Battle action. Failed and active matches remain discoverable.

## 5. State and execution model

```text
created -> running           (Start accepted)
running -> judging           (Sixth speech committed)
running -> failed            (Error / interruption)
judging -> completed         (Valid judgment committed)
judging -> failed            (Error / interruption)
failed  -> running           (Resume with fewer than six speeches)
failed  -> judging           (Resume with six speeches)
completed -> (terminal)
```

Resume transitions require a valid checkpoint, available attempt/call budgets, and a recoverable failure. There is no transition from completed back to running. New Battle creates a new session.

`status` describes the debate lifecycle. A separate `activity` field describes `generating`, `retrying`, or `evaluating`. Before each provider call, commit the current speaker/turn or judging phase so clients can show what is happening.

**(v1.2)** `hidden` is a boolean flag orthogonal to `status`, not a new lifecycle state — a `completed` debate can also be `hidden`. It only affects listing/public-GET visibility (§4D, §4F), never the state machine above.

### Execution ownership

- Deploy **one backend process, one worker, one instance** for the MVP.
- Use an application-owned asynchronous task registry. Keep strong references to tasks and retrieve their exceptions.
- A run request validates and claims the debate, registers its task, and returns promptly. Task creation failure releases the claim or records a recoverable failure.
- Tasks use their own database sessions and provider timeouts. They do not depend on the request's database session or browser connection.
- Do not hold a database transaction while waiting for model output.
- Limit simultaneous active debates to two initially. When full, reject new starts/resumes with a temporary capacity error; keep their existing state and budgets unchanged. There is no queue in v1.2.
- Once accepted, execution may finish even when nobody is watching. Explain this beside the start action: leaving the page does not stop the battle.

### Duplicate execution and stale writes

The execution claim must be atomic, not a separate unchecked read followed by an update. Assign every accepted attempt a new `run_id`. Each worker mutation verifies that its `run_id` still owns the active debate. Serialize admission and registration within this single process; use database conditions and constraints as the final guard.

An already active or completed debate rejects a run request with `409`. Include a machine-readable reason and current status so the UI can load existing progress. No duplicate request may create a second worker or consume another accepted-attempt slot.

Database uniqueness prevents duplicate turn positions and judgments. This provides one saved record per logical turn; it does **not** guarantee exactly one provider charge if a process dies after generation but before saving.

### Atomic checkpoints

- Commit a speech, its progress update, and a new revision together.
- Commit judgment, computed scores/outcome, completion timestamp, final attempt status, and debate completion together.
- Commit failure status, sanitized error, failed stage, attempt completion, and revision together.
- Only committed data is returned as completed progress.

## 6. Interruption and recovery

### Browser interruption

No server state change is required. On reconnection, GET returns the current snapshot. If the backend remained alive, execution continues independently of the viewer.

### Backend startup

Before the service becomes ready, reconcile all `running` and `judging` sessions from the previous process: mark their active attempts interrupted and their debates failed with `server_interrupted`. Preserve committed speeches and call reservations. Offer manual resume where budgets allow.

Do not automatically restart paid AI work at startup. This keeps recovery visible and bounded. The startup rule depends on the explicit single-process deployment; multiple instances require a different ownership/lease design.

### Graceful shutdown and unexpected task exit

Stop accepting new executions, allow active tasks a short grace period, then cancel remaining work. Where the database is available, persist interruption failures. Re-raise task cancellation after cleanup. Startup reconciliation covers crashes that prevent cleanup.

Wrap workers with exception handling and a final ownership check. A supervisor periodically detects an active debate with no corresponding live task and marks it interrupted. Enforce an execution deadline so a hung request cannot leave a match permanently active.

### Storage outage

Stop scheduling further provider calls if checkpoints cannot be persisted. Log the failure without secrets. If the database is unavailable, the service cannot truthfully promise a saved failure state; report service unavailability and reconcile active records when storage is restored or on restart. Do not display an unsaved speech or verdict as committed.

## 7. Progress delivery contract

Poll `GET /api/debates/{id}` approximately every 1.5 seconds while the arena is visible and status is active. Wait for one request to finish before issuing another.

- Pause polling when the browser tab is hidden; fetch immediately when it becomes visible.
- On connection errors, retry with increasing delays up to 15 seconds; keep saved content visible.
- Stop automatic polling for completed or failed matches.
- Include a monotonically increasing `revision`. Ignore a response older than the last displayed revision.
- Deduplicate speeches by their stable message IDs and sort by `turn_order`.
- Do not cache state responses in a way that serves stale progress; send explicit `Cache-Control: no-store` on all `/api/debates*` responses.

Snapshot fields include:

```json
{
  "id": "debate-uuid",
  "status": "running",
  "revision": 8,
  "progress": {
    "phase": "rebuttal",
    "current_round": 2,
    "active_speaker": "atlas",
    "active_turn": 3,
    "completed_turns": 2,
    "activity": "generating"
  },
  "execution": {
    "accepted_attempts": 1,
    "remaining_resumes": 2,
    "can_resume": false
  },
  "messages": [],
  "judgment": null,
  "error": null
}
```

The example omits topic and timestamp fields and leaves `messages` empty only for brevity; actual snapshots contain every committed message. `active_speaker` and `active_turn` are null during judging and terminal states. `current_round` is 0 before starting and 3 during judging/completion. A failed match also identifies the failed stage/turn.

No event log, SSE parser, or reconnecting stream is required for this version. Polling trades a small display delay and additional reads for a simpler recovery contract. If later needed, GET SSE can announce changed revisions while snapshots remain the source of truth.

## 8. AI behavior and judging

### Debaters

Atlas is analytical and structured; Nova is skeptical and probing. Both must stay on their assigned sides, address actual opponent arguments, avoid fabricated citations and personal attacks, and follow round-specific goals.

Treat transcript content as quoted, untrusted debate material, not instructions that can override the role or judge rubric. **(v1.2)** Render generated content as plain text only — no markdown interpretation, no `dangerouslySetInnerHTML` or equivalent, no HTML parsing of model output anywhere in the pipeline. This is the primary XSS defense; see §18 for the corresponding test.

Start with a target of 120–180 words per speech and a hard application limit of 250 words. Use a provider output-token cap as an additional control. Reject empty or excessive output rather than silently truncating meaningful content. Tune these defaults after sample matches.

### Content filter (v1.2)

Curated topics limit but don't eliminate the risk of a debater generating something inappropriate for a page anyone with the link can view (§12's "shared archive" decision). Before committing a speech or a judge explanation:

- Run it through a lexical filter (`better-profanity==0.7.0` or an equivalent static wordlist check — pinned, offline, no extra API call or latency budget beyond a string scan).
- A flagged speech is treated exactly like invalid generated output (§9): a corrective retry within the same three-call stage budget, not a new budget category.
- If the stage budget is exhausted while still flagged, fail that stage with a new sanitized error code `content_flagged`. It is resumable like any other speech failure — a manual resume re-generates and may pass.
- This is a cheap baseline, not a guarantee. An LLM-based moderation pass was considered and rejected for v1.2: it would roughly double the provider calls (and therefore cost and the 30-call-per-debate budget headroom) for a risk that curated, vetted topics make low-probability. Revisit if real-model review (§18) surfaces content problems the lexical filter misses.
- There is no public "report" button in v1.2 (deferred per §2). An admin can hide a debate directly via `POST /api/admin/debates/{id}/hide` (§14) after manual review; there is no in-app moderation queue.

### Judge

Use neutral side labels such as Side A/Pro and Side B/Con in the judge prompt; map them back to Atlas and Nova in the backend. This reduces name/personality cues but does not establish unbiased judging.

| Criterion | Maximum |
|---|---:|
| Logical reasoning | 35 |
| Relevance | 25 |
| Rebuttal quality | 25 |
| Clarity | 15 |
| Total | 100 |

Require integer scores for exactly these criteria, per-side strengths and weaknesses, and a short overall explanation. Validate that any cited message IDs exist. Ask the judge for criterion scores and explanations only; calculate totals and winner in application code. The judge's free-text explanations pass through the same content filter as debater speeches before being committed.

- Absolute difference 0–5: draw.
- Difference 6 or more: higher total wins.
- Invalid output never becomes a visible result.

Check judgment consistency manually with several topics and neutral-label variations. Do not reorder the transcript arbitrarily: that would change what rebuttals appear to answer. Evaluate speaking-order bias in separate experimental matches while retaining Atlas-first behavior in the MVP.

## 9. Failure, retry, and usage policy

Proposed initial configuration values:

| Setting | Default |
|---|---:|
| Provider request deadline | 35 seconds per call |
| Maximum calls per stage | 3 total, including the first call |
| Automatic retry backoff | Approximately 1 second, then 3 seconds, with jitter |
| Stage deadline | 120 seconds across calls and waits |
| Execution-attempt deadline | 15 minutes |
| Accepted execution attempts per debate | 3 |
| Maximum provider-call attempts per debate | 30 across all executions |
| Maximum active debates | 2 |

These are tunable safeguards, not promised completion times. Reserve and persist a model-call count immediately before every provider call. A reserved call remains counted after a crash, conservatively bounding spending. Disable hidden SDK retries or include them in the same budget.

Retry timeouts, temporary provider failures, and rate-limit errors only within all budgets. Respect provider retry delays when they fit the remaining stage deadline; otherwise stop and expose a retryable failure. Invalid generated output (including a content-filter flag, §8) may receive a corrective retry within the same three-call stage limit. Never restart successful earlier turns for an automatic retry.

Do not automatically retry bad credentials, invalid configuration, unavailable configured models, corrupt checkpoints, or exhausted budgets. Resume eligibility is computed by the server from the latest failure and budgets. For configuration failures, require the server configuration to be corrected before allowing another attempt.

| Failure | User message/action |
|---|---|
| Topic list unavailable | Retry loading topics. |
| Unknown topic | Choose an available topic. |
| Create response lost | Retry with the same creation request UUID (now recoverable across reload — see §4A). |
| Run response lost | Fetch snapshot; retry only if still created or eligible failed. |
| Temporary capacity full | Keep session; offer Try Again later. |
| Browser offline | Preserve displayed transcript; reconnect without rerunning. |
| Speech failed | Show saved transcript and Resume if eligible. |
| Judge failed | Explain that speeches are saved; Resume judges only. |
| Server interrupted | Explain interruption and offer Resume if eligible. |
| Budget exhausted | Explain limit; offer a new battle subject to global limits. |
| Configuration unavailable | Explain temporary service issue; offer navigation and status refresh. |
| **(v1.2)** Content flagged | Explain that this stage needs a retry; Resume if eligible (same as Speech/Judge failed). |
| **(v1.2)** Rate limit exceeded (per-client or global) | Explain the specific limit (not a generic error) and when it resets; no action offered beyond waiting. |

Return sanitized errors with `code`, `message`, `failed_stage`, and `can_resume`. Keep internal exception details and credentials out of API responses. Rate and spend controls must apply across newly created debates too — this is now enforced structurally, not just stated as a principle: see §10.

## 10. Anti-abuse and rate limiting (v1.2)

This section exists because v1.1 only bounded cost *within* a single debate (§9's per-debate call/attempt budgets) and left creation itself, and cross-debate behavior from one visitor, unbounded. With no accounts (§12), that's the only lever available before launch.

### Client identification

No accounts exist, so clients are identified by a **hashed fingerprint**, not a stored IP: `sha256(request_ip + daily_salt)`, truncated to 16 bytes, stored on the `Debate` row as `creator_fingerprint`. The daily salt (regenerated at midnight UTC, kept only in process memory) means the same visitor's fingerprint changes day to day and old fingerprints can't be correlated — this is a deliberate privacy choice: enough to throttle abuse within a rolling window, not enough to build a persistent per-visitor profile. Known limitation: visitors sharing a NAT/IP share a quota; acceptable for a small demo, called out explicitly rather than silently accepted.

### Limits enforced at `POST /api/debates` (creation)

| Scope | Limit | Mechanism |
|---|---|---|
| Per fingerprint | 5 debate creations per rolling 24h | DB query: `COUNT(*) WHERE creator_fingerprint = ? AND created_at > now() - 24h` |
| Per fingerprint | 1 debate in `running`/`judging` at a time | DB query against current debates for that fingerprint |
| Global | 100 debate creations per rolling 24h (tunable) | DB query: `COUNT(*) WHERE created_at > now() - 24h` |

The per-fingerprint checks are also enforced at `POST /api/debates/{id}/run` for the concurrency limit (a fingerprint can't create one debate then run many), but the creation-count and global caps are checked once, at creation, since that's where DB rows and provider-call exposure actually originate.

A fast, non-authoritative per-IP request-rate guard (`slowapi==0.1.9`, in-memory, no Redis — fits the single-process deployment already decided in §12) sits in front of these as a cheap first filter against raw request flooding (e.g., 20 requests/minute/IP across all endpoints). It resets on process restart; that's acceptable because the DB-backed checks above are the actual source of truth and survive restarts.

Exceeding any limit returns `429` with a machine-readable `code` (`client_creation_limit`, `client_concurrency_limit`, `global_creation_limit`) and, where applicable, a `retry_after` hint. These map to the "Rate limit exceeded" row in §9's failure table.

### Why not CAPTCHA

Deferred per §2. These limits bound worst-case exposure (at most 100 × 30 = 3,000 provider calls/day globally, regardless of how creation is automated) without adding a third-party dependency or a UX hurdle during the 5–10 person eval. Revisit only if observed abuse gets past the rate limits, not preemptively.

## 11. API contract

| Method | Endpoint | Behavior |
|---|---|---|
| GET | `/api/topics` | Curated topic catalogue. |
| POST | `/api/debates` | Create or retrieve a session for a repeated creation request UUID. Subject to §10 limits. |
| POST | `/api/debates/{id}/run` | Start a created session or explicitly resume an eligible failed session; return promptly. Subject to §10's per-fingerprint concurrency limit. |
| GET | `/api/debates/{id}` | Current committed snapshot, messages, and judgment. Returns `404` for a `hidden` debate (§4D). |
| GET | `/api/debates?limit=20&cursor=...` | Recent debate summaries with stable pagination. Excludes `hidden` debates. |
| GET | `/api/health` | Minimal readiness response; no secrets/configuration dump. |
| **(v1.2)** GET | `/api/admin/stats` | Active debate count, today's creation counts (per-fingerprint top offenders by count only, no raw IPs), call-budget usage, DB file size. Requires `X-Admin-Token` header matching the `ADMIN_TOKEN` env var; `401` otherwise. |
| **(v1.2)** POST | `/api/admin/debates/{id}/hide` | Sets `hidden = true`. Same admin-token guard. |

Create request:

```json
{
  "topic_id": "ai-replace-teachers",
  "client_request_id": "client-generated-uuid"
}
```

Creation returns `201` for new sessions, `200` for an identical repeated request, `409` if the same UUID is reused for different input, and `429` if a §10 limit is exceeded. Validate topic IDs against the server catalogue; do not accept arbitrary topic text from this API.

Run returns `202` once the execution is claimed and registered. Use `409` for active/completed sessions or ineligible lifecycle transitions, `404` for missing IDs, `422` for invalid input, `429` for request/rate limits, and `503` for temporary service/capacity unavailability. Provide retry guidance where meaningful.

The browser's disabled button is a UX convenience; server-side checks remain authoritative. GET requests never start or resume a debate.

## 12. Persistence

### Debate

`id`, unique `client_request_id`, topic ID and topic snapshot, status, revision, current round, active turn/speaker, activity, accepted-attempt count, model-call count, active run ID, creation/update/completion timestamps, sanitized error code/message, failed stage/turn, **(v1.2)** `creator_fingerprint` (hashed, see §10), **(v1.2)** `hidden` (boolean, default false).

### DebateMessage

`id`, debate ID, agent, side, round number, turn order, content, timestamp. Unique `(debate_id, turn_order)`. Successful debates have positions 1–6 exactly once.

### Judgment

Unique debate ID, criterion scores, backend totals/winner, per-side explanations, overall reasoning, referenced message IDs, prompt/rubric version, timestamp. One judgment per debate.

### ExecutionAttempt

`run_id`, debate ID, attempt number, status (`active`, `completed`, `failed`, `interrupted`), start/finish times, starting checkpoint, failed stage/turn, provider/model identifiers, prompt version, call count, available token usage, and sanitized failure code (now including `content_flagged`, §8).

Use UTC in storage and local presentation in the browser. SQLite must live on persistent storage. Use versioned migrations from the beginning; do not recreate the database when deploying changes. Index debate listing and transcript retrieval fields, plus **(v1.2)** `(creator_fingerprint, created_at)` and `(created_at)` to make the §10 limit checks cheap.

**(v1.2)** On every connection open, set `PRAGMA journal_mode=WAL;` and `PRAGMA busy_timeout=5000;`. The plan already allows two simultaneous active debates (§5), each committing checkpoints frequently (§5 "Atomic checkpoints"); SQLite's default rollback-journal mode serializes writers with no wait, which would surface as spurious failures under exactly the concurrency this plan permits. WAL plus a busy timeout lets a second writer wait briefly instead of failing immediately.

The database and logs should provide enough information to answer: which turn failed, how many attempts/calls ran, which model/prompt version was used, and whether the last successful speech was committed.

## 13. Data lifecycle: backups and retention (v1.2)

v1.1 had no plan for the one SQLite file growing unbounded or being lost. v1.2 separates this into a safe, automated part and a deliberately manual part:

- **Backups (automated).** An in-process scheduled job (`APScheduler==3.10.4`, running inside the same single process already decided in §12 — no separate cron container to deploy) calls SQLite's online backup API (`sqlite3.Connection.backup()`) nightly at 03:00 UTC, writing to `backups/botarena-YYYYMMDD.db` on the same persistent disk, and deletes backup files older than 7 days. Backups are non-destructive to the live database, so automating this carries no risk.
- **Retention/pruning (manual, documented, not scheduled).** Auto-deleting old debates during a 5–10 person eval window is the wrong default — it's destructive and the eval explicitly wants to look at completion/resume history (§19). Instead: log a warning when the live DB file exceeds 500 MB (checked in the same nightly job), and provide a documented, manually-run script (`scripts/prune_old_debates.py --older-than-days N --dry-run`) that an operator runs deliberately when storage actually becomes a problem. Revisit automatic pruning only once the demo moves past the small-eval stage described in §16.

## 14. Observability (v1.2)

v1.1 specified logs but nothing for a solo operator to check proactively in production. v1.2 adds the minimum that needs no extra infrastructure:

- `GET /api/admin/stats` (§11): active debate count, today's per-fingerprint and global creation counts, provider-call budget usage, DB file size. Gated by a shared-secret `X-Admin-Token` header — no session/auth system, consistent with the "no accounts" scope decision in §12.
- The nightly backup job (§13) also logs DB file size and emits a warning-level log line past the 500 MB threshold, so size growth is visible in whatever log viewer the host provides without needing a separate metrics service.
- No external APM/alerting service is added for v1.2 — out of proportion to a small demo. If this graduates past the eval stage, add one then rather than before it's needed.

## 15. Architecture and deployment boundary

Keep React + Vite, Tailwind, FastAPI, Pydantic, SQLAlchemy/SQLite, and a small Groq provider adapter. Select and verify the actual available model during the proof of concept; do not hardcode a potentially obsolete model choice into this plan.

```text
Browser: Lobby / Arena / Results / Recent Battles
    | POST create/run; GET snapshots/history
FastAPI (+ slowapi rate guard, admin-token routes)
    | application-owned task registry + capacity control
Debate orchestrator
    | prompt builders -> content filter -> provider adapter -> Groq
    | short database transactions (WAL mode)
SQLite on persistent disk
    | nightly backup job (APScheduler, in-process)
```

Suggested backend boundaries: API handlers, execution supervisor, debate engine, prompt builders, content filter, provider adapter, rate-limit guard, schemas, database models, and configuration. Avoid empty scaffolding until the component is needed.

Host the API on a persistent service that supports ongoing processes and persistent disk. Use one instance and one worker; do not deploy this task design into request-limited functions or multiple replicas. Document that host shutdown or sleep interrupts execution and requires later resume. Serve frontend/API from one origin where practical, or explicitly configure allowed origins.

The first release is a local or controlled shared demo. With no accounts, Recent Battles is a shared archive, not personal/private history. Make that visible and retain curated topics only. The §10 rate limits and §8 content filter are the explicit, implemented answer to "add enforced request/spend limits" before wider access — this plan no longer defers that to an unspecified future step. Decide whether shared visibility is acceptable before opening access beyond the initial eval group; if private ownership is required, that is a separate authentication/scope change.

For future multi-worker scaling or automatic durable recovery, replace the process-owned task mechanism with durable job ownership/queuing before adding replicas. Do not imply that the current tasks survive backend crashes.

## 16. UI requirements

Preserve the original dark arena direction: background `#0B1020`, surface `#111827`, violet `#8B5CF6`, Atlas cyan `#06B6D4`, and Nova coral `#F43F5E`. Check actual text/background contrast rather than assuming accent colors are accessible.

- Lobby: topics, opponent previews, three-round summary, Start, Recent Battles.
- Arena: topic, fixed sides, round/speaker status, completed count, ordered transcript, connection status, navigation.
- Failure view: saved transcript, failure stage, understandable explanation, Resume when available, remaining resumes, New Battle.
- Results: committed verdict, rubric details, explanations, complete transcript, New Battle, Recent Battles.
- Recent Battles: compact status list with empty, loading, error, and pagination states.

Use keyboard-operable controls, visible focus, text labels alongside status colors, and reduced-motion support. Announce status changes without having screen readers repeatedly read the entire transcript. Use one readable transcript column on mobile. Never display fabricated placeholder scores as live results.

**(v1.2)** Wrap the app's router in a single root-level React error boundary. On an uncaught render error, show a minimal "Something went wrong" screen with a reload action rather than a blank page; log the error to the console (no external error-tracking service for v1.2 — consistent with §14's minimal-observability decision). All transcript/speech/explanation text renders through plain text nodes only — never `dangerouslySetInnerHTML`, never a markdown renderer — matching the content-safety decision in §8.

## 17. Build sequence and exit criteria

| Phase | Work | Exit criterion |
|---|---|---|
| 1. Engine proof | Provider adapter, role prompts, fixed six turns, structured judge, fake provider. | CLI completes one real match; fake-provider tests prove ordering and scoring boundaries. |
| 2. Persistence and resume | Schema/migrations (incl. `creator_fingerprint`, `hidden`), WAL pragma, checkpoints, attempts, run ownership, budgets. | Failure at turn 4 resumes from turn 4; judge failure retries only judging; two concurrent debates can both commit checkpoints without lock errors. |
| 3. Execution lifecycle/API | Create/run/get/list APIs, task registry, capacity, startup reconciliation, deadlines, **§10 rate limits**, admin-token routes. | Accepted work survives browser disconnect; restart becomes resumable failure; duplicates cannot create duplicate records; a fingerprint over its creation limit gets a clear `429`. |
| 4. UI with fixtures | Lobby, Arena, Results, Recent Battles, failure/connection states, root error boundary. | Every lifecycle state is navigable and readable on desktop/mobile; a forced render error shows the fallback screen instead of a blank page. |
| 5. Integration | Snapshot polling, creation idempotency (incl. localStorage pending-creation recovery), route recovery, status/action mapping. | A real match can start, be left, reopened, and completed or resumed; a simulated reload mid-creation recovers via the same client request UUID. |
| 6. Quality and deployment | AI sample inspection, content-filter wiring, accessibility, limits, backup job, persistent-host deployment, README. | Deployed restart/recovery smoke test passes; a forced backup run produces a restorable file; setup, limits, and moderation/retention process are documented. |

A provisional planning range is **45–70 focused hours** for someone comfortable with the stack — revised up from v1.1's 35–55 to account for rate limiting, the content filter, backups, the admin endpoint, the error boundary, and their tests. This is still a new estimate, not a commitment; revise it after phases 1–2.

## 18. Required verification

### Deterministic backend checks

- Six turns have correct positions, speakers, rounds, and transcript context.
- Both sides remain fixed in prompt construction; generated quality is inspected separately.
- Judge schemas reject missing/out-of-range/non-integer scores and nonexistent references.
- Draw at a 5-point difference; winner at a 6-point difference.
- Completing a match atomically saves one judgment and terminal status.
- Concurrent run requests for one session accept only one attempt.
- **(v1.2)** Concurrent run requests across *two different* debates (the plan's own concurrency ceiling) both commit checkpoints successfully under WAL mode — no spurious lock errors.
- Repeated create UUID returns the same session; changed input conflicts.
- Failure after turn 3 preserves three messages; Resume generates turns 4–6 only.
- Judge failure preserves six messages; Resume does not call a debater.
- Late output from an obsolete `run_id` cannot change saved state.
- Call/attempt limits and capacity rejection behave as specified.
- Task-registration failure, timeout, cancellation, and missing live task do not leave permanent active state.
- Restart reconciliation preserves checkpoints and marks prior active attempts interrupted.
- **(v1.2)** A speech containing filtered content is retried within the stage budget, not a new one; exhausting the budget while still flagged produces `content_flagged` and preserves earlier speeches.
- **(v1.2)** A fingerprint exceeding the per-client creation or concurrency limit gets `429` with the correct `code`; the global daily cap, once reached, rejects further creations regardless of fingerprint.
- **(v1.2)** A `hidden` debate returns `404` from the public GET and is excluded from the list endpoint, but remains visible via `/api/admin/stats`.

### API/UI integration checks

- Polling reflects committed progress and active speaker before a call finishes.
- Old snapshots cannot overwrite newer state or duplicate messages.
- Polling disconnect keeps the transcript and does not issue a run request.
- Reload/navigation retrieves the correct state without triggering generation.
- Lost create/run responses recover without duplicate sessions/workers.
- **(v1.2)** Simulating a reload immediately after the create POST is sent (before a response arrives) recovers via the persisted `localStorage` pending-creation key on next mount, without creating a second debate.
- All failure actions follow server-provided eligibility; retry is not offered after budgets are exhausted.
- Recent Battles retrieves active, failed, and completed matches without AI calls.
- Results stay hidden until judgment and completion are committed.
- **(v1.2)** Generated speech/judgment content containing HTML or script-like strings (e.g. `<script>alert(1)</script>`, injected as test fixture data through the fake provider) renders as literal visible text in the arena and results views, never executes, and never alters the DOM structure.

### Real environment and AI checks

Run at least five varied sample debates to inspect relevance, repetition, rebuttal grounding, judgment explanations, and latency. Test on the intended deployed host: close the browser during a match, reopen it, restart the API during a speech and during judging, then resume. Verify persistent disk behavior, responsive layout, keyboard navigation, and configured spending controls. **(v1.2)** Force a nightly backup run manually and confirm the resulting file opens and contains the expected rows (a real restore test, not just "the job ran without error").

Mock-provider tests verify software correctness. Real-model review checks debate quality; neither substitutes for the other.

## 19. Definition of done and evaluation

The MVP is complete when a user can select a curated topic, watch six saved speeches appear progressively, receive a validated explained verdict, leave and return, find previous battles, and resume eligible failures without regenerating committed speeches. Duplicate execution is blocked; restart behavior and usage limits are tested; deployment uses persistent storage; setup and recovery instructions are documented. **(v1.2)** Per-client and global creation/concurrency limits are enforced and tested; generated content passes through the lexical filter before being committed; a nightly backup has been proven restorable.

For a small 5–10-person evaluation, retain the original provisional targets: at least 80% of started debates complete, at least 80% of testers understand the verdict, and at least 50% voluntarily start another debate. Track completion on first attempt separately from completion after resume, so recovery does not conceal reliability problems. Record no duplicate speeches or corrupted completed matches. **(v1.2)** Also record: zero debates blocked by rate limits during the eval itself (if testers are hitting the per-client creation limit during normal use, the limit is tuned too tight for this group size, not a sign the feature is wrong).

Tune topic count, speech length, model choice, retry limits, and the v1.2 rate-limit thresholds from observed quality, latency, and eval behavior. Add features only after the core experience proves useful and enjoyable.

## 20. Technical references checked for this revision

The lifecycle/task/transaction design uses these documented foundations. The detailed product policies and numeric defaults above are design choices.

- [FastAPI lifespan events](https://fastapi.tiangolo.com/advanced/events/): application startup and shutdown lifecycle hooks.
- [Python asyncio tasks](https://docs.python.org/3/library/asyncio-task.html): task ownership, retaining task references, cancellation, and timeouts.
- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html): atomic database transactions underpinning saved checkpoints.
- [SQLite transactions](https://www.sqlite.org/lang_transaction.html): transaction behavior and the single-writer constraint.
- **(v1.2)** [SQLite WAL mode](https://www.sqlite.org/wal.html): why WAL plus `busy_timeout` avoids writer-contention failures at the plan's own concurrency ceiling.
- **(v1.2)** [SQLite Backup API](https://www.sqlite.org/backup.html): the online backup mechanism used by the nightly job in §13.
- **(v1.2)** [slowapi documentation](https://slowapi.readthedocs.io/): in-process rate limiting used as the fast-path guard in §10.
- **(v1.2)** [APScheduler documentation](https://apscheduler.readthedocs.io/): in-process scheduled jobs used for backups in §13, avoiding a separate cron container.
