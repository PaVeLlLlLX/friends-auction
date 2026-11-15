# Changelog

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
