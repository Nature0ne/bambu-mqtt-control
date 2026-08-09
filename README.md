# Bambu MQTT Control

An unofficial, self-hosted web dashboard for monitoring and safely controlling
Bambu Lab printers and connected AMS units over the local MQTT interface. It
does not require a Bambu Cloud login and does not send printer credentials to
the browser.

> This project is not affiliated with or endorsed by Bambu Lab. Keep it on a
> trusted local network and use a VPN for remote access.

[Deutsche Kurzanleitung](docs/README.de.md)

## Features

- Multiple printers and multiple AMS units
- Live state updates over WebSocket
- Temperatures, print progress, layers, errors, lights, fans and AMS trays
- Explicitly allowlisted pause, resume, stop, speed, light and RFID commands
- AMS 2 Pro and AMS HT drying with model-specific temperature limits and
  state-confirmed start/stop handling
- X1-family local camera streaming through a credential-isolating server-side
  RTSPS-to-MJPEG proxy
- Optional camera recording, timelapse and reported-resolution controls
- Prometheus metrics and a local SQLite command audit log
- Browser onboarding with secrets stored in a private Docker volume
- Responsive interface with its own session-based login

There is deliberately no raw MQTT or raw G-code input. Filament loading,
unloading, arbitrary temperature/fan controls, axis movement, object skipping
and file-based print start remain disabled until their model- and state-specific
safety requirements can be enforced.

## Requirements

- A Bambu printer reachable from the Docker host on TCP `8883`
- For X1 camera streaming, reachability on TCP `322`
- LAN Mode access code and printer serial number
- Docker Engine or Docker Desktop with Docker Compose V2
- `linux/amd64` or `linux/arm64` containers

The full camera feature is supported inside the Linux container, including when
Docker Desktop runs that container on macOS or Windows.

## Quick start

Choose the final browser hostname or IP before onboarding. It becomes the
application's allowed HTTPS origin.

```sh
git clone https://github.com/Nature0ne/bambu-mqtt-control.git
cd bambu-mqtt-control
cp .env.example .env
docker compose up -d --build
./scripts/show-setup-token
```

Open `https://localhost:9444/`, accept or trust the local Caddy certificate, and
enter the displayed one-time setup token. The wizard asks for the web login,
printer IP, model, serial number and LAN access code. New printers start in
read-only mode.

Check the deployment with:

```sh
docker compose ps
docker compose exec -T bambu-control \
  python3 -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:9208/healthz").read().decode())'
```

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
cookies. To publish only the application port on host loopback and start it
without bundled Caddy:

```sh
docker compose \
  -f compose.yaml \
  -f deploy/compose.external-proxy.yaml \
  up -d --build bambu-control
```

Point your HTTPS proxy to `http://127.0.0.1:9208`. Preserve WebSocket upgrades,
disable buffering for the camera stream, and block external access to
`/metrics`. A generic Nginx example is included in
`deploy/nginx.conf.example`. The application must be hosted at `/`, not below a
URL path prefix.

## Security model

- Configuration and secrets are stored in the `bambu-config` named volume.
- The one-time bootstrap token is created with no-follow and exclusive-file
  semantics and is deleted after successful onboarding.
- Web sessions are bounded, stored as digests and use Secure, HttpOnly,
  SameSite cookies.
- Login attempts share a per-client rate limit across page, API, Basic Auth and
  WebSocket entry points.
- Commands require authentication, exact HTTPS Origin and CSRF validation.
- Every write command needs both `writable: true` and a per-command allowlist
  entry.
- The container runs as UID/GID `10001`, with a read-only root filesystem, no
  Linux capabilities, a PID limit and a small temporary filesystem.
- Printer TLS uses the bundled Bambu CA by default. Insecure self-signed MQTT is
  an explicit opt-in and disables camera streaming.
- Camera tickets are short-lived, single-use, session-bound and never appear in
  URLs. The LAN access code stays in the server process boundary.
- `/metrics` is blocked by the bundled public proxy.

Do not submit real credentials, serial numbers, private camera URLs or raw MQTT
payloads in issues or logs. See `SECURITY.md`.

## Supported controls

| Control | Safety behavior |
| --- | --- |
| Pause, resume, stop | QoS 1 and state-gated |
| Print speed | Only during a suitable print state |
| Chamber light | Uses the reported chamber-light capability |
| Refresh AMS RFID | Only for reported AMS and slot IDs |
| AMS drying | AMS 2 Pro: 45-65 C; AMS HT: 45-85 C; 1-24 h; confirmed by state |
| Camera live view | X1-family, reported RTSPS only, verified printer TLS |
| Recording/timelapse/resolution | Explicit opt-in and reported capabilities only |

Firmware and model behavior changes over time. Begin read-only, verify the
reported state, and enable each write capability individually under supervision.

## Configuration and data

The recommended configuration path is browser onboarding. An annotated manual
example is available at `config/printers.example.yml`.

Docker volumes:

- `bambu-mqtt-control_bambu-config`: configuration and secret files
- `bambu-mqtt-control_bambu-data`: audit database
- `bambu-mqtt-control_caddy-data`: local CA and Caddy state
- `bambu-mqtt-control_caddy-config`: Caddy runtime configuration

Back up configuration and data before upgrades:

```sh
mkdir -p backup
chmod 700 backup
docker compose cp bambu-control:/config backup/config
docker compose cp bambu-control:/var/lib/bambu-control backup/data
```

The backup contains secrets and must remain private.

## Updates and removal

```sh
git pull --ff-only
docker compose up -d --build
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
node --check app/static/app.js
node --check app/static/login.js
node --check app/static/setup.js
docker compose config --quiet
docker build --tag bambu-mqtt-control:test .
```

Tests use fake MQTT and camera transports and never contact a real printer.

## Protocol references

- [OpenBambuAPI MQTT](https://github.com/Doridian/OpenBambuAPI/blob/main/mqtt.md)
- [OpenBambuAPI video](https://github.com/Doridian/OpenBambuAPI/blob/main/video.md)
- [BambuStudio](https://github.com/bambulab/BambuStudio)
- [ha-bambulab](https://github.com/greghesp/ha-bambulab)

See `THIRD_PARTY_NOTICES.md` for certificate provenance and attribution.

## License

The project source is available under the [MIT License](LICENSE). Third-party
components and the bundled public printer CA remain subject to their respective
licenses and notices.
