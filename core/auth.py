"""
Neutral Authentication Module for Agent Executor Gateway.
Provides strict Bearer token loading, creation, and verification.
"""

from __future__ import annotations
import os
import secrets


def load_or_create_token(token_file: str) -> str:
    """
    Ensure the token file exists with secure permissions (0600).
    If it does not exist, generates a 24-byte hex token and saves it.
    Returns the loaded/created token string.
    """
    if not os.path.exists(token_file):
        parent_dir = os.path.dirname(token_file)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        token_val = secrets.token_hex(24)
        with open(token_file, "w", encoding="utf-8") as f:
            f.write(token_val)
        os.chmod(token_file, 0o600)
        return token_val

    with open(token_file, "r", encoding="utf-8") as f:
        return f.read().strip()


def verify_bearer_token(auth_header: str | None, expected_token: str) -> bool:
    """
    Verify whether the provided Authorization header matches the expected Bearer token.
    Uses constant-time comparison to prevent timing attacks.
    """
    if not auth_header or not expected_token:
        return False

    auth_header = auth_header.strip()
    expected_header = f"Bearer {expected_token}"
    return secrets.compare_digest(auth_header, expected_header)
