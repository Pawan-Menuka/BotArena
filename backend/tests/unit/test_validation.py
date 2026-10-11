import json

import pytest

from app.engine.validation import ValidationFailure, validate_judgment, validate_speech

VALID_JUDGMENT = {
    "pro": {"logical_reasoning": 30, "relevance": 20, "rebuttal_quality": 20, "clarity": 10},
    "con": {"logical_reasoning": 28, "relevance": 18, "rebuttal_quality": 18, "clarity": 10},
    "pro_strengths": "s",
    "pro_weaknesses": "w",
    "con_strengths": "s",
    "con_weaknesses": "w",
    "overall_explanation": "e",
    "referenced_message_ids": ["m1", "m2"],
}


def test_rejects_empty_speech():
    with pytest.raises(ValidationFailure):
        validate_speech("   ")


def test_accepts_normal_speech():
    assert validate_speech("  a short speech  ") == "a short speech"


def test_rejects_speech_over_word_cap():
    with pytest.raises(ValidationFailure):
        validate_speech("word " * 300)


def test_rejects_malformed_json():
    with pytest.raises(ValidationFailure):
        validate_judgment("not json", known_message_ids={"m1"})


def test_rejects_out_of_range_score():
    bad = {**VALID_JUDGMENT, "pro": {**VALID_JUDGMENT["pro"], "logical_reasoning": 999}}
    with pytest.raises(ValidationFailure):
        validate_judgment(json.dumps(bad), known_message_ids={"m1", "m2"})


def test_rejects_non_integer_score():
    bad = {**VALID_JUDGMENT, "pro": {**VALID_JUDGMENT["pro"], "logical_reasoning": "a lot"}}
    with pytest.raises(ValidationFailure):
        validate_judgment(json.dumps(bad), known_message_ids={"m1", "m2"})


def test_rejects_missing_field():
    bad = dict(VALID_JUDGMENT)
    del bad["overall_explanation"]
    with pytest.raises(ValidationFailure):
        validate_judgment(json.dumps(bad), known_message_ids={"m1", "m2"})


def test_rejects_unknown_message_reference():
    with pytest.raises(ValidationFailure):
        validate_judgment(json.dumps(VALID_JUDGMENT), known_message_ids={"m1"})  # missing m2


def test_accepts_valid_judgment():
    parsed = validate_judgment(json.dumps(VALID_JUDGMENT), known_message_ids={"m1", "m2"})
    assert parsed.pro.logical_reasoning == 30
