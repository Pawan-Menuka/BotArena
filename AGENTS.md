# AGENTS.md — BotArena

BotArena: a user picks a curated topic, watches two fixed AI debaters (Atlas = Pro, Nova = Con)
argue it over six speeches (opening, rebuttal, closing), then reads an AI verdict whose totals and
winner the backend computes. Solo-developer project. Backend is Python/FastAPI/SQLite; frontend is
React/Vite/TypeScript/Tailwind (not started yet).

## Where things live (read in this order)

1. `IMPLEMENTATION_PLAN.md` — **source of truth for progress.** Phase checklists, exit criteria,
   decisions log, open notes. If it disagrees with the blueprint, the plan file wins.
2. `docs/planning/BLUEPRINT.md` — the full design (data model, API shapes, quotas, backup
   pipeline, deployment). Section numbers are referenced from the plan.
3. `docs/planning/BotArena_MVP_Plan_v1.2.md` — historical only; ignore.

## How to work

- Do exactly ONE phase per session: the first phase not marked `done` (or the one the user
  names). Say which phase you are starting in one line.
- Stay inside the phase. If you find work belonging to a later phase or a new requirement, add a
  note to `IMPLEMENTATION_PLAN.md` instead of doing it.
- Before your final reply, update `IMPLEMENTATION_PLAN.md`: tick completed tasks, set the phase to
  `done` or `in-progress` with a one-line "remaining:" note, and log decisions/deviations. A fresh
  session must be able to resume from that file alone.
- Then stop and report: what was built, how it was verified, the next phase. Do not start the
  next phase without sign-off.
- Ship tests with each behavior. Run the verification commands below and report real output;
  never claim a pass you did not observe.

## Environment (Windows 11)

- Backend lives in `backend/`; run everything from there with `uv` (Python 3.13, `package=false`):
  - tests: `uv run pytest -q`
  - CLI debate (offline): `uv run python -m app.cli --provider fake --topic four-day-work-week`
  - add a dependency: `uv add <pkg>` (dev: `uv add --dev <pkg>`); pick current versions.
- Shell is Git Bash or PowerShell 5.1: no `&&` in PowerShell; quote Windows paths.
- Installed package versions are often newer than your training data (pydantic 2.14, groq 1.x,
  pytest 9). Check the installed package's real API before writing integration code.
- The user's network is sometimes flaky; on connection errors ask them to check it before
  debugging code.

## Secrets and network

- NEVER read, print, log, or commit `backend/.env` (it holds the real `GROQ_API_KEY`). Use
  `backend/.env.example` as the template. Do not ask the user to paste keys into chat.
- Tests must never call Groq. Use `FakeProvider`. Real-model runs
  (`--provider groq`) are for the user to trigger or approve; they cost quota.
- Do not create cloud resources or deploy (Fly.io etc.) without the user's explicit go-ahead.

## Git

- Do not commit or push unless asked. Never push to or open a PR against `main` unless explicitly
  asked; the user merges PRs themselves. If `gh` is unavailable, give a GitHub compare URL.
- Stage specific files, not `git add -A`; check `git status` for `.env` or `*.db` before any commit.

## Invariants — do not break these

- The model supplies per-criterion scores only. The backend computes totals and the winner
  (diff <= 5 → draw, >= 6 → higher total wins). Sides, speaking order, and rounds come from
  `TURN_ORDER` in `app/engine/models.py`, never from model text.
- Never hold a DB transaction while waiting for the provider. A speech and its progress/revision
  update commit together; judgment + completion commit together.
- `GET` endpoints never start or resume work. Mounting a page never starts a debate. Startup
  never auto-resumes paid work; it marks interrupted debates `failed` for manual resume.
- Execution token: client-generated, header `X-Debate-Token` only, server stores only its hash.
  Never in URLs, logs, snapshots, or history rows. Public read needs no token; Start/Resume does.
- Every provider call (once Phase 6 lands) goes through the durable reservation function. No SDK
  retries that bypass it.
- AI-generated text renders as plain text everywhere (no markdown, no `dangerouslySetInnerHTML`).
- Hidden debates return `404` on every public path. One backend process, one worker, one
  instance — do not design for multiple replicas.
- Idempotent create replays are resolved BEFORE quota checks and consume no quota.

## Provider facts (verified during Phase 1)

- Model: `openai/gpt-oss-20b` via `GROQ_MODEL` on Groq's free tier (rate-limited; expect 429s).
  `gpt-oss-120b` is the upgrade path. Llama 3.3 70B is no longer offered.
- It is a reasoning model: hidden thinking counts against the token cap. The adapter sends
  `max_completion_tokens = cap + REASONING_TOKEN_HEADROOM` with `reasoning_effort="low"` and
  errors on `finish_reason == "length"`. Do not lower these without re-testing.
- The judge call uses strict `json_schema` mode (`JUDGE_JSON_SCHEMA`); ranges and message-id
  references are still validated by `JudgeOutputModel`.
- Known quality risks: a debater can flip sides in a closing; the judge may not notice; speeches
  can run long; the judge cites all six messages every time. Watch for these in real runs.

## Deviations from the blueprint (already decided)

Python 3.13 not 3.12; `uv` with `package=false`; model is gpt-oss not Llama; judge uses strict
json_schema; blueprint library version pins are guidance — use current versions.

## Style

- Match existing code: `from __future__ import annotations`, dataclasses for domain types,
  Pydantic for validation, async provider protocol, small modules under `app/engine/`.
- No comments unless the WHY is non-obvious. No speculative abstractions or empty scaffolding.
- Do not add docs/READMEs beyond those the plan lists (`docs/DEPLOYMENT.md`, `docs/RUNBOOK.md`,
  root `README.md` in Phase 9).

## Frontend (Phase 4)

The user hand-designs their UIs. Before building any UI, ask whether they have a design to drop
in; if so, wire data/logic only and never restyle. If building from the blueprint, use only the
theme tokens in `IMPLEMENTATION_PLAN.md` Phase 4.
