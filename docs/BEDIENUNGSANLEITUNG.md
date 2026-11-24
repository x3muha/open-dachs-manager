# Open Dachs Manager – Bedienungsanleitung

Stand: 24.11.2026 · Version V3 1.8.18

Diese Anleitung beschreibt den täglichen Betrieb der Weboberfläche, CLI und
TUI. Installation und Migration stehen ausführlich in
[INSTALLATION.md](INSTALLATION.md).

## 1. Schnellstart

Nach der Installation:

```bash
open-dachs doctor
open-dachs read block --block 20
sudo systemctl status \
  open-dachs-manager-serial.service \
  open-dachs-manager-web.service --no-pager
```

Weboberfläche:

```text
http://<IP-Adresse-des-Pi>:8084
```

Bei einer frischen Installation stehen die zufällig erzeugten Erstpasswörter
einmal im Installerauszug und im ersten Webdienst-Log. Das Adminpasswort nach
der ersten Anmeldung ändern. Im eigenen Hauptbereich `System` können Admins
mehrere Konten anlegen, Rollen und Status ändern, Passwörter zurücksetzen und
Konten löschen. Änderungen an einem Konto beenden dessen offene Sitzungen.

## 2. Gemeinsamer serieller Zugriff

Nur der Hintergrunddienst `open-dachs-manager-serial.service` öffnet den
Adapter. Web, CLI und TUI verbinden sich mit
`/run/open-dachs-manager/serial.sock`.

Eine Verbindung ist eine vollständige FIFO-Sitzung. Zusammengehörige Vorgänge
wie ein Wartungs-Gesamtsnapshot oder Authentifizierung, Write und Readback
bleiben ungeteilt. Mehrere Oberflächen dürfen deshalb gleichzeitig geöffnet
sein; ein großer Auftrag kann kleinere Aufträge lediglich verzögern.

Direkte alte Programme, KNX-Reader oder Diagnosewerkzeuge verwenden diese
Queue nicht. Sie dürfen nur bei gestopptem Worker auf den Adapter zugreifen.

## 3. Weboberfläche

### Rollen

- `gast`: lesen, Diagramme und Berichte ansehen
- `admin`: zusätzlich Einstellungen vorbereiten, Wartungsdaten pflegen und
  ausdrücklich freigegebene Schreibabläufe starten

Die normale Anmeldung läuft lokal. Sitzungen enden nach zwölf Stunden oder
beim Abmelden. Passwörter werden gesalzen und gehasht in der lokalen
Benutzerdatei gespeichert. Der weiter unten beschriebene optionale
Loopback-Bedienplatz besitzt absichtlich weder Passwort noch Sitzungscookie.

Die Hauptleiste ist fest geordnet als `Übersicht → Bedienplatz → Überwachung → Einstellung
→ Backup → Audit → System → Wartung → Fehlerkatalog`. `System` wird nur für
Admins eingeblendet; die Reihenfolge der übrigen Bereiche bleibt gleich.

### Übersicht

Die Startseite beginnt mit `Wirkleistung Ist / Soll`. Der Wartungsstatus steht
oben neben `Seriell OK` als verbleibende Betriebsstunden und Tage; ein Klick
öffnet direkt die Wartungsansicht. Der Leistungs-Sollwert ist für Gäste nur
lesbar. Bei einem Admin-Schreibvorgang liest der Server Seriennummer und
Betriebsstunden frisch aus der Anlage, berechnet daraus PW4 und fordert fest
Auth-Level 4 an. Positive Reglerbestätigung und Rückleseprüfung bleiben
zwingend; der Browser übermittelt weder PW4 noch Auth-Level.

`Betriebsstunden je Start` steht fest unten im Motorbereich
`Leistungszustand`. ODM liest dafür direkt den vom Regler bereitgestellten
250-Bh-Rasterwert `Motbel250[0].bQuotientBh_Start` aus Block 38. Das Rohbyte
ist in 0,1 Bh/Start skaliert und wird im langsamen 10-s-Zyklus aktualisiert;
fehlt der Block-38-Wert oder ist die Dekodierung inkonsistent, zeigt ODM
bewusst keinen Wert.

Admins können die Live-Kacheln über `Bearbeiten` frei sortieren, entfernen und
mit `+` aus allen gemappten Werten ergänzen. Auch der gelesene Reglerwert
`Betriebsstunden je Start` steht dort optional zur Auswahl, wird aber nicht
ungefragt als obere Kachel eingeblendet. Die Kachel `Laufzeit seit Start`
wird beim Update einmalig ergänzt. Ihr Hauptwert ist die **reale** Laufzeit
seit dem letzten Start (siehe unten); fehlt sie, steht dort `—`. Die
Unterzeile `Reglerzähler: …` zeigt den internen CPU-0-RAM-Zähler `0x0f0e`,
den ODM zweimal über den reinen Lesedienst liest und bei nicht kohärenten
Werten mit einem dritten Kontrollread verwirft. Der Zähler ist identisch mit
`Hka_Mw1.ulMotorlaufsekunden`, wird vom Regler bei jedem Start auf 0 gesetzt
und kann bewusst zurückgesetzt werden. Hinter dem Reglerzähler stehen die
beiden Firmwareschwellen, die sich auf diesen Zähler beziehen: ab 6 h (21.600 s) kann die tägliche Nachtabschaltung des
Reglers greifen (Reglerzeit 03:00 plus `Starts mod 100` Minuten), ab 24 h
(86.370 s) die 24-h-Abschaltung in einem ununterbrochenen Lauf. Die Restzeiten
sind ausschließlich aktive Laufzeit und keine Wanduhrprognose. Andere
Reglerbedingungen können die Anlage außerdem vorher stoppen. Die Kachel bleibt
frei entfernbar.
Ganz oben in der Systemkarte `Status und Meldungen` stehen `Display Zeile 1`
und `Display Zeile 2`: die beiden Statuszeilen der Startseite des
MSR2-Bediendisplays (zum Beispiel `Der Dachs läuft ...` und
`Rußfilterregeneration` oder bei einer Störung `Anlagenstörung` und der
Servicecode-Text). ODM rechnet sie aus Reglerdaten mit derselben
Prioritätsreihenfolge wie die Hauptfirmware nach; die Texte sind die
Originaltexte des Displays. Weil die Lesungen nicht synchron zum Display
erfolgen, kann die Anzeige bei Zustandswechseln kurz um einen Messzyklus
abweichen. Fehlt ein benötigter Wert oder ist er veraltet, erscheint `nicht
verfügbar` statt eines alten Texts. Ein Klick auf `Display Zeile 2` öffnet
die letzten 50 gespeicherten Wechsel der beiden Zeilen. Als optionale Kachel
steht `Display-Status` (Block 22) im Kacheleditor; sie ist standardmäßig
nicht eingeblendet. `/api/live` liefert die Zeilen im Feld `display_status`.

Im Prozessbild zeigt die Karte `Betriebsfreigabe` den aus Block 22 gelesenen
Zustand `Dachs Ein · Automatik` oder `Dachs Aus`. Administratoren erhalten
die beiden getrennten Knöpfe `Dachs Ein` und `Dachs Aus`; für Gäste bleiben
sie verborgen. `Dachs Ein` setzt nur die normale Automatikfreigabe. Es ist
kein Zwangsstart: Laufanforderung, übrige Freigaben und Reglerverzögerungen
gelten weiterhin. `Dachs Aus` nimmt diese Freigabe zurück und fordert den
regulären Stoppablauf an; der Knopf ist kein Not-Aus.

Der Bestätigungsdialog liest Block 22/24 sowie die festen RAM-Adressen
`0x0ea1` (Ein/Aus) und `0x1b5a` (Hand-/Aktortest) zweimal frisch über den
Serialworker. Einschalten ist nur bei Motorstatus 15/16, Servicecode 0,
inaktivem Handbetrieb und sicheren kohärenten Temperaturen möglich. Ausschalten
bleibt auch während Lauf oder Störung als kontrollierte Stoppanforderung
möglich, wird bei aktivem Hand-/Aktortest aber gesperrt. Eine Änderung erfolgt
als byteerhaltendes Lesen-Ändern-Schreiben von Bit 6 in `0x0ea1` mit Auth 3,
genau einem Schreibtelegramm, exaktem ACK/Echo, zweimaliger exakter Rücklesung,
Auth-0-Cleanup, Einmaltoken und Prüfprotokoll ohne PW4. Der Zustand liegt im
flüchtigen RAM und wird nicht als dauerhaft über einen Reglerneustart zugesagt.
`Entstören` bleibt absichtlich ein eigener Vorgang; die Oberfläche verkettet
Entstörung und Einschalten niemals automatisch.

Bei einer aktiven Störung erscheint für Administratoren daneben der Knopf
`Entstören`, sofern die Temperatur-/Wiederanlaufpolitik für die konkrete
Installation fachlich geprüft und vom Betreiber freigeschaltet wurde. Ohne
diese ausdrückliche Freigabe bleibt der Vorgang sicher gesperrt. Der Dialog
führt zunächst nur eine frische doppelte Direktlesung von Block 22 und 24 aus
und zeigt Servicecode, Motorstatus sowie die fünf geprüften Temperaturen.
Freigegeben wird ausschließlich bei Motorstatus 15 oder 16 und innerhalb der
angezeigten konservativen Grenzen. Nach dem Haken sendet ODM exakt die
bestätigte Vorschau; eine zwischenzeitlich andere Störung wird nicht still
übernommen. Der Server liest den Zustand erneut und bindet zusätzlich
Störungszähler und Zeitpunkt im Reglerlauf; selbst nach Auth 3 erfolgt
unmittelbar vor dem einzigen möglichen Write eine weitere vollständige
Prüfung. Quittiert wird
nur flüchtig über CPU 0, RAM `0x1c9e`; es werden keine Parameter oder
NVM-Werte geschrieben. Erfolg bedeutet: exaktes positives ACK/Echo, zweimal
hintereinander Servicecode 0 und bestätigte Abmeldung auf Auth 0. Der Dialog
weist ausdrücklich darauf hin, dass die Anlage bei weiter anliegender
Anforderung anschließend selbstständig anlaufen kann.

Jede Vorschau besitzt ein kurzlebiges serverseitiges Einmaltoken; Vorschau und
Ausführung sind anlagenweit nacheinander geschaltet. Nach dem Erwerb der
seriellen Web-Sperre prüft ODM den Verbindungsschalter erneut, sodass eine beim
Warten getrennte Verbindung keinen Schreibvorgang mehr beginnen kann. Nach
einem Schreibversuch sperrt ODM denselben Servicecode 30 Sekunden gegen einen
weiteren Versuch; Browser und Server senden niemals automatisch erneut. Bei
unklarem ACK, Rücklesen oder Auth-0-Aufräumen steht das im Dialog und im
Prüfprotokoll. Dann nicht noch einmal klicken, sondern zuerst Reglerzustand und
Prüfprotokoll prüfen. Vor dem einzigen Telegramm speichert ODM zusätzlich einen
Neustartschutz in seiner Datenbank. Bleibt der Ausgang unklar, sperrt dieser
auch nach einem Webdienst-Neustart jeden weiteren Entstörversuch. Er läuft
nicht automatisch ab und darf nur nach fachlicher Prüfung von Reglerzustand
und Audit durch den Support beziehungsweise einen kontrollierten manuellen
Wartungseingriff aufgelöst werden.

Die Systemkarte `Status und Meldungen` enthält zwei Unterabschnitte mit rein
lesend über denselben Dienst 0x02 und im selben 10-s-Zyklus gelesenen
CPU-0-RAM-Werten. `Rußfilterregeneration` zeigt den Regenerationszustand
(`0x000db3`: inaktiv, Regeneration läuft, unterbrochen und beim nächsten Lauf
fortgesetzt), die Betriebsminuten seit der letzten Regeneration mit der
Restzeit bis zur nächsten (`0x000de6`, Intervall 180 Betriebsminuten, ab 180
`fällig`), die Restdauer einer laufenden Regeneration (`0x000de7`, Dauer 12
Betriebsminuten), den Zähler aufeinanderfolgender Unterbrechungen samt Latch
(`0x000d82`, Servicecode 180 ab fünf) und den Dachs-Typcode. Alle Minuten sind
aktive Betriebsminuten und pausieren bei Stillstand; es gibt bewusst keinen
Prozentfüllstand. Die Anzeige wird nur bei gesetztem Rußfilterbit (Bit 3) des
bereits in Block 24 gelesenen Typcodes `Hka_Mw1.bKraftstofftyp` gelesen;
sonst steht dort `nicht unterstützt` bzw. `nicht verfügbar`, und es wird kein
RAM-Read ausgelöst. Die Zustandsdeutung stammt aus der statischen
Firmwareanalyse (Konfidenz hoch, Typzuordnung mittel-hoch); ein read-only
Live-Abgleich auf der Anlage steht noch aus. `/api/live` liefert den Block als
`soot_filter_regeneration` getrennt von der Abgastemperatur-Schätzung
`soot_filter`.
`Interne Reglerwerte` zeigt darunter drei weitere Werte: `Internes
Leistungsziel` (`0x00222e`, W), `Abgas-Regelkorrektur` (`0x000dca`, W mit
Vorzeichen) und `Verfügbare Leistungsgrenze` (`0x001ad4`, W). Es sind interne
Firmwaregrößen ohne Bedienfunktion; Adresse und Schrittweite sind belegt, die
vollständige Zustandsdeutung der Abgas-Regelkorrektur ist nur mit mittlerer
Konfidenz bekannt (ihre Vorzeichenwirkung hängt vom Regenerationszustand
`0x000db3` ab). Bei Lesefehler oder ungültiger Antwort erscheint `nicht
verfügbar` statt eines alten Werts. Die Werte werden nicht historisiert und
sind über `/api/live` im Feld `ram_telemetry` abrufbar.
Beide Gruppen stehen zusätzlich als optionale Kacheln `Rußfilterregeneration`
und `Interne Reglerwerte` über `Bearbeiten` → `+ Wert hinzufügen` (Block 24)
zur Auswahl. Sie sind standardmäßig nicht eingeblendet und werden beim Update
nicht in bestehende Kachelauswahlen eingefügt.
Admins öffnen über das Zahnrad den Schreibdialog. Die Kachelanzeige stammt aus
dem 10-s-Zyklus; der Dialog liest beim Öffnen und unmittelbar beim Absenden
zweimal kohärent direkt aus dem Regler. Eingegeben wird ein absoluter Zielwert
von `00:00:00` bis `23:59:29`, der unverändert geschrieben wird. Der Browser
sendet nur Zielwert, frischen erwarteten Istwert und die ausdrücklich gesetzte
Hardware-Schreibfreigabe. Auth-Level 3 und PW4 werden serverseitig aus frisch
gelesenen Anlagendaten bestimmt. Eine monotone Bewegung des Ausgangswerts bis 30 s ist
zulässig; Rückwärtsbewegung oder mehr als 30 s brechen vor Authentifizierung
und Schreiben ab. Die unmittelbare Rücklesung muss zwischen Zielwert und
Zielwert plus einer Sekunde liegen.

Der manuelle Schreibdialog ist für Tests gedacht. Ein gesetzter Wert verändert
die Anzeige `Laufzeit seit Start` am Regler und die Unterzeile `Reglerzähler`
in ODM; die reale Laufzeit in ODM bleibt davon unberührt.

Unter dem manuellen Write steht im selben Dialog die **automatische
Rücksetzung** (nur Admin, standardmäßig aus): „Wenn Zähler ≥ X, dann auf Y
setzen“, nach Z Rücksetzungen je Motorlauf weiterlaufen lassen (0 =
unbegrenzt; der Zähler beginnt bei jedem neuen Start neu). Grenzen: Y ≥
max(1800 s, 60 · Mindestlaufzeit), X > Y + 600 s, X ≤ 85.470 s. Mit dem
Vorschlag X = 05:33:20, Y = 03:00:00, Z = 0 erreicht der Zähler weder die
6-h-Schwelle der Nachtabschaltung noch die 24-h-Schwelle; der Dachs stoppt
dann nicht mehr von sich aus. Geschrieben wird nur bei laufendem Motor
(Status ≥ 30), ohne Servicecode und Handbetrieb, höchstens einmal je 10 min;
jeder Write steht im Audit (`runtime-auto-reset`). Das Aktivieren verlangt
eine eigene Bestätigung. Nach einem unsicheren Ergebnis schaltet sich die
Automatik ab und muss nach Prüfung neu aktiviert werden.

**Laufzeit seit Start** ist überall (Kachel, Board-Tabelle,
Motorstatus-Details, Prozessbild, Kompaktansicht) die tatsächliche Laufzeit
seit dem letzten Start aus der ODM-Historie, unabhängig von 0x0f0e. Bei einer
Aufzeichnungslücke wird sie aus den Betriebssekunden geschätzt
(`geschätzt`); im Stillstand steht dort „steht“ bzw. „steht seit …“. Der
Reglerzähler 0x0f0e erscheint nur als Unterzeile `Reglerzähler: …` (im
Prozessbild gar nicht); ist er deutlich kleiner, erscheint der Hinweis
`zurückgesetzt`. In der Historie heißt die gespeicherte Reihe des Zählers
`Reglerzähler (0x0f0e)`; die reale Laufzeit steht als abgeleitete Reihe
`Laufzeit seit Start (real)` über die History-API bereit.
Die Pfeile funktionieren auch auf Touch-Geräten. Die gemeinsame Auswahl wird
lokal gespeichert und gilt auch für den Gastzugang. Werte aus zusätzlichen
Blöcken und der 24-h-Zähler werden alle zehn Sekunden gelesen; sie vergrößern
nicht automatisch die Zeitreihen-Datenbank. Block 26 mit der Netzfrequenz
gehört dagegen zum schnellen 0,75-s-Zyklus.

Die technische Anlagenansicht zeigt am Waben-Rußfilter links die
Motorabgastemperatur und rechts die Dachs-Abgastemperatur nach dem Filter.
Darunter steht ein ausdrücklich als Schätzung gekennzeichneter Füllstand. Die
Standardkennlinie setzt 420 °C auf 0 % und 520 °C auf 100 %; dazwischen wird
linear interpoliert. Unter `Einstellung → Geschätzter Füllstand` kann der
Admin beide Temperaturen lokal ändern. Die Anzeige schreibt nie in den Regler.

### Bedienplatz

Der Reiter `Bedienplatz` ist für die lokale Touch-Bedienung bei 1366 × 768
Pixeln verdichtet. Er verwendet direkt dasselbe technische Prozessbild wie die
normale Übersicht; Werte, Meldungen und Rußfilterschätzung werden deshalb nicht
in einer zweiten Darstellung dupliziert. Beim Verlassen kehrt das Prozessbild
in die Übersicht und zu der dort zuvor gewählten Ansicht zurück.

Unter dem Prozessbild liegt ein automatisch aktualisierter Verlauf der letzten
zwei Stunden für Drehzahl, Wirkleistung und Motorabgastemperatur. Die drei
Kurven besitzen getrennte, fest beschriftete Skalen. `Überwachung öffnen`
wechselt zu den vollständigen Diagrammen und Zeitbereichen. Der direkte lokale
Aufruf lautet `http://127.0.0.1/#bedienplatz`; auch dieser Darstellungsweg
erzeugt keinen zusätzlichen seriellen Blockread.

### Überwachung

Die Überwachung schreibt ausgewählte Werte in die lokale SQLite-Datenbank und
stellt Zeitreihen dar. `Web-Serialzugriff pausieren` stoppt nur das
Web-Polling. Worker, CLI und TUI bleiben erreichbar.

Der Schnellbereich bietet 1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 24 und 48 Stunden.
Eine freie ganze Stundenzahl von 1 bis 720 wird erst durch den Button oder
Enter übernommen. Alternativ können Start und Ende mit Datum und Uhrzeit
gesetzt und gemeinsam bestätigt werden; eine einzelne Grenze ergänzt die
Oberfläche um 24 Stunden. Unbestätigte Eingaben verändern den aktiven Zeitraum
nicht.

Temperatur-, Motor-/Generator-, Motorstatus- und Abgasdiagramm verwenden immer
dieselbe Zeitachse. Ein mit der Maus gezogener Zoom wird deshalb auf alle vier Diagramme
übertragen; `Zoom zurücksetzen` oder ein Doppelklick setzt alle gemeinsam
zurück. Der Hintergrund ist leicht grün, wenn die gespeicherte Drehzahl größer
null war, und leicht rot bei Drehzahl null. Größere Messlücken bleiben neutral,
statt einen nicht belegten Betriebszustand vorzutäuschen.

Die schmale Motorstatusleiste stammt aus dem ohnehin live gelesenen Block 24:
Aus (15/16) ist weiß, Startvorbereitung (20) orange, Start und Anlasser (21–24)
rot, Lauf und Stellmotorregelung (30–35) grün und die Abschaltroutine (11–13)
violett. Störabschaltungen (10/14) sind dunkelrot. Beim Darüberfahren zeigt der
Hinweis Nummer und Originalbezeichnung, zum Beispiel `22 · Anlasser ein`.
Statuswechsel werden ereignistreu gespeichert und gehen auch in langen
Zeiträumen nicht durch die Kurvenreduktion verloren, sofern sie im etwa
0,75-sekündigen Block-24-Lesezyklus beobachtet wurden. Ein Zustand vollständig
zwischen zwei Reads kann technisch nicht erfasst werden. Schraffierte
Abschnitte sind echte Aufzeichnungslücken und werden nicht als `Aus` ausgegeben.

Die Block-24-Werte `sStellmotorGas` und `sStellmotorOel` sowie die Zustände
33/34/35 zeigen nur die Ansteuerung beziehungsweise Bewegungsrichtung
`zu`/`aus`/`auf`. Sie sind **keine absolute Stellmotorposition**. Eine
Positions-, Winkel- oder Weg-Rückmeldung ist im gemappten MSR2-Datenbestand
nicht vorhanden und darf aus den Impulsen nicht errechnet werden.

Alle regulär überwachten Livewerte werden zusätzlich zunächst 24 Stunden als
komprimierte Roh-Snapshots gehalten. Sobald die Drehzahl größer als null ist,
bleiben der komplette Motorlauf sowie je eine Stunde davor und danach in
voller Auflösung erhalten. Übrige Stillstandszeiten werden danach in
15-Minuten-Fenster mit Anfang, Ende, Minimum, Maximum, Mittelwert, Anzahl und
Änderungszahl verdichtet.

### 250-h-Diagnose

Die Registerkarte `250-h-Diagnose` berechnet abgeschlossene und laufende
250-Betriebsstunden-Intervalle aus der lokalen SQLite-Historie. Sie erzeugt
keine zusätzlichen Blockreads, keine Authentifizierung und keinen Write. Ein
Hintergrundworker aktualisiert den privaten Cache bei Dienststart, lokaler
Mitternacht, erkanntem Motorstopp oder über `Aktualisieren`. Während eines
Laufs bleibt die Seite responsiv und kennzeichnet Pending-, veraltete oder
fehlgeschlagene Cachezustände.

Datenlücken werden getrennt von fachlichen Hinweisen angezeigt. Fehlstarts sind
mangels belegter interner Reihe nicht verfügbar; Status 14 gilt weder als
Fehlstart noch als bestätigter Motorstopp. Die Diagnose verändert die
Rußfilteranzeige und deren lokale Kennlinie nicht.

### Wartung

`Wartung starten & Pflichtbackup erstellen` liest alle 42 Sicherungsziele genau
einmal in einer gemeinsamen seriellen Sitzung: 36 Reglerblöcke sowie Block 16,
20 und 21 beider Netzschutz-CPUs. Derselbe
eingefrorene Lesezustand speist gleichzeitig das vollständige JSON-Abbild und
den Anlagenstand des Berichts; es folgt keine zweite Blockrunde. Nur wenn das
Abbild atomar gespeichert und über Abbild- sowie Datei-SHA-256 geprüft ist,
entsteht der Wartungsentwurf. Der Anlagenstand enthält Rohdaten, dekodierte
Felder, Lauf-/Servicehistorien und einzelne Lesefehler.

Der daraus erzeugte Entwurf wird lokal gespeichert. Checkliste, zusätzliche
lokale Arbeitsliste, Monteur, Messwerte und Bemerkungen lassen sich fortlaufend
ergänzen. HTML-, dreiseitiger PDF-Bericht und JSON-Export stehen zur Verfügung.

V3 1.8.18 wird standardmäßig im **Testmodus** ausgeliefert. Beim Abschluss verlangt
die Oberfläche die exakte Eingabe `DEMO ABSCHLIESSEN`. Danach wird der Bericht
lokal validiert, unveränderlich archiviert und deutlich als Demo gekennzeichnet.
Der Abschluss öffnet keine Serialworker-Sitzung, schreibt weder Block 100 noch
Block 104 und setzt kein Bestätigungsbit. Das gilt auch dann, wenn ein Browser
manipulierte API-Daten sendet, weil der Schreibschutz serverseitig aktiv ist.

Unter `System → Wartungsabschluss` kann ausschließlich der Admin diesen Modus
umschalten. Die Wahl wird lokal auf dem Pi gespeichert und überlebt Neustarts
und Updates. Das Umschalten selbst öffnet keine Serialworker-Sitzung und
schreibt nichts in den Regler. Bei deaktiviertem Testmodus verlangt der echte
Hardwareabschluss weiterhin:

- vollständig bewertete Checkliste
- Adminrolle
- eines der unterstützten Auth-Level 1 bis 5 und eine gültige PW4
- die exakte Bestätigung `WARTUNG ABSCHLIESSEN`
- positives ACK und vollständigen Readback

Neben dem PW4-Feld steht im Echtbetrieb `PW4 auslesen`. Der Button liest
schreibfrei die dafür benötigten Blöcke 20 und 22, berechnet die vier Ziffern
und übernimmt sie nur in den aktuellen Wartungsentwurf. Er authentifiziert
nicht und schreibt nichts. PW4 wird weder im Bericht noch im Audit gespeichert
und bei Berichtswechsel, Verlassen der Wartung, Logout, Abschlussversuch oder
spätestens nach 60 Sekunden wieder aus der Oberfläche entfernt.

Erst in diesem ausdrücklich freigeschalteten Echtbetrieb würden die gemappten
Wartungswerte übertragen, zurückgelesen und danach das Bestätigungsbit separat
gesetzt und nochmals gelesen. Freitext, Zusatzarbeiten und vollständige
Historie verbleiben immer lokal.

### Lokale Zwischenwartung

Im Wartungseditor steht Admins bei einem offenen Entwurf unter dem normalen Abschluss ein eigener Bereich **Zwischenwartung** zur Verfügung. Der Grund ist ein Pflichtfeld (maximal 500 Zeichen). **Zwischenwartung speichern** archiviert den aktuellen – ausdrücklich auch nur teilweise ausgefüllten – Protokollstand, Snapshot, Pflichtbackup und Exporte lokal. Offene reguläre Prüfpunkte werden nicht als erledigt ausgegeben. Dabei werden keine Zugangsdaten benötigt, keine Serialworker-Sitzung geöffnet und weder Block 100 noch Block 104 verändert.

Die Nummer wird dauerhaft aus dem beim Start eingefrorenen Regler-Wartungszähler gebildet: bei Stand 4 zuerst `4.1`, danach `4.2`. Fehlt der Zähler, verwendet ODM den höchsten bereits gespeicherten lokalen oder erfassten Basisstand; ohne solchen Stand beginnt die Reihe bei `0.1`. Liste, Detailansicht sowie HTML-, PDF- und JSON-Ausgabe kennzeichnen die Zwischenwartung und zeigen den Grund.

Admins können im lokalen Archiv offene Entwürfe und abgeschlossene Wartungen
über `Löschen` entfernen. Vorher erscheint eine Sicherheitsabfrage. Gelöscht
werden der lokale Anlagenstand, das Protokoll und seine Exporte. Das beim
Wartungsstart erzeugte Pflichtbackup bleibt getrennt im geschützten
Backup-Archiv erhalten. Ein bereits ausgeführter MSR2-Abschluss wird dadurch
nicht rückgängig gemacht; vorhandene Schreib-Audits bleiben erhalten.

Während eines echten Abschlusses zeigt die Liste `Abschluss läuft`. Dieser
Zustand kann weder erneut abgeschlossen noch gelöscht werden. Konnte nach
einem Schreibversuch kein eindeutiges Endergebnis belegt werden, erscheint
`Zielzustand unklar – prüfen` in Rot. Auch dieser Bericht bleibt gesperrt, bis
Reglerzustand, positive Bestätigung, Rückleseprüfung und Audit fachlich geklärt
sind.

### Backup und Wiederherstellung

Der Hauptbereich `Backup` ist für angemeldete Benutzer sichtbar. Beim Öffnen
sind alle 42 Sicherungsziele ausgewählt: 36 adressierbare Blöcke der
Regler-CPU 0 sowie Block 16, 20 und 21 der Netzschutz-CPU 1 und CPU 2. Über
`Alle auswählen`, `Auswahl aufheben` und die einzelnen Blockkarten kann eine
beliebige Teilmenge zusammengestellt werden. `Backup erstellen und
herunterladen` liest nur diese Ziele in einer gemeinsamen Serialworker-Sitzung
und speichert ein JSON-Abbild. Einzelne Lesefehler bleiben darin sichtbar.

Administratoren sehen darüber das geschützte Wartungsbackup-Archiv. Jede Karte
nennt Backup-ID, Zeitpunkt und Ersteller, Herkunft, verknüpften
Wartungsbericht, Zustand, Integrität, Größe, Packrevision, das Ergebnis 42/42,
die 36 Restore-Ziele sowie Abbild- und Datei-SHA-256. Es gibt bewusst weder Löschen noch einen Knopf
für manuelle Autoarchivierung. `JSON herunterladen` liefert die archivierte
V3-Datei unverändert. `Für Wiederherstellung laden` lädt dieselbe Datei und
prüft sie erneut über die bestehende Abbildprüfung. Neue Archive benötigen
42/42 erfolgreiche Sicherungsziele mit exakt 36 Restore-Zielen. Geprüfte alte
38/38-Wartungsarchive bleiben kompatibel. Prüfsummen, passende Packrevision
und Gerätebindung sind in beiden Fällen zwingend; die Zielauswahl bleibt leer
und der Dry-Run aktiv.

Im Abbild wird jedes Ziel als eindeutiges Paar aus CPU und Blocknummer geführt.
Dadurch bleiben `CPU 1 · Block 16` und `CPU 2 · Block 16` trotz gleicher
Blocknummer getrennt. Vollständige Abbilder können Anlagenkennung, Adresse,
Kontaktdaten und sicherheitsrelevante Netzschutzparameter enthalten und müssen
entsprechend vertraulich aufbewahrt werden. PW4 oder andere Passwörter werden
nicht gespeichert.

Die Wiederherstellung ist nur für Administratoren sichtbar. Zuerst wird das
JSON-Abbild eingelesen und serverseitig auf Schema, Prüfsummen, Packstand,
Reglerkennung, Blocknummern und Payloadlängen geprüft. Erst danach erscheinen
die gültigen Blöcke; aus Sicherheitsgründen ist zunächst keiner ausgewählt.
Die Seriennummer bleibt als Herkunftsinformation im hashgebundenen Abbild
erhalten, wird aber bewusst nicht mit der Zielregelung verglichen. Damit kann
das Abbild auch auf eine Austauschregelung mit anderer Seriennummer übertragen
werden.

`Auswahl als Dry-Run prüfen` liest die aktuellen Zielblöcke und zeigt
`unverändert` oder `würde geschrieben`, sendet aber weder Authentifizierung noch
Schreibtelegramme. Für eine echte Wiederherstellung müssen zusätzlich
Hardwarefreigabe, Auth-Level beziehungsweise PW4 und exakt
`SICHERUNG WIEDERHERSTELLEN` bestätigt werden. Bytegleiche Blöcke werden auch
dann ohne Schreibtelegramm übersprungen. Abweichende Blöcke werden vollständig
geschrieben und nur nach positiver Bestätigung sowie bytegenauer vollständiger
Rückleseprüfung als wiederhergestellt gemeldet.

Vor der Authentifizierung eines tatsächlich abweichenden Live-Ablaufs liest der
Dienst die Auswahl nochmals und speichert diesen unmittelbaren Ausgangszustand
atomar als SHA-256-gebundenes JSON-Abbild unter
`/var/lib/open-dachs-manager/restore-preimages/`. Verzeichnis und Dateien sind
lokal mit `0700` beziehungsweise `0600` geschützt. Ein Dry-Run oder ein
bytegleicher Live-Vergleich legt kein solches Abbild an, weil dabei kein
Schreibtelegramm folgen kann. Die Prüfsumme bindet den Inhalt, stellt aber keine
kryptografische Signatur oder Herkunftsbestätigung dar.

Eine Mehrblock-Wiederherstellung ist nicht transaktional. Ein Fehler wird am
betroffenen Block angezeigt; die übrigen ausgewählten Blöcke werden trotzdem
versucht. Erfolgreich zurückgelesene Blöcke bleiben wiederhergestellt.
Softwareversionsfelder gehören zur Zielplatine und werden nach dem Vorbild des
originalen Senertec-Platinentauschs grundsätzlich aus dem aktuellen Zielzustand
beibehalten. Laufzustände, Zähler oder Zeitwerte im Abbild können älter
sein und werden bei Auswahl des jeweiligen Rohblocks ebenfalls übernommen. Hat
der Dienst bereits ein Schreibtelegramm gesendet, aber keine eindeutige
Bestätigung oder Rückleseprüfung erhalten, wird der Block ausdrücklich als
`Zustand unklar` gekennzeichnet und muss vor jedem weiteren Versuch neu gelesen
und geprüft werden.

### SC123-Abgasgrenze (nur passende Patch-Firmware)

Unter `System → Firmware` befindet sich die Karte
`Abgas-Abschaltgrenze 180–240 °C`. Zuerst `Firmwarefähigkeit prüfen`
wählen. ODM liest Block 50 und Block 60 jeweils zweimal frisch. Nur wenn der
Patch-Heartbeat in Block 50 exakt um eins weiterläuft, die übrigen relevanten
Block-50-Bytes stabil bleiben und Block 60 bytegenau stabil ist, werden Wert
und Schaltfläche freigegeben. Bei Stock-Firmware oder einem Rückflash bleibt
die Karte gesperrt; die sichtbare Firmwareversionsnummer allein genügt nicht.

Dann eine ganze Temperatur zwischen 180 und 240 °C wählen und
`Änderung frisch prüfen` drücken. Die Vorschau zeigt Istwert, Zielwert und den
Firmwarebeweis. Erst nach Setzen des Bestätigungshakens kann
`Grenze einmalig schreiben` ausgeführt werden. ODM authentifiziert sich mit
Level 3, prüft beide Blöcke und den vollständigen Ausgangszustand nochmals,
setzt in Block 60 ausschließlich den sechs Byte langen Datensatz aus Kennung,
Wert und Komplement und schreibt höchstens einmal. Danach folgen genau eine
bytegenaue Block-60-RAM-Rücklesung, eine begrenzte Lesekontrolle des
persistenten NVM-Datensatzes und Auth 0. Bei Fehler oder unklarem Ergebnis nicht
sofort wiederholen; zuerst Status und Prüfprotokoll kontrollieren.

Die Firmware wertet weiterhin `Dachs-Abgastemperatur > Grenzwert` für länger
als elf Sekunden aus. Eine falsche Kennung, ein Wert außerhalb 180–240 °C oder
ein falsches Komplement aktiviert den sicheren Fallback 180 °C. Historisch
belegt sind nur 180 °C und 200 °C. Werte von 201 bis 240 °C sind zwar technisch
codierbar, aber nicht durch eine Herstellerfreigabe oder thermische
Liveprüfung validiert. Die Oberfläche installiert oder flasht keine Firmware.

### Einstellung und Register

**Täglicher Uhrabgleich (nur Admin).** Das Panel `Regleruhr` stellt die
Regleruhr einmal täglich zur gewählten ODM-Zeit (06:00–22:00) auf die
ODM-Zeit, wenn der Versatz die Schwelle (Standard 30 s) erreicht. Es ist
standardmäßig aus; das Aktivieren verlangt eine eigene Bestätigung.
`Jetzt synchronisieren` führt nach Rückfrage dieselben Prüfungen sofort aus.
Übersprungen wird ohne NTP-Synchronität, unter der Schwelle, über dem
Maximalversatz oder wenn der Sprung Mitternacht, die Nachtabschalt-Sekunde
(03:00 + Starts mod 100 min) oder eine Zeitumstellung ±15 min berührt.
Status, letzter Lauf und Grund stehen im Panel; jeder Stellversuch steht im
Audit (`controller-clock-sync`). Die Firmware stellt dabei RAM-Uhr und RTC.

Der integrierte `Fehlerkatalog` löst bekannte Service- und Warncodes direkt in
deutschen Klartext auf. Die Suche nimmt Code oder Text an; beispielsweise wird
`SC 163` exakt als `Leistung zu klein` angezeigt. Aktive Meldungen im
Anlagenbild sowie die Servicehistorie verwenden denselben Katalog. Eine
optionale lokale Diagnosedatei kann Ursachen und Maßnahmen ergänzen. Für einen
noch nicht zugeordneten Code bleibt die Nummer sichtbar und wird ausdrücklich
als unbekannt gekennzeichnet.

Die Registeransicht gruppiert alle adressierbaren Mapping-Felder nach Block.
Auswahllisten und Wertebereiche sind Eingabehilfen; ein Rohwert-Fallback bleibt
für unbekannte Varianten erhalten.

In Block 20 liegt `Zeitsynchronisierung aktiv` physisch an Offset 41. Die
Originaloberfläche dokumentiert keine Wertetabelle; `0` und `1` werden deshalb
als plausibel inaktiv/aktiv beschriftet, andere Werte bleiben unbekannte
Rohwerte. Offset 40 ist der Display-Kontrast. In Block 24 wird der
Kraftstofftyp mit den exakten Originalbezeichnungen angezeigt, beispielsweise
`8 (Heizöl EL)`. `Luftdruck` ist ein barometrischer 16-Bit-Rohwert ohne
dokumentierte Einheit oder Skalierung und ist nicht auf Gasanlagen beschränkt;
ein gelesener Wert `0` wird nicht künstlich umgerechnet. Die Temperatur an
Offset 20 heißt `Kapseltemperatur` und bleibt in °C skaliert.

Die gemeinsame Dachs-Laufhistorie der Blöcke 28, 30, 31 und 32 zeigt die fünf
Abschaltwerte zusätzlich als Hexwert und Klartext. Dabei handelt es sich um
eine aktive-low 16-Bit-Freigabemaske, nicht um Servicecodes; mehrere gelöschte
Bits ergeben mehrere gleichzeitig angezeigte Abschaltgründe.

Ohne aktivierte Hardwarefreigabe erzeugt Speichern lediglich einen Dry-Run.
Vor jedem echten Write werden Block, Feld, Datentyp, Authentifizierung und der
aktuelle Payload geprüft.

Zusätzlich erscheinen je Überwachungs-CPU drei rote Registerkarten. Die
Blocknummern der drei CPUs sind voneinander unabhängig; Block 20 der
Netzschutz-CPU ist also nicht Block 20 der Regler-CPU.

- `B16 · Netzschutz (Legacy)` zeigt das bekannte 18-Byte-Layout mit
  Ländercode, Schutzart, festen und variablen Spannungs-/Frequenzgrenzen,
  Abschaltzeiten und Impedanzschutz. Nach der fachlich abgelehnten
  Vollblock-Rücksicherung bleibt auch dieser Block strikt nur lesbar.
- `B20 · Schutzparameter` zeigt 39 Werte aus 59 Byte: Schutzprofil,
  zweistufige Grenzen und profilabhängige Abschaltzeiten, 10-Minuten-Spannung,
  Frequenzreduktion sowie Impedanz-/LOM-Status. Die originale
  Layout-4-Datenzuordnung leitet dafür nominal Dienst 21 ab. Beide U-3.050-CPUs
  bestätigten einen Testwrite, übernahmen ihn aber nicht; die 39 Felder bleiben
  deshalb strikt nur lesbar.
- `B21 · Live-Netzwerte` zeigt 28 Werte aus 56 Byte: Spannung, Strom und
  Frequenz je Phase, Impedanzen, Wirkleistung, Phasenlage, Cosinus Phi und
  Kalibrierfaktoren. Dieser laufende Messwertblock besitzt keinen Schreibdienst
  und bleibt strikt nur lesbar.

Auch die 18 Legacy-Felder von Block 16 je CPU bleiben nur lesbar: Beide CPUs
lehnten selbst eine unveränderte Vollblock-Rücksicherung fachlich mit
`7F 11 01` ab; vier Rücklesungen blieben bytegleich. Der physische B20-Test
ist abgeschlossen: Auth 5 und ACK waren positiv, der einzelne
Testwert blieb auf beiden CPUs nach vier Rücklesungen unverändert. B20 wird
daher nicht als editierbar oder erfolgreich restaurierbar angeboten.
Block 16, 20 und 21 werden deshalb im 42-Ziele-Backup nur gelesen, geprüft und
gesichert. Wiederherstellbar bleiben ausschließlich die 36 Reglerblöcke. Für
B16 und B20 fehlt nach den abgeschlossenen, negativen Livebefunden ein sicherer
Write-/Recovery-Vertrag; Block 21 darf als flüchtiger Messwertblock niemals als
Konfiguration zurückgeschrieben werden. Das reine Öffnen oder
Neuladen einer Netzschutzkarte sendet weiterhin nur ein Lesetelegramm.

Die geprüften Standarddefinitionen enthalten für die Netzüberwachungs-CPUs
keine weiteren Datenblöcke. Die zusätzlich live gelesenen Diagnosedienste 17
und 18 liegen nur als Rohbefund ohne belegte Feldstruktur vor und erscheinen
daher nicht als dekodierte Einstellfelder.

Profilbezeichnungen wie `VDE 4105` stammen aus der historischen
Controllerdefinition und sind kein Nachweis der Konformität mit einer heutigen
Normausgabe. Cosinus-Phi-Werte können bei stehender Anlage geringfügig über
1 liegen und sind dann als Diagnosewert zu verstehen.

Der Aufbau von Zieladressen und Block-Lesevorgängen ist in
[PROTOKOLL.md](PROTOKOLL.md) bytegenau beschrieben.

### System, Benutzer und API-Tokens

Der Hauptbereich `System` ist nur für Admins sichtbar und ausdrücklich von
den Dachs-Einstellungen getrennt. Unter `Benutzer & Berechtigungen` lassen
sich mehrere Gast- und Admin-Konten verwalten. Mindestens ein aktiver Admin
bleibt erzwungen; das aktuell verwendete Admin-Konto kann sich nicht selbst
deaktivieren oder löschen.

Im selben Bereich kann `Lokaler Benutzer` für einen unmittelbar am Server
angeschlossenen Touch-Bedienplatz aktiviert werden. Einstellbar sind:

- `Aktiv`
- Port als ganze Zahl von 1 bis 65535; Standard 80
- Rechte `Admin` oder `Gast`; Standard Admin

Die Funktion selbst ist standardmäßig deaktiviert. Nach dem Speichern bindet
der Dienst den gewählten Port ausschließlich an `127.0.0.1`; ein Portwechsel
wird sofort wirksam. Die angezeigte Adresse lautet beispielsweise
`http://127.0.0.1/` beziehungsweise mit Base Path
`http://127.0.0.1/dachs/`. Ein belegter Port oder der normale Web-Port
wird abgewiesen, ohne den bisherigen Listener oder die gespeicherte Einstellung
zu ersetzen.

Der lokale Benutzer wird intern eindeutig als `localhost-override` geführt
und verwendet keinen Cookie. Ein auf `Gast` gesetzter lokaler Listener bleibt
deshalb auch dann Gast, wenn der Browser einen normalen Admin-Cookie mitsendet.
Der normale Port und der Reverse Proxy verlangen weiterhin einen regulären
Login; `/api/v1/*` bleibt auf beiden Ports Bearer-Token-pflichtig. Der lokale
Admin darf dieselben Webfunktionen wie ein normaler Admin bedienen, umgeht aber
nicht die gesonderte Schreibfreigabe, MSR2-Authentifizierung/PW4, ACK,
Rückleseprüfung oder das Audit.

Unter `API & Tokens` wird die lokale EDOMI-API verwaltet. Ein Token erhält
getrennte Rechte:

- `read`: Livecache, Katalog und kontrollierte Block-Lesevorgänge
- `history`: adaptive Roh- und Verdichtungsdaten
- `write`: serverseitige Aktion `set-value`

Das vollständige Token wird nur unmittelbar nach der Erzeugung angezeigt;
dauerhaft liegt ausschließlich sein SHA-256-Hash vor. API-Schreiben ist
zusätzlich global standardmäßig deaktiviert. Erst wenn ein Admin es im
Systembereich freischaltet, kann ein aktives Token mit `write`-Recht eine
Aktion auslösen. EDOMI übergibt dabei weder PW4 noch rohe Blockbytes.

Beispiel:

```http
POST /api/v1/actions/set-value
Authorization: Bearer <Token>
Content-Type: application/json

{"block":50,"key":"Hka_Ew.usSollGenerator","value":4.7,"request_id":"edomi-4711"}
```

Die eindeutige `request_id` verhindert, dass eine bereits verarbeitete
Anfrage bei einer Wiederholung nochmals geschrieben wird. Sie ist fest an
Token, Block, CPU, Feld und Wert gebunden. Eine parallele Wiederholung oder die
Wiederverwendung mit anderem Inhalt liefert HTTP 409 und löst keinen zweiten
Write aus. Für die Generatornennleistung prüft der Server vor Auth und Write
zusätzlich den vom Regler gelesenen Kraftstofftyp und dessen dokumentierten
Leistungsbereich. HTTP bleibt für das lokale Netz verfügbar; eine externe
TLS-Terminierung erfolgt bei Bedarf vor dem Dienst durch nginx.

### E-Mail-Benachrichtigungen

Unter `System → E-Mail` kann ein regulär angemeldeter Admin SMTP und Empfänger
einrichten. Der Hauptschalter ist zunächst aus. Auswählbar sind `Wartung`,
`Neuer aktiver Fehler`, `Fehler verschwindet ohne bestätigte ODM-Entstörung`
und `Temperaturgrenze`. Betreff und Nachrichtentext jeder Kategorie lassen
sich direkt bearbeiten; die Oberfläche zeigt die erlaubten Platzhalter.

Temperaturmeldungen besitzen absichtlich keine Vorgabeschwelle. Für jeden
Sensor werden Aktivierung, Richtung, absolute °C-Schwelle und Hysterese
festgelegt. Eine Mail entsteht erst nach drei frischen Grenzmessungen; nach
einer Meldung gilt zusätzlich eine Stunde Cooldown. Nach dem Speichern kann
eine Testmail eingereiht werden, auch wenn
der automatische Hauptschalter noch aus ist. Der Ausgang zeigt Empfänger,
Status, Versuche und Fehler. `Gesendet` bestätigt die Annahme durch das Relay;
`Aufgegeben` bedeutet acht erfolglose Versuche. Mehrere Empfänger erhalten
getrennte Nachrichten und sehen die anderen Adressen nicht.

Die öffentliche ODM-Adresse muss mit `https://` beginnen und einen
gegebenenfalls verwendeten Base Path enthalten. STARTTLS mit TLS ≥1.2 ist der
Standard. Ein privates TLS-Relay benötigt eine rootseitige Allowlist. Für ein
internes Relay ohne TLS gibt es den gesonderten Modus `plain-internal`; er
erlaubt weder SMTP-Benutzer noch Passwort, nur interne Zieladressen und eine
separate rootseitige Host:Port-Freigabe. Beide Sperren können nicht aus der
Weboberfläche gelockert werden. Passwortfelder bleiben nach dem Laden leer;
leer lassen erhält im STARTTLS-Modus das gespeicherte Secret, das gesonderte
Löschhäkchen entfernt es. Ein Authentifizierungs-, TLS- oder Zielpolicyfehler pausiert alle weiteren
Versuche, bis ein Admin die Konfiguration korrigiert und erneut speichert.
Entfernte Empfänger oder abgewählte Kategorien widerrufen wartende Mails.

### Audit

Schreibversuche und deren Ergebnis werden lokal protokolliert. Ein fehlendes
ACK oder abweichendes Readback gilt als Fehler, nicht als erfolgreicher Write.

## 4. CLI-Grundlagen

Globale Optionen stehen vor dem Unterkommando:

```text
open-dachs [Optionen] <Kommando> [Kommandooptionen]
```

Wichtige globale Optionen:

| Option | Standard | Bedeutung |
|---|---:|---|
| `--serial-socket` | `/run/open-dachs-manager/serial.sock` | gemeinsamer Worker |
| `--port` | `/dev/ttyUSB0` | nur für direkten Wartungsmodus |
| `--baud` | `19200` | Baudrate |
| `--timeout` | `0.9` | Antworttimeout |
| `--direct-serial` | aus | Worker umgehen; nur bei gestopptem Worker |
| `--pack-rev` | `50` | Mappingrevision |

Diagnose:

```bash
open-dachs doctor
open-dachs doctor --json
```

Blöcke und Felder suchen:

```bash
open-dachs list-blocks --addressable-only
open-dachs list-keys --block 20
open-dachs list-keys --search temperatur
```

## 5. Lesen

Rohblock:

```bash
open-dachs read block --block 20
open-dachs read block --block 20 --json
```

Dekodierte Blöcke:

```bash
open-dachs read decoded --blocks 20,22,24,26
open-dachs read decoded --blocks 20,22 --json
```

Link beobachten:

```bash
open-dachs watch --count 10 --interval 0.5
```

CRC- und Protokollfehler werden getrennt gezählt. Ungültige Frames werden
verworfen.

## 6. Backups

Ausgewählte Blöcke:

```bash
open-dachs backup create \
  --blocks 20,22,24,26 \
  --output open-dachs-backup.json
```

Alle über das Einbyte-Protokoll adressierbaren Blöcke:

```bash
open-dachs backup create \
  --all-blocks \
  --output open-dachs-full-backup.json
```

Backups enthalten Geräte- und Konfigurationsdaten und dürfen nicht ungeprüft
veröffentlicht werden. Automatische Wartungsbackups liegen ausschließlich im
geschützten Serverarchiv; die Weboberfläche erlaubt dort nur Anzeige,
unveränderten Download und erneutes Prüfen für die Wiederherstellung.

## 7. Authentifizierung

```bash
open-dachs auth --level 4
```

Das Programm liest die benötigten Gerätedaten und berechnet die vierstellige
PW4. Eine manuelle Vorgabe ist möglich:

```bash
open-dachs auth --level 4 --pass4 1234
```

PW4 nur bewusst mit `--show-secret` anzeigen und nie in gemeinsame Logs
kopieren.

Zulässig sind ausschließlich Auth-Level 1 bis 5. Die Original-Java-Oberfläche
benennt Rollen 1 bis 4; die älteren V2-Schreib- und Backup-Werkzeuge verwenden
zusätzlich Level 5, der deshalb in den Auswahllisten neutral `Legacy/V2` heißt.
Das Protokollfeld ist zwar ein Byte breit, der getestete Regler spiegelt aber
auch höhere Werte zurück. CLI, TUI, Web und API weisen deshalb 0 sowie Werte ab
6 bereits vor dem seriellen Auth-Lesevorgang ab und akzeptieren nur eine exakt
zum angeforderten Level passende Antwort.

## 8. Schreiben mit der CLI

Dry-Run:

```bash
open-dachs write plan \
  --block <BLOCK> \
  --key '<TECHNISCHER_KEY>' \
  --value '<NEUER_WERT>'
```

Echter Write:

```bash
open-dachs write apply \
  --block <BLOCK> \
  --key '<TECHNISCHER_KEY>' \
  --value '<NEUER_WERT>' \
  --auth-level <LEVEL> \
  --write-enabled
```

`--write-enabled` allein reicht nicht: Auth-Level, ACK und bytegenaues
Readback müssen ebenfalls erfolgreich sein.

## 9. TUI

Nur lesen bzw. Änderungen vorbereiten:

```bash
open-dachs tui --block 20 --all-blocks --dry-run
```

Schreibfähiger Start:

```bash
open-dachs tui \
  --block 20 \
  --all-blocks \
  --auth-level <LEVEL> \
  --write-enabled
```

Tasten:

| Taste | Funktion |
|---|---|
| Pfeil hoch/runter oder `k`/`j` | Feld wählen |
| Pfeil links/rechts | Block wechseln |
| `Enter` | Feld bearbeiten |
| `F2` oder `s` | Dry-Run bzw. Speichern starten |
| `r` | Block neu laden |
| `q`, `Esc`, `F10` | beenden |

Die TUI hält die Queue nicht während der gesamten Bedienzeit. Laden und
Speichern sind jeweils eigene kurze Sitzungen.

## 10. Updates und Datensicherung

Normale Aktualisierung: Als angemeldeter Administrator `System → Updates` öffnen.
ODM zeigt neue stabile, signierte Versionen immer an; installiert wird erst nach
Passwort, exakter Vorschau und Klick. Automatische Aktualisierungen sind
standardmäßig aus und können dort ausdrücklich aktiviert werden.

Der Aufruf `sudo ./install.sh` aus einem separat geprüften Quellbaum bleibt
ausschließlich als manueller Wiederherstellungsweg erhalten.

Dateisicherung der lokalen Webdaten:

```bash
sudo systemctl stop open-dachs-manager-web.service
sudo tar -C /var/lib -czf open-dachs-manager-data.tgz open-dachs-manager
sudo systemctl start open-dachs-manager-web.service
```

Die Daten liegen standardmäßig unter `/var/lib/open-dachs-manager`.

## 11. Fehlerbehebung

Worker und Web prüfen:

```bash
sudo systemctl status \
  open-dachs-manager-serial.service \
  open-dachs-manager-web.service --no-pager

sudo journalctl \
  -u open-dachs-manager-serial.service \
  -u open-dachs-manager-web.service -n 100 --no-pager
```

Portbesitzer prüfen:

```bash
fuser -v /dev/ttyUSB0
```

Im Normalbetrieb darf nur der Open-Dachs-Worker erscheinen. Weitere
Fehlerbilder und der direkte Wartungsfallback stehen in
[OPERATIONS.md](OPERATIONS.md).

## Open Dachs Manager aktualisieren

Unter **System → Updates** ist jederzeit sichtbar, ob eine neuere stabile
Version vorliegt. Für ein manuelles Update zuerst **Jetzt prüfen**, dann das
Admin-Passwort für die exakte Vorschau eingeben. Erst die vollständig
übernommene Bestätigungszeile schaltet die Installation frei. Automatische
Updates sind ab Werk aus und können sichtbar ein- oder ausgeschaltet werden.
