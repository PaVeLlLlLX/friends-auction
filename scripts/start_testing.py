#!/usr/bin/env python3
"""Run a Telegram integration environment until interrupted."""

import http.client
import json
import os
import queue
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from auction.app import DELETE_AFTER
from auction.contracts import JSONObject, JSONValue
from auction.i18n import translate
from auction.telegram import TelegramError, call

CONFIG = ROOT / "data" / "testing-config.json"
STATUS = ROOT / "data" / "testing-status.json"
PORT = 8090


def free_port(port: int) -> bool:
    """Return whether localhost can currently bind the requested TCP port.

    This is an early diagnostic only; the server must still handle a bind race
    after this probe closes its socket.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def read_url(
    process: subprocess.Popen[str], messages: queue.Queue[str], diagnostics: deque[str]
) -> None:
    """Read a tunnel's stderr, queue its public URL and retain diagnostic lines.

    The caller must start the process with a text-mode stderr pipe. The bounded
    deque is consumed after the reader thread stops to avoid exposing raw logs.
    """
    if process.stderr is None:
        raise ValueError("Tunnel URL discovery requires a stderr pipe.")
    for line in iter(process.stderr.readline, ""):
        if " ERR " in line or "Registered tunnel connection" in line:
            diagnostics.append(line.strip())
        match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if match:
            messages.put(match.group(0))


def read_diagnostics(process: subprocess.Popen[str], diagnostics: deque[str]) -> None:
    """Drain a text stderr pipe into the caller's bounded startup-log buffer."""
    if process.stderr is None:
        raise ValueError("Process diagnostics require a stderr pipe.")
    for line in iter(process.stderr.readline, ""):
        diagnostics.append(line.rstrip())


def save_diagnostics(diagnostics: Mapping[str, Iterable[str]], token: str) -> str:
    """Write supplied diagnostic tails to a private log and return its path.

    The exact bot token and HTTP(S) URLs are redacted before writing. The caller
    supplies bounded tails; the new file uses ``0600`` permissions and remains
    outside Git in the integration data directory.
    """
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="testing-launch-",
        suffix=".log",
        dir=CONFIG.parent,
        delete=False,
    ) as log:
        for component, lines in diagnostics.items():
            for line in lines:
                line = line.replace(token, "[redacted]") if token else line
                line = re.sub(r"https?://\S+", "[url]", line)
                log.write(f"[{component}] {line}\n")
        return str(log.name)


def stop(processes: Sequence[subprocess.Popen[str]]) -> None:
    """Stop child processes in reverse startup order, escalating after five seconds.

    Already exited children are reaped without being signalled. A child that
    ignores termination is killed, then given another bounded wait to exit.
    """
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
    for process in reversed(processes):
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def verify_public_url(url: str) -> bool:
    """Check that an HTTPS Quick Tunnel serves the expected catalog response.

    DNS-over-HTTPS avoids caching a newly issued but unready name in the local
    resolver. TLS still verifies the tunnel hostname. If that DNS request fails,
    the system resolver is tried instead; failed probes return ``False``.
    """
    host = urllib.parse.urlsplit(url).hostname
    if not host or not re.fullmatch(r"[a-z0-9-]+\.trycloudflare\.com", host):
        return False
    query = "https://cloudflare-dns.com/dns-query?" + urllib.parse.urlencode(
        {"name": host, "type": "A"}
    )
    request = urllib.request.Request(query, headers={"Accept": "application/dns-json"})
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            dns = cast(JSONValue, json.load(response))
        if not isinstance(dns, dict):
            raise ValueError("DNS response must be a JSON object.")
        answers = dns.get("Answer", [])
        if not isinstance(answers, list):
            raise ValueError("DNS answers must be a JSON list.")
        addresses: list[str] = []
        for answer in answers:
            if not isinstance(answer, dict):
                continue
            address = answer.get("data")
            if answer.get("type") == 1 and isinstance(address, str):
                addresses.append(address)
        for address in addresses:
            context = ssl.create_default_context()
            connection = http.client.HTTPSConnection(host, timeout=6, context=context)
            try:
                raw = socket.create_connection((address, 443), timeout=6)
                connection.sock = context.wrap_socket(raw, server_hostname=host)
                connection.request("GET", "/api/lots", headers={"Host": host})
                response = connection.getresponse()
                document = cast(JSONValue, json.load(response))
                if (
                    response.status == 200
                    and isinstance(document, dict)
                    and isinstance(document.get("lots"), list)
                ):
                    return True
            except (OSError, ValueError, http.client.HTTPException):
                pass
            finally:
                connection.close()
    except (OSError, urllib.error.URLError, ValueError, KeyError):
        # If DNS-over-HTTPS itself is unavailable, use the system resolver.
        try:
            with urllib.request.urlopen(url + "/api/lots", timeout=6) as response:
                document = cast(JSONValue, json.load(response))
                return (
                    response.status == 200
                    and isinstance(document, dict)
                    and isinstance(document.get("lots"), list)
                )
        except (OSError, urllib.error.URLError, ValueError):
            return False
    return False


def cleanup_expired_environment(env: Mapping[str, str], token: str) -> int:
    """Run only the expired integration environment's retention worker.

    ``env`` selects the isolated database and uploads. Return zero on successful
    cleanup, otherwise return one and save a private sanitized diagnostic tail
    when available. No public server, tunnel or Telegram polling is started.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "auction.worker"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        print(translate("launcher_cleanup_failed"))
        return 1
    if result.returncode != 0:
        print(translate("launcher_cleanup_failed"))
        if result.stderr:
            try:
                tail = deque(result.stderr.splitlines(), maxlen=20)
                print(translate("launcher_log_saved", save_diagnostics({"worker": tail}, token)))
            except OSError:
                print(translate("launcher_log_failed"))
        return 1
    print(translate("launcher_retention_completed"))
    return 0


def main() -> int:
    """Start and supervise the isolated Telegram integration environment.

    Load and validate private configuration before launching child processes.
    Expired environments run cleanup only. For an active environment, configure
    the public bot menu after readiness checks and stop all children on exit.
    Return zero on interruption or successful cleanup and one on startup failure.
    """
    os.umask(0o077)
    if not CONFIG.is_file():
        print(translate("launcher_configuration_missing"))
        return 1
    tunnel_bin = ROOT / ".tools" / "cloudflared"
    server_bin = ROOT / ".venv" / "bin" / "gunicorn"
    try:
        with CONFIG.open(encoding="utf-8") as file:
            config = cast(JSONValue, json.load(file))
        if not isinstance(config, dict):
            raise ValueError("Integration configuration must be a JSON object.")
        token, admin_id, username = config["token"], config["admin_id"], config["username"]
        if (
            not isinstance(token, str)
            or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token)
            or type(admin_id) is not int
            or admin_id < 1
            or not isinstance(username, str)
            or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username)
        ):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        print(translate("launcher_configuration_invalid"))
        return 1
    env = os.environ.copy()
    env.update(
        {
            "AUCTION_DEMO": "0",
            "AUCTION_TRIAL": "1",
            "TELEGRAM_BOT_TOKEN": token,
            "TELEGRAM_ADMIN_ID": str(admin_id),
            "AUCTION_DB": str(ROOT / "data" / "testing.sqlite3"),
            "AUCTION_UPLOADS": str(ROOT / "data" / "testing-photos"),
        }
    )
    if time.time() >= DELETE_AFTER:
        return cleanup_expired_environment(env, token)
    for binary in (tunnel_bin, server_bin):
        if not binary.is_file() or not os.access(binary, os.X_OK):
            print(translate("launcher_missing_binary", binary.relative_to(ROOT)))
            return 1
    if not free_port(PORT):
        print(translate("launcher_port_in_use", PORT))
        return 1
    STATUS.unlink(missing_ok=True)
    processes: list[subprocess.Popen[str]] = []
    readers: list[threading.Thread] = []
    diagnostics: dict[str, deque[str]] = {
        name: deque(maxlen=20) for name in ("server", "tunnel", "worker")
    }
    status_written = False
    failed_run = False
    try:
        print(translate("launcher_starting"), flush=True)
        server = subprocess.Popen(
            [
                str(server_bin),
                "--bind",
                f"127.0.0.1:{PORT}",
                "--workers",
                "1",
                "--threads",
                "4",
                "run:app",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(server)
        reader = threading.Thread(
            target=read_diagnostics, args=(server, diagnostics["server"]), daemon=True
        )
        reader.start()
        readers.append(reader)
        local_url = f"http://127.0.0.1:{PORT}/api/lots"
        ready = False
        for _ in range(40):
            if server.poll() is not None:
                raise RuntimeError(translate("launcher_server_failed"))
            try:
                with urllib.request.urlopen(local_url, timeout=1) as response:
                    ready = response.status == 200
                if ready:
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(0.25)
        if not ready:
            raise RuntimeError(translate("launcher_server_timeout"))
        print(translate("launcher_creating_tunnel"), flush=True)
        tunnel = subprocess.Popen(
            [
                str(tunnel_bin),
                "tunnel",
                "--no-autoupdate",
                "--protocol",
                "http2",
                "--url",
                f"http://127.0.0.1:{PORT}",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        processes.append(tunnel)
        messages: queue.Queue[str] = queue.Queue()
        reader = threading.Thread(
            target=read_url, args=(tunnel, messages, diagnostics["tunnel"]), daemon=True
        )
        reader.start()
        readers.append(reader)
        try:
            url = messages.get(timeout=60)
        except queue.Empty:
            raise RuntimeError(translate("launcher_tunnel_timeout"))
        print(translate("launcher_tunnel_ready", url), flush=True)
        reached = False
        for _ in range(15):
            reached = verify_public_url(url)
            if reached:
                break
            time.sleep(2)
        if not reached:
            raise RuntimeError(translate("launcher_public_endpoint_failed"))
        print(translate("launcher_configuring_menu"), flush=True)
        try:
            call(
                token,
                "setChatMenuButton",
                {
                    "menu_button": {
                        "type": "web_app",
                        "text": translate("launcher_menu_button"),
                        "web_app": {"url": url},
                    }
                },
            )
        except TelegramError:
            raise RuntimeError(translate("launcher_menu_failed")) from None
        print(translate("launcher_starting_worker"), flush=True)
        worker_env = dict(env, AUCTION_BOT_POLL="1", AUCTION_APP_URL=url)
        worker = subprocess.Popen(
            [str(ROOT / ".venv" / "bin" / "python"), "-m", "auction.worker"],
            cwd=ROOT,
            env=worker_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(worker)
        reader = threading.Thread(
            target=read_diagnostics, args=(worker, diagnostics["worker"]), daemon=True
        )
        reader.start()
        readers.append(reader)
        time.sleep(0.5)
        if worker.poll() is not None:
            if worker.returncode == 0 and time.time() >= DELETE_AFTER:
                print(translate("launcher_retention_completed"))
                return 0
            raise RuntimeError(translate("launcher_worker_failed"))
        fd = os.open(STATUS, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as status:
            status_document: JSONObject = {"pid": os.getpid(), "bot_username": username, "url": url}
            json.dump(status_document, status)
        status_written = True
        print(translate("launcher_ready"))
        print(translate("launcher_bot_url", username))
        print(translate("launcher_open_instructions"))
        print(translate("launcher_catalog_access"))
        print(translate("launcher_approval_required"))
        print(translate("launcher_storage_info"))
        print(translate("launcher_process_lifetime"), flush=True)
        while True:
            failed = next((p for p in processes if p.poll() is not None), None)
            if failed:
                if failed is worker and worker.returncode == 0 and time.time() >= DELETE_AFTER:
                    print(translate("launcher_retention_completed"))
                    return 0
                raise RuntimeError(translate("launcher_component_stopped"))
            time.sleep(2)
    except (RuntimeError, OSError) as error:
        failed_run = True
        print(
            "\n"
            + (
                str(error)
                if isinstance(error, RuntimeError)
                else translate("launcher_process_error")
            )
        )
        return 1
    except KeyboardInterrupt:
        print(translate("launcher_stopping"))
        return 0
    finally:
        if status_written:
            STATUS.unlink(missing_ok=True)
        stop(processes)
        for reader in readers:
            reader.join(timeout=1)
        if failed_run and any(diagnostics.values()):
            try:
                print(translate("launcher_log_saved", save_diagnostics(diagnostics, token)))
            except OSError:
                print(translate("launcher_log_failed"))


if __name__ == "__main__":
    raise SystemExit(main())
