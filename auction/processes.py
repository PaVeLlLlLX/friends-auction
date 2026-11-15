"""Bounded process shutdown, diagnostic readers and HTTPS tunnel readiness probes."""

import http.client
import json
import queue
import re
import socket
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Sequence
from typing import cast

from auction.contracts import JSONValue


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


def verify_public_url(url: str, *, quick_only: bool = True) -> bool:
    """Check that an HTTPS tunnel serves the expected catalog response.

    DNS-over-HTTPS avoids caching a newly issued but unready name in the local
    resolver. TLS still verifies the tunnel hostname. If that DNS request fails,
    the system resolver is tried instead; failed probes return ``False``.
    Set ``quick_only=False`` only for an operator-configured HTTPS origin.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or (quick_only and not re.fullmatch(r"[a-z0-9-]+\.trycloudflare\.com", host))
    ):
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
