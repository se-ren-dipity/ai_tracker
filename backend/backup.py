"""Consistent online SQLite backup: python -m backend.backup [destination.db]."""
import argparse
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv


def backup(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if not source.is_file():
        raise ValueError("Source database does not exist")
    if source == target or target.exists():
        raise ValueError("Backup destination must be a new file")
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)) as src:
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst, pages=256)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup integrity check failed")
    return target


if __name__ == "__main__":
    load_dotenv()
    source = Path(os.getenv("DB_PATH", "data/tracker.db"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", nargs="?", type=Path,
                        default=source.parent / "backups" / ("tracker-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".db"))
    args = parser.parse_args()
    print(backup(source, args.destination))
