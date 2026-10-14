from __future__ import annotations

import secrets

from app.core.auth import token_hash, verify_token


def test_execution_token_is_32_random_bytes_encoded_as_hex():
    token = secrets.token_hex(32)
    hashed = token_hash(token)
    assert len(hashed) == 64
    assert hashed != token
    assert verify_token(token, hashed)
    assert verify_token(token.upper(), hashed)
    assert not verify_token(secrets.token_hex(32), hashed)
    for invalid in (None, "", "x" * 64, "a" * 62, "a" * 66):
        assert token_hash(invalid) is None
        assert not verify_token(invalid, hashed)
