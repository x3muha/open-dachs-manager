# Technische Analyse des MSR2-Serienprotokolls

Dieses Dokument beschreibt die im Open Dachs Manager implementierte
Kommunikation. Es ist eine eigenständig formulierte Arbeitsgrundlage aus
reproduzierbaren Telegrammtests, Bytevergleichen und dem Verhalten des
aktuellen Programmcodes. Unbestätigte Bedeutungen werden als solche markiert.

## Physische Verbindung

Der getestete Aufbau verwendet einen optischen USB-Lesekopf und ein unter
Linux sichtbares serielles Gerät:

```text
19200 Baud · 8 Datenbits · keine Parität · 1 Stoppbit (8N1)
```

Die optische Bauform kann DIN EN 62056-21 beziehungsweise IEC 62056-21
entsprechen. Das hier beschriebene binäre MSR2-Telegramm ist jedoch kein
Zählerprotokoll nach IEC 62056-21. Die Normangabe beschreibt beim empfohlenen
Lesekopf die optische Schnittstelle, nicht den Inhalt der MSR2-Nutzdaten.

## Datenrahmen

Ein Datenrahmen besitzt diese Struktur:

| Position | Länge | Inhalt |
|---|---:|---|
| 0 | 1 Byte | `0x02` – Beginn eines Datenrahmens |
| 1 | 1 Byte | Quelladresse, beim Manager `0x00` |
| 2 | 1 Byte | Zieladresse |
| 3 | 1 Byte | Paketnummer im oberen Nibble, Längenbits 8–11 im unteren Nibble |
| 4 | 1 Byte | Nutzdatenlänge, Bits 0–7 |
| 5… | variabel | Nutzdaten |
| Ende | 2 Byte | CRC16, Big Endian |

Die Nutzdatenlänge ist zwölf Bit breit und damit auf 4095 Byte begrenzt. Die
Paketnummer läuft von 0 bis 15 und beginnt danach wieder bei 0.

## CRC

Die CRC wird über Kopf und Nutzdaten einschließlich `0x02`, aber ohne die
beiden CRC-Bytes gebildet:

```text
Breite:       16 Bit
Polynom:      0x1021
Initialwert:  0x0000
Ausgabe:      Big Endian
```

Frames mit falscher CRC werden verworfen und nie dekodiert.

## ACK und NACK

Eine positive Antwort beginnt mit `0x06`, eine negative mit `0x15`. Danach
folgen die beiden Steuer-/Längenbytes des bestätigten Datenrahmens und wieder
eine CRC16:

```text
06 <Steuerbyte> <Längenbyte> <CRC16>
15 <Steuerbyte> <Längenbyte> <CRC16>
```

Empfängt der Manager einen Datenrahmen, bestätigt er diesen ebenfalls mit
einem positiven ACK.

## CPU-Adressierung

Die CPU wird nicht in der Blocknummer kodiert. Sie steckt im unteren Nibble
der Zieladresse:

```text
Zieladresse = (Modul << 4) | CPU
```

Der Manager verwendet Modul 1:

| logische CPU | Zieladresse | Aufgabe |
|---:|---:|---|
| CPU 0 | `0x10` | MSR2-Regler |
| CPU 1 | `0x11` | Netzüberwachung 1 |
| CPU 2 | `0x12` | Netzüberwachung 2 |

Damit können drei verschiedene CPUs jeweils einen eigenen Block 16 besitzen.
Es sind keine drei seriellen Ports: Alle Telegramme laufen nacheinander über
denselben optischen Anschluss und unterscheiden sich nur im Zielbyte.

## Block lesen

Ein Lesevorgang besteht aus zwei Telegrammen an dieselbe CPU:

1. Synchronisation mit leerer Nutzlast und Paketnummer `n`.
2. Leseanforderung mit dem Block als einzigem Nutzdatenbyte und Paketnummer
   `n + 1`.

Die Antwort enthält als erstes Byte einen Status. Erst danach folgen die
eigentlichen Blockdaten. Open Dachs Manager trennt deshalb:

```text
Antwortnutzdaten = <Statusbyte> + <Blockpayload>
```

Beispiel für Block 16 mit Paketnummern 0 und 1:

```text
CPU 0 Sync:  02 00 10 00 00 07 E0
CPU 0 Read:  02 00 10 10 01 10 F2 84

CPU 1 Sync:  02 00 11 00 00 30 D0
CPU 1 Read:  02 00 11 10 01 10 84 30

CPU 2 Sync:  02 00 12 00 00 69 80
CPU 2 Read:  02 00 12 10 01 10 1F EC
```

Nur das Zielbyte und dadurch die CRC unterscheiden sich. Der angeforderte
Block bleibt in allen drei Leseanforderungen `0x10`, also dezimal 16.

## Block schreiben

Schreiben verwendet die Dienstnummer `Block + 1` und überträgt anschließend
den vollständigen Blockpayload:

```text
Nutzdaten = <Block + 1> + <vollständiger Blockpayload>
```

Ein einzelnes Feld wird daher nicht isoliert gesendet. Der Manager liest den
aktuellen Block, verändert nur die ausgewählten Bytes im lokalen Abbild und
schreibt den kompletten Block zurück. Danach müssen positives ACK und
bytegenauer Readback vorliegen.

## Netzschutzblock 16

CPU 1 und CPU 2 liefern für Block 16 jeweils 18 Nutzdatenbytes. Diese werden
unabhängig dekodiert und im Web rot dargestellt. Die rote Markierung ist eine
Verwechslungssicherung, weil Spannungs-, Frequenz- und Abschaltgrenzen
sicherheitsrelevant sind. Technisch nutzt der Block denselben geprüften
Schreibablauf wie andere Register:

```text
Lesen → lokal ändern → Auth → vollständigen Block schreiben
→ positives ACK → bytegenauer Readback → Audit
```

Das bloße Öffnen oder Aktualisieren der Seite führt ausschließlich den oben
beschriebenen Lesevorgang aus.

## Serialworker und Warteschlange

Nur der Serialworker öffnet das Linux-Gerät. Weboberfläche, CLI und TUI senden
JSON-RPC-Aufträge über einen lokalen Unix-Socket. Jede Socket-Sitzung ist ein
FIFO-Auftrag und behält den seriellen Zugriff bis zum Sitzungsende. Dadurch
werden mehrstufige Abläufe nicht von einem zweiten Client unterbrochen.

## Grenzen des derzeitigen Wissens

- Die Adressierung und die hier genannten Rahmenformate sind praktisch
  getestet.
- CPU 1 und CPU 2, Block 16, sind am Testgerät lesend bestätigt.
- Die Bedeutung nicht dokumentierter Reservebytes wird nicht geraten.
- Gerätevarianten können andere Blocklängen oder Feldbelegungen verwenden.
- Vor Live-Schreibtests sind Backup, exakter Gerätevergleich und Readback
  zwingend.

Die zugehörige Implementierung liegt in `transport.py`, `serial_worker.py`,
`service.py` und `network_protection.py`.
