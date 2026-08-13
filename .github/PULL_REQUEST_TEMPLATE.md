## What changed

Describe the user-visible result and the scope of the change.

## Safety checklist

- [ ] No credentials, serial numbers, private IPs, private camera URLs, or raw user payloads are included.
- [ ] New write operations are explicitly allowlisted and gated by model, reported capability, current state, and permission.
- [ ] Raw MQTT and raw G-code passthrough remain unavailable.
- [ ] Success, rejection, unsafe-state, and timeout behavior is covered where applicable.
- [ ] Documentation and `CHANGELOG.md` are updated for user-visible changes.

## Verification

- [ ] `python -m unittest discover -s tests -v`
- [ ] `ruff check app tests`
- [ ] All JavaScript files pass `node --check`.
- [ ] Both Compose variants pass `docker compose config --quiet`.
