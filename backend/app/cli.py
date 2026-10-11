"""Phase 1 exit-criterion runner: `uv run python -m app.cli` produces one coherent,
end-to-end debate from the fixed six-turn engine plus judging.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from app.engine.models import DebateResult, Topic
from app.engine.orchestrator import OrchestratorError, run_debate
from app.engine.provider import FakeProvider, Provider

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
    provider = build_provider(args.provider)
    try:
        result = await run_debate(topic=topic, provider=provider)
    except OrchestratorError as exc:
        print(f"Debate failed at stage '{exc.stage}': {exc.reason}", file=sys.stderr)
        return 1
    print_result(result)
    return 0


def main() -> None:
    load_dotenv()
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
    parser = argparse.ArgumentParser(description="Run one BotArena debate end to end.")
    parser.add_argument("--topic", default="ai-replace-teachers")
    parser.add_argument("--provider", choices=["fake", "groq"], default="fake")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
