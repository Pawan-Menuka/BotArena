from __future__ import annotations

from better_profanity import Profanity

_filter = Profanity()


def contains_profanity(text: str) -> bool:
    return _filter.contains_profanity(text)
