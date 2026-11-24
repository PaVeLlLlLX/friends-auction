# Data retention and storage snapshots

## Scope and deadline

The configured retention boundary is **2027-02-01 00:00:00 UTC+7**. After that
boundary, API and photograph requests return 410. The worker deletes the
configured database records, uploaded JPEGs and application-owned snapshots,
then exits. If the host or worker is stopped at the deadline, deletion runs at
the next worker start. This is an operational dependency, not an exact-time
erasure guarantee for an offline disk.

Account refreshes and API mutations check retention while holding SQLite's
write reservation. They cannot recreate records after a completed cleanup.
The worker uses the same database serialization, including delivery receipts,
polling cursors and its heartbeat. Candidate-notification and lot-operation
receipts have the same retention boundary as bids and applications.

## Managed locations

| Data                                                   | Location                             | Cleanup                                                                                                                                                                            |
| ------------------------------------------------------ | ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Database and delivery/operation receipts               | `AUCTION_DB`                         | All application tables are cleared, followed by `VACUUM`; foreign keys remain enforced.                                                                                            |
| Normalized uploaded photographs                        | `AUCTION_UPLOADS`                    | Registered/generated JPEG storage; cleanup removes JPEG files in this directory.                                                                                                   |
| Application-owned snapshots                            | `AUCTION_BACKUPS`                    | Marked snapshot directories, including interrupted snapshots, are removed at expiry. Unmarked directories and symbolic links are preserved.                                        |
| Runtime secrets and diagnostic logs                    | Deployment configuration and `logs/` | Separate operator action: stop/revoke the bot if no longer required, remove configuration and obsolete diagnostics. The application does not claim to revoke Telegram credentials. |
| Original imports, demo/trial storage, external backups | Separate directories                 | Inventory and delete separately by the same retention policy; runtime cleanup does not discover arbitrary files on the host.                                                       |

The native service sets `AUCTION_BACKUPS` to `backups/` beside its data directory.
Compose sets it to `/app/data/backups` in the persistent volume. Direct web/worker
launches must set the same variable explicitly to enable managed backup cleanup.
Do not point these settings at shared or unrelated storage.

An operator inventory should identify each location, responsible owner, retention
boundary, deletion mechanism and verification result. Include original image
imports, integration databases, copied exports, retired deployment directories
that contain data, cloud backups, Time Machine and APFS snapshots. Keep the
inventory private; it may contain personal filesystem paths. Source-only release
directories need not be treated as participant databases, but must be checked
for accidentally included data before archiving.

SQLite deletion and vacuuming do not demonstrate physical erasure of every
previous block, SSD copy, filesystem snapshot or remote backup. Use operating
system encryption and a backup policy consistent with the retention scope;
record any copies that cannot be immediately removed.

## Create and verify a native-service snapshot

Use the prepared release's Python interpreter and the service's private
configuration. No Telegram requests are made by these commands:

```sh
python scripts/backup_storage.py --config /absolute/private/config.json --name before-update
python scripts/backup_storage.py --verify /absolute/private/backups/before-update
```

Use a unique name for each snapshot. Existing snapshots are never overwritten.
The destination is private (`0700` directory, `0600` data files); the command
prints only integrity metadata, not credentials or participant identities.
Creation is rejected after retention has expired.

The snapshot operation reserves SQLite writes, uses a separate connection for
SQLite's backup API, and copies the registered JPEG files while the reservation
is held. Read-only access remains possible. Large copies delay new writes, so
schedule the operation away from the final bidding period. Prefer stopping the
service for a deployment snapshot, which also avoids in-flight HTTP ambiguity.

The manifest records database and photograph SHA-256 values. Verification checks
the manifest, SQLite integrity, foreign keys and the exact photograph inventory.
It detects changed or missing bytes; it does not certify the historical truth of
the records. Interrupted staging directories carry the same ownership marker and
are covered by expiry cleanup.

In Compose, the same API is available inside the runtime package:

```sh
docker compose exec -T web python -c 'import os; from pathlib import Path; from auction.app import DELETE_AFTER; from auction.storage import snapshot_storage; snapshot_storage(Path(os.environ["AUCTION_DB"]), Path(os.environ["AUCTION_UPLOADS"]), Path(os.environ["AUCTION_BACKUPS"]), "before-update", DELETE_AFTER)'
```

Snapshots in the same volume protect against operator/application mistakes,
not loss of that volume. An external copy requires its own access controls and
expiry procedure.

## Restore procedure

1. Stop the web process and worker; prevent a second supervisor from starting.
2. Verify the snapshot and retain the current storage as a separate managed
   rollback copy when the retention deadline has not passed.
3. Restore **both** `auction.sqlite3` and `photos/` into the configured data
   directory. Keep owner-only permissions. Do not combine unrelated database and
   photograph snapshots or manually discard a live SQLite journal.
4. Start a compatible release. Check availability, packaged identity, aggregate
   record counts and photograph inventory. Perform functional restore tests only
   with synthetic copies, isolated credentials and no real notification delivery.
5. Keep a record of the snapshot identity, outcome and expiry. Restoring a copy
   after retention does not authorize extending retention: cleanup still applies.

Rollback from 1.2.0 requires a release that understands revisions, operation
receipts and archived-lot visibility. Do not launch 1.0.0 against a database with
archived lots; it does not enforce the archive filter.

## External service and client boundaries

Telegram retains messages already delivered to users under its own service
behavior. Cloudflare processes proxy/network data under its policies. Browsers,
WebViews and recipients can retain downloaded content or screenshots. Local
database cleanup cannot establish deletion of these copies. User-facing text
must distinguish server storage from those external boundaries.
