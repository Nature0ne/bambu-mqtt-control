# Third-party notices and protocol references

This is an unofficial community project and is not affiliated with or endorsed
by Bambu Lab.

## Bambu printer CA bundle

`certs/bambu-lab-ca.pem` contains public CA certificates used to validate local
Bambu printer certificates. It contains no private key. Provenance and the
source commit are documented in `certs/README.md`.

Primary references:

- BambuStudio printer certificate:
  https://github.com/bambulab/BambuStudio/blob/master/resources/cert/printer.cer
- OpenBambuAPI CA bundle and TLS documentation:
  https://github.com/Doridian/OpenBambuAPI
- ha-bambulab interoperability reference:
  https://github.com/greghesp/ha-bambulab

## Runtime dependencies

Python dependencies are installed from PyPI during the container build. FFmpeg
is installed from Alpine Linux packages. Caddy is run from its official Docker
image. Their respective licenses apply to those components.
