"""Offline checks for private supervision, shutdown and macOS startup configuration."""

import io
import json
import logging
import plistlib
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from auction import service
from auction.app import DELETE_AFTER
from auction.processes import verify_public_url
from scripts.macos_service import launch_agent


def test_service_configuration_is_private_and_validated(tmp_path: Path) -> None:
    """Reject readable-by-others credentials and malformed configuration before startup."""
    path = tmp_path / "config.json"
    token = "123456:" + "x" * 24
    document = {"token": token, "admin_id": 1, "cloudflared": "/opt/bin/cloudflared"}
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    config = service.load_config(path)
    assert token not in repr(config)
    assert config.data_dir == tmp_path / "data"
    path.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        service.load_config(path)
    path.chmod(0o600)
    document["public_url"] = "https://name.example/private?token=secret"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="root origin"):
        service.load_config(path)


def test_service_environment_cannot_inherit_demo_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force real Telegram authorization and dedicated storage regardless of shell flags."""
    monkeypatch.setenv("AUCTION_DEMO", "1")
    monkeypatch.setenv("AUCTION_TRIAL", "1")
    monkeypatch.setenv("AUCTION_BOT_POLL", "1")
    config = service.ServiceConfig("fake-test-token", 1, tmp_path / "data", Path("/cloudflared"))
    environment = service.service_environment(config)
    assert environment["AUCTION_DEMO"] == environment["AUCTION_TRIAL"] == "0"
    assert environment["AUCTION_BOT_POLL"] == "0"
    assert environment["AUCTION_CLOUDFLARE_PROXY"] == "1"
    assert environment["AUCTION_DB"] == str(tmp_path / "data/auction.sqlite3")


def test_launch_agent_restarts_failures_without_embedding_secrets(tmp_path: Path) -> None:
    """Keep completed retention stopped and scope power assertions to the running release."""
    release = tmp_path / "releases/v1.0.0"
    config = tmp_path / "config.json"
    agent = launch_agent(release, config)
    decoded = plistlib.loads(plistlib.dumps(agent))
    assert decoded["KeepAlive"] == {"SuccessfulExit": False}
    assert decoded["Umask"] == 0o077
    assert decoded["ProgramArguments"][:2] == ["/usr/bin/caffeinate", "-s"]
    assert str(config) == decoded["ProgramArguments"][-1]
    assert "EnvironmentVariables" not in decoded
    assert "TELEGRAM_BOT_TOKEN" not in str(decoded)


class CompletedCleanup:
    """A completed child process whose captured output contains private test details."""

    returncode = 0

    def __init__(self, token: str) -> None:
        """Expose text output to exercise the production diagnostic reader."""
        self.stdout = io.StringIO(f"Failed {token} https://example.invalid/private\n")

    def poll(self) -> int:
        """Report that this finite cleanup process has already exited successfully."""
        return 0

    def wait(self, timeout: float | None = None) -> int:
        """Return a successful exit without creating or waiting on an OS process."""
        return 0


def test_expired_service_only_cleans_and_redacts_child_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Skip web, tunnel and menu startup after retention while draining sanitized diagnostics."""
    config = service.ServiceConfig("private-test-token", 1, tmp_path / "data", Path("/cloudflared"))
    calls: list[list[str]] = []

    def launch(command: Sequence[str], **options: object) -> CompletedCleanup:
        """Record the sole cleanup command and provide its deterministic text output."""
        calls.append(list(command))
        return CompletedCleanup(config.token)

    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(time, "time", lambda: DELETE_AFTER + 1)
    with caplog.at_level(logging.INFO):
        assert service.run_service(config, logging.getLogger("service-test")) == 0
    assert len(calls) == 1 and calls[0][-2:] == ["-m", "auction.worker"]
    assert config.token not in caplog.text and "example.invalid" not in caplog.text
    assert "[redacted]" in caplog.text and "[url]" in caplog.text


@pytest.mark.parametrize(
    "url",
    [
        "http://name.example",
        "https://name.example:bad",
        "https://[broken",
        "https://user:password@name.example",
        "https://name.example?secret=value",
    ],
)
def test_public_probe_rejects_invalid_origins_without_network(url: str) -> None:
    """Reject invalid schemes, ports, credentials and queries before resolving a hostname."""
    assert not verify_public_url(url, quick_only=False)
