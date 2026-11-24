#!/usr/bin/env python3
"""Offline reference model of the MSR2 MMI start-page status lines 1 and 2.

Firmware: main controller target-10-00f00000.bin (R 000.004.043), Ghidra
M16C/80 @ 0xF00000.  Display field switch 0xf62ac5: field 0x0c18 -> 0xf5edf3
(status line 1, index byte 0x00291c, table 0xf3c2b0, 4 x 14 entries);
field 0x0c19 -> 0xf5eebf (status line 2, index byte 0x00291d, table
0xf3c390, 4 x 34 entries; service-code branch 0xf5f0a8, table 0xf3c5ac,
4 x 256 entries).  German texts only (language 0x002918 == 0).

`ram` maps absolute CPU-0 RAM addresses to unsigned byte values.  Every
statement below cites the instruction address it reproduces.  This is an
analysis artifact, not product code.
"""

from __future__ import annotations

import json
import sys

LINE1_DE = [
    "Keine Statusanzeige 1", "Dachs ausgeschaltet", "Startvorbereitung",
    "Starteinleitung", "Der Dachs läuft ...", "25s Abschaltzeit",
    "Keine Anforderung", "Startverzögerung ein", "Keine Lauf-Freigabe",
    "Wartezustand", "Anlagenstörung", "IB erforderlich",
    "Platine getauscht !", "Wartezeit Stromnetz",
]

LINE2_DE = [
    "Keine Statusanzeige 2", "Dachs G 5.5", "Dachs G 5.0", "Dachs HR 5.3",
    "Dachs RS 5.0", "Dachs SE G 5.5", "Dachs SE G 5.0", "Dachs SE HR 5.3",
    "Dachs SE RS 5.0", "Dachs Modulregler", "Wärmeanforderung",
    "Wärmeanforderung HK 1", "Wärmeanforderung HK 2", "Anf. wegen Warmwasser",
    "Stromanf. intern Uhr", "Stromanf. extern", "Anf. über Stromzähler",
    "Anf. über Leitregler", "Busfehler Notbetrieb", "Biv. Umschaltz. aktiv",
    "Ansteuerung Kessel", "Ansteuerung Heizstab", "Mindestlaufzeit aktiv",
    "Rußfilterregeneration", "Anf. Prüflauf Dachs", "Eintrittstemp. > Soll",
    "Interne Temp. > Soll", "Takten / Stillstand", "Keine Modulfreig. Uhr",
    "K. Modulfreig. extern", "Sicherheitskette auf", "Systemcheck anfordern",
    "Anf. zykl. Dachslauf", "WW:therm Desinfektion",
]


def bit(ram: dict[int, int], addr: int, n: int) -> bool:
    return bool(ram[addr] >> n & 1)


def line1_index(ram: dict[int, int]) -> int:
    idx = 0                                                   # f5edf3
    if not bit(ram, 0x0EA1, 6):                               # f5edf8 BTST 6,0ea1 ; JNE
        idx = 1                                               # f5ee85
    elif ram[0x0E91] != 0:                                    # f5ee02
        idx = 9 if ram[0x1C9F] == 1 else 10                   # f5ee6f..f5ee7e
    else:
        ms = ram[0x0ED4]
        if ms == 0x14:                                        # f5ee09/f5ee0f STZ
            idx = 2
        if ms == 0x15:                                        # f5ee15/f5ee1b STZ
            idx = 3
        if ms >= 0x16:                                        # f5ee21 JLTU
            idx = 4
        if not bit(ram, 0x0EA0, 0):                           # f5ee2e STZ
            idx = 6
        frei = ram[0x0EA0] | ram[0x0EA1] << 8                 # f5ee39 MOV.W
        if frei not in (0xFFFE, 0xFFFF):                      # f5ee3e..f5ee44 STNZ
            idx = 8
        if not bit(ram, 0x0EA1, 1):                           # f5ee4a STZ
            idx = 7
        if not bit(ram, 0x0EA0, 1):                           # f5ee55 STZ
            idx = 5
        if not bit(ram, 0x0EA1, 2):                           # f5ee60 JNE / MOV
            idx = 13
    if not bit(ram, 0x0EA1, 7):                               # f5ee8a STZ
        idx = 11
    if ram[0x1BCC] == 1:                                      # f5ee95 STZ
        idx = 12
    return idx                                                # f5eea4..f5eeb7


def line2(ram: dict[int, int]) -> tuple[str, int]:
    """Return ("status", index) or ("servicecode", table_index)."""
    if ram[0x0E91] != 0:                                      # f5eec4 -> f5f0a8
        sc = ram[0x0E91]
        if bit(ram, 0x1B5C, 0):                               # f5f0ae
            if sc == 0x16:
                sc = 0xF0                                     # f5f0b9
            elif sc == 0x18:
                sc = 0xF1                                     # f5f0bf STZ
        return "servicecode", sc
    idx = 0                                                   # f5eebf
    mehrmodul = ram[0x1ADD]
    slave = mehrmodul != 0 and (ram[0x1AF4] & 0x0F) != 1      # f5eece..f5eede
    if slave:
        idx = 9                                               # f5ef54
    else:
        art, typ = ram[0x1ADA], ram[0x0F0A]                   # f5eee0/f5eee5
        se = art in (1, 2)
        if typ & 0x80:                                        # f5eeea gas
            g50 = typ == 0x90
            idx = (6 if g50 else 5) if se else (2 if g50 else 1)
        elif typ & 0x08:                                      # f5ef1f oil
            rs = typ == 0x0A
            idx = (8 if rs else 7) if se else (4 if rs else 3)
    if ram[0x1B12] & 0x0C == 0x04:                            # f5ef5a..f5ef63
        idx = 31
    anf = ram[0x0E9E] | ram[0x0E9F] << 8
    if anf != 0:                                              # f5ef69
        if anf & 0x0001: idx = 22                             # f5ef73
        if anf & 0x000C: idx = 13                             # f5ef7e
        if anf & 0x0002: idx = 10                             # f5ef8b
        if anf & 0x0040:                                      # f5ef96
            sf = ram[0x0EA4]
            if sf & 0x02: idx = 14                            # f5ef9d
            if sf & 0x24: idx = 15                            # f5efa8
            if sf & 0x18: idx = 16                            # f5efb5
        if mehrmodul == 1 and (ram[0x1AF4] & 0x0F) != 1:      # f5efc2..f5efd2
            if anf & 0x0080: idx = 17                         # f5efd4
            if ram[0x1ED4] == 1: idx = 18                     # f5efdf
        if anf & 0x0100: idx = 32                             # f5efea (0e9f bit 0)
    if ram[0x0ED4] >= 0x20 and bit(ram, 0x0EFA, 0):           # f5eff5..f5f002
        idx = 19
    if not bit(ram, 0x0EFF, 3) and ram[0x1ADC] != 0:          # f5f008..f5f014
        idx = 21 if ram[0x1AE2] & 0xE0 == 0x20 else 20        # f5f016..f5f01f STZX
    if ram[0x0DB3] != 0: idx = 23                             # f5f026
    if bit(ram, 0x0F96, 1): idx = 33                          # f5f031
    if bit(ram, 0x0E9E, 5): idx = 24                          # f5f03c
    if not bit(ram, 0x0EA0, 6): idx = 25                      # f5f047
    if not bit(ram, 0x0EA0, 5): idx = 26                      # f5f052
    if not bit(ram, 0x0EA0, 2): idx = 27                      # f5f05d
    if not bit(ram, 0x0EA1, 5): idx = 28                      # f5f068
    if not bit(ram, 0x0EA1, 4): idx = 29                      # f5f073
    if not bit(ram, 0x0F33, 4): idx = 30                      # f5f07e
    return "status", idx                                      # f5f08d..f5f0a0


def ram_from_blocks(blocks: dict[int, bytes], extra: dict[int, int]) -> dict[int, int]:
    """Block payload = contiguous RAM copy (block service switch 0xf47d2d)."""
    bases = {22: 0x0E7A, 24: 0x0ED4, 26: 0x0F1A, 50: 0x1ACC, 76: 0x0F80, 104: 0x1B12}
    ram: dict[int, int] = {}
    for block, payload in blocks.items():
        base = bases.get(block)
        if base is None:
            continue
        for offset, value in enumerate(payload):
            ram[base + offset] = value
    ram.update(extra)
    return ram


def main(paths: list[str]) -> None:
    for path in paths:
        data = json.load(open(path, encoding="utf-8"))
        blocks = {
            int(k): bytes.fromhex(v["payload_hex"])
            for k, v in data["blocks"].items() if v.get("ok")
        }
        ram = ram_from_blocks(blocks, {})
        missing = [a for a in (0x0DB3, 0x0F96, 0x1BCC, 0x1C9F, 0x1B5C, 0x1ED4) if a not in ram]
        print(f"== {path.rsplit('/', 1)[-1]}  (nicht im Mitschnitt: {', '.join(hex(a) for a in missing)})")
        for label, db3 in (("db3=0", 0), ("db3=1", 1)):
            trial = dict(ram)
            trial.setdefault(0x0F96, 0)   # block 76 not captured: assume WW-Desinfektion inaktiv
            trial.setdefault(0x1BCC, 0)
            trial.setdefault(0x1C9F, 0)
            trial.setdefault(0x1B5C, 0)
            trial.setdefault(0x1ED4, 0)
            trial[0x0DB3] = db3
            i1 = line1_index(trial)
            kind, i2 = line2(trial)
            t2 = LINE2_DE[i2] if kind == "status" else f"Servicecode-Index {i2}"
            print(f"   {label}: Zeile1[{i1}]={LINE1_DE[i1]!r}  Zeile2[{i2}]={t2!r}")


if __name__ == "__main__":
    main(sys.argv[1:])
