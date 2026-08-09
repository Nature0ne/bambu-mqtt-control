# Contributing

Contributions are welcome, especially protocol fixtures with all credentials,
serial numbers, IP addresses, job names, and user data removed.

Before opening a pull request:

1. Keep commands allowlisted and model/state gated. Do not add raw MQTT or raw
   G-code passthrough.
2. Add tests for success, rejection, timeout, and unsafe-state paths.
3. Run:

   ```sh
   python -m unittest discover -s tests -v
   ruff check app tests
   node --check app/static/app.js
   node --check app/static/login.js
   node --check app/static/setup.js
   docker compose config --quiet
   ```

Real printer, heating, movement, filament, and camera-setting operations must
never be part of automated tests.
