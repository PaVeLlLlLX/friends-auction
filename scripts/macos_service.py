#!/usr/bin/env python3
"""Install and control the per-user macOS launch agent for a prepared release."""

import argparse
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from auction.contracts import JSONObject
from auction.service import load_config, local_ready

LABEL = "local.friends-auction.server"


def launch_agent(release_dir: Path, config_path: Path, *, allow_sleep: bool = False) -> JSONObject:
    """Describe a release-specific user agent without putting credentials in its plist.

    Unsuccessful exits restart after a bounded throttle; successful retention
    cleanup stays stopped. AC-powered sleep prevention lasts only while this
    service runs, and does not modify system-wide power settings.
    """
    arguments = [
        str(release_dir / ".venv/bin/python"),
        "-m",
        "auction.service",
        "--config",
        str(config_path),
    ]
    if not allow_sleep:
        arguments = ["/usr/bin/caffeinate", "-s", *arguments]
    logs = config_path.parent / "logs"
    return {
        "Label": LABEL,
        "ProgramArguments": [argument for argument in arguments],
        "WorkingDirectory": str(release_dir),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 15,
        "ExitTimeOut": 45,
        "ProcessType": "Background",
        "Umask": 0o077,
        "StandardOutPath": str(logs / "launchd-output.log"),
        "StandardErrorPath": str(logs / "launchd-error.log"),
    }


def launchctl(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one bounded launchctl command and retain its original diagnostic text."""
    return subprocess.run(
        ["launchctl", *arguments], capture_output=True, text=True, check=False, timeout=10
    )


def show_failure(result: subprocess.CompletedProcess[str]) -> int:
    """Print original command diagnostics and return its failure code."""
    for output in (result.stdout, result.stderr):
        if output:
            print(output.rstrip(), file=sys.stderr)
    return result.returncode


def agent_present(target: str) -> bool | None:
    """Distinguish a registered agent from a positively identified missing service.

    Unexpected print failures remain unknown and retain their diagnostics; a
    permission or domain error must not be mistaken for completed teardown.
    """
    result = launchctl(["print", target])
    if result.returncode == 0:
        return True
    diagnostic = (result.stdout + result.stderr).lower()
    if "could not find service" in diagnostic or "no such process" in diagnostic:
        return False
    show_failure(result)
    return None


def wait_absent(target: str, *, timeout: float = 20) -> bool:
    """Wait for launchd registration to disappear, with a bounded poll interval."""
    deadline = time.monotonic() + timeout
    while True:
        present = agent_present(target)
        if present is False:
            return True
        if present is None:
            return False
        if time.monotonic() >= deadline:
            print("The previous launch agent is still registered after shutdown.", file=sys.stderr)
            return False
        time.sleep(0.25)


def control(action: str, plist: Path) -> int:
    """Control one agent, waiting for teardown before bounded transient bootstrap retries.

    Missing agents are safe to restart. Other bootout errors and non-transient
    bootstrap errors are preserved. No unrelated launchd jobs are touched.
    """
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LABEL}"
    try:
        if action in ("stop", "restart"):
            stopped = launchctl(["bootout", target])
            if stopped.returncode != 0 and agent_present(target) is not False:
                return show_failure(stopped)
            if not wait_absent(target):
                return stopped.returncode or 1
            if action == "stop":
                return 0
        for attempt in range(3):
            started = launchctl(["bootstrap", domain, str(plist)])
            if started.returncode == 0:
                return 0
            # launchd may briefly retain internal teardown state even after print
            # reports absence. Retry only its known transient I/O/busy failure.
            diagnostic = (started.stdout + started.stderr).lower()
            transient = started.returncode in (5, 16) and (
                "input/output error" in diagnostic or "resource busy" in diagnostic
            )
            if not transient or attempt == 2 or agent_present(target) is not False:
                return show_failure(started)
            time.sleep(0.5 * (attempt + 1))
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"Unable to control the launch agent: {error}", file=sys.stderr)
        return 1
    return 1


def main() -> int:
    """Validate a private deployment, write its agent or control an existing installation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "start", "stop", "restart", "status"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--release-dir", type=Path, default=ROOT)
    parser.add_argument("--allow-sleep", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("The launch agent requires macOS.")
    os.umask(0o077)
    config_path = args.config.resolve()
    try:
        config = load_config(config_path)
    except (OSError, ValueError):
        parser.error("Invalid or inaccessible private service configuration.")
    plist = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    if args.action == "status":
        status = config.data_dir.parent / "status.json"
        if not status.is_file() or not local_ready(config.port):
            print("Service is stopped or not ready.")
            return 1
        print(json.dumps(json.loads(status.read_text()), indent=2))
        return 0
    if args.action == "install":
        release = args.release_dir.resolve()
        if (
            not (release / ".venv/bin/python").is_file()
            or not (release / "auction/service.py").is_file()
        ):
            parser.error("Prepare the release and its virtual environment before installation.")
        (config_path.parent / "logs").mkdir(parents=True, mode=0o700, exist_ok=True)
        plist.parent.mkdir(parents=True, exist_ok=True)
        if plist.exists():
            parser.error(
                "An agent already exists; stop it and preserve its plist before replacement."
            )
        plist.write_bytes(
            plistlib.dumps(launch_agent(release, config_path, allow_sleep=args.allow_sleep))
        )
        plist.chmod(0o600)
        return control("start", plist)
    return control(args.action, plist)


if __name__ == "__main__":
    raise SystemExit(main())
