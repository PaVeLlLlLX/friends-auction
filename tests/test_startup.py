import json
import os
import stat
import subprocess
import sys
from collections import deque
from contextlib import closing
from pathlib import Path

import demo
from auction.app import connect
from scripts import start_testing


def test_demo_is_isolated_and_preserves_existing_lots(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "ROOT", tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-test-token")
    monkeypatch.setenv("TELEGRAM_ADMIN_ID", "999")
    monkeypatch.setenv("AUCTION_DB", str(tmp_path / "external.sqlite3"))
    app = demo.create_demo_app()
    assert app.config["BOT_TOKEN"] == app.config["ADMIN_ID"] == ""
    assert app.config["DB"] == str(tmp_path / "data/demo.sqlite3")
    with closing(connect(app.config["DB"])) as db:
        assert db.execute("SELECT count(*) FROM lots").fetchone()[0] == 3
        db.execute("UPDATE lots SET title='Edited sample' WHERE id=1")
    again = demo.create_demo_app()
    with closing(connect(again.config["DB"])) as db:
        assert db.execute("SELECT count(*) FROM lots").fetchone()[0] == 3
        assert db.execute("SELECT title FROM lots WHERE id=1").fetchone()[0] == "Edited sample"
    assert not (tmp_path / "external.sqlite3").exists()


def test_importing_demo_does_not_create_storage(tmp_path):
    root = Path(__file__).resolve().parent.parent
    script = "import demo; print(hasattr(demo, 'app'))"
    environment = dict(
        os.environ, PYTHONPATH=str(root), AUCTION_DB=str(tmp_path / "unused.sqlite3")
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False"
    assert not list(tmp_path.iterdir())


def test_startup_logs_redact_secrets_and_are_private(tmp_path, monkeypatch):
    monkeypatch.setattr(start_testing, "CONFIG", tmp_path / "configuration.json")
    token = "123456:fake-private-token"
    diagnostics = {"server": deque([f"Failed {token} https://example.invalid/private"])}
    path = Path(start_testing.save_diagnostics(diagnostics, token))
    text = path.read_text()
    assert token not in text and "example.invalid" not in text
    assert "[redacted]" in text and "[url]" in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_launcher_retries_an_interrupted_dependency_install(tmp_path):
    binary = tmp_path / ".venv/bin/python"
    binary.parent.mkdir(parents=True)
    binary.write_text("""#!/bin/bash
if [ "$1" = -m ]; then
  count=0
  [ ! -f attempts ] || count=$(cat attempts)
  count=$((count + 1))
  printf '%s' "$count" > attempts
  [ "$count" -gt 1 ]
else
  exit 0
fi
""")
    binary.chmod(0o755)
    helper = Path(__file__).resolve().parent.parent / "scripts/messages.sh"
    command = ["bash", "-c", 'source "$1"; ensure_environment', "bash", str(helper)]
    first = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    second = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert first.returncode == 1 and second.returncode == 0
    assert (tmp_path / "attempts").read_text() == "2"


def test_expired_integration_cleans_without_starting_public_services(tmp_path, monkeypatch):
    config = tmp_path / "configuration.json"
    config.write_text(
        json.dumps({"token": "123456:" + "x" * 24, "admin_id": 1, "username": "test_bot"})
    )
    monkeypatch.setattr(start_testing, "CONFIG", config)
    monkeypatch.setattr(start_testing, "ROOT", tmp_path)
    monkeypatch.setattr(start_testing.time, "time", lambda: start_testing.DELETE_AFTER + 1)
    monkeypatch.setattr(start_testing.os, "umask", lambda mode: 0)
    calls = []

    def cleanup(command, **options):
        calls.append((command, options))
        return subprocess.CompletedProcess(command, 0, stderr="")

    def unexpected(*args, **kwargs):
        raise AssertionError("Public services must not start after retention expiry")

    monkeypatch.setattr(start_testing.subprocess, "run", cleanup)
    monkeypatch.setattr(start_testing.subprocess, "Popen", unexpected)
    assert start_testing.main() == 0
    assert len(calls) == 1 and calls[0][0][-2:] == ["-m", "auction.worker"]
    assert calls[0][1]["env"]["AUCTION_DB"] == str(tmp_path / "data/testing.sqlite3")
