#!/usr/bin/env python3
"""Create or verify a private, retention-managed auction storage snapshot."""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from auction.app import DELETE_AFTER
from auction.service import load_config
from auction.storage import snapshot_storage, verify_backup


def main() -> int:
    """Verify a supplied snapshot or back up the configured storage without bot calls."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--verify", type=Path)
    parser.add_argument(
        "--name", default=datetime.now(timezone.utc).strftime("snapshot-%Y%m%dT%H%M%SZ")
    )
    args = parser.parse_args()
    try:
        if args.verify:
            summary = verify_backup(args.verify.resolve())
        else:
            if not args.config:
                parser.error("--config is required when creating a snapshot.")
            config = load_config(args.config)
            directory = snapshot_storage(
                config.data_dir / "auction.sqlite3",
                config.data_dir / "photos",
                config.data_dir.parent / "backups",
                args.name,
                DELETE_AFTER,
            )
            summary = verify_backup(directory)
        print(json.dumps(summary, indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error):
        print("Storage snapshot creation or verification failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
