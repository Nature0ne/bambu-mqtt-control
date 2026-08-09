# Security policy

## Reporting a vulnerability

Please do not open a public issue for a vulnerability that could expose printer
credentials, camera data, or remote-control capabilities. Use GitHub's private
security advisory feature for this repository instead.

Include the affected commit, impact, and a minimal reproduction. Never include a
real LAN access code, web password, serial number, credential-bearing RTSPS URL,
or unredacted raw MQTT payload.

## Supported version

Until the first stable release, only the latest commit on `main` receives
security fixes.

## Deployment boundary

This service is intended for trusted local networks. Do not forward its HTTPS,
MQTT, or camera ports from the public internet. Use a VPN for remote access.
