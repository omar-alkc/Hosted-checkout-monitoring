"""
Remove all detection records (detections table). CASCADE truncates dependent
notes and status_history. Sequences restart from 1.

Usage (from repository root):
  python -m app.scripts.truncate_detections --yes
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from app.database import engine


def main() -> int:
    p = argparse.ArgumentParser(description="Truncate all detections (and related notes/history).")
    p.add_argument(
        "--yes",
        action="store_true",
        help="Required: confirm destructive truncate.",
    )
    args = p.parse_args()
    if not args.yes:
        print("Refusing to run without --yes.", file=sys.stderr)
        return 2

    with engine().begin() as conn:
        conn.execute(text("TRUNCATE TABLE detections RESTART IDENTITY CASCADE"))

    print("Truncated detections (notes and status_history cleared via CASCADE).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
