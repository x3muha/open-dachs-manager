# Externe Abhängigkeiten

Der Inhalt dieses Repositories steht unter der MIT-Lizenz. Die folgenden
Pakete werden bei der Installation aus ihren eigenen Projekten bezogen und
nicht als Quelltext oder Binärdatei in diesem Repository mitgeliefert:

| Paket | Aufgabe | Projekt |
|---|---|---|
| pySerial | Zugriff auf das serielle Linux-Gerät | <https://github.com/pyserial/pyserial> |
| ReportLab | Erzeugung der PDF-Wartungsberichte | <https://www.reportlab.com/> |
| Pillow | Bildunterstützung als Abhängigkeit von ReportLab | <https://python-pillow.github.io/> |

Für diese installierten Pakete gelten jeweils deren eigene Lizenzbedingungen.
Sie ändern nicht die MIT-Lizenz des Open-Dachs-Manager-Repositories.

## Mitgelieferte Laufzeitdaten

Im Python-Paket liegen nur Daten, die der Manager zur Laufzeit tatsächlich
benötigt:

- adressierbare Block- und Felddefinitionen einschließlich Byteoffsets,
  Datentypen, Skalierungen und Einheiten
- deutsche Feldbezeichnungen und kompakte Auswahlwerte
- Metadaten für Eingabegrenzen und gepackte Wartungsfelder
- Meldungstypen sowie die Konfiguration des Schreibablaufs

Nicht über den ein Byte breiten Blockdienst adressierbare Datensätze werden
nicht mitgeliefert. Ein Katalog mit ausführlichen Service-, Ursachen- oder
Maßnahmentexten ist ebenfalls nicht Bestandteil des Repositories. Unbekannte
Codes bleiben deshalb als numerische Codes sichtbar, ohne fremde Langtexte zu
vervielfältigen.
