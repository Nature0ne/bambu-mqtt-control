# Changelog

All notable changes to this project are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [0.2.1] - 2026-08-13

### Fixed

- The first-start command for an external HTTPS proxy now selects the Compose
  model through `COMPOSE_FILE`, so the root `.env` is loaded consistently.

## [0.2.0] - 2026-08-13

### Added

- In-browser administration for printers, web identity, origins, credentials
  and granular write permissions, applied without a container restart.
- Privacy-safe diagnostics for Wi-Fi, door, SD card, print stage, firmware and
  bounded HMS codes.
- Filterable, paginated command audit history with 90-day/100,000-row retention.
- Dynamic controls for all supported light nodes reported by the printer.
- Transactional backup and restore commands with path, size, configuration and
  SQLite integrity checks plus byte-exact rollback.
- Version endpoint/build metadata and OCI image labels.
- Published GHCR images for `linux/amd64` and `linux/arm64`, including SBOM,
  provenance and build attestations.
- SHA-pinned CI, release and CodeQL workflows.

### Changed

- Normal print-section write commands now fail closed unless Developer LAN mode
  is positively reported; state-confirmed `stop_drying` remains the explicit
  safety exception.
- Light commands are restricted to nodes reported in the current full state.
- Compose now uses the published GHCR image by default; local source builds use
  `deploy/compose.build.yaml`.
- Container logs are size- and file-count-limited.
- The proxy healthcheck now uses a non-published loopback HTTP endpoint and
  verifies the expected JSON body, avoiding false failures from TLS hostname
  mismatch and false positives from empty responses.

### Fixed

- Hardened administrative configuration reads, staged commits, directory sync,
  rollback and obsolete managed-secret cleanup.
- Backup restore no longer leaves a partially replaced secret, database or main
  configuration after a failed commit.
- Legacy self-signed MQTT TLS now verifies the exact leaf certificate before
  any credential-bearing connection and retries safely while a printer is offline.
- Command request bodies and restore candidates now have stricter size, path
  and audit-schema validation.
- Dashboard controls now reflect Developer LAN mode and reported capabilities.
- Added authenticated logout and direct navigation to administration.

## [0.1.0] - 2026-08-08

- Initial standalone public release with onboarding, monitoring, allowlisted
  controls, AMS drying, X1 camera streaming, local HTTPS and MIT licensing.
