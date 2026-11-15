#!/usr/bin/env python3
"""Install and control the per-user macOS launch agent for a prepared release."""

import argparse
import json
import os
import plistlib
import subprocess
import sys
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
        "ExitTimeOut": 30,
        "ProcessType": "Background",
        "Umask": 0o077,
        "StandardOutPath": str(logs / "launchd-output.log"),
        "StandardErrorPath": str(logs / "launchd-error.log"),
    }


def control(action: str, plist: Path) -> int:
    """Perform a scoped launchctl action; report its exit code without touching other agents."""
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LABEL}"
    if action in ("stop", "restart"):
        stopped = subprocess.run(["launchctl", "bootout", target], check=False)
        if action == "stop":
            return stopped.returncode
    started = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=False)
    return started.returncode


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
