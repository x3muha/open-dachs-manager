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
- CPU-0-RAM-Anzeigen (`Interne Reglerwerte`, `Rußfilterregeneration`) nutzen
  ausschließlich den Lesedienst 0x02 mit festen Allowlisten im langsamen
  10-s-Zyklus derselben Serialworker-Sitzung; keine Authentifizierung, kein
  Write, keine Historisierung. Echo, Länge und Wertebereich werden geprüft,
  jeder Fehler ergibt `nicht verfügbar` statt eines Altwerts. Die
  Rußfilterregeneration wird nur bei gesetztem Rußfilterbit des gemappten
  Typcodes gelesen, zeigt aktive Betriebsminuten statt Wanduhrzeit und niemals
  einen erfundenen Prozentfüllstand; die Semantik stammt aus der statischen
  Firmwareanalyse (Konfidenz hoch, Typzuordnung mittel-hoch) ohne Live-Abgleich
- Die Display-Statuszeilen lesen zusätzlich nur die Ein-Byte-Allowlist
  `0x0db3`, `0x0f96`, `0x1bcc` sowie bedingt `0x1c9f`, `0x1b5c`, `0x1ed4`
  über Dienst 0x02 (höchstens einmal je 10-s-Intervall und Adresse;
  `0x0db3` wird aus dem Rußfilter-Read übernommen); ihre Textwechsel werden
  nur lesend in `display_status_events` protokolliert

## Lokale Zwischenwartung

Der Endpoint `complete-local-intermediate` ist ein eigener, fail-closed Lokalpfad und darf den Live-Wartungsabschluss nicht wiederverwenden. Er ist nur für Admins und ausschließlich für Entwürfe zulässig, validiert den vorhandenen – bei einer Teilwartung bewusst auch unvollständigen – Protokollstand, verlangt einen Grund (1–500 Zeichen) und führt nur eine atomare SQLite-Transaktion aus. Offene Prüfpunkte bleiben im Bericht offen. Der Pfad öffnet keine Serialworker-Sitzung, fordert keine Authentifizierung an und liest oder schreibt weder Block 100 noch Block 104. Der Abschluss dokumentiert diese Eigenschaften explizit im Bericht.

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

## Authentifizierungsstufen

Das Telegramm besitzt ein Byte für den Auth-Level. Die Original-Java-
Oberfläche benennt Rollen 1 bis 4; die älteren V2-Schreib- und Backup-Werkzeuge
verwenden zusätzlich Level 5. Der getestete Regler spiegelt auch höhere
Bytewerte zurück; dieses Echo ist kein Beleg für weitere Rollen. CLI, TUI, Web
und API akzeptieren deshalb ausschließlich echte Integer von 1 bis 5, prüfen
vor jedem Auth-Lesevorgang und verlangen eine exakt passende Antwort.
Boolesche, textuelle, gebrochene und außerhalb liegende Werte werden nicht
still konvertiert.

## Dachs Ein/Aus

Die manuelle Betriebsfreigabe ist ein eigener fest verdrahteter Adminpfad. Der
Browser wählt nur `on` oder `off`; CPU 0, RAM-Adresse `0x0ea1`, Länge 1,
Bitmaske `0x40` und Auth-Level 3 sind serverseitig fest. Das Bit ist
`Hka_Bd.UHka_Frei.Freigabe.fTasteOnOff`: gesetzt bedeutet Automatikfreigabe,
nicht erzwungener Start; gelöscht bedeutet normale Stoppanforderung, nicht
Not-Aus. Es wird ausschließlich das flüchtige RAM verändert. NVM und das
benachbarte, nicht maßgebliche Zustandsbyte `0x0fe8` bleiben unberührt.

Jede Vorschau liest Block 22/24, RAM `0x0ea1` und den Hand-/Aktortest
`0x1b5a` zweimal in derselben exklusiven Serialworker-Sitzung. Das direkte
Byte muss mit dem hohen Byte der Block-22-Lauffreigabe übereinstimmen; der
gesamte Ausgangsbytewert, Servicecode, Motorstatus und Handbetrieb müssen über
beide Lesungen stabil sein. Zum Einschalten sind zusätzlich Servicecode 0,
Motorstatus 15/16 und dieselben konservativen Temperaturgrenzen wie bei der
Entstörung erforderlich. Ausschalten ist als sicherheitsgerichtete reguläre
Stoppanforderung auch bei laufendem Motor oder aktiver Störung zulässig. Ein
aktiver Hand-/Aktortest sperrt jede tatsächlich nötige Änderung.

Ein kurzlebiges benutzer- und aktionsgebundenes Einmaltoken bindet den gesamten
Ausgangsbytewert. Nach Auth 3 wird der vollständige Zustand erneut geprüft. Nur
dann bildet der Server den Zielwert mit Lesen-Ändern-Schreiben und bewahrt alle
anderen sieben Bits. Es gibt höchstens ein Telegramm `03 0E A1 01 xx`; nur
positives Transport-ACK und das exakte Echo `83 0E A1 01` gelten als
bestätigt. Zwei unmittelbare Dienst-0x02-Lesungen müssen den vollständigen
Zielbytewert liefern. Nach jedem möglichen Authtelegramm folgt Auth 0. Token,
PW4 und vom Browser erfundene Adressen, Werte oder Auth-Level werden weder
akzeptiert noch gespeichert. Ein verbrauchtes Token und eine monotone
30-s-Wache verhindern Doppelklick und blinde Wiederholung bei unklarem Ausgang.
`Dachs Ein/Aus` und `Entstören` werden nie automatisch verkettet.

## Manuelle Entstörung

Die manuelle Entstörung ist ein eigener, fest verdrahteter Adminpfad und kein
allgemeiner RAM-Writer. Der Browser kann weder CPU, Adresse, Datenbyte,
Auth-Level noch PW4 wählen. Er erhält nach einer sicheren Vorschau nur ein
kurzlebiges, benutzergebundenes Einmaltoken; global existiert höchstens eine
offene Vorschau. Vorschau und Ausführung sind über eine eigene Ablaufsperre
serialisiert. Das Token wird vor der seriellen Sitzung atomar verbraucht. Nach
dem Erwerb der seriellen Web-Sperre wird der Verbindungsschalter erneut
geprüft; eine während der Wartezeit getrennte Verbindung kann deshalb keine
Sitzung, Authentifizierung oder Schreibaktion mehr beginnen. Nach jedem
tatsächlichen Schreibversuch sperrt eine monotone 30-s-Wache denselben
Servicecode gegen eine automatische oder gleichzeitig wartende Wiederholung.

Die Oberfläche sendet bei der Bestätigung exakt die bereits angezeigte
Vorschau; sie liest nicht heimlich einen neueren Servicecode ein. Das Token ist
an Benutzer, Servicecode, Störungszähler und Betriebssekunden des letzten
Störereignisses gebunden. Die 30-s-Gültigkeit wird nach dem Warten auf den
Serialworker, vor der Authentifizierung und unmittelbar vor dem Write erneut
geprüft. Ändert sich das Ereignis oder läuft die Bestätigung ab, endet der
Ablauf mit Konflikt und verlangt eine neue Vorschau samt neuer Bestätigung.
Eine Doppellesung, die insgesamt länger als zehn Sekunden dauert, gilt nicht
mehr als frisch.

Unter dem exklusiven Serialworker-Lock werden Block 22 und 24 zweimal direkt
gelesen. Erforderlich sind ein unveränderter aktiver Servicecode, Motorstatus
15 oder 16 und kohärente plausible Messwerte innerhalb der konservativen
Stillstandsgrenzen: Kühlwasser höchstens 90 °C, Dachs-Eintritt 72 °C,
Motorabgas 450 °C, Dachs-Abgas 170 °C und Kapsel 110 °C.
Noch vor Auth-Level 3 wird der persistente Einmal-Intent angelegt. Erst danach
und nach exakt gewährtem Auth-Level 3 wird dieselbe vollständige Prüfung
unmittelbar vor dem Write wiederholt. Zwischen ihrem Abschluss und dem
Telegramm darf höchstens eine Sekunde liegen. Jede Abweichung oder
Fristüberschreitung führt fail-closed zur Abmeldung ohne Write; ein dann sicher
nicht verwendeter Intent wird nur gemeinsam mit dem Pflichtaudit aufgelöst.

Diese Temperaturwerte sind ein konservativer ODM-Wiederanlaufvertrag, aber
keine nachgewiesene allgemeine Herstellerfreigabe für jede Installation.
Darum ist der Entstörwrite standardmäßig gesperrt. Er wird erst angeboten,
wenn die Politik für die konkrete Anlage fachlich geprüft und der Webdienst
absichtlich mit
`OPEN_DACHS_SERVICE_RESET_TEMPERATURE_POLICY_APPROVED=1` beziehungsweise
`--service-reset-temperature-policy-approved` gestartet wurde. Diese
Freigabe ersetzt weder die frischen Messungen noch die übrigen Sperren.

Der einzige zulässige Mutationsframe ist CPU 0, flüchtiges RAM `0x1c9e`,
`03 1C 9E 01 01`; ausschließlich positives Transport-ACK plus bytegenaues
Echo `83 1C 9E 01` gelten als bestätigt. Es gibt höchstens einen Writeversuch.
Danach muss Block 22 den Servicecode zweimal hintereinander als 0 liefern.
Unabhängig von Auth-Antwort, Write-ACK und Readback wird nach jedem möglichen
Authtelegramm Auth 0 mit exakt geprüfter Antwort angefordert. Bleibt Auth 0
unklar, folgt nach einem zuvor bestätigten Clear eine weitere rein lesende
Servicecode-0-Prüfung. Write-, Cleanup- und Nachlese-Ungewissheit bleiben
getrennt im Audit; PW4 und Einmaltoken werden nicht gespeichert. Der Pfad
öffnet niemals `/dev/ttyUSB0` direkt und berührt weder NVM noch
Serviceparameter.

Vor Authentifizierung und letzter Zustandsprüfung schreibt ODM einen
persistenten Intent in die lokale SQLite-Datenbank. Seine Auflösung und das
vollständige Pflichtaudit einschließlich Auth-0-Ausgang erfolgen in derselben
SQLite-Transaktion. Erst exaktes ACK/Echo zusammen mit stabilem
Servicecode-0-Readback erlaubt dabei die Auflösung als bestätigter Write. Bei
Prozessabbruch, Audit-/Transaktionsfehler, Transportambiguität oder
unvollständiger Bestätigung bleibt der Intent offen und
sperrt weitere Vorschauen auch nach einem Webdienst-Neustart. Es gibt dafür
keinen automatischen Timeout, Retry oder öffentlichen Löschendpunkt. Ein
offener Intent darf erst nach fachlicher Prüfung von Reglerzustand und Audit
durch einen unterstützten manuellen Wartungseingriff aufgelöst werden; bloßes
Löschen der Datenbankzeile ist kein regulärer Bedienvorgang.

## Backup-Wiederherstellung

Ein vollständiges aktuelles Sicherungsabbild umfasst 42 Ziele: die 36
adressierbaren Rohblöcke der Regler-CPU 0 sowie Block 16, 20 und 21 der
Netzschutz-CPU 1 und der Netzschutz-CPU 2. Wiederherstellbar bleiben davon nur
die 36 geprüften Konfigurationsziele auf CPU 0; alle Netzschutzblöcke bleiben
read/backup-only. Die Ziele werden immer als eindeutiges Paar aus CPU und
Blocknummer behandelt. Vor
dem Import werden Schema, Packrevision, Reglerkennung, eindeutige CPU-/Blockziele,
Payloadlängen und SHA-256-Prüfsummen geprüft. Alte Abbilder ohne Prüfsummen sind
nur für den Dry-Run zugelassen.

Die Wiederherstellung startet mit einer vollständigen Lesevorprüfung aller
ausgewählten Zielblöcke. Bytegleiche Blöcke werden ohne Authentifizierung und
ohne Schreibtelegramm übersprungen. Abweichende Blöcke erfordern eine
ausdrückliche Hardwarefreigabe, den exakten Bestätigungstext, eine einmalige
Authentifizierung der exklusiven Sitzung, eine erneute Stabilitätsprüfung,
positive Bestätigung und vollständige bytegenaue Rückleseprüfung. Fehler werden
je Ziel protokolliert; weitere ausgewählte Blöcke werden trotzdem versucht.
Softwareversionsfelder der Zielplatine werden nicht aus dem Abbild übertragen.

Der getrennte Schreibpfad des flüchtigen CPU-0-24-h-Zählers nimmt vom Browser
nur absoluten Zielwert, frisch gelesenen erwarteten Istwert und die explizite
Hardwarefreigabe an. Auth-Level 3 und PW4-Berechnung sind fest serverseitig;
PW4 wird nicht auditiert. Der Ausgangswert darf ausschließlich monoton um höchstens
30 Sekunden vorwärts laufen. Rückwärtsbewegung oder mehr als 30 Sekunden
brechen vor Authentifizierung und Schreiben ab und verändern den Zielwert nicht. Die unmittelbare
Dienst-0x02-Rücklesung bleibt auf Zielwert bis Zielwert plus eine Sekunde begrenzt.
Die anschließende Abmeldung akzeptiert ausschließlich Auth-Level 0 allein oder
Auth-Level 0 mit exakt zurückgespiegelter eigener PW4; abweichende Antworten
bleiben gesperrt und werden ohne mögliche Zugangsdaten protokolliert.

Der zwischenzeitliche Autoreset `runtime_24h_auto` ist entfernt. Seit
2026-10-26 gibt es eine neue, getrennte **automatische Rücksetzung**
(standardmäßig aus, Aktivieren nur durch Admin mit ausdrücklicher Bestätigung,
wer/wann wird gespeichert). Sie schreibt ausschließlich den festen Zielwert Y
der gespeicherten Regel; Y ≥ max(1800 s, 60 · `bMindestlaufzeit`) wird bei
jedem Lauf gegen einen frischen Block-50-Read geprüft. Vor Auth 3 werden in
einer Sitzung Block 24 (Status ≥ 30, vor der Authentifizierung erneut),
Block 22 (kein Servicecode), RAM 0x1b5a (kein Handbetrieb) und 0x0f0e
doppelt gelesen. Rate-Limit und Zähler je Motorlauf werden vor dem Write
persistiert; ein Prozessabbruch kann keinen zweiten Write auslösen.
Ein unsicherer Write-, Readback- oder Deauth-Ausgang schaltet die Automatik
ab; es gibt keinen blinden Wiederholversuch.

Der **Uhrabgleich** schreibt Block 50 als Read-Modify-Write: nur Offset 36
(`Hka_Ew.ulSystemTime`) und Kommando-Bit 5 in Offset 0 ändern sich; alle
übrigen Bytes müssen vor und nach dem Write gleich sein, das Bit muss nach
dem Write wieder gelöscht sein. Voraussetzungen: Auth 3, NTP-synchrone
ODM-Zeit, kein Mitternachts-/Nachtabschalt-/Zeitumstellungsfenster ±15 min,
Block-50-Offset 54 (Spiegel von RAM 0x202f) ≠ 4. Ein Stellvorgang schreibt
in der Firmware RAM-Uhr und RTC und verschiebt alle uhrbasierten Schaltzeiten
um den Korrekturbetrag. Eine ältere
`runtime_24h_auto_state.json` und die Tabelle `runtime_24h_auto_setting_tx`
werden ignoriert und nicht gelöscht. `0x0f0e` ist identisch mit
`Hka_Mw1.ulMotorlaufsekunden` (`Laufzeit seit Start`); ein Write verändert
deshalb auch diese Anzeige, Historie und Exporte.

Der **SC123-Grenzwertpfad** ist ausschließlich für die passende einstellbare
R-000.004.043-Firmware bestimmt. Offset 31 in Block 50 ist ein firmwareeigener
8-Bit-Heartbeat, der vor jeder Block-50-Antwort um eins erhöht wird. Der
persistente Grenzwert liegt getrennt als sechs Byte langer, versionierter
Datensatz an Block 60 Offset 40 bis 45: `53 43 31 01 Wert Komplement`. Nur
richtige Kennung, 180–240 °C und `Wert XOR Komplement = FF` gelten; sonst
nutzt die Firmware fail-closed 180 °C. ODM verlangt bei jeder Status- und
Vorschauprüfung zwei frische Block-50-Lesungen mit exakt +1 einschließlich
255→0 und zwei bytegenau gleiche Block-60-Lesungen. Stock-Firmware, Rückflash,
falsche Blocklänge oder Drift eines nicht freigegebenen Feldes sperrt den Pfad.

Ein Grenzwertvorgang nimmt nur den ganzzahligen Zielwert, ausdrückliche
Bestätigung und Hardwarefreigabe an. Das Einmaltoken bindet den vollständigen
Block-60-Ausgangszustand. Vor Authentifizierung und nochmals nach exakt
gewährtem Auth-Level 3 werden beide Blöcke doppelt frisch geprüft; aktive
Einmal-Befehlsbits in Block 50 Offset 0 oder 18 sperren das Schreiben. Der
Server ersetzt nur den Datensatz an Block 60 Offset 40 bis 45, sendet höchstens
einen vollständigen Block-60-Schreibvorgang, liest Block 60 genau einmal
bytegenau aus dem RAM zurück und prüft danach den persistenten Datensatz
begrenzt mit Lesedienst 0x04. Erst beide Nachweise bestätigen den Vorgang.
Anschließend wird Auth-Level 0 versucht; Fehler, unklarer Schreibausgang oder
unklarer Cleanup werden ohne PW4 auditiert und nie blind wiederholt. Restore
bewahrt sowohl das Block-50-Heartbeat-Byte als auch den firmwareeigenen
Block-60-Datensatz und schreibt beide nicht aus alten Sicherungsabbildern
zurück. Der zentrale generische Schreibdienst verweigert außerdem jede
tatsächliche Byteänderung an Block 50 Offset 31 oder Block 60 Offset 40 bis 45.
Browser, Kommandozeile und TUI prüfen diese Sperre vor einer Authentifizierung;
der dedizierte SC123-Ablauf ist der einzige zulässige Schreiber dieser Bytes.

Die Temperaturprüfung SC123 bleibt `Abgastemperatur > Grenzwert` für länger
als elf Sekunden; SC101 und die unabhängige S114-Hardwarekette werden nicht
verändert. Historische Herstellerstände belegen nur 180 °C und 200 °C. Der
Bereich 201–240 °C ist technisch codierbar, aber nicht durch Herstellerfreigabe
oder thermische Liveprüfung validiert. ODM enthält keinen Flashpfad und gibt die
Bedienung ohne den dynamischen Firmwarebeweis nicht frei.

Vor der Authentifizierung eines abweichenden Live-Ablaufs wird der frisch
gelesene Ausgangszustand als SHA-256-gebundenes JSON-Sicherheitsabbild atomar im
geschützten Unterverzeichnis `restore-preimages` des lokalen Datenverzeichnisses
gespeichert. Damit bleibt auch nach einem Prozess- oder Stromausfall ein
bytegenaues Vorzustandsabbild erhalten. Dry-Run und bytegleicher Live-Vergleich
benötigen und erzeugen dieses lokale Sicherheitsabbild nicht.

SHA-256 schützt hier die Inhaltsbindung und erkennt unbeabsichtigte Änderungen;
es ist keine kryptografische Signatur und bestätigt nicht die Herkunft einer
Datei. Fehlende oder unpassende Packdaten sperren jede echte
Wiederherstellung. Die Seriennummer des Abbilds wird absichtlich nicht mit der
Zielregelung verglichen, weil der Ablauf auch für den Austausch einer Regelung
vorgesehen ist. Sie bleibt dennoch als hashgebundene Herkunftsinformation im
Abbild erhalten.

Der Ablauf ist nicht transaktional: Bereits erfolgreich wiederhergestellte
Blöcke werden bei einem späteren Fehler nicht automatisch zurückgerollt.
Dynamische Zustände, Zähler und Zeitwerte können im Abbild veraltet sein. Das
Abbild kann außerdem vertrauliche Anlagen-, Adress- und Kontaktdaten enthalten;
PW4 wird dagegen nie darin gespeichert. Nach einem gesendeten Schreibtelegramm
ohne eindeutige positive Bestätigung und vollständige Rückleseprüfung gilt der
Zielzustand als unklar; vor einem weiteren Versuch ist der Block frisch zu
lesen und fachlich zu prüfen.

## Geschütztes Wartungsbackup-Archiv

Jeder Wartungsstart verwendet einen einzigen exklusiven Lesevorgang für alle
42 Sicherungsziele. Erst wenn daraus ein vollständiges
`dachs-msr2-backup/v3`-Abbild entstanden ist, dessen Abbild- und
Datei-SHA-256 geprüft wurden und dessen Datei atomar gespeichert ist, darf der
zugehörige Wartungsbericht angelegt werden. Der Anlagenstand des Berichts wird
aus denselben CPU-0-Antworten gebildet; eine zweite Blockrunde könnte bereits
einen anderen Reglerzeitpunkt abbilden und wird deshalb vermieden.

`/var/lib/open-dachs-manager/backup-archive` gehört dem Dienstbenutzer und hat
Modus `0700`; Dateien haben `0600`. Der Installer öffnet Eltern- und
Archivverzeichnis mit `O_DIRECTORY|O_NOFOLLOW` und setzt Eigentümer sowie
Rechte nur über den geöffneten Dateideskriptor. Ein vorgelegter symbolischer
Link oder ein anderes Nicht-Verzeichnis führt zum Abbruch, statt als root ein
fremdes Ziel umzueignen oder dessen Rechte zu ändern.

Listen- und Download-API sind nur für Administratoren erreichbar. Es gibt
bewusst keinen Archiv-Löschweg. Das Löschen eines Wartungsberichts lässt sein
Sicherungsabbild bestehen. Vor der Übernahme in die Wiederherstellung werden
Download, Abbildprüfsumme, Packrevision, Gerätebindung, 42/42 erfolgreiche
Sicherungsziele und exakt 36 Restore-Ziele erneut geprüft. Geprüfte ältere
38/38-Wartungsarchive bleiben unter ihrem unveränderten Altvertrag nutzbar.
Danach bleibt die Zielauswahl leer und der schreibfreie Probelauf aktiv.

Ein echter Wartungsabschluss wird vor dem ersten möglichen Reglerwrite als
`completing` gespeichert. Dieser Zustand ist gegen Wiederholung und Löschen
gesperrt. Kann ein gesendeter Auftrag nicht eindeutig per ACK und Readback
bestätigt werden, bleibt der Bericht als `uncertain` gesperrt und verlangt eine
fachliche Prüfung; die Oberfläche bietet keinen Wiederholen- oder Löschknopf.

## Netzschutz-CPUs

CPU 0 (Regler), CPU 1 und CPU 2 (Netzüberwachung) haben voneinander getrennte
Blockräume. Die live bestätigten Blöcke 16, 20 und 21 von CPU 1 und CPU 2 sind
wegen ihrer Netzschutzbedeutung rot markiert, damit sie nicht mit normalen
Reglerwerten verwechselt werden.

Alle Netzschutzblöcke sind serverseitig read/backup-only. B16 wurde nach
fachlicher Ablehnung `7F 11 01` und unveränderten Rücklesungen aus Editor, API
und Restore entfernt. Block 20 bleibt nach der physischen Abnahme nur lesbar: Beide
U-3.050-CPUs gewährten Auth 5 und quittierten den nominalen Dienst 21 positiv,
ignorierten den minimalen Testwert aber in vier Rücklesungen.

```text
Lesen → kodieren → authentifizieren → bytegenauer CAS
→ Dienst 21 → positives ACK → exakte Vollblock-Rückleseprüfung → Audit
```

Ein positiver ACK ohne passenden Readback gilt ausdrücklich als Fehler. B20
enthält Netzschutzgrenzen und Abschaltzeiten und wird deshalb weder editiert
noch restauriert oder durch einen vermeintlichen Erfolg stillschweigend
bestätigt.

Block 21 besitzt keinen Schreibdienst und bleibt als laufender Messwertblock
serverseitig strikt nur lesbar. Das Öffnen oder Aktualisieren einer roten Seite
schreibt weiterhin nichts. Block 16, 20 und 21 werden im 42-Ziele-Abbild gelesen,
auf exakte Länge und Dekodierbarkeit geprüft und gehasht, bleiben aber außerhalb
der 36 Restore-Ziele. Block 20 ist nach der physischen Abnahme nicht bestätigt schreibbar;
flüchtige Messwerte aus Block 21 dürfen niemals als Konfiguration
zurückgeschrieben werden.

## 250-h-Diagnose

Die 250-h-Auswertung ist ein reiner Offline-Lesepfad über bereits gespeicherte
SQLite-Snapshots. API, Browser und Hintergrundworker öffnen dafür keine
serielle Sitzung und führen weder Authentifizierung noch Controllerwrites aus.
Cachedatei und Fehlerzustand sind lokal und privat; eine fehlgeschlagene
Neuberechnung ersetzt niemals das letzte gültige Ergebnis. Fehlende Daten und
nicht belegbare Fehlstarts werden sichtbar als Einschränkung geführt und nicht
zu vermeintlichen Messwerten ergänzt.

## Webzugriff

Der eingebaute Webserver liefert HTTP und kein TLS. Er gehört in ein
vertrauenswürdiges lokales Netzwerk oder hinter einen korrekt eingerichteten
HTTPS-Reverse-Proxy. Den Standardport nicht direkt ins Internet weiterleiten.

Anfangspasswörter sofort ändern und `/var/lib/open-dachs-manager` als
vertrauliche Betriebsinformation schützen.

### Optionaler lokaler Bedienplatz

Der Login-Override wird niemals aus Quell-IP- oder Proxy-Headern abgeleitet.
Er existiert ausschließlich als Eigenschaft einer zweiten HTTP-Serverinstanz,
deren Socket hart an `127.0.0.1` gebunden ist. Der normale Listener kennt
diesen Principal nicht und bleibt auch bei gefälschten `Forwarded`,
`X-Forwarded-For` oder `X-Real-IP` loginpflichtig.

HTTP-Cookies sind nicht portgebunden. Der lokale Listener erzeugt und
akzeptiert deshalb keinen lokalen Admin-Cookie, sondern verwendet den
cookie-losen Principal `localhost-override` mit der konfigurierten Rolle. Ein
normaler Admin-Cookie kann eine lokale Gastrolle nicht hochstufen. Exakter
`Host`, `Origin`, Same-Origin-Request-Marker, Fetch-Metadaten, CSP und
Frame-Schutz sperren DNS-Rebinding, Cross-Site-Requests und normale
Proxyweiterleitungen. `/api/v1/*` bleibt unabhängig davon ausschließlich mit
passendem Bearer-Token zugänglich.

Ein Reverse Proxy auf demselben Rechner muss ausschließlich den normalen
Web-Port als Upstream verwenden. Würde er den Loopback-Port absichtlich
weiterleiten und Host, Origin sowie Proxyspuren passend umschreiben, könnte der
Webdienst diesen Proxy technisch nicht von einem lokalen Browser
unterscheiden. Diese Upstream-Konfiguration ist daher ausdrücklich unzulässig.

Der Override ersetzt nur die Webanmeldung. Ein lokaler Admin muss weiterhin
alle getrennten Schutzstufen eines Reglerwrites erfüllen: explizite
Schreibfreigabe, fachliche Validierung, MSR2-Authentifizierung/PW4, stabiler
Ausgangszustand, positives ACK, Readback und Audit. Im Audit erscheint er als
`localhost-override`, niemals als regulärer Passwortbenutzer `admin`.

API-Tokens werden nur beim Erstellen vollständig angezeigt und danach nur als
SHA-256-Hash gespeichert. Berechtigungen für Lesen, Historie und Schreiben
sind getrennt. Ein Schreibrecht im Token allein genügt nicht: API-Schreiben
bleibt standardmäßig global gesperrt und muss von einem Administrator bewusst
aktiviert werden. Die API akzeptiert keine fertigen Rohtelegramme oder
externen PW4-Werte. Schreibaktionen laufen durch denselben validierten
Read-/Auth-/Write-/ACK-/Readback-/Audit-Pfad wie die Weboberfläche und
benötigen eine eindeutige `request_id`. Diese wird vor dem Serialzugriff
atomar reserviert und an den konkreten Aktionsinhalt gebunden. Bei der
Generatornennleistung wird außerdem die kraftstoffabhängige Originalgrenze
vor Authentifizierung und Write geprüft.

Tokenwerte gehören wie Passwörter behandelt. Sie dürfen nicht in EDOMI-Logs,
URLs, Browser-Lesezeichen oder öffentliche Fehlerberichte gelangen.

## E-Mail-Benachrichtigungen

Der Versand ist standardmäßig deaktiviert. Ereignisse werden erst nach dem
seriellen Teil eines Monitorzyklus aus dessen frisch zeitgestempelten,
typisierten Werten abgeleitet und zusammen mit ihrem Edge-Zustand atomar in
eine lokale SQLite-Outbox geschrieben. SMTP läuft ausschließlich in einem
eigenen Hintergrundthread; weder Monitor- noch Serialworker-Lock werden
während DNS, Transportaufbau oder Versand gehalten.

Aktive Fehler erhalten eine lokal persistierte Episodennummer ausschließlich bei
0→Code oder Codewechsel. Änderungen von Störungszähler oder Reglerlaufzeit
während desselben aktiven Codes erzeugen keine zweite Mail; ein erneut
auftretender gleicher Code nach der Nullphase erhält sicher eine neue Episode.
SC122/SC124 verwenden den frisch berechneten, firmwareabhängigen Displaytext
oder ersatzweise eine grenzwertneutrale Formulierung. Die Kategorie für
verschwundene Fehler bedeutet bewusst nur
„ohne von ODM bestätigten manuellen Entstörwrite nicht mehr aktiv“; eine
Quittierung direkt am Regler kann damit nicht sicher als automatisch erkannt
werden. Wartungsmails folgen ausschließlich den gelesenen Block-104-Restwerten
und Zustandsübergängen. Temperaturalarme besitzen keinerlei voreingestellte
Grenze: Sensor, Richtung, absolute Schwelle und Hysterese müssen vom Admin
bewusst festgelegt werden. Entstörgrenzen, 250-Bh-Hinweise und
Rußfilterkennlinie werden nicht als vermeintliche Herstellergrenze
wiederverwendet.

Einstellungen und Testmail-Outbox sind nur für regulär angemeldete Admins mit
Same-Origin-Request-Marker schreibbar; der cookie-lose lokale Override darf
SMTP-Secrets weder ändern noch testen. Das Passwort liegt getrennt in einer
versionierten Datei `secrets/smtp-<generation>.json` in einem Verzeichnis mit
Modus 0700 und einer Datei mit Modus 0600. Die Konfiguration bindet atomar nur
die neue opaque Referenz; ein Crash kann höchstens eine unreferenzierte Datei
hinterlassen, niemals ein neues Passwort mit einem alten SMTP-Host koppeln.
API, DOM, Audit und Outbox erhalten das Secret nie. Standard ist
verpflichtendes STARTTLS mit TLS mindestens 1.2 und normaler Zertifikats-/
Hostnamenprüfung. Private, Loopback-, Link-Local- oder reservierte TLS-Ziele
sind gesperrt, außer root hat exakt Host und Port in
`OPEN_DACHS_SMTP_PRIVATE_ALLOWLIST` freigegeben. Zusätzlich kann ein bewusst
unverschlüsseltes internes Relay gewählt werden. Dieser Modus verbietet
SMTP-Benutzer und Passwort vollständig, lehnt öffentlich routbare Adressen ab
und verlangt die separate exakte root-Freigabe
`OPEN_DACHS_SMTP_PLAIN_ALLOWLIST=host:port`.

Jeder Empfänger besitzt einen getrennten Outbox-Eintrag und eine eigene Mail.
Settingswechsel, Beobachtung, Testmail-Enqueue sowie Claim/Versand sind innerhalb
des einzelnen Webprozesses gemeinsam serialisiert. Ein bestätigter Settings-POST
kann deshalb nicht von einem Versand mit alter Relay-/Empfängerkonfiguration
überholt werden. Eventzustand und Outbox werden atomar persistiert, Event-Keys
deduplizieren Neustarts, und dieselbe stabile Message-ID wird bei Retry
wiederverwendet.
Authentifizierungs-, Zertifikats-, STARTTLS-, Plain-Relay- und SSRF-Policyfehler pausieren
den gesamten Dispatcher neustartfest bis zur nächsten gespeicherten
Konfigurationsrevision; permanente Empfängerfehler gehen direkt ins Dead Letter, nur temporäre
Netzwerk-/4xx-Fehler werden wiederholt. Entfernte Empfänger und deaktivierte
Kategorien widerrufen wartende Nachrichten vor dem Versand.

Acht begrenzte Versuche enden im sichtbaren Dead-Letter-Zustand. Die aktive
Queue ist auf 500 Einträge begrenzt, Testmails auf eine je 60 Sekunden.
Temperaturalarme verlangen drei frische Grenzüberschreitungen, Hysterese und
eine Stunde Cooldown. Abgeschlossene Outbox-Inhalte werden nach 90 Tagen, das
Benachrichtigungsprotokoll nach 365 Tagen entfernt; wartende Nachrichten nie.
Diese Retention läuft im SMTP-Dispatcher und bleibt auch bei pausiertem
Serialmonitor aktiv.
SMTP bleibt technisch at-least-once: Nach Relay-Annahme, aber vor lokalem Commit kann ein
Neustart eine Nachricht mit derselben Message-ID erneut zustellen. „Gesendet“
bedeutet nur, dass das SMTP-Relay alle Empfänger dieser Einzelmail akzeptiert
hat, nicht dass sie im Zielpostfach zugestellt wurde.

## Reifegrad

Version 1.8.19 basiert auf dem ersten stabilen öffentlichen Stand und wird ohne
Garantie bereitgestellt. Gerätevarianten und Packstände können abweichen.
Leseergebnisse und Feldlagen am tatsächlichen Zielgerät verifizieren, bevor
Schreibvorgänge freigegeben werden.

## Sicherheitsvertrag des integrierten Updaters

Der Checker kennt nur HTTPS-Endpunkte des festen GitHub-Repositories und folgt
Redirects nur zu GitHub-Assethosts. Prereleases, gleiche/ältere Versionen,
abweichende Abhängigkeiten oder DB-Schemata und nicht rollback-kompatible
Manifeste werden nicht selbst installiert. Der lokale Loopback-Override kann
weder Anwenden noch Automatik aktivieren. Checker und Helfer führen keinerlei
Serial-, Regler-, Auth- oder Protokolloperation aus.
Der root-Helfer akzeptiert keine Argumente, URLs oder Webpfade, öffnet feste
Dateien ohne Symlink-Folge, prüft alles erneut, sperrt global, sichert SQLite
konsistent und rollt einen fehlgeschlagenen Symlinkwechsel atomar zurück.
