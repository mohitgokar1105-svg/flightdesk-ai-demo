"""Manage FlightDesk logins. Passwords are typed (never passed on the command line) and stored only as scrypt hashes.

Usage:
    python scripts/manage_users.py list
    python scripts/manage_users.py create --username alex --role user --name "Alex (ops)"
    python scripts/manage_users.py deactivate --username alex
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from src.auth import STAFF_ROLES, SYSTEM, Role, as_principal, create_user  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.database import init_db, session_scope  # noqa: E402
from src.models import User  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["list", "create", "deactivate"])
    parser.add_argument("--username")
    parser.add_argument("--role", choices=[r.value for r in STAFF_ROLES], default=Role.USER.value)
    parser.add_argument("--name")
    args = parser.parse_args()
    settings = get_settings()
    init_db(settings.database_url)
    with session_scope(settings.database_url) as s, as_principal(SYSTEM):
        if args.command == "list":
            for u in s.scalars(select(User).order_by(User.username)):
                print(f"{u.username:20} {u.role:15} {'active' if u.is_active else 'inactive'}")
            return 0
        if not args.username:
            parser.error("--username is required")
        if args.command == "create":
            password = getpass.getpass("Password (8+ characters): ")
            if password != getpass.getpass("Repeat password: "):
                print("Passwords don't match.")
                return 1
            user = create_user(s, args.username, password, Role(args.role), args.name)
            print(f"Created {user.username} ({user.role}).")
            return 0
        user = s.scalar(select(User).where(func.lower(User.username) == args.username.lower()))
        if user is None:
            print("No such user.")
            return 1
        user.is_active = False
        print(f"Deactivated {user.username}.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
