# Contributing

Contributions are welcome, especially protocol fixtures with every credential,
serial number, IP address, job name and user datum removed.

## Safety boundary

1. Keep write commands explicitly allowlisted and gated by printer/AMS model,
   reported capability, current state and configured permission.
2. Do not add raw MQTT or raw G-code passthrough.
3. Do not make real printing, heating, movement, filament or camera-setting
   operations part of automated tests.
4. Add tests for success, rejection, timeout and unsafe-state paths.
5. Do not log or persist plaintext passwords, LAN codes or credential-bearing
   camera URLs. Browser UI must use DOM text nodes for server-provided values.

## Local checks

```sh
python -m unittest discover -s tests -v
ruff check app tests
for file in app/static/*.js; do node --check "$file"; done
for script in scripts/*; do sh -n "$script"; done
docker compose config --quiet
docker compose -f compose.yaml -f deploy/compose.build.yaml config --quiet
```

If the change affects the container itself, also run:

```sh
docker compose -f compose.yaml -f deploy/compose.build.yaml build bambu-control
```

## Pull requests

- Keep changes focused and explain user-visible behavior.
- Update tests, documentation and `CHANGELOG.md` when applicable.
- Link public protocol evidence for new device behavior; never attach raw,
  credential-bearing captures.
- Avoid dependency upgrades unrelated to the change.
- Confirm that both first-time onboarding and existing installations remain
  supported.

Security vulnerabilities belong in a private GitHub security report as
described in [SECURITY.md](SECURITY.md), not a public issue.
