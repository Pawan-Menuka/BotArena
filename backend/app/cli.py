"""Run one local debate with persisted checkpoints and provider reservations."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
from pathlib import Path
from uuid import uuid4

from app.engine.models import DebateResult, Topic
from app.engine.orchestrator import OrchestratorError
from app.engine.provider import FakeProvider, Provider
from app.config import Settings
from app.core.auth import token_hash
from app.db.base import create_database
from app.db.migrate import upgrade_database
from app.db.repo import DebateRepository

TOPICS_PATH = Path(__file__).parent / "data" / "topics.json"


def load_topics() -> list[Topic]:
    raw = json.loads(TOPICS_PATH.read_text(encoding="utf-8"))
    return [Topic(**entry) for entry in raw]


def find_topic(topic_id: str) -> Topic:
    for topic in load_topics():
        if topic.id == topic_id:
            return topic
    available = ", ".join(t.id for t in load_topics())
    raise SystemExit(f"Unknown topic '{topic_id}'. Available: {available}")


def build_provider(name: str) -> Provider:
    if name == "fake":
        return FakeProvider()
    if name == "groq":
        from app.engine.provider import GroqProvider  # lazy: only needed for this path

        return GroqProvider()
    raise SystemExit(f"Unknown provider '{name}'")


def print_result(result: DebateResult) -> None:
    print(f"=== {result.topic.title} ===\n")
    for speech in result.speeches:
        print(f"[{speech.message_id}] {speech.agent.value.upper()} ({speech.side.value}, "
              f"{speech.round.value}):")
        print(speech.content)
        print()

    j = result.judgment
    print("--- Judgment ---")
    print(f"Pro: {j.pro_scores.total}  Con: {j.con_scores.total}  Winner: {j.winner.upper()}")
    print(f"Pro strengths: {j.pro_strengths}")
    print(f"Pro weaknesses: {j.pro_weaknesses}")
    print(f"Con strengths: {j.con_strengths}")
    print(f"Con weaknesses: {j.con_weaknesses}")
    print(f"Overall: {j.overall_explanation}")
    print(f"Referenced: {', '.join(j.referenced_message_ids)}")


async def main_async(args: argparse.Namespace) -> int:
    topic = find_topic(args.topic)
    settings = Settings()
    if settings.deployment_mode != "local":
        print("The CLI is available only in local mode; shared execution uses the authenticated API.", file=sys.stderr)
        return 1
    model_id = "fake" if args.provider == "fake" else os.getenv("GROQ_MODEL", "unconfigured")
    try:
        settings.validate_execution_budget(model_id)
    except ValueError:
        print("Provider pricing, input accounting or spending configuration needs attention.", file=sys.stderr)
        return 1
    provider = build_provider(args.provider)
    engine, sessions = create_database(settings.database_path)
    try:
        await upgrade_database(engine)
        repo = DebateRepository(sessions, settings=settings)
        debate_id = await repo.create(topic=topic, client_request_id=str(uuid4()), execution_token_hash=token_hash(secrets.token_hex(32)))
        result = await repo.execute(debate_id, provider=provider, model_id=model_id)
    except OrchestratorError as exc:
        print(f"Debate failed at stage '{exc.stage}': {exc.reason}", file=sys.stderr)
        return 1
    finally:
        close = getattr(provider, "aclose", None)
        if close is not None:
            await close()
        await engine.dispose()
    print_result(result)
    return 0


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
    parser = argparse.ArgumentParser(description="Run one BotArena debate end to end.")
    parser.add_argument("--topic", default="ai-replace-teachers")
    parser.add_argument("--provider", choices=["fake", "groq"], default="fake")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
