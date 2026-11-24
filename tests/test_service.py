"""Offline checks for private supervision, shutdown and macOS startup configuration."""

import io
import json
import logging
import plistlib
import subprocess
import time
import urllib.request
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from auction import __version__, service
from auction.app import DELETE_AFTER, connect
from auction.processes import stop, verify_public_url
from scripts import macos_service
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
    assert environment["AUCTION_BACKUPS"] == str(tmp_path / "backups")


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
    assert decoded["ExitTimeOut"] >= 40


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


def test_worker_probe_is_read_only_and_checks_freshness(tmp_path: Path) -> None:
    """Do not create missing storage; reject missing, old and future heartbeats."""
    database = tmp_path / "worker.sqlite3"
    assert not service.worker_recent(database, now=1000)
    assert not database.exists()
    with closing(connect(database)) as db:
        db.execute("CREATE TABLE jobs(name TEXT PRIMARY KEY, completed REAL)")
        assert not service.worker_recent(database, now=1000)
        for completed, expected in ((990, True), (1001, True), (1100, False), (700, False)):
            db.execute("INSERT OR REPLACE INTO jobs VALUES('worker_heartbeat',?)", (completed,))
            assert service.worker_recent(database, now=1000) is expected


@pytest.mark.parametrize("mode", ("healthy", "demo", "oversized", "malformed"))
def test_local_health_probe_validates_mode_and_bounds_response(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject invalid or oversized readiness documents without trusting an arbitrary listener."""
    documents = [json.dumps({"version": __version__}).encode()]
    profile = json.dumps({"trial": False, "demo": mode == "demo"}).encode()
    documents.append(
        b"x" * 20_000 if mode == "oversized" else b"{" if mode == "malformed" else profile
    )
    calls: list[str] = []

    @contextmanager
    def open_local(url: str, *, timeout: float) -> Iterator[io.BytesIO]:
        """Provide finite synthetic JSON responses and verify the socket deadline."""
        calls.append(url)
        assert timeout == 1
        yield io.BytesIO(documents[len(calls) - 1])

    monkeypatch.setattr(urllib.request, "urlopen", open_local)
    assert service.local_ready(8081) is (mode == "healthy")
    assert calls == ["http://127.0.0.1:8081/api/version", "http://127.0.0.1:8081/api/me"]


def test_health_monitor_recovers_and_requires_consecutive_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ignore one transient failure and restart only after three successive bad probes."""
    config = service.ServiceConfig("fake-test-token", 1, tmp_path / "data", Path("/cloudflared"))
    web_checks = iter((False, True, False, False, False))
    calls: list[int] = []

    def ready(port: int) -> bool:
        """Return controlled probe results without opening a loopback connection."""
        calls.append(port)
        return next(web_checks)

    monkeypatch.setattr(service, "local_ready", ready)
    monkeypatch.setattr(service, "worker_recent", lambda path: True)
    monitor = service.HealthMonitor(0, 30)
    logger = logging.getLogger("health-test")
    monitor.check(config, logger, now=29)
    assert calls == []
    for now, failures in ((30, 1), (60, 0), (90, 1), (120, 2)):
        monitor.check(config, logger, now=now)
        assert monitor.failures == failures
    with pytest.raises(RuntimeError, match="Repeated runtime health"):
        monitor.check(config, logger, now=150)
    assert len(calls) == 5


def test_health_monitor_waits_for_first_worker_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Allow bounded startup grace, then count a live but unresponsive worker as unhealthy."""
    config = service.ServiceConfig("fake-test-token", 1, tmp_path / "data", Path("/cloudflared"))
    monkeypatch.setattr(service, "local_ready", lambda port: True)
    monkeypatch.setattr(service, "worker_recent", lambda path: False)
    monitor = service.HealthMonitor(0, 30)
    logger = logging.getLogger("health-startup-test")
    for now in (30, 60, 90):
        monitor.check(config, logger, now=now)
        assert monitor.failures == 0
    monitor.check(config, logger, now=120)
    assert monitor.failures == 1
    monitor.check(config, logger, now=150)
    with pytest.raises(RuntimeError, match="Repeated runtime health"):
        monitor.check(config, logger, now=180)


class ShutdownProcess(subprocess.Popen[str]):
    """Represent a child deterministically without constructing an operating-system process."""

    def __init__(self, actions: list[str], *, stubborn: bool = False) -> None:
        """Retain one shutdown trace and whether SIGTERM must time out."""
        self.actions = actions
        self.stubborn = stubborn
        self.returncode = None
        self.killed = False

    def poll(self) -> int | None:
        """Return the simulated liveness state."""
        return self.returncode

    def terminate(self) -> None:
        """Record delivery of graceful shutdown without signalling an actual process."""
        self.actions.append("terminate")

    def kill(self) -> None:
        """Record escalation after the shared grace period."""
        self.actions.append("kill")
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        """Time out once for a stubborn child, otherwise finish its simulated shutdown."""
        self.actions.append("wait")
        if self.stubborn and not self.killed:
            assert timeout is not None
            raise subprocess.TimeoutExpired("synthetic-child", timeout)
        self.returncode = 0
        return 0


def test_shutdown_signals_all_children_before_waiting_and_escalates() -> None:
    """Give accepted requests a graceful deadline while eventually killing a stuck child."""
    actions: list[str] = []
    server = ShutdownProcess(actions)
    worker = ShutdownProcess(actions, stubborn=True)
    stop([server, worker], grace_period=0)
    assert actions[:2] == ["terminate", "terminate"]
    assert worker.killed and not server.killed
    assert server.returncode == worker.returncode == 0


def test_restart_waits_for_absence_then_retries_only_transient_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not bootstrap until print confirms teardown, and tolerate one known busy failure."""
    results = iter(
        (
            (0, ""),
            (0, "registered"),
            (113, "Could not find service"),
            (5, "Bootstrap failed: 5: Input/output error"),
            (113, "Could not find service"),
            (0, ""),
        )
    )
    commands: list[list[str]] = []

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        """Consume fake launchctl replies and record exact command ordering."""
        commands.append(command)
        code, error = next(results)
        assert options["timeout"] == 10
        return subprocess.CompletedProcess(command, code, stdout="", stderr=error)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(time, "sleep", lambda delay: None)
    assert macos_service.control("restart", tmp_path / "agent.plist") == 0
    assert [command[1] for command in commands] == [
        "bootout",
        "print",
        "print",
        "bootstrap",
        "print",
        "bootstrap",
    ]


def test_bootstrap_permission_error_is_preserved_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Retain a real bootstrap failure rather than disguising it with unconditional retries."""
    commands: list[list[str]] = []

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        """Return a non-transient permissions error without invoking launchctl."""
        commands.append(command)
        return subprocess.CompletedProcess(command, 77, stdout="", stderr="Permission denied")

    monkeypatch.setattr(subprocess, "run", run)
    assert macos_service.control("start", tmp_path / "agent.plist") == 77
    assert len(commands) == 1
    assert "Permission denied" in capsys.readouterr().err


def test_restart_timeout_never_bootstraps_over_registered_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bound the teardown wait and refuse to start a second still-registered deployment."""
    commands: list[list[str]] = []
    clock = [0.0]

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        """Keep the original agent registered after a nominally successful bootout."""
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="registered", stderr="")

    def advance(delay: float) -> None:
        """Move virtual time without delaying the test suite."""
        clock[0] += delay

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", advance)
    assert macos_service.control("restart", tmp_path / "agent.plist") == 1
    assert clock[0] == 20
    assert not any(command[1] == "bootstrap" for command in commands)
