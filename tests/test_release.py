"""Validate release provenance without reading any deployment configuration."""

import json
from pathlib import Path

import pytest

from auction import __version__
from auction.release import release_metadata


def test_packaged_identity_requires_matching_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing or stale metadata never claims a commit for another package version."""
    monkeypatch.delenv("AUCTION_SOURCE_COMMIT", raising=False)
    path = tmp_path / "build-info.json"
    assert release_metadata(path)["source_commit"] is None
    path.write_text(json.dumps({"version": __version__, "source_commit": "a" * 40}))
    assert release_metadata(path)["source_commit"] == "a" * 40
    path.write_text(json.dumps({"version": "0.0.0", "source_commit": "a" * 40}))
    assert release_metadata(path)["source_commit"] is None


def test_invalid_environment_identity_is_not_published(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a SHA-shaped value crosses the environment-to-public-metadata boundary."""
    monkeypatch.setenv("AUCTION_SOURCE_COMMIT", "must-not-be-exposed")
    assert release_metadata()["source_commit"] is None
    monkeypatch.setenv("AUCTION_SOURCE_COMMIT", "b" * 40)
    assert release_metadata()["source_commit"] == "b" * 40
