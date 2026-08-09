# Sicherheitsmodell

Open Dachs Manager kommuniziert mit einer realen Heizungs- und
BHKW-Steuerung. Ein falscher Schreibwert kann das Betriebsverhalten verändern.
Lese- und Schreibpfad sind deshalb bewusst unterschiedlich aufgebaut.

## Lesepfad

- der Serialworker ist alleiniger Besitzer des Adapters
- alle Clients verwenden FIFO-Sitzungen
- Frames mit ungültiger CRC werden verworfen
- Blocknummern außerhalb des Ein-Byte-Protokollbereichs werden abgewiesen und
  niemals still auf das niedrige Byte gekürzt

## Schreibpfad

Jeder echte Schreibvorgang erfordert:

```text
aktuellen Block lesen
→ Feld, Datentyp und Block validieren
→ authentifizieren / PW4 berechnen
→ Schreibfreigabe ausdrücklich aktivieren
→ vollständigen Block schreiben
→ positive Antwort erhalten
→ bytegenau zurücklesen
→ Ergebnis im Audit protokollieren
```

Dry-Run bleibt der Standard. Ein vorbereiteter Wert beweist nicht, dass eine
Änderung technisch unbedenklich ist. Vorher die Bedeutung anhand der richtigen
Geräteunterlagen prüfen und ein Backup erstellen.

## Netzschutz-CPUs

CPU 0 (Regler), CPU 1 und CPU 2 (Netzüberwachung) haben voneinander getrennte
Blockräume. Block 16 von CPU 1 und CPU 2 ist wegen seiner Netzschutzparameter
rot markiert, damit diese Felder nicht mit normalen Reglerwerten verwechselt
oder versehentlich geändert werden. Sie sind wie alle gemappten Register über
den globalen Admin-/Auth-/Schreib-Haken veränderbar. Lesen des
Ausgangszustands, vollständiges Blockschreiben, ACK, Readback und Audit gelten
unverändert. Das Öffnen oder Aktualisieren der roten Seite schreibt nichts.

## Webzugriff

Der eingebaute Webserver liefert HTTP und kein TLS. Er gehört in ein
vertrauenswürdiges lokales Netzwerk oder hinter einen korrekt eingerichteten
HTTPS-Reverse-Proxy. Den Standardport nicht direkt ins Internet weiterleiten.

Anfangspasswörter sofort ändern und `/var/lib/open-dachs-manager` als
vertrauliche Betriebsinformation schützen.

## Reifegrad

Version 1.0 ist der erste stabile öffentliche Stand und wird ohne Garantie
bereitgestellt. Gerätevarianten und Packstände können abweichen. Leseergebnisse
und Feldlagen am tatsächlichen Zielgerät verifizieren, bevor Schreibvorgänge
freigegeben werden.
