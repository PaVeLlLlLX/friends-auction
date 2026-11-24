"""Offline regression checks for consistent backups and scoped expiry cleanup."""

import json
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from auction.storage import MARKER, purge_managed_backups, snapshot_storage, verify_backup


def storage(tmp_path: Path) -> tuple[Path, Path]:
    """Create a small synthetic database and registered JPEG-shaped file."""
    database = tmp_path / "source.sqlite3"
    uploads = tmp_path / "photos"
    uploads.mkdir()
    (uploads / "one.jpg").write_bytes(b"synthetic-photo")
    with closing(sqlite3.connect(database)) as db:
        db.execute("CREATE TABLE photos(id INTEGER PRIMARY KEY, filename TEXT)")
        db.execute("INSERT INTO photos VALUES(1, 'one.jpg')")
        db.commit()
    return database, uploads


def test_snapshot_verifies_and_rejects_changed_photos(tmp_path: Path) -> None:
    """A verified database/photo pair detects later file corruption."""
    database, uploads = storage(tmp_path)
    result = snapshot_storage(database, uploads, tmp_path / "backups", "one", time.time() + 60)
    assert verify_backup(result)["photos"] == 1
    assert result.stat().st_mode & 0o077 == 0
    assert (result / "auction.sqlite3").stat().st_mode & 0o077 == 0
    (result / "photos/one.jpg").write_bytes(b"changed")
    with pytest.raises(ValueError, match="photo hash"):
        verify_backup(result)


def test_snapshot_missing_photo_rolls_back_staging(tmp_path: Path) -> None:
    """A missing registered file leaves no partial backup or changed source."""
    database, uploads = storage(tmp_path)
    (uploads / "one.jpg").unlink()
    backups = tmp_path / "backups"
    with pytest.raises(ValueError, match="registered photo"):
        snapshot_storage(database, uploads, backups, "one", time.time() + 60)
    assert list(backups.iterdir()) == []
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT count(*) FROM photos").fetchone()[0] == 1


def test_purge_only_owned_snapshots_after_retention(tmp_path: Path) -> None:
    """Expiry covers complete and interrupted backups while preserving foreign paths."""
    database, uploads = storage(tmp_path)
    backups = tmp_path / "backups"
    result = snapshot_storage(database, uploads, backups, "one", time.time() + 60)
    foreign = backups / "unrelated"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("keep")
    linked = backups / "linked"
    linked.symlink_to(foreign, target_is_directory=True)
    pending = backups / ".pending-test"
    pending.mkdir()
    (pending / MARKER).write_text(json.dumps({"format": "friends-auction-backup-v1"}))
    assert purge_managed_backups(backups, 99, 100) == 0
    assert result.exists()
    assert purge_managed_backups(backups, 100, 100) == 2
    assert not result.exists() and not pending.exists()
    assert (foreign / "keep.txt").read_text() == "keep" and linked.is_symlink()


def test_expired_snapshot_never_copies_storage(tmp_path: Path) -> None:
    """An elapsed retention boundary rejects creation before allocating a snapshot."""
    database, uploads = storage(tmp_path)
    with pytest.raises(ValueError, match="retention deadline"):
        snapshot_storage(database, uploads, tmp_path / "backups", "one", time.time() - 1)
    assert not (tmp_path / "backups").exists()


def test_snapshot_serializes_database_and_photo_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer replacing a registered photo cannot split the snapshot's database/file pair."""
    database, uploads = storage(tmp_path)
    copying = threading.Event()
    writer_started = threading.Event()
    writer_finished = threading.Event()
    original_copy = shutil.copyfile

    def copy_photo(source: Path, destination: Path) -> str:
        """Pause while a separate writer attempts to acquire the held reservation."""
        copying.set()
        assert writer_started.wait(5)
        assert not writer_finished.wait(0.05)
        return str(original_copy(source, destination))

    def replace_photo() -> None:
        """Replace both registered filename and file contents under a write transaction."""
        assert copying.wait(5)
        with closing(sqlite3.connect(database, timeout=5, isolation_level=None)) as db:
            writer_started.set()
            db.execute("BEGIN IMMEDIATE")
            (uploads / "two.jpg").write_bytes(b"new-photo")
            db.execute("UPDATE photos SET filename='two.jpg'")
            (uploads / "one.jpg").unlink()
            db.commit()
        writer_finished.set()

    monkeypatch.setattr(shutil, "copyfile", copy_photo)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(replace_photo)
        result = snapshot_storage(database, uploads, tmp_path / "backups", "one", time.time() + 60)
        future.result(timeout=5)
    assert writer_finished.is_set()
    assert verify_backup(result)["photos"] == 1
    assert (result / "photos/one.jpg").read_bytes() == b"synthetic-photo"
    assert (uploads / "two.jpg").read_bytes() == b"new-photo"
