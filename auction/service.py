"""Supervise a non-demo web server, HTTPS tunnel and notification worker."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import math
import os
import queue
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import FrameType
from typing import NoReturn, cast

from auction import __version__
from auction.app import DELETE_AFTER
from auction.contracts import JSONValue
from auction.i18n import translate
from auction.processes import free_port, stop, verify_public_url
from auction.telegram import call

ROOT = Path(__file__).resolve().parent.parent
HEALTH_INTERVAL = 30.0
HEALTH_FAILURE_LIMIT = 3
PUBLIC_HEALTH_INTERVAL = 60.0
WORKER_HEARTBEAT_MAX_AGE = 180.0
WORKER_STARTUP_GRACE = 120.0


@dataclass(frozen=True)
class ServiceConfig:
    """Private credentials and local paths for one independently stored deployment."""

    token: str = field(repr=False)
    admin_id: int
    data_dir: Path
    cloudflared: Path
    port: int = 8081
    public_url: str = ""
    tunnel_config: Path | None = None


def load_config(path: Path) -> ServiceConfig:
    """Read an owner-only JSON configuration; reject invalid credentials or paths.

    Optional fixed HTTPS URLs require a root origin without query parameters.
    A managed tunnel additionally requires its private cloudflared configuration.
    No network requests or storage initialization occur during parsing.
    """
    path = path.resolve()
    if path.stat().st_mode & 0o077:
        raise ValueError("Service configuration must have owner-only permissions.")
    document: JSONValue = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Service configuration must be a JSON object.")
    token, admin = document.get("token"), document.get("admin_id")
    data = document.get("data_dir", str(path.parent / "data"))
    tunnel = document.get("cloudflared", str(path.parent / "bin/cloudflared"))
    port = document.get("port", 8081)
    public = document.get("public_url", "")
    tunnel_config = document.get("tunnel_config")
    if (
        not isinstance(token, str)
        or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token)
        or type(admin) is not int
        or not 0 < admin <= 2**63 - 1
        or not isinstance(data, str)
        or not Path(data).is_absolute()
        or not isinstance(tunnel, str)
        or not Path(tunnel).is_absolute()
        or type(port) is not int
        or not 1024 <= port <= 65535
        or not isinstance(public, str)
        or (tunnel_config is not None and not isinstance(tunnel_config, str))
    ):
        raise ValueError("Invalid service configuration fields.")
    if public:
        parsed = urllib.parse.urlsplit(public)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("The public URL must be an HTTPS root origin.")
    if tunnel_config is not None and (not public or not Path(tunnel_config).is_absolute()):
        raise ValueError("A managed tunnel requires an absolute configuration path and HTTPS URL.")
    return ServiceConfig(
        token=token,
        admin_id=admin,
        data_dir=Path(data),
        cloudflared=Path(tunnel),
        port=port,
        public_url=public.rstrip("/"),
        tunnel_config=Path(tunnel_config) if tunnel_config is not None else None,
    )


def service_environment(config: ServiceConfig) -> dict[str, str]:
    """Select live storage and signed authorization, ignoring inherited demo/trial flags."""
    return dict(
        os.environ,
        AUCTION_DEMO="0",
        AUCTION_TRIAL="0",
        AUCTION_CLOUDFLARE_PROXY="1",
        AUCTION_BOT_POLL="0",
        AUCTION_APP_URL="",
        AUCTION_DB=str(config.data_dir / "auction.sqlite3"),
        AUCTION_UPLOADS=str(config.data_dir / "photos"),
        AUCTION_BACKUPS=str(config.data_dir.parent / "backups"),
        TELEGRAM_BOT_TOKEN=config.token,
        TELEGRAM_ADMIN_ID=str(config.admin_id),
    )


def read_output(
    process: subprocess.Popen[str],
    name: str,
    logger: logging.Logger,
    token: str,
    urls: queue.Queue[str] | None = None,
) -> None:
    """Drain child output into bounded private logs, redacting tokens and HTTP URLs.

    The optional queue receives Quick Tunnel addresses before log redaction.
    The caller owns the pipe and joins this reader after stopping the child.
    """
    if process.stdout is None:
        raise ValueError("Service output capture requires a text pipe.")
    for line in process.stdout:
        if urls is not None:
            match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if match:
                urls.put(match.group(0))
        safe = re.sub(r"https?://\S+", "[url]", line.replace(token, "[redacted]"))
        logger.info("%s: %s", name, safe.rstrip())


def local_ready(port: int) -> bool:
    """Check bounded loopback responses for this version and signed-identity mode.

    Each of two local requests has a one-second socket timeout and a 16 KiB
    response cap. Failures return False without exposing response contents.
    """
    try:
        documents: list[JSONValue] = []
        for endpoint in ("version", "me"):
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/{endpoint}", timeout=1
            ) as response:
                body = response.read(16_385)
                if len(body) > 16_384:
                    return False
                documents.append(cast(JSONValue, json.loads(body)))
        version, profile = documents
        return (
            isinstance(version, dict)
            and version.get("version") == __version__
            and isinstance(profile, dict)
            and profile.get("trial") is False
            and profile.get("demo") is False
        )
    except (OSError, ValueError, urllib.error.URLError):
        return False


def worker_recent(database_path: Path, *, now: float | None = None) -> bool:
    """Read a recent successful worker heartbeat without creating or mutating storage.

    Missing, locked or malformed storage is unhealthy. A bounded read-only
    connection prevents a health check from holding up the supervisor indefinitely.
    Small forward clock skew is tolerated; timestamps far in the future are rejected.
    """
    now = time.time() if now is None else now
    try:
        with closing(
            sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        ) as db:
            row = db.execute("SELECT completed FROM jobs WHERE name='worker_heartbeat'").fetchone()
        if row is None or not isinstance(row[0], (int, float)):
            return False
        completed = float(row[0])
        return math.isfinite(completed) and -30 <= now - completed <= WORKER_HEARTBEAT_MAX_AGE
    except (OSError, sqlite3.Error):
        return False


@dataclass
class HealthMonitor:
    """Track consecutive bounded health failures independently of process liveness."""

    worker_started: float
    next_probe: float
    failures: int = 0
    public_url: str = ""
    next_public_probe: float = 0
    public_failures: int = 0

    def check(self, config: ServiceConfig, logger: logging.Logger, *, now: float) -> None:
        """Probe at most once per interval; raise after three consecutive failed probes.

        A newly launched worker has a grace period to complete its first batch.
        Local and public failure streaks reset independently on recovery. Public
        HTTPS is checked once per minute while local HTTP is ready; a live but
        disconnected tunnel must not remain healthy solely because its PID exists.
        The caller restarts the stack after this method raises, preserving graceful
        shutdown and refreshing the bot's URL during startup.
        """
        if now < self.next_probe:
            return
        self.next_probe = now + HEALTH_INTERVAL
        web_ok = local_ready(config.port)
        worker_ok = now - self.worker_started < WORKER_STARTUP_GRACE or worker_recent(
            config.data_dir / "auction.sqlite3"
        )
        if web_ok and worker_ok:
            self.failures = 0
        else:
            self.failures += 1
            logger.warning(
                "Health probe failed (%s/%s, web=%s, worker=%s)",
                self.failures,
                HEALTH_FAILURE_LIMIT,
                web_ok,
                worker_ok,
            )
            if self.failures >= HEALTH_FAILURE_LIMIT:
                raise RuntimeError("Repeated runtime health failures; restarting the stack.")
        if not web_ok or not self.public_url or now < self.next_public_probe:
            return
        self.next_public_probe = now + PUBLIC_HEALTH_INTERVAL
        if verify_public_url(self.public_url, quick_only=False):
            self.public_failures = 0
            return
        self.public_failures += 1
        logger.warning(
            "Public HTTPS health probe failed (%s/%s)",
            self.public_failures,
            HEALTH_FAILURE_LIMIT,
        )
        if self.public_failures >= HEALTH_FAILURE_LIMIT:
            raise RuntimeError("Repeated public HTTPS failures; recreating the service tunnel.")


def write_status(path: Path, config: ServiceConfig, url: str) -> None:
    """Atomically publish readiness metadata without credentials or participant data."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"pid": os.getpid(), "version": __version__, "port": config.port, "url": url}),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)


def run_service(config: ServiceConfig, logger: logging.Logger) -> int:
    """Run one stack until stopped or a component fails; always stop and reap children.

    A successful retention cleanup returns zero without opening a tunnel.
    Other component failures return one so the process manager can restart the
    complete stack and refresh Telegram's menu after a Quick Tunnel URL change.
    """
    environment = service_environment(config)
    status = config.data_dir.parent / "status.json"
    processes: list[subprocess.Popen[str]] = []
    readers: list[threading.Thread] = []

    def launch(
        name: str, command: list[str], urls: queue.Queue[str] | None = None
    ) -> subprocess.Popen[str]:
        """Start a child under the release directory and immediately drain its merged output."""
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        processes.append(process)
        reader = threading.Thread(
            target=read_output, args=(process, name, logger, config.token, urls), daemon=True
        )
        reader.start()
        readers.append(reader)
        return process

    try:
        status.unlink(missing_ok=True)
        if time.time() >= DELETE_AFTER:
            worker = launch("cleanup", [sys.executable, "-m", "auction.worker"])
            return worker.wait(timeout=60)
        if not free_port(config.port):
            raise RuntimeError("The configured loopback port is already occupied.")
        server = launch(
            "web",
            [
                str(Path(sys.executable).parent / "gunicorn"),
                "--bind",
                f"127.0.0.1:{config.port}",
                "--workers",
                "1",
                "--threads",
                "4",
                "run:app",
            ],
        )
        for _ in range(40):
            if server.poll() is not None:
                raise RuntimeError("The web process exited during startup.")
            if local_ready(config.port):
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("The web process did not become ready.")
        url = config.public_url
        if not url or config.tunnel_config is not None:
            command = [str(config.cloudflared), "tunnel"]
            if config.tunnel_config is not None:
                command.extend(["--config", str(config.tunnel_config)])
            command.extend(["--no-autoupdate", "--protocol", "http2"])
            urls: queue.Queue[str] = queue.Queue()
            if config.tunnel_config is None:
                command.extend(["--url", f"http://127.0.0.1:{config.port}"])
            else:
                command.append("run")
            launch("tunnel", command, urls)
            if not url:
                url = urls.get(timeout=60)
        for _ in range(10):
            if any(process.poll() is not None for process in processes):
                raise RuntimeError("A component exited before public readiness.")
            if verify_public_url(url, quick_only=not config.public_url):
                break
            time.sleep(2)
        else:
            raise RuntimeError("The public HTTPS endpoint did not become ready.")
        call(
            config.token,
            "setChatMenuButton",
            {
                "menu_button": {
                    "type": "web_app",
                    "text": translate("launcher_menu_button"),
                    "web_app": {"url": url},
                }
            },
        )
        environment.update(AUCTION_BOT_POLL="1", AUCTION_APP_URL=url)
        worker = launch("worker", [sys.executable, "-m", "auction.worker"])
        worker_started = time.monotonic()
        health = HealthMonitor(
            worker_started,
            worker_started + HEALTH_INTERVAL,
            public_url=url,
            next_public_probe=worker_started + PUBLIC_HEALTH_INTERVAL,
        )
        time.sleep(0.5)
        if worker.poll() is not None:
            raise RuntimeError("The notification worker exited during startup.")
        write_status(status, config, url)
        logger.info("Release %s is ready on loopback port %s", __version__, config.port)
        while True:
            for process in processes:
                if process.poll() is not None:
                    if (
                        process is worker
                        and process.returncode == 0
                        and time.time() >= DELETE_AFTER
                    ):
                        return 0
                    raise RuntimeError("A service component stopped; restarting the stack.")
            # Expired APIs deliberately return 410 while the worker completes its
            # final cleanup. Do not misclassify that transition as a web failure.
            if time.time() < DELETE_AFTER:
                health.check(config, logger, now=time.monotonic())
            elif time.time() >= DELETE_AFTER + 120:
                raise RuntimeError("Retention cleanup did not complete within its grace period.")
            time.sleep(2)
    except KeyboardInterrupt:
        logger.info("Service shutdown requested")
        return 0
    except Exception as error:
        logger.error("Service failed: %s", str(error).replace(config.token, "[redacted]"))
        return 1
    finally:
        status.unlink(missing_ok=True)
        stop(processes)
        for reader in readers:
            reader.join(timeout=1)


def interrupt(signum: int, frame: FrameType | None) -> NoReturn:
    """Translate process-manager termination into the supervisor's cleanup path."""
    raise KeyboardInterrupt


def main() -> int:
    """Load private configuration, hold the deployment lock and supervise the live stack."""
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
    except (OSError, ValueError):
        print("Invalid or inaccessible service configuration.", file=sys.stderr)
        return 1
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    logs = config.data_dir.parent / "logs"
    logs.mkdir(mode=0o700, exist_ok=True)
    logger = logging.getLogger("auction.service")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(logs / "service.log", maxBytes=2_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    with (config.data_dir.parent / "service.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.error("Another supervisor already owns this deployment.")
            return 1
        return run_service(config, logger)


if __name__ == "__main__":
    raise SystemExit(main())
