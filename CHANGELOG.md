# Changelog

## V3 0.9.0 - 2026-08-03

- neuer kompakter dreiseitiger Wartungsnachweis in HTML und PDF
- zusätzliche lokale Arbeitsliste neben den MSR2-Prüfpunkten
- serverseitig gesperrter Wartungs-Demomodus ohne Register-Write
- gemeinsamer Anlagen-Snapshot und lokales Berichtsarchiv
- korrigierte physische Block-24-Offsets; Gas/Heizöl wird beim Wartungsstart
  wieder automatisch aus `Hka_Mw1.bKraftstofftyp` erkannt
- getrennte Adressierung der beiden Netzüberwachungs-CPUs und geprüftes
  Block-16-Mapping für den eingesetzten Überwachungscontroller
- rot gekennzeichneter Netzschutzeditor mit dem bestehenden Admin-/Auth-/
  Hardware-Haken sowie ACK, Readback und Audit

## 0.1.0 - 2026-08-03

- erstes Open-Dachs-Manager-Repository
- installierbares Python-Paket mit CLI, TUI und Weboberfläche
- zentraler FIFO-Serialworker als alleiniger Besitzer des Adapters
- lokale Messwerthistorie und Wartungsberichte
- kontrollierter Schreibpfad mit Authentifizierung, ACK, Readback und Audit
- systemd-Dienste und `install.sh`
