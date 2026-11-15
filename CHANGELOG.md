# Changelog

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
