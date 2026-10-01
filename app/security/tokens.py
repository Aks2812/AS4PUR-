"""
Single-use-token generation/hashing, shared by anything that needs a
random credential a database read alone can't hand out - currently just
Invite (app/models.py, Part C of the RBAC build). Deliberately the same
generate-random / store-only-the-hash discipline app/security/sessions.py
already uses for session tokens, kept in its own small module rather than
duplicated inline or imported from sessions.py (whose own `_hash_token`
stays module-private and session-specific on purpose).
"""
from __future__ import annotations

import hashlib
import secrets


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
