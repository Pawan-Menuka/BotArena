"""Scoring: the model supplies per-criterion scores only; the backend computes totals and
the winner. blueprint.md §14 — draw at <=5 point difference, win at >=6.
"""

from __future__ import annotations

from app.engine.models import CriterionScores, Judgment
from app.engine.validation import JudgeOutputModel

DRAW_THRESHOLD = 5


def score_judgment(parsed: JudgeOutputModel) -> Judgment:
    pro_scores = CriterionScores(**parsed.pro.model_dump())
    con_scores = CriterionScores(**parsed.con.model_dump())

    diff = abs(pro_scores.total - con_scores.total)
    if diff <= DRAW_THRESHOLD:
        winner = "draw"
    elif pro_scores.total > con_scores.total:
        winner = "pro"
    else:
        winner = "con"

    return Judgment(
        pro_scores=pro_scores,
        con_scores=con_scores,
        winner=winner,
        pro_strengths=parsed.pro_strengths,
        pro_weaknesses=parsed.pro_weaknesses,
        con_strengths=parsed.con_strengths,
        con_weaknesses=parsed.con_weaknesses,
        overall_explanation=parsed.overall_explanation,
        referenced_message_ids=list(parsed.referenced_message_ids),
    )
