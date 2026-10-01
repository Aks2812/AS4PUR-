"""Argon2 password hashing (CLAUDE.md Section 5: bcrypt or argon2, never
stored/logged in plaintext). Argon2 is OWASP's current default
recommendation, hence the choice between the two the brief allows."""
from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError

_hasher = PasswordHasher()

# Shared between invite-based registration (app/auth/registration.py,
# Part C) and the mandatory must_change_password page (app/auth/
# password_change.py, Part D) - one constant, one validation function, so
# the two flows can never quietly drift apart in what counts as an
# acceptable password.
MIN_PASSWORD_LENGTH = 12


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, plain)
    except (VerifyMismatchError, InvalidHash):
        return False


def validate_new_password(password: str, confirm_password: str) -> str | None:
    """Returns None when `password` is acceptable, else a user-facing
    error message. Shared by every place in this app that lets someone
    set their own new password (registration, the mandatory
    must_change_password page) - never duplicated inline, so the rule
    only ever needs to change in one place."""
    if not password:
        return "Enter a password."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if password != confirm_password:
        return "Passwords did not match."
    return None


def generate_temporary_password(length_bytes: int = 16) -> str:
    """Cryptographically random (secrets, never random/uuid4 or anything
    derived from the account it's for - the username, a timestamp, etc.)
    - Part D's admin-triggered "reset password directly" action.
    token_urlsafe(16) yields ~22 characters, comfortably clearing
    MIN_PASSWORD_LENGTH above without needing a separate check."""
    return secrets.token_urlsafe(length_bytes)
