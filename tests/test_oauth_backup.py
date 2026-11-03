"""garmin-mcp-backup: a consistent archive of the OAuth data directory."""
from __future__ import annotations

import io
import sqlite3
import tarfile

from garmin_mcp.oauth.backup import write_backup


def _db(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    con.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    con.commit()
    return con  # left open, so the WAL file exists while the backup runs


def test_backup_holds_consistent_sqlite_and_plain_files(tmp_path):
    root = tmp_path / "data"
    open_con = _db(root / ".mcp-auth.sqlite3", ["client-a"])
    _db(root / "user-1" / "summaries.sqlite3", ["sleep"]).close()
    (root / "user-1" / "tokens.json").write_text("{}")
    (root / ".inbox" / "retry").mkdir(parents=True)
    assert (root / ".mcp-auth.sqlite3-wal").exists()

    buf = io.BytesIO()
    count = write_backup(root, buf)
    open_con.close()

    buf.seek(0)
    restore = tmp_path / "restore"
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        names = set(tar.getnames())
        tar.extractall(restore, filter="data")
    assert count == 3
    assert ".mcp-auth.sqlite3-wal" not in names
    assert ".inbox/retry" in names
    rows = sqlite3.connect(restore / ".mcp-auth.sqlite3").execute("SELECT v FROM t").fetchall()
    assert rows == [("client-a",)]
    assert (restore / "user-1" / "tokens.json").read_text() == "{}"


def test_backup_writes_nothing_inside_the_data_dir(tmp_path):
    root = tmp_path / "data"
    _db(root / ".mcp-auth.sqlite3", ["x"]).close()
    before = sorted(p.relative_to(root) for p in root.rglob("*"))
    write_backup(root, io.BytesIO())
    assert sorted(p.relative_to(root) for p in root.rglob("*")) == before
