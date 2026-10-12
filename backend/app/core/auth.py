from __future__ import annotations

import hashlib
import hmac
import re


def token_hash(token: str | None) -> str | None:
    if token is None or re.fullmatch(r"[0-9a-fA-F]{64}", token) is None:
        return None
    return hashlib.sha256(bytes.fromhex(token)).hexdigest()


def verify_token(token: str | None, expected_hash: str) -> bool:
    actual = token_hash(token)
    return actual is not None and hmac.compare_digest(actual, expected_hash)
