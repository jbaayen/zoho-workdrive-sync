"""SQLite sync state database."""

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from .config import STATE_DB, ensure_config_dir

logger = logging.getLogger(__name__)

HASH_CHUNK = 65536


@dataclass
class FileRecord:
    rel_path: str
    local_mtime: float = 0.0
    local_hash: str = ""
    # Remote modification time as epoch milliseconds. WorkDrive also exposes
    # a "modified_time" string, but that is a localized display value whose
    # rendering shifts with the session timezone, DST and the year, so it
    # cannot be used as a change key.
    remote_mtime_ms: int = 0
    remote_id: str = ""


class StateDB:
    """Tracks per-file sync state in SQLite."""

    def __init__(self, path: Optional[Path] = None):
        ensure_config_dir()
        self.path = path or STATE_DB
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _create_table(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                rel_path        TEXT PRIMARY KEY,
                local_mtime     REAL NOT NULL DEFAULT 0,
                local_hash      TEXT NOT NULL DEFAULT '',
                remote_mtime_ms INTEGER NOT NULL DEFAULT 0,
                remote_id       TEXT NOT NULL DEFAULT ''
            )
        """)

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(files)")}
        if cols and "remote_mtime_ms" not in cols:
            # Old schema keyed remote state on resource_etag + modified_time.
            # resource_etag is never returned by the API and modified_time is a
            # display string, so neither baseline is worth keeping. Local
            # baselines are preserved; remote_mtime_ms is backfilled by the
            # next scan.
            logger.info("Migrating sync state to epoch-based remote timestamps")
            self.conn.execute("ALTER TABLE files RENAME TO files_old")
            self._create_table()
            self.conn.execute(
                "INSERT INTO files (rel_path, local_mtime, local_hash, remote_mtime_ms, remote_id) "
                "SELECT rel_path, local_mtime, local_hash, 0, remote_id FROM files_old"
            )
            self.conn.execute("DROP TABLE files_old")
        else:
            self._create_table()
        self.conn.commit()

    def get(self, rel_path: str) -> Optional[FileRecord]:
        row = self.conn.execute(
            "SELECT rel_path, local_mtime, local_hash, remote_mtime_ms, remote_id "
            "FROM files WHERE rel_path = ?", (rel_path,)
        ).fetchone()
        return FileRecord(*row) if row else None

    def all(self) -> Dict[str, FileRecord]:
        rows = self.conn.execute(
            "SELECT rel_path, local_mtime, local_hash, remote_mtime_ms, remote_id FROM files"
        ).fetchall()
        return {r[0]: FileRecord(*r) for r in rows}

    def upsert(self, rec: FileRecord) -> None:
        self.conn.execute(
            "INSERT INTO files (rel_path, local_mtime, local_hash, remote_mtime_ms, remote_id) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(rel_path) DO UPDATE SET "
            "local_mtime=excluded.local_mtime, local_hash=excluded.local_hash, "
            "remote_mtime_ms=excluded.remote_mtime_ms, remote_id=excluded.remote_id",
            (rec.rel_path, rec.local_mtime, rec.local_hash, rec.remote_mtime_ms, rec.remote_id)
        )
        self.conn.commit()

    def remove(self, rel_path: str) -> None:
        self.conn.execute("DELETE FROM files WHERE rel_path = ?", (rel_path,))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def file_hash(path: Path) -> str:
    """Compute SHA-256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


