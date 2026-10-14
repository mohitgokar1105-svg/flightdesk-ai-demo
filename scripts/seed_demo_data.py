"""Load the fictional sample dataset into the configured database.

Usage:
    python scripts/seed_demo_data.py            # adds samples if not present (never deletes)
    python scripts/seed_demo_data.py --reset    # DELETES ALL RECORDS, then reseeds (asks for confirmation)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.auth import SYSTEM, as_principal  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.database import RESET_CONFIRMATION_TOKEN, init_db, record_counts, session_scope  # noqa: E402
from src.logging_config import configure_logging  # noqa: E402
from src.seed import reset_demo_data, seed_demo_data  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="delete all records first (requires typing RESET)")
    args = parser.parse_args()
    configure_logging(to_file=False)
    settings = get_settings()
    init_db(settings.database_url)
    with session_scope(settings.database_url) as s, as_principal(SYSTEM):  # trusted command-line run
        if args.reset:
            token = input(f"This permanently deletes ALL records in {settings.database_url}. Type {RESET_CONFIRMATION_TOKEN} to continue: ")
            result = reset_demo_data(s, token.strip(), settings)
            print(f"Deleted {sum(result['deleted'].values())} records; seeded: {result['seeded']}")
        else:
            print(seed_demo_data(s, settings))
        print("Record counts:", record_counts(s))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
