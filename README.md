# Bambu MQTT Control

An unofficial, self-hosted web dashboard for monitoring and safely controlling
Bambu Lab printers and connected AMS units over the local MQTT interface. It
does not require a Bambu Cloud login and never sends printer credentials to the
browser.

> This project is not affiliated with or endorsed by Bambu Lab. Keep it on a
> trusted local network and use a VPN for remote access.

[Deutsche Kurzanleitung](docs/README.de.md) · [Changelog](CHANGELOG.md) ·
[Security policy](SECURITY.md)

## Highlights

- Multiple printers and multiple AMS units in one responsive dashboard
- Live state updates over WebSocket, temperatures, print progress, layers,
  stages, fans, doors, Wi-Fi, SD-card state, firmware and bounded HMS details
- Per-device lights for every supported light node actually reported by the
  printer
- Explicitly allowlisted pause, resume, stop, speed, light and AMS RFID commands
- AMS 2 Pro and AMS HT drying with model-specific temperature limits,
  state-confirmed start/stop handling and an always-available stop safety path
- X1-family local camera streaming through a credential-isolating server-side
  RTSPS-to-MJPEG proxy
- Optional camera recording, timelapse and reported-resolution controls
- In-browser administration for printers, permissions, credentials and origins,
  without disclosing saved secrets
- Filterable, retained command audit history and privacy-safe diagnostics
- Transactional, validated backup and restore utilities
- Prometheus metrics inside the private container network
- Browser onboarding with secrets stored in a private Docker volume
- Published `linux/amd64` and `linux/arm64` container images with SBOM and
  provenance attestations

There is deliberately no raw MQTT or raw G-code input. Filament loading,
unloading, arbitrary temperature/fan controls, axis movement, object skipping
and file-based print start remain disabled until their model- and state-specific
safety requirements can be enforced.

## Requirements

- A Bambu printer reachable from the Docker host on TCP `8883`
- For X1 camera streaming, reachability on TCP `322`
- LAN Mode access code and printer serial number
- Docker Engine or Docker Desktop with Docker Compose V2
- A `linux/amd64` or `linux/arm64` system

The complete camera feature runs inside the Linux container, including when
Docker Desktop hosts that container on macOS or Windows.

## Quick start

Choose the final browser hostname or IP before onboarding. It becomes the
application's allowed HTTPS origin.

```sh
git clone https://github.com/Nature0ne/bambu-mqtt-control.git
cd bambu-mqtt-control
cp .env.example .env
docker compose up -d
./scripts/show-setup-token
```

Open `https://localhost:9444/`, accept or trust the local Caddy certificate, and
enter the displayed one-time setup token. The wizard asks for the web login,
printer IP, model, serial number and LAN access code. New printers start in
read-only mode.

The default Compose deployment downloads
`ghcr.io/nature0ne/bambu-mqtt-control:latest`. To build the checked-out source
locally instead:

```sh
docker compose -f compose.yaml -f deploy/compose.build.yaml up -d --build
```

Check the deployment with:

```sh
docker compose ps
docker compose exec -T bambu-control \
  python3 -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:9208/healthz").read().decode())'
```

Both containers should become healthy. The proxy check uses a dedicated HTTP
listener bound only to `127.0.0.1` inside its container; port `2018` is neither
published nor exposed.

## Administration

After signing in, open **Manage** to update web access, add or remove printers,
rotate credentials and grant individual write permissions. A save requires the
current web password. Blank password and access-code fields preserve their
existing secret. Configuration is validated and the printer connections are
swapped without restarting the container.

The same area provides:

- a paginated command history with filters for time, printer, command, result
  and actor;
- privacy-safe diagnostics without IP addresses, serial numbers or secrets;
- bounded audit retention (90 days and at most 100,000 records).

Normal print-section write commands fail closed unless the printer positively
reports Developer LAN mode. `stop_drying` is the explicit safety exception: it
cannot initiate heat and still requires the configured permission and reported
AMS state.

## Access from another device on the LAN

Edit `.env` before the first start. Use a stable LAN hostname or the server's
LAN IP for `BAMBU_SITE_HOST`, and bind only to the intended host address when
possible:

```dotenv
BAMBU_SITE_HOST=192.168.1.20
BAMBU_BIND_ADDRESS=192.168.1.20
BAMBU_HTTPS_PORT=9444
TZ=Europe/Berlin
```

Then open `https://192.168.1.20:9444/`. If `BAMBU_BIND_ADDRESS=0.0.0.0` is used,
restrict TCP `9444` with the host firewall. Never forward this port from the
public internet.

The bundled Caddy proxy creates a private local CA. Export its root certificate
if clients should trust it without a browser warning:

```sh
docker compose cp \
  proxy:/data/caddy/pki/authorities/local/root.crt \
  ./bambu-local-ca.crt
```

Install that certificate only on devices that should trust this local service.
Keep the Caddy data volume private because it also contains the local CA key.

For access without exposing a LAN listener, leave the default loopback binding
and use an SSH tunnel:

```sh
ssh -L 9444:127.0.0.1:9444 user@docker-host
```

## Using an existing HTTPS reverse proxy

The application requires HTTPS because login and camera tickets use Secure
cookies. Publish only the application port on host loopback and start it
without bundled Caddy:

```sh
docker compose -f deploy/compose.external-proxy.yaml up -d --remove-orphans
```

The external-proxy file is a complete Compose model containing only the app.
`--remove-orphans` also stops and removes a previously created bundled Caddy
container, while preserving its named volumes for a later switch back.

Point your HTTPS proxy to `http://127.0.0.1:9208`. Preserve WebSocket upgrades,
disable buffering for the camera stream, and block external access to
`/metrics`. A generic Nginx example is included in
`deploy/nginx.conf.example`. The application must be hosted at `/`, not below a
URL path prefix.

## Security model

- Configuration and secrets are stored in the `bambu-config` named volume.
- The one-time bootstrap token is created with no-follow and exclusive-file
  semantics and deleted after successful onboarding.
- Web sessions are bounded, stored as digests and use Secure, HttpOnly,
  SameSite cookies.
- Login attempts share a per-client rate limit across page, API, Basic Auth and
  WebSocket entry points.
- Commands require authentication, exact HTTPS Origin and CSRF validation.
- Every write command needs both `writable: true` and a per-command allowlist
  entry, then passes capability, model and current-state gates.
- Containers have bounded local logs, PID limits, read-only root filesystems,
  dropped capabilities and `no-new-privileges`.
- Printer TLS uses the bundled Bambu CA by default. Insecure self-signed MQTT is
  an explicit opt-in and disables camera streaming.
- Camera tickets are short-lived, single-use, session-bound and never appear in
  URLs. The LAN access code stays inside the server process boundary.
- `/metrics` is blocked by the bundled public proxy.
- Base images and all GitHub Actions are pinned to immutable digests or SHAs.

Do not submit real credentials, serial numbers, private camera URLs or raw MQTT
payloads in issues or logs. See [SECURITY.md](SECURITY.md).

## Supported controls

| Control | Safety behavior |
| --- | --- |
| Pause, resume, stop | QoS 1, permission and current-state gated |
| Print speed | Only during a suitable print state |
| Printer lights | Only allowlisted light nodes currently reported by the printer |
| Refresh AMS RFID | Only for reported AMS and slot IDs |
| AMS drying | AMS 2 Pro: 45–65 °C; AMS HT: 45–85 °C; 1–24 h; confirmed by state |
| Camera live view | X1-family, reported RTSPS only, verified printer TLS |
| Recording/timelapse/resolution | Explicit opt-in and reported capabilities only |

Firmware and model behavior changes over time. Begin read-only, verify the
reported state, and enable each write capability individually under supervision.

## Backup and restore

Create a consistent archive while the service is running:

```sh
mkdir -p backup
chmod 700 backup
./scripts/backup backup/bambu-control-$(date +%F).tar.gz
```

The archive contains passwords and access codes. Keep it encrypted and private.
The utility snapshots SQLite using its backup API and includes only active,
regular configuration files.

Restore a verified archive with:

```sh
./scripts/restore backup/bambu-control-2026-08-13.tar.gz
```

The scripts use the same Compose model as your running installation. For the
external-proxy deployment, prefix both backup and restore with
`BAMBU_COMPOSE_FILES=deploy/compose.external-proxy.yaml`. For a local-build
deployment, use
`BAMBU_COMPOSE_FILES=compose.yaml:deploy/compose.build.yaml` (on Windows use the
Compose path separator appropriate for your shell). The standard Compose
`COMPOSE_FILE` variable is supported too.

Restore validates paths, entry types, sizes, configuration and database
integrity before replacement. The commit is transactional and rolls back the
previous bytes on failure. In the exceptional case that rollback itself cannot
be completed, the script exits with status `2`, leaves the application stopped
and preserves hidden recovery snapshots for manual inspection.

Docker volumes:

- `bambu-mqtt-control_bambu-config`: configuration and secret files
- `bambu-mqtt-control_bambu-data`: audit database
- `bambu-mqtt-control_caddy-data`: local CA and Caddy state
- `bambu-mqtt-control_caddy-config`: Caddy runtime configuration

## Updates and removal

Use the same Compose selection as your installation for every lifecycle
command. Examples:

```sh
# Bundled Caddy / published image
docker compose pull
docker compose up -d

# Existing external HTTPS proxy
COMPOSE_FILE=deploy/compose.external-proxy.yaml docker compose pull
COMPOSE_FILE=deploy/compose.external-proxy.yaml docker compose up -d

# Local build
COMPOSE_FILE=compose.yaml:deploy/compose.build.yaml docker compose build
COMPOSE_FILE=compose.yaml:deploy/compose.build.yaml docker compose up -d
```

`docker compose down` keeps all named volumes. `docker compose down -v`
permanently deletes configuration, credentials, audit history and the Caddy CA;
use it only after a verified backup.

## Metrics

Prometheus-compatible metrics are available inside the Compose network at
`http://bambu-control:9208/metrics`. They intentionally are not exposed through
Caddy because printer names, models and status may be sensitive.

## Development

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --requirement requirements-dev.txt
python -m unittest discover -s tests -v
ruff check app tests
for file in app/static/*.js; do node --check "$file"; done
for script in scripts/*; do sh -n "$script"; done
docker compose config --quiet
docker compose -f compose.yaml -f deploy/compose.build.yaml config --quiet
docker compose -f compose.yaml -f deploy/compose.build.yaml build bambu-control
```

Tests use fake MQTT and camera transports and never contact a real printer.

## Protocol references

- [OpenBambuAPI MQTT](https://github.com/Doridian/OpenBambuAPI/blob/main/mqtt.md)
- [OpenBambuAPI video](https://github.com/Doridian/OpenBambuAPI/blob/main/video.md)
- [BambuStudio](https://github.com/bambulab/BambuStudio)
- [ha-bambulab](https://github.com/greghesp/ha-bambulab)

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for certificate provenance
and attribution.

## License

The project source is available under the [MIT License](LICENSE). Third-party
components and the bundled public printer CA remain subject to their respective
licenses and notices.
