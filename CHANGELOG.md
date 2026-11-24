# Änderungsverlauf

## V3 1.8.17 - 2026-11-24

- Die Netzfrequenzkurve und ihre Y-Achse verwenden jetzt dieselbe tatsächlich
  dargestellte Schrittweite. Achsgrenzen werden nach weiterhin mindestens
  0,100 Hz Abstand zu den Messwerten auf 0,05-/0,1-Hz-Raster ausgerichtet;
  unpassende Zwischenwerte wie 50,167 / 50,082 / 49,996 entfallen. Der
  Asset-Cache-Schlüssel für `app.js` wurde erhöht.
- `Betriebsstunden je Start` wird nicht länger aus den Lebenszeitzählern von
  Block 22 berechnet. ODM liest den vom Regler gelieferten 250-Bh-Rasterwert
  `Motbel250[0].bQuotientBh_Start` aus Block 38 im langsamen 10-s-Zyklus und
  zeigt dessen originale 0,1-Bh/Start-Skalierung. Gespeicherte Kacheln mit dem
  alten berechneten Schlüssel werden automatisch auf den Reglerwert migriert.
- Die signierte stabile Aktualisierung trennt unprivilegierte Prüfung und
  Bereitstellung vom fest installierten Root-Hilfsdienst. Veröffentlichungs-,
  Hash-, Alters-, Rückfall-, Speicherplatz-, Sicherungsabbild-, Dienst- und
  Zustandsprüfverträge sperren bei Fehlern; jede Phase ist ausfallsicher belegt.
  Automatische Aktualisierungen benötigen eine
  Passwort-Reauthentifizierung, ausdrückliche Bestätigung und ein striktes
  Wartungsfenster.
- Der neue Adminbereich `System → E-Mail` bietet standardmäßig deaktivierte,
  auswählbare Benachrichtigungen für Wartung, aktive Fehler, ohne bestätigten
  ODM-Entstörwrite verschwundene Fehler und bewusst konfigurierte
  Temperaturgrenzen. Betreff und Text sind je Kategorie editierbar; es gibt
  keine erfundene Temperaturvorgabe.
- Ereigniskante und getrennte Empfänger-Outbox sind atomar in SQLite
  gespeichert. SMTP läuft außerhalb des Serialworkers, verwendet stabile
  Message-IDs, begrenzte Retries und einen Dead-Letter-Status. Jeder Empfänger
  erhält eine eigene Mail. STARTTLS mit TLS ≥1.2 ist Standard; private TLS-Ziele
  brauchen eine rootverwaltete Host:Port-Allowlist. Ein optionales internes
  Klartext-Relay ist nur zu einer getrennt rootfreigegebenen privaten Adresse
  und vollständig ohne SMTP-Zugangsdaten erlaubt. Das SMTP-Passwort liegt
  getrennt in generationengebundenen 0700/0600-Secretdateien und erscheint
  weder in API, DOM, Outbox noch Benachrichtigungsprotokoll. Konfigurations-
  fehler öffnen einen Circuit-Breaker; entfernte Ziele werden widerrufen.
  Queue-Limit, Testmail-Rate-Limit, Temperatur-Dwell/Cooldown und 90-/365-Tage-
  Retention begrenzen Flooding und private Langzeitspeicherung.

## V3 1.8.16 - 2026-11-06

- Unter `System → Firmware` kann ein Administrator mit der dazu passenden,
  einstellbaren R-000.004.043-Firmware die persistente SC123-Abgasgrenze in
  ganzen Grad von 180 bis 240 °C setzen. Die sichtbare Versionsnummer genügt
  nicht: Zwei frische Block-50-Lesungen müssen am reservierten Offset 31 den
  exakten +1-Heartbeat zeigen, während die übrigen stabilen Felder unverändert
  bleiben. Stock-Firmware oder ein Rückflash sperrt die Bedienung dadurch
  automatisch.
- Der Grenzwert liegt nicht mehr in einem einzelnen unversionierten Reservebyte.
  Block 60 enthält an Offset 40 bis 45 den firmwareeigenen Datensatz
  `53 43 31 01 Wert Komplement`. Nur richtige Kennung, 180–240 °C und
  `Wert XOR Komplement = FF` sind gültig; jede andere oder uninitialisierte
  Kombination aktiviert in der Firmware den sicheren Fallback 180 °C.
- Vorschau und Schreiben lesen Block 50 und Block 60 jeweils zweimal frisch.
  Das Einmaltoken bindet den vollständigen Block-60-Ausgangszustand. Vor Auth 3
  und nochmals unmittelbar vor dem Schreiben müssen Firmwarefähigkeit,
  Block-60-Stabilität, Ausgangszustand und Block-50-Befehlsbits unverändert
  passen. Danach wird höchstens einmal der vollständige Block 60 geschrieben,
  genau einmal bytegenau aus dem RAM rückgelesen und der persistente
  Sechs-Byte-Datensatz begrenzt über Lesedienst 0x04 geprüft. Auth 0 und
  Prüfprotokoll ohne PW4 bleiben Pflicht; nach einem Schreibversuch verhindert eine
  30-s-Sperre automatische Wiederholung.
- Historische Herstellerstände belegen nur 180 °C und 200 °C. Werte von 201 bis
  240 °C sind technisch codierbar, aber weder durch eine Herstellerfreigabe
  noch durch eine thermische Liveprüfung validiert; Oberfläche und API weisen
  darauf ausdrücklich hin. ODM enthält keinen Live-Flashpfad und aktiviert die
  Funktion ohne den dynamischen Firmwarebeweis nicht.
- Normale Block-50-Feldänderungen und Uhrabgleich behandeln das Heartbeat-Byte
  als firmwareeigenen Laufwert. Backup-Restore bewahrt zusätzlich den
  firmwareeigenen SC123-Datensatz in Block 60 und schreibt ihn niemals aus
  einem alten Sicherungsabbild zurück. Alle übrigen Felder bleiben streng
  geprüft.
- Der gemeinsame Schreibdienst sperrt jede generische Byteänderung am
  Block-50-Heartbeat und am Block-60-Datensatz anhand des tatsächlichen
  Vorher-/Nachher-Payloads. Browser, Kommandozeile und TUI prüfen das bereits
  vor einer Authentifizierung; ausschließlich der dedizierte SC123-Ablauf darf
  diese firmwareeigenen Bytes schreiben.
- Eine nachgeholte Sicherheitsprüfung des manuellen Entstörpfads beseitigt
  eine Bestätigungsrace: Der Browser sendet jetzt exakt den Servicecode und
  das Einmaltoken der vom Bediener bestätigten Vorschau und ersetzt sie nie
  still durch eine spätere Lesung. Das Token bindet zusätzlich den
  Störungszähler und die Betriebssekunden des Störereignisses. Vor Auth und
  letzter Zustandsprüfung wird ein persistenter Schreibintent gespeichert;
  Tokenfrist und maximal eine Sekunde alter Prewrite-Zustand gelten bis zum
  tatsächlichen Frame. Intent-Auflösung und vollständiges Prüfprotokoll
  einschließlich Auth-0-Ausgang sind eine SQLite-Transaktion. Ein unklarer
  Ausgang oder Fehler beim Prüfprotokoll bleibt dadurch auch nach einem
  Webdienst-Neustart gesperrt; nach
  unbestätigtem Auth 0 folgt zusätzlich eine rein lesende Zustandsprüfung.
  Eine fachlich nicht allgemein belegte Temperatur-/Wiederanlaufpolitik ist
  standardmäßig fail-closed und muss je Installation ausdrücklich mit
  `OPEN_DACHS_SERVICE_RESET_TEMPERATURE_POLICY_APPROVED=1` freigegeben werden;
  der Installer setzt neue Anlagen auf `0` und bewahrt nur eine vorhandene
  ausdrückliche Freigabe. Offene Schreibintents werden niemals automatisch
  aufgelöst oder wiederholt.

## V3 1.8.15 - 2026-11-06

- Die Betriebsfreigabe im Prozessbild flackert während eines laufenden
  Monitorzyklus nicht mehr zwischen dem echten Zustand und „nicht verfügbar“.
  Eine Block-22-Zeile aus dem gerade laufenden Zyklus darf neuer als der letzte
  vollständig abgeschlossene Zyklus sein; eine ältere Zeile nach einem
  fehlgeschlagenen Zyklus bleibt weiterhin sicher gesperrt. Serverseitige
  Vorschau sowie Authentifizierungs-, Schreib- und Rücklesevertrag bleiben
  unverändert.

## V3 1.8.14 - 2026-11-06

- Das Prozessbild zeigt die aus Block 22 gelesene Betriebsfreigabe und bietet
  Administratoren die getrennten Aktionen `Dachs Ein` und `Dachs Aus`.
  Ein setzt nur die normale Automatikfreigabe und erzwingt keinen Start; Aus
  fordert den regulären Stoppablauf an und ist kein Not-Aus. Vor einer Änderung
  werden Block 22/24, RAM `0x0ea1` und der Hand-/Aktortest `0x1b5a` zweimal
  über den exklusiven Serialworker gelesen. Einschalten verlangt sicheren
  Stillstand, Servicecode 0 und konservative Temperaturgrenzen. Der Server
  ändert mit Auth-Level 3 ausschließlich Bit 6 des frisch gelesenen Bytes,
  bewahrt alle übrigen Bits, sendet höchstens ein Schreibtelegramm und verlangt
  exaktes ACK/Echo, zwei vollständige Rücklesungen, Auth-Level 0, Einmaltoken,
  30-s-Wiederholungssperre und Prüfprotokoll ohne PW4. Entstören und Einschalten
  bleiben absichtlich getrennt; der flüchtige RAM-Zustand wird nicht als
  dauerhaft über einen Reglerneustart zugesagt.

- Die Anlagenübersicht bietet Administratoren bei einer aktiven Störung einen
  manuellen Knopf `Entstören`. Vor dem einzigen möglichen flüchtigen
  CPU-0-RAM-Schreibvorgang an `0x1c9e` liest ODM Block 22/24 doppelt direkt über den
  exklusiven Serialworker, verlangt Motorstatus 15/16 und konservative
  Temperaturgrenzen, prüft denselben Zustand nach exakt gewährtem Auth-Level 3
  nochmals, akzeptiert nur das exakte positive ACK/Echo `83 1C 9E 01`, liest
  den Servicecode zweimal stabil als 0 zurück und bestätigt Auth-Level 0.
  Ein serverseitiges Einmaltoken, global nur eine offene Vorschau, eine
  anlagenweit serialisierte Vorschau/Ausführung und eine 30-s-Sperre nach jedem
  Schreibversuch verhindern Doppelklicks und automatische Wiederholungen auch
  bei unklarem Ausgang. Nach dem Erwerb der seriellen Sperre wird eine
  zwischenzeitliche Verbindungstrennung nochmals geprüft. Jeder Versuch wird
  ohne PW4 im Prüfprotokoll festgehalten; NVM, Serviceparameter und das serielle
  Gerät werden nicht direkt geöffnet.

## V3 1.8.13 - 2026-10-28

- Hinweistext-Review abgeschlossen: Die temporäre rote Markierung mit Nummern ist vollständig entfernt (Klasse, Attribute, CSS und SVG-Badge). Wartungstexte 3–7 bleiben wie abgenommen. In Wartungsprotokoll und Löschabfrage heißt der lokale Speicherort jetzt „ODM“ statt „Pi“.

## V3 1.8.12 - 2026-10-28

- Wartungsprotokoll: Der Speicherhinweis steht als eigener Zwischenpunkt „Entwurf speichern“ über der Schaltfläche, im selben Stil wie die lokale Arbeitsliste; der Hinweis zu Altberichten ohne Pflichtbackup entfällt. Review-Markierung an Diagnose 5 und Einstellungen 6 entfernt; rot bleiben Wartung 4 (neue Darstellung zur Abnahme) sowie Wartung 5–7.

## V3 1.8.11 - 2026-10-28

- Hinweistexte: Prozessbild-Hinweise „Schema · nicht maßstäblich“ (Technik) und „L1 / L2 / L3 · Werte werden langsam nachgeladen“ entfernt; die Legende des Temperaturdiagramms nennt wie die übrigen Diagramme nur noch die Zuordnung (`Vorlauf = Kühlwasser Motor · Rücklauf = Dachs-Eintritt`). Die temporäre rote Review-Markierung bleibt nur noch an den acht noch offenen Texten (Wartung 3–8, Diagnose 5, Einstellungen 6); alle abgenommenen Texte erscheinen wieder normal.

## V3 1.8.10 - 2026-10-28

- Hinweistexte nach Bobers Review gekürzt oder entfernt (Prozessbild, Überwachung, 250-h-Diagnose, Einstellungen, Backup, System, Wartung, Fehlerkatalog). Die 250-h-Kennwerte zeigen nur noch Wertebereiche (z. B. `72–85 °C`, Leistung `… %`), die Feldhilfe in den Einstellungen nur noch Klartexte wie `0 = inaktiv` bzw. den Bereich `min–max` mit Einheit; Netzschutz-Blöcke behalten die rote Kopfzeile ohne Zusatztext. Die temporäre Review-Markierung (rot, Nummern) bleibt für die verbliebenen Texte bis zur Abnahme bestehen.
- Netzfrequenz-Diagramm: Die Y-Achse folgt dynamisch der niedrigsten und höchsten Frequenz im Zeitfenster, jeweils ±0,100 Hz, statt fest 48–52 Hz.
- Wiederherstellung: Das Textfeld `Bestätigung` (`SICHERUNG WIEDERHERSTELLEN`) entfällt in der Oberfläche; die Freigabe ist der Haken `Hardware-Wiederherstellung aktivieren` plus die Sicherheitsabfrage. Die API verlangt die Bestätigungsphrase unverändert, die Oberfläche liefert sie beim gesetzten Haken mit. Lesen, Authentifizierung, ACK und bytegenaue Rückleseprüfung bleiben unverändert.

## V3 1.8.9 - 2026-10-27

- Phase-6-Verifikationslogger: Der Basisvergleich P0 (Blöcke 22/24/26 gegen zwei RAM-Lesungen) zählt Bytes, die sich zwischen den beiden Lesungen bewegen, als erklärt und nicht mehr gegen die 90-%-Quote. Es bleibt bei höchstens vier unerklärten Bytes. Bei laufendem Motor enthält Block 26 (0x0f1a) zu viele Live-Messwerte; die reine Gleichheitsquote (54–55 von 64) ließ die Basisprüfung im Betriebszustand S2 am 2026-10-27 zweimal abbrechen, obwohl kein Byte unerklärt war. Das Protokoll `p0` nennt jetzt zusätzlich `volatile`. Keine Änderung an Leseumfang, Allowlist, Ratenlimits oder Abbruchregeln; weiterhin rein lesend.

## V3 1.8.8 - 2026-10-24

- Regleruhr-Abgleich (Admin, `Einstellung → Täglicher Uhrabgleich`, standardmäßig aus): einmal täglich zur gewählten ODM-Zeit (06:00–22:00, Vorschlag 12:00) und manuell über `Jetzt synchronisieren` mit Bestätigung. Gestellt wird nur bei |Versatz| ≥ Schwelle (Standard 30 s) und ≤ Maximum (Standard 3600 s), wenn ODM laut `timedatectl` NTP-synchron ist und das Zielintervall weder Mitternacht noch die Nachtabschalt-Sekunde (03:00 + Starts mod 100 min, auch für den nächsten Start) noch eine Zeitumstellung ±15 min berührt. Pfad laut Firmwareanalyse: Block-50-Lesen-Ändern-Schreiben von Offset 36 (`Hka_Ew.ulSystemTime`) mit Kommando-Bit 5 in Offset 0 (Firmware übernimmt die Zeit in RAM und RTC und löscht das Bit), Auth 3, Rücklesung ±3 s, Abmeldung, Prüfprotokoll `controller-clock-sync`. Bleibt das Bit stehen oder ist der Ausgang unklar, schaltet sich die Automatik ab.
- Automatische Rücksetzung `Laufzeit seit Start` (0x0f0e, Admin, Zahnrad der 24-h-Kachel, standardmäßig aus): Regel „Zähler ≥ X → auf Y setzen“, nach Z Rücksetzungen je Motorlauf weiterlaufen lassen (Z = 0 unbegrenzt). Harte Grenzen Y ≥ max(1800 s, 60 · Mindestlaufzeit aus Block 50), X > Y + 600 s, X ≤ 85.470 s, Z ≤ 999; Vorschlag X = 20.000 s, Y = 10.800 s, Z = 0. Nur bei Motorstatus ≥ 30, ohne Servicecode und Handbetrieb (RAM 0x1b5a), nach frischem, kohärentem doppeltem Lesen; gleiche Sicherheitskette wie das manuelle Setzen (Auth 3, Rücklesung +1 s, Abmeldung), Prüfprotokoll `runtime-auto-reset` mit Regelparametern, höchstens ein Schreibvorgang je 10 min. Ein unsicherer Ausgang schaltet die Automatik ab. Neu und getrennt vom entfernten `runtime_24h_auto`.
- `Laufzeit seit Start` zeigt überall die reale Laufzeit (`real_runtime`): Laufbeginn aus der ODM-Motorstatus-Historie (Übergang auf Status ≥ 30), bei Aufzeichnungslücke über die unabhängigen Betriebssekunden; im Stillstand „steht“ bzw. „steht seit …“, ohne verfügbare Rekonstruktion „—“ (kein stiller Rückfall auf 0x0f0e). Betroffen: Kachel `Laufzeit seit Start` (vormals `Laufzeit seit Start (24-h-Zähler)`), Board-Tabelle, Motorstatus-Details, Prozessbild v95 (Übersicht und Technik) und Kompaktansicht. Der Reglerzähler 0x0f0e erscheint nur noch als Unterzeile `Reglerzähler: …` (Kachel mit 6-h-/24-h-Schwellen, Board-Tabelle, Details), mit `zurückgesetzt`, wenn er um mehr als 120 s nach unten abweicht; im SVG-Prozessbild steht nur der reale Wert. Der Schreibdialog heißt `Reglerzähler (0x0f0e) setzen`. `/api/live` liefert `real_runtime`, `controller_clock_sync` und `runtime_auto_reset`.
- Die zwischenzeitliche Einzelkachel `Laufzeit (real)` (`derived.real_runtime`) geht in der Kachel `Laufzeit seit Start` auf: In gespeicherten Auswahlen wird sie beim Laden auf diese Kachel abgebildet (ohne Dopplung); unbekannte oder ungültige Kachel-Keys werden einzeln verworfen, statt die ganze Auswahl auf die Standardkacheln zurückzusetzen. Neue Übersichten enthalten den rohen 0x0f0e-Wert nicht mehr als eigene Kachel; in bestehenden Auswahlen bleibt er erhalten und heißt `Reglerzähler (0x0f0e)`.
- Historie: Die Serie `betriebsstunden` (Block 24 `Hka_Mw1.ulMotorlaufsekunden`) heißt `Reglerzähler (0x0f0e)`; Schlüssel und Speicherposition bleiben unverändert. Neu ist die abgeleitete Serie `laufzeit_real` „Laufzeit seit Start (real)“ (`schema.derived_series`, `/api/history-batch` mit Key `derived.real_runtime`), zur Abfragezeit aus den Motorstatuspunkten rekonstruiert – ohne Datenbankmigration; Läufe mit Beginn in einer Aufzeichnungslücke werden ausgelassen statt geschätzt.
- 250-h-Diagnose: Servicecodes und Warncodes werden mit dem Format-Aufschlag angezeigt (Rohwert 31 → SC 131, Warnung +600), auch in der Störabschaltungsliste.
- Die Systemkarte `Status und Meldungen` zeigt ganz oben `Display Zeile 1` und `Display Zeile 2`: die beiden Statuszeilen der MSR2-Startseite, aus Reglerdaten exakt nach der Prioritätskette der Hauptfirmware R 000.004.043 nachgerechnet (inklusive Servicecode-Text mit Sonderfall 122/124, `IB erforderlich`, `Platine getauscht !`, Busfehler nur bei Anforderung). Die deutschen Texte stammen unverändert aus den Firmwaretabellen. Eingänge kommen aus den ohnehin gelesenen Blöcken 22/24/26 (schnell) und 50/104 (langsam) sowie wenigen Ein-Byte-Reads über Dienst 0x02 im 10-s-Zyklus (`0x0f96`, `0x1bcc`, `0x0db3` – bei Öltyp aus dem Rußfilter-Read übernommen; bedingt `0x1c9f`, `0x1b5c`, `0x1ed4`). Fehlt ein Eingang oder ist er veraltet, steht dort `nicht verfügbar`. Ein Klick auf `Display Zeile 2` öffnet die letzten 50 Textwechsel (neue SQLite-Tabelle `display_status_events`, nur bei Textwechsel). `/api/live` liefert `display_status`, `/api/display-status/events` den Verlauf; optional gibt es die standardmäßig ausgeblendete Kachel `Display-Status` (Block 22, berechnet).
- Die Systemkarte `Status und Meldungen` zeigt den neuen read-only Unterabschnitt `Rußfilterregeneration` aus CPU-0-RAM (Dienst 0x02, derselbe 10-s-Zyklus): Zustand 0x000db3 (inaktiv / läuft / unterbrochen), Betriebsminuten seit der letzten Regeneration mit Rest bis zum 180-Minuten-Intervall bzw. `fällig` (0x000de6), Restdauer der 12-minütigen Regeneration (0x000de7) sowie Unterbrechungszähler und Latch (0x000d82, Servicecode 180 ab fünf). Die drei Reads laufen nur bei gesetztem Rußfilterbit 3 des bereits in Block 24 gelesenen Typcodes `Hka_Mw1.bKraftstofftyp` (RAM 0x000f0a); ohne Bit oder ohne Typcode erscheint `nicht unterstützt` bzw. `nicht verfügbar` ohne jeden RAM-Read. Jeder Teilfehler oder ein unbelegter Zustandswert führt fail-closed zu `nicht verfügbar`; Minuten sind aktive Betriebsminuten, ein Prozentfüllstand wird nicht erfunden. `/api/live` liefert `soot_filter_regeneration` getrennt von der Abgastemperatur-Schätzung `soot_filter`. Grundlage ist die statische Firmwareanalyse vom 2026-10-24 (Zustandsdeutung hoch, Typzuordnung mittel-hoch); ein read-only Live-Abgleich steht aus.
- Die Rußfilterregeneration und die drei internen Reglerwerte stehen zusätzlich als optionale Kacheln `Rußfilterregeneration` und `Interne Reglerwerte` (Block 24, berechnet) im Kacheleditor zur Auswahl. Beide sind standardmäßig ausgeblendet; bestehende Kachelauswahlen werden beim Versionswechsel nicht verändert.
- Der Abschnitt `Interne Reglerwerte` steht nicht mehr als eigenes Panel in der Übersicht, sondern als Unterabschnitt der Systemkarte `Status und Meldungen` unterhalb der Rußfilterregeneration. Dort erscheinen drei rein lesend über Dienst 0x02 geholte CPU-0-RAM-Werte: `Internes Leistungsziel` (0x00222e, uint16 LE, W), `Abgas-Regelkorrektur` (0x000dca, int16 LE, W mit Vorzeichen) und `Verfügbare Leistungsgrenze` (0x001ad4, uint16 LE, W). Sie laufen im bestehenden 10-s-Zyklus über denselben Serialworker wie der 24-h-Zähler; Adresse und Länge stammen aus einer festen Allowlist, Echo und Antwortlänge werden exakt geprüft, jeder Fehler ergibt `nicht verfügbar` statt eines Altwerts. `/api/live` liefert sie als `ram_telemetry`. Die Deutung der Abgas-Regelkorrektur hat mittlere Konfidenz; es werden weder Authentifizierung noch Schreibzugriffe noch Historisierung ausgelöst.
- Die Kachel `24-h-Abschaltung` heißt jetzt `Laufzeit seit Start (24-h-Zähler)`. Der Zähler `0x0f0e` ist identisch mit `Hka_Mw1.ulMotorlaufsekunden` und wird vom Regler bei jedem Start auf 0 gesetzt. Die Kachel zeigt die aktive Laufzeit seit Start und nennt beide Firmwareschwellen: 6 h (21.600 s), ab der die tägliche Nachtabschaltung greifen kann, und 24 h (86.370 s). `/api/live` liefert unter `runtime_24h` zusätzlich `night_shutdown_gate_seconds`, `remaining_to_night_gate_seconds` und `night_shutdown_gate_reached`.
- Der zwischenzeitlich eingeführte automatische 24-h-Zähler-Autoreset ist wieder vollständig entfernt (Schalter, Statusanzeige, Endpunkt `/api/settings/runtime-24h-auto`, Zustandsmaschine). Er hätte die Nachtabschaltung nicht verhindert. Eine vorhandene `runtime_24h_auto_state.json` und die SQLite-Tabelle `runtime_24h_auto_setting_tx` bleiben ungenutzt liegen; es gibt keine destruktive Migration. Der manuelle Zählerwrite im Zahnraddialog (Auth-Level 3, Rücklesung, Abmeldung, Prüfprotokoll) bleibt für Tests unverändert.
- Abschaltgrund Bit 3 heißt jetzt `Sicherheitsabschaltung nach 24h bzw. tägliche Nachtabschaltung nach ≥ 6 h Lauf`; der Herstellertext bleibt erhalten.
- `Status und Meldungen` zeigt die Zeile `Regleruhr`: Reglerzeit aus dem ohnehin gelesenen Block 50 (`Hka_Ew.ulSystemTime`) und die Abweichung zur ODM-Zeit im Lesezeitpunkt (z. B. `+5:51 min`). `/api/live` liefert `controller_clock` (`controller_time`, `offset_seconds`, `available`). Fehlt der letzte Block-50-Read oder ist er älter als zwei langsame Zyklen plus Reserve, steht dort `nicht verfügbar`. Rein lesend, die Uhr wird nicht gestellt.
- Wartungsberichte: Der Brennstoff kommt weiterhin aus dem Typcode `Hka_Mw1.bKraftstofftyp` (RAM 0x000f0a, 0x08 = Heizöl). Er wird jetzt zusätzlich gegen das Rohbyte 54 im Block-24-Payload geprüft und mit Herkunft (`fuel_source`) im Bericht gespeichert. Bei neuen Berichten ist die Kraftstoffart dadurch festgelegt. Berichte vor 1.1.0 hatten das Feld mit falschem Offset gelesen (Rohwert 0) und fielen auf Gas zurück. Sie bleiben unverändert gespeichert; die Anzeige nutzt das führende Berichtsfeld und weist eine Abweichung zum beim Wartungsstart eingefrorenen Anlagenstand sichtbar aus (`fuel_check`).

## V3 1.8.7 - 2026-10-24

- Block 26 mit der Netzfrequenz gehört nun wie Block 22 und 24 zum schnellen 0,75-s-Messzyklus. Die zuvor gemessene zusätzliche Antwortzeit von rund 102 ms passt in die vorhandene Zyklusreserve; Block 26 wird im langsamen Zyklus nicht doppelt gelesen.
- Statusanzeigen, technische Ansichten und Frequenzdiagramm weisen den schnellen Takt aus. Der vorhandene zentrale Serialworker bleibt alleiniger Besitzer des Adapters; es entstehen weder Authentifizierung noch Schreibzugriffe.
- Die Abmeldung auf Auth-Level 0 akzeptiert neben der kurzen Antwort nun auch die live beobachtete exakte PW4-Rückspiegelung. Fremde Anhänge bleiben gesperrt; mögliche Zugangsdaten erscheinen bei abweichenden Antworten nicht mehr in Fehlertext oder Prüfprotokoll.

## V3 1.8.6 - 2026-10-24

- Der 24-h-Zählerschreibdialog folgt nun dem Bedienmuster des Leistungs-Sollwerts: Der Browser übermittelt nur den absoluten Zielwert, den frisch gelesenen erwarteten Istwert und die ausdrückliche Hardwarefreigabe. Auth-Level 3 und die aus aktuellen Anlagendaten berechnete PW4 bleiben vollständig serverseitig; separate Felder für Auth-Level, PW4 und Bestätigungstext entfallen.
- Vor Authentifizierung und Schreibvorgang sind bis zu 30 Sekunden ausschließlich monotones Vorwärtszählen des frischen Ausgangswerts zulässig. Rückwärtsbewegung oder 31 Sekunden und mehr brechen vor Authentifizierung und Schreiben ab. Der Zielwert wird dadurch nie verschoben: Auch ein kleinerer Zielwert wird exakt als absoluter Wert geschrieben. Die unmittelbare Rückleseprüfung bleibt streng bei Zielwert bis Zielwert plus einer Sekunde.

## V3 1.8.5 - 2026-10-24

- Der 24-h-Zählerschreibdialog akzeptiert vor Authentifizierung und Schreibvorgang bis zu zehn Sekunden ausschließlich monotones Vorwärtszählen. Damit bleibt er bei Wartezeit auf den exklusiven Serialworker robust; Rückwärtsbewegung und größere Sprünge brechen weiterhin vor Authentifizierung und Schreibvorgang ab.
- Die unmittelbare Rückleseprüfung erlaubt höchstens eine Sekunde Fortschritt. Der Zielwert selbst bleibt frei von 0 bis 86.369 Sekunden wählbar.

## V3 1.8.4 - 2026-10-24

- Die Wirkleistungsachse beginnt auf ausdrücklichen Betreiberwunsch wieder bei 0 kW. Negative Messwerte bleiben in der Historie erhalten, werden im Leistungsdiagramm aber nicht unterhalb der Nulllinie dargestellt.
- Der 24-h-Zählerschreibdialog bleibt unverändert: Jeder Wert von 0 bis 86.369 Sekunden ist zulässig; die Ein-Sekunden-Toleranz betrifft nur das natürliche Weiterzählen zwischen zwei Sicherheitslesungen.

## V3 1.8.3 - 2026-10-24

- Administratoren können den flüchtigen 24-h-Sicherheitszähler über ein Zahnrad in der Kachel kontrolliert setzen. Der Pfad nutzt zwei kohärente Lesungen, einen frischen Ein-Sekunden-Ausgangswertvergleich, Auth-Level 3, den bestätigten CPU-0-RAM-Dienst `0x03`, unmittelbare Rückleseprüfung, Abmeldung auf Auth-Level 0 und Prüfprotokoll; Schwelle und höhere Werte bleiben gesperrt.
- Ein eigenes Diagramm zeichnet die bereits alle zehn Sekunden gelesene Netzfrequenz aus Block 26 mit drei Nachkommastellen auf. Frequenz- und Leistungsskalen wurden datengetreu angepasst; negative Wirkleistung wird nicht mehr bei null abgeschnitten.

## V3 1.8.2 - 2026-10-24

- Die Übersicht zeigt den internen 24-h-Sicherheitszähler als frei sortierbare Kachel. Zwei kohärente CPU-0-RAM-Lesungen liefern den aktuellen Sekundenwert und die verbleibende aktive Laufzeit bis 86.370 s; bei einem möglichen Byte-Übertrag folgt ein dritter Kontrollread. Die Anzeige bezeichnet die Restzeit ausdrücklich nicht als Wanduhrprognose, da der Zähler bei Stillstand pausiert.

## V3 1.8.1 - 2026-10-23

- Wartungsentwürfe können Admins jetzt über einen strikt getrennten lokalen Abschluss als **Zwischenwartung** archivieren. Der Pflichtgrund ist serverseitig auf 500 Zeichen begrenzt; der Weg öffnet keine Serialworker-Sitzung, authentifiziert sich nicht und schreibt weder Block 100 noch Block 104.
- Zwischenwartungen erhalten atomar eine dauerhafte Nummer aus dem im Zustandsabbild erfassten Regler-Wartungszähler (`4.1`, `4.2`, …). Fehlt ein gültiger Zähler, wird stabil der höchste bereits gespeicherte lokale bzw. erfasste Basisstand, andernfalls `0`, verwendet. Nummer und Grund erscheinen in Liste, Bericht sowie HTML/PDF/JSON-Export.
- Bestehende reguläre Wartungsabschlüsse und Altberichte behalten ihr bisheriges Verhalten und Format; SQLite ergänzt die neuen Metadaten transaktional.
## V3 1.8.0 - 2026-10-22

- Die neue 250-h-Diagnose bildet abgeschlossene und laufende
  Betriebsstundenintervalle ausschließlich aus der lokalen SQLite-Historie.
  Ein dedizierter Hintergrundworker aktualisiert einen atomaren, privaten Cache
  bei Dienststart, lokaler Mitternacht, erkanntem Motorstopp und manueller
  Anforderung; API und Browser bleiben auch bei großen Historien responsiv.
  Datenlücken und nicht belegbare Fehlstarts werden ausdrücklich ausgewiesen.
- Netzschutzblock 16 ist nach den neueren Livebefunden wie B20 und B21 strikt
  read/backup-only. Beide U-3.050-CPUs lehnten selbst eine unveränderte
  B16-Vollblock-Rücksicherung fachlich mit `7F 11 01` ab. Schema, UI, Web- und
  Maschinen-API, Low-Level-Schreibpfad und Restorevertrag sperren alle drei
  Blöcke vor serieller Sitzung oder Authentifizierung; 42 Ziele werden
  weiterhin gesichert, nur die 36 Reglerblöcke sind restaurierbar.
- Das offline arbeitende Firmware-Center begrenzt nun auch die tatsächliche
  Dekompressionsausgabe während des Entpackens. Übergröße, Timeout und
  Dekompressorfehler führen zu sofortigem, sauberem Prozessabbruch; der native
  Flash bleibt unverändert gesperrt.
- Die seit 1.7.4 ergänzten Bedien- und Betriebshärtungen umfassen den
  konfigurierbaren Web-Base-Path, atomare lokale Zugriffseinstellungen,
  strengere Request-/Origin-Grenzen sowie das fortgesetzte, fehlerisolierte
  Mehrblock-Restore mit geschützten Zielversionsfeldern.

## V3 1.7.4 - 2026-08-27

- Die reale B20-Abnahme ist abgeschlossen. Beide U-3.050-CPUs gewährten Auth 5
  und bestätigten Dienst 21 positiv, übernahmen den minimalen Testwert aber
  nach vier Rücklesungen nicht. B20 bleibt deshalb in UI und API nur lesbar und
  wird weiterhin ausschließlich gesichert, nicht restauriert.
- Das neue Firmware-Center unter `System → Firmware` prüft `.muc`-Dateien
  offline auf Containerlängen, SHA-256 und alle acht Modulabbilder. Es speichert
  die Datei nicht und öffnet keine serielle Sitzung.
- Ein nativer Live-Flash bleibt sicher gesperrt, bis der aufgezeichnete
  Bootloader-Ablauf einschließlich Frames, Baudwechseln, ACKs und Abschluss
  vollständig verifiziert ist.

## V3 1.7.3 - 2026-08-27

- Die Mehrblock-Wiederherstellung versucht nach einem Bestätigungs- oder Rücklesefehler auch
  alle übrigen ausgewählten Ziele und zeigt die Fehler weiterhin einzeln und
  auditiert an; erfolgreiche Blöcke werden nicht zurückgerollt.
- Softwareversionsfelder der Zielplatine werden nach dem belegten Verhalten des
  originalen Senertec-Platinentauschs nicht mehr aus einem Backup übertragen.
  Das Abbild behält sie weiterhin als Diagnoseinformation der Quelle.

## V3 1.7.2 - 2026-08-27

- Eine Live-Wiederherstellung darf jetzt ausdrücklich auf einer
  Austauschregelung mit abweichender Seriennummer ausgeführt werden. Die im
  Sicherungsabbild gespeicherte Seriennummer wird weiterhin als Bestandteil
  des hashgebundenen Abbilds geprüft und angezeigt, aber nicht mehr mit der
  Zielregelung oder der Authentifizierung verglichen.
- Abbild- und Packprüfung, vollständige Lesevorprüfung, Vorzustandsabbild,
  Hardwarefreigabe, Authentifizierung, positives ACK, bytegenaue Rücklesung
  und Prüfprotokoll bleiben unverändert zwingend.

## V3 1.7.1 - 2026-08-24

- Der neue Reiter `Bedienplatz` verwendet dieselbe technische
  Anlagenübersicht wie der normale Übersichtsbereich und ist für den lokalen
  1366-x-768-Touch-PC verdichtet. Es gibt keine zweite Kopie des Prozessbilds.
- Darunter zeigt ein gemeinsamer, automatisch aktualisierter Zwei-Stunden-
  Verlauf Drehzahl, Wirkleistung und Motorabgastemperatur mit jeweils eigener
  nachvollziehbarer Skala. Ein Tastendruck öffnet die vollständige
  Überwachung.
- Der Bedienplatz lässt sich direkt mit `#bedienplatz` aufrufen. Beim Verlassen
  kehrt das Prozessbild mitsamt der zuvor gewählten Darstellung in die normale
  Übersicht zurück. Die Änderung erzeugt keine zusätzlichen seriellen Reads
  und verändert weder Authentifizierung noch Regler-Schreibpfade.
- `Motbel250[*].bQuotientBh_Start` aus Block 38 und den weiteren
  250-Betriebsstunden-Datensätzen verwendet wieder die originale
  Zehntel-Skalierung. Ein Regler-Rohwert `38` erscheint damit als
  `3,8 Bh/Start`; die Rückumrechnung beim Bearbeiten bleibt symmetrisch.

## V3 1.7.0 - 2026-08-24

- Unter `System → Benutzer & Berechtigungen` kann ein optionaler lokaler
  Bedienplatz aktiviert werden. Port und Rechte sind frei wählbar; Standard
  sind Port 80 und Adminrechte, die Funktion selbst bleibt zunächst aus.
- Der lokale Zugang verwendet einen eigenen, ausschließlich an `127.0.0.1`
  gebundenen Listener und einen cookie-losen, eindeutig als
  `localhost-override` protokollierten Principal. Der normale Web-Port und der
  Reverse Proxy bleiben unverändert loginpflichtig; Forwarding-Header verleihen
  keine Rechte.
- Exakter Host und Origin, ein Same-Origin-Request-Marker, CSP und Frame-Schutz
  härten den lokalen Listener gegen DNS-Rebinding, fremde Webseiten und normale
  Proxyweiterleitungen. Die Maschinen-API `/api/v1/*` bleibt auch lokal
  ausschließlich Bearer-Token-pflichtig.
- Portwechsel werden zuerst auf Loopback gebunden und erst danach atomar
  gespeichert. Ein belegter oder mit dem Hauptlistener kollidierender Port
  lässt den bisherigen Zustand unverändert; ein fehlerhafter lokaler Start
  legt den normalen Webdienst nicht lahm.
- Der Login-Override ersetzt nur die Webanmeldung. Schreibfreigabe,
  MSR2-Authentifizierung/PW4, Stabilitätsprüfung, positives ACK,
  Rückleseprüfung und Prüfprotokoll bleiben unverändert zwingend.

## V3 1.6.3 - 2026-08-21

- Frei gewählte Kacheln für Motorstatus und aktuellen Fehlercode zeigen ihre
  vorhandenen Klartexte jetzt zuverlässig an. Gültige Zustände wie
  `35 (KEINE Stellmotorbewegung)` und `Kein aktiver Servicecode` werden nicht
  länger fälschlich als ausstehende Messung dargestellt.
- Die zahlenbasierte Plausibilitätsprüfung gilt nur noch für Messreihen mit
  ausdrücklich hinterlegten Sensor- oder Bereichsgrenzen. Deren bisherige
  Grenz- und Sentinelprüfung bleibt unverändert wirksam.
- Die Korrektur betrifft ausschließlich die Darstellung vorhandener Werte. Sie
  erzeugt keinen zusätzlichen seriellen Lesevorgang und keine Schreibaktion.

## V3 1.6.2 - 2026-08-20

- Die Überwachung besitzt ein viertes, schmales Motorstatusdiagramm aus dem
  ohnehin im schnellen Zyklus gelesenen Block 24. Aus ist weiß,
  Startvorbereitung orange, Start/Anlasser rot, Lauf beziehungsweise Regelung
  grün, Abschaltroutinen violett und Störabschaltungen dunkelrot. Mouseover
  zeigt Statusnummer und quellenbelegte Bezeichnung.
- Kurze Zustände werden nicht über die auf 2.000 Punkte begrenzte
  Kurvenhistorie bezogen. Eine eigene kompakte Ereignisspur speichert jeden im
  etwa 0,75-sekündigen Lesezyklus beobachteten Wechsel und zusätzlich alle
  30 Sekunden einen Stützpunkt. Dadurch bleiben auch sehr kurze erfasste
  Start- und Stellmotorzustände erhalten; ein Zustand vollständig zwischen
  zwei Reads ist technisch nicht beobachtbar. Echte Aufzeichnungslücken werden
  schraffiert und nicht als Motor-Aus interpretiert.
- Die Ereignisspur wird beim ersten Start rückwirkend aus den vorhandenen
  30-Tage-Momentaufnahmen aufgebaut und anschließend direkt aus jedem
  erfolgreichen Block-24-Zyklus fortgeschrieben. Sie erzeugt weder einen
  weiteren seriellen Blockread noch Authentifizierung oder Reglerwrite. Der
  gemeinsame Mauszoom umfasst jetzt alle vier Diagramme.

## V3 1.6.1 - 2026-08-19

- Auth-Level 5 ist zusätzlich zu den benannten Rollen 1 bis 4 in CLI, TUI,
  Web-Schreiben, Netzschutz, Wiederherstellung, Wartungsabschluss und API-
  Einstellungen verfügbar. Die ältere V2-Schreib- und Backup-Implementierung
  verwendet diese Stufe bereits; in der Oberfläche heißt sie neutral
  `Legacy/V2`, weil die originale Java-Oberfläche dafür keinen Rollennamen
  liefert.
- Die zentrale, typstrikte Grenze lautet jetzt 1 bis 5. Boolesche Werte,
  Strings, Fließkommazahlen, 0 und Werte ab 6 scheitern weiterhin vor jeder
  Authentifizierung und jedem Live-Schreiben. Eine Antwort gilt nur bei
  exakter Übereinstimmung mit der angeforderten Stufe als erfolgreich.

## V3 1.6.0 - 2026-08-19

- Die Historienansicht besitzt Schnellbereiche von 1 bis 48 Stunden, eine
  bestätigte freie Eingabe von 1 bis 720 Stunden sowie einen frei wählbaren
  Start-/Endzeitraum. Unbestätigte Eingaben beeinflussen das laufende Diagramm
  nicht.
- Alle drei Diagramme verwenden dieselbe Zeitachse und denselben Mauszoom.
  Helle grüne beziehungsweise rote Zeitbänder kennzeichnen anhand der bereits
  gespeicherten Drehzahl, wann der Dachs lief oder stand; Messlücken bleiben
  neutral.
- Die Hauptnavigation folgt jetzt der Reihenfolge Übersicht, Überwachung,
  Einstellung, Backup, Schreibprotokoll, System, Wartung und Fehlerkatalog.
- Im echten Wartungsabschluss kann PW4 über einen eigenen, schreibfreien
  Leseknopf aus frischen Block-20-/Block-22-Daten berechnet und übernommen
  werden. Der Wert wird weder gespeichert noch protokolliert und nach dem
  Vorgang verworfen.
- Auth-Level sind in Oberfläche und Server auf die vier quellenbelegten Rollen
  1 bis 4 begrenzt. Der Gerätetest zeigte, dass höhere Einbytewerte lediglich
  zurückgespiegelt werden und keine dokumentierten zusätzlichen Rollen
  darstellen.
- Backups sichern zusätzlich Block 20 und 21 beider Netzschutz-CPUs. Der neue
  vollständige Umfang beträgt 42 Ziele; wiederherstellbar bleiben bewusst nur
  die bisherigen 38 Konfigurationsziele. Vorhandene geprüfte 38er-
  Wartungsarchive bleiben kompatibel.

## V3 1.5.3 - 2026-08-19

- `Betriebsstunden je Start` steht jetzt fest im Motorbereich
  `Leistungszustand` und füllt dort den bislang freien Platz.
- Der berechnete Block-22-Wert ist zusätzlich über `Bearbeiten` als obere
  Kachel auswählbar, sortierbar und wieder entfernbar; standardmäßig wird er
  dort nicht mehr ungefragt eingeblendet.
- Berechnung, kohärenter Block-22-Zeitpunkt und schreibfreier Leseweg bleiben
  unverändert. Die neue Auswahl löst keinen zusätzlichen seriellen Zugriff und
  keinen eigenen Verlaufseintrag aus.

## V3 1.5.2 - 2026-08-18

- die Stabilitätsprüfung vor dem Schreiben von Block 50 behandelt ausschließlich
  die belegte Systemzeit an Byte 36 bis 39 als laufenden Wert; Lage, Breite,
  Blocknummer, CPU und Nutzdatenlänge müssen dafür exakt zur Felddefinition passen
- der gewünschte Leistungswert wird auf den unmittelbar zuvor frisch gelesenen
  Block gesetzt, sodass niemals eine veraltete Systemzeit zurückgeschrieben wird;
  jede gleichzeitige Änderung an einem anderen Bit bricht den Vorgang weiterhin ab
- Zielwert, positive Reglerbestätigung und Rückleseprüfung bleiben zwingend;
  falsche Bestätigungen, abweichende Zielwerte und veränderte Nachbarfelder werden
  weiterhin ohne stillschweigende Übernahme als Fehler protokolliert

## V3 1.5.1 - 2026-08-18

- der Leistungs-Sollwert im Hauptmenü verwendet fest Auth-Level 4 und lässt
  PW4 beim Schreiben serverseitig aus frisch gelesener Seriennummer und
  Betriebsstundenzahl berechnen; ein vorheriger Wechsel in die Einstellungen
  ist nicht mehr nötig
- die neue feste Übersichtskachel „Betriebsstunden je Start“ berechnet sich
  quellentreu aus den rohen Betriebssekunden und Starts desselben
  Block-22-Telegramms und zeigt das Ergebnis als `Bh/Start`
- bei null Starts, ungültigen Zählern oder zeitlich nicht zusammengehörigen
  Quelldaten zeigt die Kennzahl bewusst keinen Wert; zusätzliche serielle
  Lesezugriffe entstehen für die Berechnung nicht

## V3 1.5.0 - 2026-08-18

- jeder Wartungsstart erzeugt automatisch ein vollständiges Sicherungsabbild
  aller 38 freigegebenen CPU-/Blockziele; ein unvollständiges oder nicht
  prüfbares Abbild lässt keinen neuen Wartungsbericht entstehen
- Sicherungsabbilder werden atomar und mit Rechten `0600` im nur für den
  Dienstbenutzer zugänglichen Verzeichnis `backup-archive` abgelegt; Metadaten,
  Dateiinhalt und Abbild sind per SHA-256 gebunden
- der nur für Administratoren sichtbare Backup-Bereich zeigt Zeitpunkt,
  Ersteller, Herkunft, Wartungsbericht, Größe, Packrevision, Integrität und das
  Ergebnis 38/38; Abbilder können unverändert heruntergeladen werden
- „Für Wiederherstellung laden“ lädt die archivierte Datei erneut, prüft sie
  noch einmal über die bestehende Abbildprüfung und übernimmt sie mit leerer
  Zielauswahl sowie schreibfreiem Probelauf als sicherem Standard
- Wartungsansicht und Bericht nennen Backup-ID, Zeitpunkt, 38/38-Ergebnis und
  SHA-256; das Löschen eines Wartungsberichts lässt das getrennte
  Sicherungsabbild ausdrücklich erhalten
- die Zwischenzustände `Abschluss läuft` und `Zielzustand unklar` verhindern
  erneutes Abschließen und Löschen; ein unklarer Reglerzustand muss anhand von
  Bestätigung, Rückleseprüfung und Prüfprotokoll fachlich geklärt werden

## V3 1.4.0 - 2026-08-17

- Block 16 mit 18 Feldern und Block 20 mit 39 Feldern sind auf beiden
  Netzüberwachungs-CPUs schreibbar; damit stehen 114 eindeutig einer CPU und
  einem Block zugeordnete Feldinstanzen zur Verfügung
- die originale Layout-4-Datenzuordnung belegt für Block 20 den
  Vollblock-Schreibdienst 21; der geschützte Ablauf lautet Lesen, kodieren,
  authentifizieren, Ausgangszustand bytegenau vergleichen, Dienst 21 ausführen,
  positive Bestätigung prüfen und den vollständigen Block exakt zurücklesen
- profilabhängige und andere nicht eindeutig umkehrbare Anzeigen werden nicht
  geraten; für eine ausdrückliche Experteneingabe steht `raw:<Rohwert>` bereit
- Block 21 bleibt als laufender Messwertblock ohne Schreibdienst strikt nur
  lesbar; Block 20 bleibt bis zu einer realen Abnahme außerhalb von Sicherung
  und Wiederherstellung, deren Umfang unverändert 38 geprüfte Ziele umfasst
- Kodierung, Schutzprüfungen und schreibfreie Probeläufe wurden geprüft; ein
  physischer Schreibvorgang auf Block 20 wurde an der Anlage noch nicht
  ausgeführt
- die geprüften Standarddefinitionen enthalten keine weiteren Datenblöcke der
  Netzüberwachungs-CPUs; die zusätzlich live gelesenen Diagnosedienste 17 und
  18 bleiben mangels belegter Feldstruktur reine Rohdaten

## V3 1.3.0 - 2026-08-17

- beide Netzüberwachungs-CPUs zeigen neben dem bekannten Legacy-Block 16 jetzt
  auch die live bestätigten Layout-4-Blöcke 20 und 21 in getrennten roten
  Registerkarten
- Block 20 dekodiert 39 Schutzparameter aus 59 Byte: Schutzprofil,
  zweistufige Spannungs- und Frequenzgrenzen, profilabhängige Abschaltzeiten,
  10-Minuten-Spannungsgrenze, Frequenzreduktion, Impedanz- und LOM-Status
- Block 21 dekodiert 28 aktuelle Werte aus 56 Byte: dreiphasige Spannung, Strom und
  Frequenz, Impedanzen, Wirkleistung, Phasenlage, Cosinus Phi und
  Kalibrierfaktoren; signierte und Little-Endian-Werte werden quellentreu
  behandelt
- die neuen Blöcke sind bewusst nur lesbar: Herstellerdefinition und
  Alt-Oberfläche belegen ausschließlich Lesezugriff, daher existiert weder im
  Browser noch serverseitig ein Schreibpfad für Block 20 oder 21
- der vorhandene, ausdrücklich freigegebene Block-16-Schreibablauf bleibt
  unverändert; blockbezogene Lese-URLs verhindern eine Verwechslung zwischen
  den drei CPU-Blockräumen
- Backup und Wiederherstellung bleiben bei den geprüften 38 Zielen; Block 20
  und die flüchtigen Messwerte aus Block 21 werden nicht stillschweigend als
  restaurierbare Rohblöcke aufgenommen

## V3 1.2.1 - 2026-08-16

- die Backup-Auswahl umfasst jetzt neben den 36 Reglerblöcken auch die beiden
  getrennten Netzschutz-Ziele `CPU 1 · Block 16` und `CPU 2 · Block 16`
- Sicherungsabbilder und Prüfsummen unterscheiden Ziele eindeutig über das Paar
  aus CPU und Blocknummer; gleichnamige Blocknummern verschiedener CPUs können
  dadurch weder verwechselt noch als Duplikat zusammengefasst werden
- Prüfung, Dry-Run, Sicherheitsabbild, ACK, vollständige bytegenaue
  Rückleseprüfung und Prüfprotokoll gelten unverändert auch für ausgewählte
  Netzschutzblöcke
- bestehende CPU-0-Abbilder aus Version 1.2.0 bleiben einschließlich ihrer
  bereits gespeicherten SHA-256-Prüfsumme importierbar
- die reale Abnahme bleibt schreibfrei: Netzschutz wird nur gelesen und über
  Dry-Run beziehungsweise einen bytegleichen Überspringvorgang geprüft
- fehlende oder unpassende Packdaten sperren eine echte Wiederherstellung jetzt
  strikt; serielle Datenantworten werden zusätzlich an Quell-CPU und Zieladresse
  gebunden, damit kein verspätetes Telegramm der anderen Netzschutz-CPU gilt
- Sicherheitsabbilder werden nach dem atomaren Umbenennen zusätzlich über das
  Elternverzeichnis auf den Datenträger synchronisiert; ihre SHA-256-Prüfsumme
  schützt die Inhaltsbindung, ist jedoch ausdrücklich keine kryptografische
  Signatur

## V3 1.2.0 - 2026-08-16

- eigener Hauptbereich **Backup** zum Sichern aller 36 adressierbaren
  Reglerblöcke oder einer frei gewählten Teilmenge in einem JSON-Abbild
- Wiederherstellungsdateien werden zunächst rein offline eingelesen und auf
  Schema, Packstand, Reglerkennung, eindeutige Blocknummern, Payloadlängen sowie
  SHA-256-Prüfsummen geprüft; erst danach wird die Blockauswahl freigeschaltet
- Dry-Run bleibt der Standard und führt ausschließlich Lesevergleiche aus;
  echte Wiederherstellung verlangt Administratorrolle, Hardwarefreigabe,
  Authentifizierung und den exakten Bestätigungstext
- bytegleiche Blöcke werden ohne Authentifizierung und ohne Schreibtelegramm
  übersprungen; geänderte Rohblöcke benötigen stabile Ausgangsdaten, eine
  positive Bestätigung (ACK) und eine vollständige bytegenaue Rückleseprüfung
- vollständiger Mehrblock-Vorlauf und anschließende Ausführung innerhalb einer
  exklusiven Serialworker-Sitzung; Block 20 und 22 werden wegen der PW4-Grundlage
  zuletzt bearbeitet und nach dem ersten Fehler folgen keine weiteren
  Schreibversuche
- vor der Authentifizierung eines tatsächlich abweichenden Live-Ablaufs wird
  der frisch gelesene Ausgangszustand als SHA-256-gebundenes Sicherheitsabbild mit
  lokalen Rechten `0600` atomar gespeichert
- altes `dachs-msr2-backup/v3` bleibt lesbar; neu erzeugte Abbilder ergänzen
  Produkt-, Pack-, Regler- und Blocknamen sowie Payload- und Abbildprüfsummen
- Regler-CPU 0 bleibt der Sicherungsumfang; die getrennten Netzschutzräume der
  CPU 1 und 2 sind nicht Bestandteil dieses Abbildformats

## V3 1.1.1 - 2026-08-13

- Wartungsversion des unveränderten Funktionsstands von V3 1.1.0 mit
  konsistenter Kennzeichnung als 1.1.1 in Paket, Kommandozeile,
  HTTP-Zustandsprüfung, Serverkennung, Weboberfläche, Wartungsberichten und
  Dokumentation
- zentrale Laufzeit-Versionsquelle in `open_dachs_manager.__version__`; Paketbau,
  Webdienst und Berichte leiten ihre Kennzeichnung daraus ab, damit sich
  installierte und sichtbare Version nicht mehr unbemerkt auseinanderentwickeln
- eigener Eintrag im direkt in der Weboberfläche erreichbaren Änderungsverlauf,
  der den reinen Wartungscharakter dieser Version vom Funktionsumfang
  der V3 1.1.0 trennt
- der technische und der in der Oberfläche angezeigte Änderungsverlauf wurden
  vollständig deutsch formuliert; feststehende Bezeichnungen technischer
  Schnittstellen und Dateiformate bleiben erhalten
- keine Änderung an MSR2-Protokoll, Feldzuordnung, Authentifizierung,
  Schreibfreigabe, positiver Bestätigung (ACK), Rückleseprüfung, Verhalten der
  API, Historisierung oder lokalen Betriebsdaten; die Aktualisierung selbst
  schreibt nichts in den Regler

## V3 1.1.0 - 2026-08-10

- eigener, ausschließlich für Administratoren sichtbarer Hauptbereich
  **System**, getrennt von den Dachs-Einstellungen, mit Bereichen für Benutzer,
  API-Zugänge und Wartungsabschluss
- mehrere Benutzerkonten mit Rolle, Aktivstatus und eigenständigem
  Passwortwechsel; Schutz vor dem Löschen oder Deaktivieren des letzten
  Administrators
- lokale EDOMI-API mit Zugangsschlüsseln, deren vollständiger Wert nur einmal
  angezeigt und anschließend ausschließlich als kryptografischer Hash gespeichert
  wird, sowie getrennten Lese-, Historien- und Schreibrechten
- API-Schreiben standardmäßig global deaktiviert; übergeben werden nur
  logische Aktionen, während Authentifizierung, PW4, Schreiben, positive
  Bestätigung (ACK), Rückleseprüfung, Idempotenz und Prüfprotokoll vollständig im
  Dienst bleiben
- eine atomare, an den Inhalt gebundene Reservierung der `request_id` verhindert
  parallele Doppelausführungen und weist die Wiederverwendung für eine abweichende
  Aktion mit HTTP 409 zurück
- kraftstoffabhängige Originalgrenzen der Generatornennleistung werden für die
  dokumentierten Anlagentypen vor Authentifizierung und seriellem Schreiben
  geprüft
- adaptive Historie für alle regulären aktuellen Werte: volle Auflösung zunächst
  24 Stunden sowie dauerhaft für eine Stunde vor Motorstart, die gesamte
  Laufzeit und eine Stunde Nachlauf; übrige Stillstandszeit wird in
  15-Minuten-Fenster mit Anfang, Ende, Minimum, Maximum und Mittelwert
  verdichtet
- Webdienst bleibt lokales HTTP; externes HTTPS ist Aufgabe eines
  vorgeschalteten nginx
- Block-20-Feldzuordnung anhand der Originalstruktur um das ausgelassene
  Genehmigungsbyte korrigiert: Anzeigekontrast bleibt bei Byte-Offset 40,
  Zeitsynchronisierung liegt korrekt bei Byte-Offset 41
- exakte Kraftstoffbezeichnungen, eindeutige Kapseltemperatur und dokumentierter
  Hinweis zum unskalierten barometrischen Luftdruck in Block 24; unbekannter
  Kraftstoff bricht den Wartungsstart sicher ab, statt als Gas zu gelten
- Abschaltcodes der gemeinsamen Laufhistorie als Freigabemaske, bei der gelöschte
  Bits aktiv sind, mit Hexwert und deutschen Original-Klartexten dekodiert

## V3 1.0.1 - 2026-08-09

- Historisierung auf 21 ausgewählte Betriebs- und Messwerte aus den beiden
  schnellen Blöcken 22/24 begrenzt und pro Messzyklus als ein gemeinsamer
  kompakter SQLite-Datensatz gespeichert; Block 20 mit seinen weitgehend
  statischen Geräteinformationen läuft nur noch im langsamen Zyklus
- das konfigurierte Intervall von 0,75 s ist jetzt der Zielabstand kompletter
  Zyklen statt einer zusätzlichen Pause nach sämtlichen seriellen Lesevorgängen
- feste MSR2-Textfelder bewahren bei unveränderten und geänderten Schreibvorgängen
  ihre bereits vorhandene Leerzeichen- oder NUL-Auffüllung, statt vorhandene
  Leerzeichen unbemerkt durch NUL-Bytes zu ersetzen

## V3 1.0 - 2026-08-09

- erster stabiler öffentlicher 1.0-Stand mit der seit V3 0.9.0 erweiterten
  Bedien-, Überwachungs- und Wartungsoberfläche
- zentrale serielle FIFO-Architektur, Bearbeitung der zugeordneten Register,
  Wartungsarchiv, Fehlerkatalog, frei konfigurierbare Übersichtsseite und beide
  Bedienansichten gemeinsam als Version 1.0 veröffentlicht
- kontrollierte Hardware-Schreibvorgänge bleiben unverändert an ausdrückliche
  Freigabe, Authentifizierung, positive Bestätigung (ACK), Rückleseprüfung und
  Prüfprotokoll gebunden

## V3 0.9.10 - 2026-08-09

- bisherige generische Motorillustration durch eine eigens erzeugte technische
  3D-Darstellung in der charakteristischen kompakten Dachs-Bauform ersetzt:
  schwarzer Einzylindermotor, offene Schwungscheibe und silberne Seitendeckel
- transparentes, logo- und textfreies PNG in Anlagenübersicht und technischem
  Funktionsschema; aktuelle Werte, Generator und Medienwege bleiben unverändert

## V3 0.9.9 - 2026-08-08

- eigene generische Motorillustration für die Bedienoberfläche erzeugt: liegendes,
  stationäres Einzylinder-Aggregat in horizontaler Seitenansicht statt des
  bisherigen abstrakten Linienrasters
- transparent freigestelltes PNG ohne Herstellerlogo, Markenbezug oder
  übernommenes Fremdmaterial; Einbindung in Anlagenübersicht und technisches
  Funktionsschema bei weiterhin getrennt dargestelltem Generator

## V3 0.9.8 - 2026-08-08

- Rußfilter in der technischen Funktionsansicht als Wabenkörper neu gezeichnet;
  Motorabgastemperatur steht links vor dem Filter und Dachs-Abgastemperatur
  rechts nach dem Filter, ohne doppelte Temperaturzeile darunter
- geschätzter Rußfilter-Füllstand linear aus der Motorabgastemperatur:
  standardmäßig 0 % bis 420 °C und 100 % ab 520 °C; unter 60 % grün,
  von 60 bis 89 % orange und ab 90 % rot
- 0-%- und 100-%-Temperatur als lokale, nur durch Administratoren änderbare
  Einstellung in der Weboberfläche; die Kennlinie bleibt nach Neustarts und
  Aktualisierungen erhalten, und ihre Änderung löst keinen seriellen
  Schreibvorgang aus

## V3 0.9.7 - 2026-08-08

- technische Funktionsansicht überarbeitet: Beschriftung und aktueller Wert stehen
  in Motor-, Generator-, Kühlkreis-, Regler- und Netzgruppen jeweils eindeutig
  in derselben Tabellenzeile
- Kapseltemperatur fachlich vom Generator zum Regler verschoben; Dachs-Eintritt,
  Dachs-Abgas nach Rußfilter und elektrische Messstelle eindeutiger benannt
- Versionsnummer in Anmeldung und Kopfzeile anklickbar; der Änderungsverlauf
  öffnet als zugängliches Dialogfenster direkt in der Weboberfläche

## V3 0.9.6 - 2026-08-08

- Fehlerkatalog aus den Einstellungen herausgelöst und als eigener Hauptbereich
  neben Übersicht, Überwachung, Wartung, Einstellungen und dem Bereich für
  Schreibprotokolle verfügbar
- Anlagenmeldung springt weiterhin direkt zum passenden Servicecode, jetzt aber
  in den neuen Fehlerkatalog

## V3 0.9.5 - 2026-08-08

- neues, aufgeräumtes Anlagenbild mit eindeutigen Flussrichtungen für roten
  Dachs-Austritt, blauen Dachs-Eintritt und den außen ansetzenden Abgasweg
- zusätzliche technische Funktionsansicht mit Motor, Generator, Kühlkreis,
  MSR2-Regler, Sensorpunkten, Rußfilter und Netzseite
- Störungs- und Warnstatus direkt über beiden Ansichten; Zustand wird durch
  Form, Text und Farbe statt allein durch Farbe dargestellt
- eigener kompakter Fehlerkatalog im JSON-Schema
  `open-dachs-manager/fault-catalog/v1` mit 222 deutschen Klartexten
- aktive Meldungen zeigen Code und Beschreibung gemeinsam, zum Beispiel
  `SC 163 · Leistung zu klein`; optionale lokale Diagnosedaten können weiterhin
  Ursachen und Maßnahmen ergänzen

## V3 0.9.4 - 2026-08-08

- Anlagenbild durch eine übersichtliche, farbcodierte Anlagentafel mit klaren
  Tabellenwerten und eindeutigen Flussrichtungen ersetzt
- roter Dachs-Austritt, blauer Dachs-Eintritt und ein ausschließlich außen am
  Dachs ansetzender schwarz-silberner Abgasweg
- Rückleseprüfung kontrolliert nach positivem ACK gezielt die geänderten Feldbytes;
  laufende Zähler im selben Block erzeugen keinen falschen Schreibfehler mehr
- verzögerte Wiederholungen der Rückleseprüfung für Regler, die einen neuen
  Sollwert erst nach einer kurzen Verarbeitungszeit zurückmelden
- Prüfprotokoll unterscheidet vollständige Block-Rückleseprüfung und gezielte
  Feld-Rückleseprüfung und nennt die Zahl der benötigten Versuche

## V3 0.9.3 - 2026-08-08

- Wartungsrestzeit direkt im Kopf neben dem seriellen Verbindungsstatus; ein
  Klick öffnet die Wartungsansicht
- Wirkleistung Ist/Soll steht als erstes Element der Übersicht
- frei auswählbare und sortierbare Übersichtskacheln mit allen zugeordneten
  Anlagenwerten; Auswahl bleibt lokal auf dem Pi gespeichert
- optionaler lokaler Original-Fehlerkatalog mit vollständiger Suche nach
  Servicecodes, Ursachen und Maßnahmen; SC 163 wird als „Leistung zu klein“
  aufgelöst
- konfigurierbarer URL-Grundpfad für Unterpfade eines vorgeschalteten Webservers

## V3 0.9.2 - 2026-08-08

- Administratoren können offene und abgeschlossene Wartungen aus dem lokalen
  Archiv löschen
- Sicherheitsabfrage vor dem Entfernen von Zustandsabbild, Protokoll und Exporten
- ein bereits erfolgter Reglerabschluss sowie das Schreibprotokoll bleiben unberührt

## V3 0.9.1 - 2026-08-08

- dauerhafter Administratorschalter für den Wartungs-Testmodus in den Einstellungen
- standardmäßig weiterhin rein lokaler Wartungsabschluss ohne Schreibvorgang
  am Regler
- bei deaktiviertem Testmodus kontrollierter Echtabschluss mit Authentifizierung,
  exakter Bestätigung, positiver Bestätigung (ACK) und Rückleseprüfung
- Test-/Echtmodus wird in `/var/lib/open-dachs-manager/maintenance_settings.json`
  gespeichert und bleibt nach Neustarts und Aktualisierungen erhalten

## V3 0.9.0 - 2026-08-03

- neuer kompakter dreiseitiger Wartungsnachweis in HTML und PDF
- zusätzliche lokale Arbeitsliste neben den MSR2-Prüfpunkten
- serverseitig gesperrter Wartungs-Demomodus ohne Register-Schreibvorgang
- gemeinsames Anlagen-Zustandsabbild und lokales Berichtsarchiv
- korrigierte physische Byte-Offsets in Block 24; Gas/Heizöl wird beim
  Wartungsstart wieder automatisch aus `Hka_Mw1.bKraftstofftyp` erkannt
- getrennte Adressierung der beiden Netzüberwachungs-CPUs und geprüfte
  Block-16-Feldzuordnung für den eingesetzten Überwachungscontroller
- rot gekennzeichneter Netzschutzeditor mit dem bestehenden Administrator-,
  Authentifizierungs- und Hardware-Freigabeschalter sowie positiver Bestätigung
  (ACK), Rückleseprüfung und Prüfprotokoll

## 0.1.0 - 2026-08-03

- erstmalige Veröffentlichung des Open-Dachs-Manager-Quellcodes
- installierbares Python-Paket mit Kommandozeile, Terminal- und Weboberfläche
- zentraler FIFO-Dienst als alleiniger Besitzer des seriellen Adapters
- lokale Messwerthistorie und Wartungsberichte
- kontrollierter Schreibpfad mit Authentifizierung, positiver Bestätigung (ACK),
  Rückleseprüfung und Prüfprotokoll
- systemd-Dienste und `install.sh`
