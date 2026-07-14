"""CLI runner for the v3 unified-substrate migration.

Usage:
    python scripts/migrate_to_unified.py            # run (idempotent guard)
    python scripts/migrate_to_unified.py --force    # rebuild from scratch
    python scripts/migrate_to_unified.py --no-backfill   # skip embedding backfill
    python scripts/migrate_to_unified.py --verify   # just print verification

Sidecar by design: produces ~/.null/unified.db. Does NOT touch live DBs.
"""

from __future__ import annotations

import argparse
import json
import sys

from null_memory.migrate_v3 import DEFAULT_UNIFIED_PATH, migrate, verify


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=DEFAULT_UNIFIED_PATH)
    ap.add_argument("--force", action="store_true", help="rebuild from scratch")
    ap.add_argument("--no-backfill", action="store_true", help="skip embedding backfill")
    ap.add_argument("--verify", action="store_true", help="only run verification")
    args = ap.parse_args()

    if args.verify:
        print(json.dumps(verify(args.target), indent=2, default=str))
        return 0

    print(f"→ migrating to {args.target} (force={args.force}, backfill={not args.no_backfill})")
    stats = migrate(target_path=args.target, force=args.force, backfill=not args.no_backfill)
    print(json.dumps(stats.__dict__, indent=2, default=str))
    print()
    print("=== verify ===")
    print(json.dumps(verify(args.target), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
