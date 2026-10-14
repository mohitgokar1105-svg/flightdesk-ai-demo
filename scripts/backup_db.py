"""Consistent online backup of the SQLite database, with retention.

    python scripts/backup_db.py                    # -> data/backups/flightdesk-<UTC timestamp>.db
    python scripts/backup_db.py --keep 14 --dest /backups

Uses SQLite's backup API, so it is safe while the app is running. Restore by stopping the app and copying a backup over
the live file. Only ``sqlite:///`` databases are supported.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import DATA_DIR, get_settings  # noqa: E402


def backup(source: Path, dest_dir: Path, keep: int) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"flightdesk-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.db"
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    check = sqlite3.connect(target)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            target.unlink()
            raise RuntimeError("Backup failed its integrity check and was discarded.")
    finally:
        check.close()
    for old in sorted(dest_dir.glob("flightdesk-*.db"))[:-keep]:
        old.unlink()
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dest", type=Path, default=DATA_DIR / "backups")
    parser.add_argument("--keep", type=int, default=14, help="number of newest backups to keep")
    args = parser.parse_args()
    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        print("Only sqlite:/// databases are supported by this script.", file=sys.stderr)
        return 2
    source = Path(url.removeprefix("sqlite:///"))
    if not source.exists():
        print(f"Database not found: {source}", file=sys.stderr)
        return 1
    print(backup(source, args.dest, max(1, args.keep)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
