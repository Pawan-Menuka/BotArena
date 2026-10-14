"""Core domain types for a single debate."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    PRO = "pro"
    CON = "con"


class Agent(str, Enum):
    ATLAS = "atlas"
    NOVA = "nova"


class RoundName(str, Enum):
    OPENING = "opening"
    REBUTTAL = "rebuttal"
    CLOSING = "closing"


# Fixed speaking order: blueprint.md §10 "User journeys — Watch the debate".
# (round, agent, side) indexed by turn_order 1..6.
TURN_ORDER: list[tuple[RoundName, Agent, Side]] = [
    (RoundName.OPENING, Agent.ATLAS, Side.PRO),
    (RoundName.OPENING, Agent.NOVA, Side.CON),
    (RoundName.REBUTTAL, Agent.ATLAS, Side.PRO),
    (RoundName.REBUTTAL, Agent.NOVA, Side.CON),
    (RoundName.CLOSING, Agent.ATLAS, Side.PRO),
    (RoundName.CLOSING, Agent.NOVA, Side.CON),
]

ROUND_NUMBER: dict[RoundName, int] = {
    RoundName.OPENING: 1,
    RoundName.REBUTTAL: 2,
    RoundName.CLOSING: 3,
}

ROUND_OBJECTIVE: dict[tuple[RoundName, Side], str] = {
    (RoundName.OPENING, Side.PRO): "Establish the supporting case for the topic.",
    (RoundName.OPENING, Side.CON): "Establish the opposing case for the topic.",
    (RoundName.REBUTTAL, Side.PRO): "Address the opponent's actual opening claims.",
    (RoundName.REBUTTAL, Side.CON): "Address the opponent's actual opening and rebuttal arguments.",
    (RoundName.CLOSING, Side.PRO): "Restate your own side's strongest case in your own voice, then explain why the opponent's best objections do not defeat it. Never concede your side's position. Avoid major new claims.",
    (RoundName.CLOSING, Side.CON): "Restate your own side's strongest case in your own voice, then explain why the opponent's best objections do not defeat it. Never concede your side's position. Avoid major new claims.",
}


@dataclass(frozen=True)
class Topic:
    id: str
    title: str
    pro_framing: str
    con_framing: str


@dataclass(frozen=True)
class Speech:
    turn_order: int  # 1..6
    round: RoundName
    round_number: int
    agent: Agent
    side: Side
    content: str
    saved_id: str | None = None

    @property
    def message_id(self) -> str:
        return self.saved_id or f"m{self.turn_order}"


@dataclass(frozen=True)
class CriterionScores:
    logical_reasoning: int
    relevance: int
    rebuttal_quality: int
    clarity: int

    @property
    def total(self) -> int:
        return (
            self.logical_reasoning
            + self.relevance
            + self.rebuttal_quality
            + self.clarity
        )


@dataclass(frozen=True)
class Judgment:
    pro_scores: CriterionScores
    con_scores: CriterionScores
    winner: str  # "pro" | "con" | "draw"
    pro_strengths: str
    pro_weaknesses: str
    con_strengths: str
    con_weaknesses: str
    overall_explanation: str
    referenced_message_ids: list[str]


@dataclass(frozen=True)
class DebateResult:
    topic: Topic
    speeches: list[Speech]
    judgment: Judgment
