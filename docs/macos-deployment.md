# Deployment on macOS

The native service uses Gunicorn, the notification worker, and an optional
Cloudflare Tunnel. A per-user launch agent supervises the stack independently
of Terminal. Docker is not required for this deployment method.

## Prerequisites

- Python 3.11 or newer, Git and `cloudflared`.
- A Telegram bot token and the organizer's numeric Telegram ID.
- A macOS user session with network access. The agent starts when that user logs in.

For predictable source identity, deploy an annotated release tag into a separate
directory. Do not edit or overwrite the directory of a running release.

## Prepare a release

Run these commands from a clone containing the release tag:

```sh
git fetch --tags
AUCTION_DEPLOY_ROOT="$HOME/Library/Application Support/Friends Auction"
AUCTION_RELEASE_DIR="$AUCTION_DEPLOY_ROOT/releases/v1.2.0"
mkdir -p "$AUCTION_RELEASE_DIR" "$AUCTION_DEPLOY_ROOT/bin"
chmod 700 "$AUCTION_DEPLOY_ROOT"
git archive v1.2.0 | tar -x -C "$AUCTION_RELEASE_DIR"
python3.12 -m venv "$AUCTION_RELEASE_DIR/.venv"
"$AUCTION_RELEASE_DIR/.venv/bin/python" -m pip install -r "$AUCTION_RELEASE_DIR/requirements.txt"
install -m 700 "$(command -v cloudflared)" "$AUCTION_DEPLOY_ROOT/bin/cloudflared"
cp "$AUCTION_RELEASE_DIR/docs/service-config.example.json" "$AUCTION_DEPLOY_ROOT/config.json"
chmod 600 "$AUCTION_DEPLOY_ROOT/config.json"
```

Replace `python3.12` if another supported version is installed. Fill in `token`
and `admin_id` in the private `config.json`; never commit or share that file.
The optional `data_dir` and `cloudflared` fields override the default sibling
directories `data/` and `bin/cloudflared`. All configured paths must be absolute.

The data directory contains `auction.sqlite3` and `photos/`. For an existing
auction, stop its source web server and worker, preserve a backup, then migrate
the complete database and its registered photographs before starting this
service. SQLite's backup API provides a consistent database snapshot. Copying
only the database or only the photographs is insufficient. Existing accounts,
approval decisions, bids and subscriptions remain valid after migration.

## Start and control the service

```sh
"$AUCTION_RELEASE_DIR/.venv/bin/python" "$AUCTION_RELEASE_DIR/scripts/macos_service.py" install --config "$AUCTION_DEPLOY_ROOT/config.json"
"$AUCTION_RELEASE_DIR/.venv/bin/python" "$AUCTION_RELEASE_DIR/scripts/macos_service.py" status --config "$AUCTION_DEPLOY_ROOT/config.json"
```

The installer creates `~/Library/LaunchAgents/local.friends-auction.server.plist`
with file paths but no credentials. Unsuccessful service exits restart after
a 15-second throttle. The supervisor stops the entire stack before restarting
it, so a stale worker cannot continue polling the same bot. Successful cleanup
at the end of retention stops the service without a restart loop.

Use the same command with `stop`, `start` or `restart` to control an installed
agent. Stop the integration environment before enabling this service's bot
polling. The service binds only to `127.0.0.1`, disables demo and trial modes,
and records readiness in `$AUCTION_DEPLOY_ROOT/status.json`.

`GET /api/version` reports the packaged semantic version. Private rotating logs
are stored in `$AUCTION_DEPLOY_ROOT/logs/`; child output has tokens and HTTP URLs
redacted. The launch agent also captures otherwise unhandled startup output.
Backups and retired deployment directories remain the operator's responsibility
under the data retention policy.

Version 1.2.0 adds a validated `source_commit` field. Populate the private
release's `auction/build-info.json` with `version` and the full source commit
when preparing it; a missing commit is reported as null. Container builds accept
the public `AUCTION_SOURCE_COMMIT` build argument through Compose. Do not put
secrets in build metadata or arguments. These declared values support diagnosis,
not remote attestation.

The supervisor checks HTTP readiness every 30 seconds and restarts after three
consecutive failures. A worker heartbeat detects stalled background processing,
with startup and cleanup grace periods. Graceful shutdown permits in-flight
requests up to 30 seconds before escalation; the launch agent uses a 45-second
exit timeout. `restart` waits for the old registration to disappear and retries
only recognized transient bootstrap failures.

Before switching release directories, stop the agent, create and verify a
[managed storage snapshot](data-retention.md), and preserve the old plist. Write
the new release-specific plist while the service is stopped, then bootstrap it
and check packaged identity, HTTP readiness and fresh worker progress. Never
overwrite the source or virtual environment of a running release. Existing
1.1.0 plists must be regenerated to receive the new shutdown timeout.

## HTTPS access and availability

With an empty `public_url`, the supervisor creates a Quick Tunnel and updates
the Telegram menu and `/start` replies to its current HTTPS URL. Reconnection
can change that URL; participants should open the application through the bot.
Cloudflare documents Quick Tunnels as temporary development endpoints with no
uptime guarantee: [Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/).

For a stable origin, set `public_url` to a root HTTPS URL. Provide `tunnel_config`
as an absolute path to a configured managed tunnel file, or manage the HTTPS
reverse proxy separately. The supervisor checks public readiness before enabling
bot polling.

The default launch agent uses `caffeinate -s` to prevent system sleep while the
Mac is on AC power. It does not change power settings or prevent lid-triggered
sleep. Network loss, shutdown, logout and sleep interrupt availability. The
service resumes after the next login. Pass `--allow-sleep` during installation
to omit the power assertion.
