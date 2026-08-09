# Bambu Lab CA bundle

`bambu-lab-ca.pem` stammt aus OpenBambuAPI, Commit
`6fbd95d7aaeb4740cb05331f274677bbe376bedb` vom 13. Mai 2026:

https://github.com/Doridian/OpenBambuAPI/blob/6fbd95d7aaeb4740cb05331f274677bbe376bedb/examples/ca_cert.pem

Der Inhalt entspricht (abgesehen vom abschliessenden Zeilenumbruch) der von
Bambu Studio veroeffentlichten Datei und dem von ha-bambulab verwendeten Bundle:

- https://github.com/bambulab/BambuStudio/blob/master/resources/cert/printer.cer
- https://github.com/greghesp/ha-bambulab/blob/main/custom_components/bambu_lab/pybambu/certs/bambu.cert

Die Datei enthaelt die von Bambu-Druckerzertifikaten verwendeten CA-Zertifikate
und keine privaten Schluessel. Aktualisierungen muessen bewusst gegen die
OpenBambuAPI-TLS-Dokumentation und mit `openssl` validiert werden.
