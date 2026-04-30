"""
Create an initial admin user (run after migrations).

Usage (from repository root):
  python -m app.scripts.create_admin admin "YourPassword" "Display name"
"""

from __future__ import annotations

import argparse
import sys

from app.database import session_factory
from app.services.users_service import create_user


def main() -> int:
    p = argparse.ArgumentParser(description="Create an admin user")
    p.add_argument("username")
    p.add_argument("password")
    p.add_argument("display_name", nargs="?", default="")
    args = p.parse_args()
    SessionLocal = session_factory()
    db = SessionLocal()
    try:
        u = create_user(
            db,
            username=args.username,
            password=args.password,
            display_name=args.display_name or args.username,
            role="admin",
        )
        print(f"Created admin user id={u.id} username={u.username!r}.")
        return 0
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
