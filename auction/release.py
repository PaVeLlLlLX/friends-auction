"""Expose validated, non-secret release provenance for diagnostics."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TypedDict

from auction import __version__


class ReleaseMetadata(TypedDict):
    """Declared build identity; this is metadata rather than remote attestation."""

    version: str
    source_commit: str | None


def release_metadata(metadata_file: Path | None = None) -> ReleaseMetadata:
    """Return the package version and a strictly validated optional Git commit.

    A release may supply ``AUCTION_SOURCE_COMMIT`` or its packaged build-info
    document. Unknown or malformed values are omitted rather than exposing
    arbitrary environment text. Missing metadata remains valid for development.
    """
    candidate: object = os.environ.get("AUCTION_SOURCE_COMMIT", "")
    if not candidate:
        path = metadata_file or Path(__file__).with_name("build-info.json")
        try:
            document: object = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(document, dict) and document.get("version") == __version__:
                candidate = document.get("source_commit")
        except (OSError, ValueError):
            candidate = None
    commit = (
        candidate
        if isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{40}", candidate)
        else None
    )
    return {"version": __version__, "source_commit": commit}
