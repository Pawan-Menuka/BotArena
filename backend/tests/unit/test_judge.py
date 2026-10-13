from app.engine.judge import score_judgment
from app.engine.validation import JudgeOutputModel

BASE = {
    "pro_strengths": "s",
    "pro_weaknesses": "w",
    "con_strengths": "s",
    "con_weaknesses": "w",
    "overall_explanation": "e",
    "referenced_message_ids": ["m1"],
}


def _scores(total: int) -> dict:
    # Spread an arbitrary total across the four criteria, capped at each max.
    return {
        "logical_reasoning": min(total, 35),
        "relevance": max(0, min(total - 35, 25)),
        "rebuttal_quality": max(0, min(total - 60, 25)),
        "clarity": max(0, min(total - 85, 15)),
    }


def test_draw_at_exactly_five_point_difference():
    parsed = JudgeOutputModel.model_validate(
        {**BASE, "pro": _scores(70), "con": _scores(65)}
    )
    judgment = score_judgment(parsed)
    assert judgment.winner == "draw"


def test_win_at_exactly_six_point_difference():
    parsed = JudgeOutputModel.model_validate(
        {**BASE, "pro": _scores(70), "con": _scores(64)}
    )
    judgment = score_judgment(parsed)
    assert judgment.winner == "pro"


def test_con_wins_when_higher():
    parsed = JudgeOutputModel.model_validate(
        {**BASE, "pro": _scores(50), "con": _scores(70)}
    )
    judgment = score_judgment(parsed)
    assert judgment.winner == "con"


def test_model_never_supplies_its_own_total_or_winner():
    # JudgeOutputModel intentionally has no "total" or "winner" field — scoring is
    # entirely computed by score_judgment from the four criteria.
    assert "total" not in JudgeOutputModel.model_fields
    assert "winner" not in JudgeOutputModel.model_fields
