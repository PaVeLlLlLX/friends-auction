"""Consistent private snapshots and expiry cleanup for application-owned backups."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import TypedDict

FORMAT = "friends-auction-backup-v1"
MARKER = ".friends-auction-backup.json"


class BackupSummary(TypedDict):
    """Verified snapshot metadata without account information or credentials."""

    database_sha256: str
    photos: int
    expires_at: float


def digest(path: Path) -> str:
    """Hash a regular file without loading its entire contents into memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def private_json(path: Path, document: object) -> None:
    """Replace an owner-only JSON document atomically in its private parent directory."""
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        temporary.chmod(0o600)
        json.dump(document, stream, indent=2)
        stream.write("\n")
    temporary.replace(path)


def snapshot_storage(
    database: Path,
    uploads: Path,
    backup_root: Path,
    name: str,
    expires_at: float,
) -> Path:
    """Snapshot SQLite and its registered JPEGs while excluding concurrent writers.

    The source must exist and ``name`` must be a new single path component.
    An independent BEGIN IMMEDIATE connection holds the write reservation while
    a separate reader uses SQLite's backup API and copies registered files.
    Both completed and interrupted snapshots carry an expiry marker. Failures
    remove this call's staging directory and propagate; existing backups remain.
    No Telegram calls or source-data mutations occur.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", name):
        raise ValueError("Invalid backup name.")
    database, uploads = database.resolve(), uploads.resolve()
    backup_root = backup_root.absolute()
    if (
        not database.is_file()
        or not uploads.is_dir()
        or not math.isfinite(expires_at)
        or expires_at <= time.time()
    ):
        raise ValueError("Storage must exist and the retention deadline must be in the future.")
    if backup_root.is_symlink():
        raise ValueError("The backup root must not be a symbolic link.")
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup_root.chmod(0o700)
    final = backup_root / name
    if final.exists():
        raise FileExistsError(final)
    staging = backup_root / (".pending-" + uuid.uuid4().hex)
    staging.mkdir(mode=0o700)
    document: dict[str, object] = {
        "format": FORMAT,
        "complete": False,
        "created_at": time.time(),
        "expires_at": expires_at,
    }
    try:
        private_json(staging / MARKER, document)
        with closing(sqlite3.connect(database, timeout=30, isolation_level=None)) as guard:
            guard.execute("BEGIN IMMEDIATE")
            try:
                if time.time() >= expires_at:
                    raise ValueError("The retention deadline has passed.")
                rows = guard.execute("SELECT filename FROM photos ORDER BY id").fetchall()
                target_database = staging / "auction.sqlite3"
                with (
                    closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source,
                    closing(sqlite3.connect(target_database)) as target,
                ):
                    source.backup(target)
                target_database.chmod(0o600)
                photos = staging / "photos"
                photos.mkdir(mode=0o700)
                hashes: dict[str, str] = {}
                for (filename,) in rows:
                    if (
                        not isinstance(filename, str)
                        or Path(filename).name != filename
                        or not filename.endswith(".jpg")
                    ):
                        raise ValueError("Invalid registered photo filename.")
                    source_photo = uploads / filename
                    if source_photo.is_symlink() or not source_photo.is_file():
                        raise ValueError("A registered photo is missing or is a symbolic link.")
                    target_photo = photos / filename
                    shutil.copyfile(source_photo, target_photo)
                    target_photo.chmod(0o600)
                    hashes[filename] = digest(target_photo)
                if time.time() >= expires_at:
                    raise ValueError("The retention deadline passed during backup.")
                document.update(
                    complete=True,
                    database_sha256=digest(target_database),
                    photos=hashes,
                )
                private_json(staging / MARKER, document)
                verify_backup(staging)
                staging.rename(final)
            finally:
                guard.rollback()
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return final


def verify_backup(directory: Path) -> BackupSummary:
    """Verify marker, database consistency and exact registered photo hashes.

    Reject incomplete, changed or unexpected files and foreign-key violations.
    The function reads the snapshot only; it does not restore or initialize an
    application. Successful verification does not prove historical validity.
    """
    directory = directory.resolve()
    document = json.loads((directory / MARKER).read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or document.get("format") != FORMAT
        or document.get("complete") is not True
        or not isinstance(document.get("database_sha256"), str)
        or type(document.get("expires_at")) not in (int, float)
        or not isinstance(document.get("photos"), dict)
    ):
        raise ValueError("Invalid or incomplete backup manifest.")
    database = directory / "auction.sqlite3"
    expected = document["database_sha256"]
    if database.is_symlink() or digest(database) != expected:
        raise ValueError("Backup database hash does not match.")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Backup database integrity check failed.")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Backup database has foreign-key violations.")
        registered = {row[0] for row in db.execute("SELECT filename FROM photos")}
    photos = document["photos"]
    if registered != set(photos) or {p.name for p in (directory / "photos").iterdir()} != set(
        photos
    ):
        raise ValueError("Backup photo inventory does not match.")
    for filename, expected_hash in photos.items():
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ValueError("Invalid backup photo name.")
        photo = directory / "photos" / filename
        if photo.is_symlink() or digest(photo) != expected_hash:
            raise ValueError("Backup photo hash does not match.")
    return {
        "database_sha256": expected,
        "photos": len(photos),
        "expires_at": float(document["expires_at"]),
    }


def purge_managed_backups(root: Path, now: float, expires_at: float) -> int:
    """Remove this application's marked snapshots only after configured retention.

    Foreign directories and symbolic links are never followed or removed.
    Interrupted marked snapshots are included. Filesystem failures propagate so
    the worker retries instead of reporting successful final cleanup.
    """
    if now < expires_at or not root.exists():
        return 0
    if root.is_symlink() or not root.is_dir():
        raise ValueError("The managed backup root must be a real directory.")
    removed = 0
    for directory in root.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        marker = directory / MARKER
        if marker.is_symlink() or not marker.is_file():
            continue
        document = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("format") != FORMAT:
            continue
        shutil.rmtree(directory)
        removed += 1
    return removed
