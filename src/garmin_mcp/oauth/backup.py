"""Online backup of the OAuth data directory as a gzipped tar on stdout.

SQLite files are copied with SQLite's backup API, so a write in progress is never captured
half-done; WAL, SHM and journal files are skipped because the copy already contains their
pages. Everything else (tokens, the webhook spool) is added as is. Nothing is written inside
the data directory, so a deregistered user's data does not linger next to the live data.

    docker compose exec -T garmin-mcp-oauth garmin-mcp-backup > garmin-oauth-$(date +%F).tar.gz
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import BinaryIO

from garmin_mcp.oauth.config import DEFAULT_TOKEN_ROOT, TOKEN_ROOT_ENV

_SKIP_SUFFIXES = ("-wal", "-shm", "-journal")


def write_backup(root: Path, out: BinaryIO) -> int:
    """Write a .tar.gz of ``root`` to ``out``. Returns the number of files archived."""
    count = 0
    with tarfile.open(fileobj=out, mode="w|gz") as tar, tempfile.TemporaryDirectory() as scratch:
        for path in sorted(root.rglob("*")):
            name = str(path.relative_to(root))
            if path.is_dir():
                tar.add(path, arcname=name, recursive=False)
            elif path.suffix == ".sqlite3":
                copy = Path(scratch) / "copy.sqlite3"
                copy.unlink(missing_ok=True)
                src, dst = sqlite3.connect(path), sqlite3.connect(copy)
                try:
                    src.backup(dst)
                finally:
                    src.close()
                    dst.close()
                info = tar.gettarinfo(copy, arcname=name)
                info.mode = path.stat().st_mode & 0o777
                with copy.open("rb") as fh:
                    tar.addfile(info, fh)
                count += 1
            elif path.is_file() and not path.name.endswith(_SKIP_SUFFIXES):
                tar.add(path, arcname=name)
                count += 1
    return count


def main() -> None:
    root = Path(os.environ.get(TOKEN_ROOT_ENV) or DEFAULT_TOKEN_ROOT)
    if not root.is_dir():
        sys.exit(f"garmin-mcp-backup: {root} is not a directory")
    if sys.stdout.isatty():
        sys.exit("garmin-mcp-backup: writes a binary archive to stdout; redirect it to a file")
    count = write_backup(root, sys.stdout.buffer)
    print(f"garmin-mcp-backup: {count} files from {root}", file=sys.stderr)


if __name__ == "__main__":
    main()
