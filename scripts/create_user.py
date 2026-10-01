#!/usr/bin/env python3
"""
Creates (or resets the password/role of) an AS4PUR login user.

Run directly on the host - AS4PUR has no self-registration page by design
(CLAUDE.md Section 5: real login is required, and who gets an account is
an explicit admin decision, not a public signup flow).

Usage:
    python scripts/create_user.py <username> <role: operator|viewer>
    (prompts for a password interactively - never pass it as an argument,
    it would land in shell history)
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Role, User  # noqa: E402
from app.security.passwords import hash_password  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or update an AS4PUR user.")
    parser.add_argument("username")
    parser.add_argument("role", choices=[r.value for r in Role])
    args = parser.parse_args()

    password = getpass.getpass("New password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords did not match.", file=sys.stderr)
        sys.exit(1)
    if len(password) < 12:
        print("Password must be at least 12 characters.", file=sys.stderr)
        sys.exit(1)

    init_db()
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == args.username).one_or_none()
        if user is None:
            user = User(username=args.username, role=Role(args.role), password_hash=hash_password(password))
            db.add(user)
            action = "Created"
        else:
            user.password_hash = hash_password(password)
            user.role = Role(args.role)
            user.is_active = True
            action = "Updated"
        db.commit()
        print(f"{action} user '{args.username}' with role '{args.role}'.")


if __name__ == "__main__":
    main()
