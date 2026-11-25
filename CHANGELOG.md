# Changelog

## [1.2.1] - 2026-11-26

### Fixed

- Continue checking the public HTTPS catalog after startup, independently of local HTTP and worker health.
- Recreate the supervised stack and refresh the bot menu after three consecutive external failures; a transient recovered failure resets the streak.
- Avoid redundant external probes while the local web process is unavailable; preserve graceful shutdown and retention behavior.

## [1.2.0] - 2026-11-24

### Fixed

- Serialize all account and API writes with retention cleanup, preventing late requests from recreating expired records.
- Bind bids to the observed lot revision as well as price, including the first zero-price bid.
- Deduplicate lot creation by operation identifier and content fingerprint after lost HTTP responses.
- Reserve candidate notifications and expose sent/unknown delivery reconciliation before candidate changes.
- Return authoritative personal-history states for archived and cancelled lots without broken public links.
- Preserve keyboard focus during live detail/admin updates and improve secondary-text contrast.
- Apply separate rate limits to public photographs, assets and page requests.
- Wait for launchd teardown before restart, monitor runtime HTTP/worker health and allow graceful request completion.

### Added

- Verified private SQLite/JPEG snapshots and expiry cleanup for application-owned backups.
- Public, validated source-commit metadata for prepared native and container releases.
- Runtime dependency auditing in CI and a complete runtime version lock.

### Dependencies

- Upgrade Gunicorn to the published 27.0.1 release, including bounded HTTP/1 chunk parsing.
- Pin all ten runtime package versions; artifact hashes remain outside the lock scope.

### Deployment notes

- Startup adds lot revisions and operation/delivery receipt tables without removing existing lots, approvals or bids.
- Regenerate native launch-agent plists to use the 45-second exit timeout.
- Older open clients must reload to submit the new revision/operation fields.
- Managed backup deletion requires the same `AUCTION_BACKUPS` setting in web/worker; native service and Compose set it automatically.

## [1.1.0] - 2026-11-15

### Added

- Visible organizer edit and delete actions on lot pages and management lists.
- Private lot archive preserving accepted bids, participant histories and retained photographs.
- Bid-count checks that serialize deletion with incoming bids and reject stale confirmations.
- Live updates for organizer lists and archived lots.

### Deployment notes

- Startup adds an archive flag to the existing lot table without changing bids or approvals.
- Earlier releases do not filter this flag; do not roll back to version 1.0.0 after archiving lots without a compatible data migration.

## [1.0.0] - 2026-11-15

### Added

- Public catalog, manually approved participation and authenticated Telegram bidding.
- Organizer tools, private bid histories, subscriptions and scheduled notifications.
- Image normalization, transactional bid checks and configured data retention.
- Docker Compose deployment with persistent storage and non-root containers.
- A supervised macOS deployment with per-user startup, component recovery and bounded private logs.
- A public version endpoint and explicit loopback Cloudflare proxy configuration.
- Strict Python and JavaScript type checks, documentation checks and container verification in CI.

### Deployment notes

- One configured auction runs on one host with local SQLite storage.
- Quick Tunnel addresses change after reconnection; the supervisor updates the Telegram menu.
- Stable public hosting requires an operator-configured HTTPS origin. Quick Tunnels provide no uptime guarantee.
- Auction closing and retention dates remain defined in the server configuration documented in the README.
