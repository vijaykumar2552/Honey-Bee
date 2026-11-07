"""
scripts/create_admin_user.py
=============================
Create the first ADMIN user.

``POST /api/auth/register`` deliberately requires an ADMIN caller -- there is
no public self-registration endpoint, on purpose, so nobody can sign up as
ADMIN over the network. That leaves a bootstrapping problem: how does the
*first* admin get created? This script, run locally/once against the real
database file, exactly like Django's ``createsuperuser`` or Rails' db seed
scripts.

Usage
-----
    python scripts/create_admin_user.py --email admin@example.com --name "Root Admin"

You will be prompted for a password (not taken as a CLI argument, so it never
ends up in your shell history). Run with ``--help`` for all options.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from honeychain import auth, config, db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the first HoneyChain ADMIN user.")
    parser.add_argument("--email", required=True, help="Login email for the new admin")
    parser.add_argument("--name", required=True, help="Display name")
    parser.add_argument("--user-id", default=None, help="External user id (default: auto)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Create the user even if an ADMIN already exists",
    )
    args = parser.parse_args()

    if not config.AUTH_SECRET:
        print(
            "HONEYCHAIN_AUTH_SECRET is not set. Set it before creating any user, "
            "since a token cannot be issued for them otherwise.",
            file=sys.stderr,
        )
        return 1

    db.initialise_database()

    if db.get_user_by_email(args.email.strip().lower()) is not None:
        print(f"A user with email '{args.email}' already exists.", file=sys.stderr)
        return 1

    if not args.force:
        existing_admins = [
            u for u in db.query_all("SELECT 1 FROM users WHERE role = 'ADMIN';")
        ]
        if existing_admins:
            print(
                "An ADMIN user already exists. Re-run with --force if you really "
                "want to create another one.",
                file=sys.stderr,
            )
            return 1

    password = getpass.getpass("New admin password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords did not match.", file=sys.stderr)
        return 1
    if len(password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        return 1

    import secrets

    user_id = args.user_id or f"USR-{secrets.token_hex(4).upper()}"
    with db.transaction() as conn:
        db.create_user(
            conn,
            user_id=user_id,
            organization_pk=None,
            email=args.email.strip().lower(),
            password_hash=auth.hash_password(password),
            name=args.name,
            role="ADMIN",
        )

    print(f"Created ADMIN user '{user_id}' <{args.email}>.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
