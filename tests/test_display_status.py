import http.client
import importlib.util
import json
import random
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from open_dachs_manager.display_status import (
    DISPLAY_STATUS_BLOCK_BASES,
    DISPLAY_STATUS_BLOCK_INPUTS,
    DISPLAY_STATUS_RAM_ADDRESSES,
    LINE1_TEXTS,
    LINE2_TEXTS,
    SERVICE_CODE_TEXTS,
    compute_display_status,
    inputs_from_payloads,
    line1_index,
    line2_result,
    ram_reads_needed,
)
from open_dachs_manager.transport import Frame, Response, TransportError
from open_dachs_manager.web import (
    DASHBOARD_DISPLAY_STATUS_KEY,
    DISPLAY_STATUS_RAM_FIELDS,
    DachsHTTPServer,
    DachsStore,
    DachsWebApp,
    init_users,
    ram_telemetry_request,
)


ROOT = Path(__file__).resolve().parents[1]
REFERENCE_PATH = ROOT / "tests" / "fixtures" / "display_status_reference.py"
TEXTS_PATH = ROOT / "src" / "open_dachs_manager" / "data" / "display_status_texts_de.json"


def load_reference():
    spec = importlib.util.spec_from_file_location("display_status_reference", REFERENCE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def base_ram(**changes):
    """Switched on, all releases set, no demand, single module, HR 5.3."""
    ram = {
        0x0E91: 0, 0x0E9E: 0, 0x0E9F: 0, 0x0EA0: 0xFF, 0x0EA1: 0xFF, 0x0EA4: 0,
        0x0ED4: 0, 0x0EFA: 0, 0x0EFF: 0x08, 0x0F0A: 0x08,
        0x0F33: 0x10,
        0x1ADA: 5, 0x1ADC: 0, 0x1ADD: 0, 0x1AE2: 0, 0x1AF4: 0x11,
        0x1B12: 0,
        0x0DB3: 0, 0x0F96: 0, 0x1BCC: 0, 0x1C9F: 0, 0x1B5C: 0, 0x1ED4: 0,
    }
    for name, value in changes.items():
        ram[int(name[1:], 16)] = value
    return ram


def line_texts(ram):
    status = compute_display_status(ram, recorded_at="2026-10-24T18:00:00+00:00")
    return status["line1"].strip(), status["line2"].strip(), status


def payloads_from_ram(ram):
    payloads = {}
    for block, base in DISPLAY_STATUS_BLOCK_BASES.items():
        payload = bytearray(70)
        for address, value in ram.items():
            if 0 <= address - base < 70:
                payload[address - base] = value
        payloads[block] = bytes(payload)
    return payloads


def random_ram(rng):
    def biased():
        roll = rng.random()
        if roll < 0.35:
            return 0
        if roll < 0.6:
            return 0xFF
        if roll < 0.75:
            return 1
        return rng.randrange(256)

    ram = {address: biased() for address in base_ram()}
    if rng.random() < 0.6:
        ram[0x0E91] = 0
    if rng.random() < 0.2:
        ram[0x0E91] = rng.choice((0x16, 0x18))
    ram[0x0F0A] = rng.choice((0x08, 0x0A, 0x80, 0x90, 0xC0, 0xD0, 0x00, rng.randrange(256)))
    ram[0x0ED4] = rng.choice((0, 10, 0x14, 0x15, 0x16, 0x1F, 0x20, 0x21, rng.randrange(256)))
    ram[0x1AF4] = rng.choice((0x11, 0x12, 0x01, 0x02, rng.randrange(256)))
    ram[0x1ADD] = rng.choice((0, 1, 2))
    ram[0x1ED4] = rng.choice((0, 1, 0x09, 0x08))
    return ram


class FakeRamSession:
    def __init__(self, values=None, fail=()):
        self.values = dict(values or {})
        self.fail = set(fail)
        self.calls = []
        self.packet = 0

    def next_packet(self):
        self.packet += 1
        return self.packet

    def request(self, payload, packet, timeout, cpu):
        self.calls.append((bytes(payload), timeout, cpu))
        address = payload[1] << 8 | payload[2]
        if address in self.fail:
            raise TransportError("Timeout")
        length = payload[3]
        body = bytes([0x82, payload[1], payload[2], length, self.values.get(address, 0)]) + bytes(length - 1)
        return Response(
            tx=b"",
            ack=Frame("ack", packet, b"", positive=True),
            data=Frame("data", packet, b"", body),
            elapsed_ms=1.0,
        )


class DisplayStatusTextTests(unittest.TestCase):
    def test_texts_are_the_extracted_firmware_tables(self):
        data = json.loads(TEXTS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["source"]["sha256"], "07dbe0d12833a731fbe5af47613b93236a54397335d0df0f66bcb832ba1a30bc")
        self.assertEqual(len(LINE1_TEXTS), 14)
        self.assertEqual(len(LINE2_TEXTS), 34)
        self.assertEqual(len(SERVICE_CODE_TEXTS), 256)
        reference = load_reference()
        self.assertEqual([text.strip() for text in LINE1_TEXTS], reference.LINE1_DE)
        self.assertEqual(list(LINE2_TEXTS), reference.LINE2_DE)
        # Only right padding is trimmed; centred texts keep their indent.
        self.assertEqual(LINE1_TEXTS[9], "    Wartezustand")
        self.assertEqual(LINE1_TEXTS[12], " Platine getauscht !")
        self.assertTrue(all(text == text.rstrip() for text in LINE1_TEXTS + LINE2_TEXTS + SERVICE_CODE_TEXTS))
        self.assertEqual(SERVICE_CODE_TEXTS[0], "000 kein Servicecode")
        self.assertEqual(SERVICE_CODE_TEXTS[0x16], "122 Kuehlw.Motor>95°C")
        self.assertEqual(SERVICE_CODE_TEXTS[0xF0], "122 Kuehlw.Motor>100°")
        self.assertEqual(SERVICE_CODE_TEXTS[0xF1], "124 Kuehlw.Gen. >82°C")
        self.assertEqual(SERVICE_CODE_TEXTS[255], "355 Int. Stack > Soll")
        for index, text in enumerate(SERVICE_CODE_TEXTS[1:0xF0], start=1):
            self.assertTrue(text.startswith(f"{100 + index:03d}"), text)


class DisplayStatusPriorityTests(unittest.TestCase):
    CASES = (
        # (name, changes, line1, line2)
        ("idle", {}, "Keine Statusanzeige 1", "Dachs HR 5.3"),
        ("aus", {"x0ea1": 0xBF}, "Dachs ausgeschaltet", "Dachs HR 5.3"),
        ("startvorbereitung", {"x0ed4": 0x14}, "Startvorbereitung", "Dachs HR 5.3"),
        ("starteinleitung", {"x0ed4": 0x15}, "Starteinleitung", "Dachs HR 5.3"),
        ("lauf", {"x0ed4": 0x16}, "Der Dachs läuft ...", "Dachs HR 5.3"),
        ("keine anforderung", {"x0ea0": 0xFE, "x0ed4": 0x16}, "Keine Anforderung", "Dachs HR 5.3"),
        ("25s abschaltzeit", {"x0ea0": 0xFD}, "25s Abschaltzeit", "Dachs HR 5.3"),
        ("freigabe 0xfffe", {"x0ea0": 0xFE}, "Keine Anforderung", "Dachs HR 5.3"),
        ("freigabewort", {"x0ea0": 0xF7}, "Keine Lauf-Freigabe", "Dachs HR 5.3"),
        ("startverzoegerung", {"x0ea1": 0xFD}, "Startverzögerung ein", "Dachs HR 5.3"),
        ("wartezeit stromnetz", {"x0ea1": 0xFB, "x0ed4": 0x16}, "Wartezeit Stromnetz", "Dachs HR 5.3"),
        ("ib erforderlich", {"x0ea1": 0x7F, "x0ed4": 0x16}, "IB erforderlich", "Dachs HR 5.3"),
        ("ib auch ausgeschaltet", {"x0ea1": 0x3F}, "IB erforderlich", "Dachs HR 5.3"),
        ("platine getauscht", {"x0ea1": 0x7F, "x1bcc": 1}, "Platine getauscht !", "Dachs HR 5.3"),
        ("platine nur bei 1", {"x1bcc": 3}, "Keine Statusanzeige 1", "Dachs HR 5.3"),
        ("sicherheitskette auf", {"x0f33": 0x00, "x0db3": 1, "x0f96": 2}, "Keine Statusanzeige 1", "Sicherheitskette auf"),
        ("servicecode", {"x0e91": 0x1F}, "Anlagenstörung", "131 Drehzahl<100U/min"),
        ("servicecode wartezustand", {"x0e91": 0x1F, "x1c9f": 1}, "Wartezustand", "131 Drehzahl<100U/min"),
        ("servicecode ausgeschaltet", {"x0e91": 0x1F, "x0ea1": 0xBF}, "Dachs ausgeschaltet", "131 Drehzahl<100U/min"),
        ("servicecode ueberdeckt sicherheitskette", {"x0e91": 0x14, "x0f33": 0}, "Anlagenstörung", "120 Abgastemp.Motor"),
        ("122 normal", {"x0e91": 0x16}, "Anlagenstörung", "122 Kuehlw.Motor>95°C"),
        ("122 alternativ", {"x0e91": 0x16, "x1b5c": 0x01}, "Anlagenstörung", "122 Kuehlw.Motor>100°"),
        ("124 alternativ", {"x0e91": 0x18, "x1b5c": 0xFF}, "Anlagenstörung", "124 Kuehlw.Gen. >82°C"),
        ("124 bit 0 aus", {"x0e91": 0x18, "x1b5c": 0xFE}, "Anlagenstörung", "124 Kuehlw.Gen. >77°C"),
        ("123 ohne sonderfall", {"x0e91": 0x17, "x1b5c": 0x01}, "Anlagenstörung", "123 Abgastemp.zu hoch"),
        ("russfilterregeneration", {"x0db3": 1, "x0ed4": 0x22}, "Der Dachs läuft ...", "Rußfilterregeneration"),
        ("regeneration unterbrochen", {"x0db3": 2}, "Keine Statusanzeige 1", "Rußfilterregeneration"),
        ("desinfektion", {"x0f96": 0x02, "x0db3": 1}, "Keine Statusanzeige 1", "WW:therm Desinfektion"),
        ("desinfektion nur bit 1", {"x0f96": 0xFD}, "Keine Statusanzeige 1", "Dachs HR 5.3"),
        ("pruefllauf", {"x0e9e": 0x20, "x0f96": 2}, "Keine Statusanzeige 1", "Anf. Prüflauf Dachs"),
        ("mindestlaufzeit", {"x0e9e": 0x01}, "Keine Statusanzeige 1", "Mindestlaufzeit aktiv"),
        ("warmwasser", {"x0e9e": 0x05}, "Keine Statusanzeige 1", "Anf. wegen Warmwasser"),
        ("waermeanforderung", {"x0e9e": 0x0F}, "Keine Statusanzeige 1", "Wärmeanforderung"),
        ("stromanf uhr", {"x0e9e": 0x40, "x0ea4": 0x02}, "Keine Statusanzeige 1", "Stromanf. intern Uhr"),
        ("stromanf extern", {"x0e9e": 0x40, "x0ea4": 0x04}, "Keine Statusanzeige 1", "Stromanf. extern"),
        ("stromzaehler", {"x0e9e": 0x40, "x0ea4": 0x10}, "Keine Statusanzeige 1", "Anf. über Stromzähler"),
        ("stromflag ohne bit 6", {"x0e9e": 0x02, "x0ea4": 0x10}, "Keine Statusanzeige 1", "Wärmeanforderung"),
        ("leitregler", {"x1add": 1, "x1af4": 0x12, "x0e9e": 0x80}, "Keine Statusanzeige 1", "Anf. über Leitregler"),
        ("leitregler nur bei 1add==1", {"x1add": 2, "x1af4": 0x12, "x0e9e": 0x80}, "Keine Statusanzeige 1", "Dachs Modulregler"),
        ("leitmodul selbst", {"x1add": 1, "x1af4": 0x11, "x0e9e": 0x80}, "Keine Statusanzeige 1", "Dachs HR 5.3"),
        ("busfehler", {"x1add": 1, "x1af4": 0x12, "x0e9e": 0x80, "x1ed4": 1}, "Keine Statusanzeige 1", "Busfehler Notbetrieb"),
        ("busfehler ohne anforderung", {"x1add": 1, "x1af4": 0x12, "x1ed4": 1}, "Keine Statusanzeige 1", "Dachs Modulregler"),
        ("busfehler exakt 1", {"x1add": 1, "x1af4": 0x12, "x0e9e": 0x80, "x1ed4": 0x09}, "Keine Statusanzeige 1", "Anf. über Leitregler"),
        ("zyklischer lauf", {"x0e9f": 0x01, "x1add": 1, "x1af4": 0x12, "x1ed4": 1}, "Keine Statusanzeige 1", "Anf. zykl. Dachslauf"),
        ("modulregler", {"x1add": 1, "x1af4": 0x02}, "Keine Statusanzeige 1", "Dachs Modulregler"),
        ("se g 5.0", {"x1ada": 1, "x0f0a": 0x90}, "Keine Statusanzeige 1", "Dachs SE G 5.0"),
        ("se g 5.5", {"x1ada": 2, "x0f0a": 0x80}, "Keine Statusanzeige 1", "Dachs SE G 5.5"),
        ("g 5.0", {"x0f0a": 0x90}, "Keine Statusanzeige 1", "Dachs G 5.0"),
        ("g 5.5 typ c0", {"x0f0a": 0xC0}, "Keine Statusanzeige 1", "Dachs G 5.5"),
        ("se rs 5.0", {"x1ada": 2, "x0f0a": 0x0A}, "Keine Statusanzeige 1", "Dachs SE RS 5.0"),
        ("se hr 5.3", {"x1ada": 1, "x0f0a": 0x08}, "Keine Statusanzeige 1", "Dachs SE HR 5.3"),
        ("rs 5.0", {"x0f0a": 0x0A}, "Keine Statusanzeige 1", "Dachs RS 5.0"),
        ("kein typ", {"x0f0a": 0x00}, "Keine Statusanzeige 1", "Keine Statusanzeige 2"),
        ("systemcheck", {"x1b12": 0x05}, "Keine Statusanzeige 1", "Systemcheck anfordern"),
        ("systemcheck nicht bei 0x0c", {"x1b12": 0x0C}, "Keine Statusanzeige 1", "Dachs HR 5.3"),
        ("bivalent", {"x0ed4": 0x20, "x0efa": 0x01}, "Der Dachs läuft ...", "Biv. Umschaltz. aktiv"),
        ("bivalent unter 32", {"x0ed4": 0x1F, "x0efa": 0x01}, "Der Dachs läuft ...", "Dachs HR 5.3"),
        ("kessel", {"x0eff": 0x00, "x1adc": 1, "x1ae2": 0x40}, "Keine Statusanzeige 1", "Ansteuerung Kessel"),
        ("heizstab", {"x0eff": 0x00, "x1adc": 1, "x1ae2": 0x3F}, "Keine Statusanzeige 1", "Ansteuerung Heizstab"),
        ("kessel unterdrueckt", {"x0eff": 0x08, "x1adc": 1}, "Keine Statusanzeige 1", "Dachs HR 5.3"),
        ("eintrittstemp", {"x0ea0": 0xBF}, "Keine Lauf-Freigabe", "Eintrittstemp. > Soll"),
        ("interne temp", {"x0ea0": 0x9F}, "Keine Lauf-Freigabe", "Interne Temp. > Soll"),
        ("takten", {"x0ea0": 0xFB}, "Keine Lauf-Freigabe", "Takten / Stillstand"),
        ("modulfreigabe uhr", {"x0ea1": 0xDF}, "Keine Lauf-Freigabe", "Keine Modulfreig. Uhr"),
        ("modulfreigabe extern", {"x0ea1": 0xCF}, "Keine Lauf-Freigabe", "K. Modulfreig. extern"),
    )

    def test_priority_chain_table(self):
        for name, changes, line1, line2 in self.CASES:
            with self.subTest(name):
                got1, got2, status = line_texts(base_ram(**changes))
                self.assertEqual((got1, got2), (line1, line2))
                self.assertTrue(status["available"])
                self.assertIsNone(status["error"])

    def test_service_code_fields(self):
        _l1, _l2, status = line_texts(base_ram(x0e91=0x16, x1b5c=1))
        self.assertEqual(status["service_code"], 122)
        self.assertEqual(status["service_code_index"], 0xF0)
        self.assertEqual(status["line2_kind"], "servicecode")
        self.assertIsNone(status["line2_index"])
        _l1, _l2, plain = line_texts(base_ram())
        self.assertIsNone(plain["service_code"])
        self.assertEqual(plain["line2_kind"], "status")
        self.assertEqual(plain["line2_index"], 3)
        self.assertEqual(plain["line1_index"], 0)
        self.assertEqual(plain["inputs"]["0x0e91"], 0)
        self.assertEqual(plain["inputs"]["0x1ed4"], 0)

    def test_matches_reference_model(self):
        reference = load_reference()
        rng = random.Random(20261024)
        for _ in range(4000):
            ram = random_ram(rng)
            self.assertEqual(line1_index(ram), reference.line1_index(ram), ram)
            self.assertEqual(line2_result(ram), reference.line2(ram), ram)
        for _name, changes, _line1, _line2 in self.CASES:
            ram = base_ram(**changes)
            self.assertEqual(line1_index(ram), reference.line1_index(ram))
            self.assertEqual(line2_result(ram), reference.line2(ram))

    def test_reference_capture_case_regeneration(self):
        # Capture 2026-08-25: HR 5.3, all releases, 0x0e9e=0x0040, 0x0ea4=0x04.
        ram = base_ram(x0ed4=0x22, x0e9e=0x40, x0ea4=0x04, x0f33=0xF8, x0eff=0x0C, x0db3=1)
        self.assertEqual(line_texts(ram)[:2], ("Der Dachs läuft ...", "Rußfilterregeneration"))
        ram[0x0DB3] = 0
        self.assertEqual(line_texts(ram)[:2], ("Der Dachs läuft ...", "Stromanf. extern"))

    def test_conditional_reads_match_accessed_bytes(self):
        # Every RAM-only byte the firmware branch evaluates is in the read
        # plan, and every conditional read is actually evaluated.
        rng = random.Random(42)
        for _ in range(3000):
            ram = random_ram(rng)
            needed = set(ram_reads_needed({a: v for a, v in ram.items() if a not in DISPLAY_STATUS_RAM_ADDRESSES}))
            accessed_by_logic = set()
            for address in DISPLAY_STATUS_RAM_ADDRESSES:
                probe = dict(ram)
                del probe[address]
                status = compute_display_status(probe)
                if not status["available"]:
                    accessed_by_logic.add(address)
            self.assertTrue(accessed_by_logic <= needed, (ram, accessed_by_logic, needed))
            self.assertEqual(needed - accessed_by_logic - {0x0DB3, 0x0F96, 0x1BCC}, set(), ram)

    def test_read_plan_examples(self):
        blocks_only = {a: v for a, v in base_ram().items() if a not in DISPLAY_STATUS_RAM_ADDRESSES}
        self.assertEqual(ram_reads_needed(blocks_only), (0x0DB3, 0x0F96, 0x1BCC))
        self.assertEqual(ram_reads_needed({**blocks_only, 0x0E91: 0x16}), (0x0DB3, 0x0F96, 0x1BCC, 0x1C9F, 0x1B5C))
        self.assertEqual(ram_reads_needed({**blocks_only, 0x0E91: 0x16, 0x0EA1: 0xBF}), (0x0DB3, 0x0F96, 0x1BCC, 0x1B5C))
        slave = {**blocks_only, 0x1ADD: 1, 0x1AF4: 0x12}
        self.assertEqual(ram_reads_needed(slave), (0x0DB3, 0x0F96, 0x1BCC))
        self.assertEqual(ram_reads_needed({**slave, 0x0E9F: 1}), (0x0DB3, 0x0F96, 0x1BCC, 0x1ED4))
        self.assertEqual(ram_reads_needed({}), (0x0DB3, 0x0F96, 0x1BCC))

    def test_missing_input_makes_only_that_line_unavailable(self):
        ram = base_ram()
        del ram[0x1BCC]
        status = compute_display_status(ram, input_errors={0x1BCC: "Timeout"})
        self.assertFalse(status["available"])
        self.assertFalse(status["line1_available"])
        self.assertIsNone(status["line1"])
        self.assertTrue(status["line2_available"])
        self.assertEqual(status["line2"], "Dachs HR 5.3")
        self.assertIn("Zeile 1", status["error"])
        self.assertIn("0x1bcc", status["error"])
        self.assertIn("Timeout", status["error"])
        self.assertEqual(status["missing_inputs"], ["0x1bcc"])
        ram = base_ram()
        del ram[0x0F33]
        status = compute_display_status(ram)
        self.assertTrue(status["line1_available"])
        self.assertFalse(status["line2_available"])
        self.assertIsNone(status["line2"])
        # Out-of-range values never count as valid bytes.
        status = compute_display_status(base_ram(x0f33=256))
        self.assertFalse(status["line2_available"])
        status = compute_display_status(base_ram(x0f33=True))
        self.assertFalse(status["line2_available"])

    def test_payload_offsets(self):
        ram = base_ram(x0e91=0x17, x0e9e=0x40, x0ea0=0xFE, x0ea4=0x24, x0efa=1, x0eff=0x0C, x0f0a=0x0A,
                       x0f33=0xF8, x1ada=2, x1adc=1, x1add=1, x1ae2=0x20, x1af4=0x12, x1b12=0x04)
        payloads = payloads_from_ram(ram)
        self.assertEqual(payloads[22][23], 0x17)
        self.assertEqual(payloads[22][36], 0x40)
        self.assertEqual(payloads[22][38], 0xFE)
        self.assertEqual(payloads[24][54], 0x0A)
        self.assertEqual(payloads[26][25], 0xF8)
        self.assertEqual(payloads[50][40], 0x12)
        values = inputs_from_payloads(payloads)
        expected = {a for addresses in DISPLAY_STATUS_BLOCK_INPUTS.values() for a in addresses}
        self.assertEqual(set(values), expected)
        for address in expected:
            self.assertEqual(values[address], ram[address])
        self.assertEqual(inputs_from_payloads({22: b"\x00" * 20}), {})


class DisplayStatusAppTests(unittest.TestCase):
    AT = "2026-10-24T18:00:00+00:00"

    def test_ram_allowlist_framing(self):
        by_address = {int(field["address"]): field for field in DISPLAY_STATUS_RAM_FIELDS}
        self.assertEqual(set(by_address), set(DISPLAY_STATUS_RAM_ADDRESSES))
        for address, field in by_address.items():
            self.assertEqual(field["length"], 1)
            self.assertEqual(
                ram_telemetry_request(field), bytes([0x02, address >> 8, address & 0xFF, 0x01])
            )
        self.assertEqual(by_address[0x0DB3]["key"], "CPU0.RAM.0x000db3")

    def test_refresh_reads_plan_reuses_soot_byte_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            ram = base_ram(x0ed4=0x22, x0db3=1)
            payloads = payloads_from_ram(ram)
            session = FakeRamSession({0x0DB3: 1})

            # Slow cycle, 0x0db3 already read by the soot reader: two frames.
            app._update_display_slow_payloads(payloads, set(), 100.0)
            app._store_display_ram_sample(0x0DB3, {"available": True, "raw": 1}, 100.0)
            status = app._refresh_display_status(session, payloads, 100.0, self.AT, True, {0x0DB3})
            self.assertEqual([call[0] for call in session.calls], [b"\x02\x0f\x96\x01", b"\x02\x1b\xcc\x01"])
            self.assertTrue(all(call[1] == 0.9 and call[2] == 0 for call in session.calls))
            self.assertEqual((status["line1"], status["line2"]), ("Der Dachs läuft ...", "Rußfilterregeneration"))
            self.assertEqual(status["inputs"]["0x0db3"], 1)

            # Fast cycle: no RAM frame at all.
            session.calls.clear()
            status = app._refresh_display_status(session, payloads, 101.0, self.AT, False)
            self.assertEqual(session.calls, [])
            self.assertTrue(status["available"])

            # Gas type without soot reader: the slow cycle reads 0x0db3 itself.
            session.calls.clear()
            status = app._refresh_display_status(session, payloads, 110.0, self.AT, True)
            self.assertEqual(
                [call[0] for call in session.calls],
                [b"\x02\x0d\xb3\x01", b"\x02\x0f\x96\x01", b"\x02\x1b\xcc\x01"],
            )

            # A service code appearing between slow cycles reads 0x1c9f/0x1b5c
            # once immediately, then not again before the slow interval.
            fault = payloads_from_ram(base_ram(x0e91=0x16))
            session.values.update({0x1C9F: 1, 0x1B5C: 1})
            session.calls.clear()
            status = app._refresh_display_status(session, fault, 111.0, self.AT, False)
            self.assertEqual([call[0] for call in session.calls], [b"\x02\x1c\x9f\x01", b"\x02\x1b\x5c\x01"])
            self.assertEqual((status["line1"].strip(), status["line2"]), ("Wartezustand", "122 Kuehlw.Motor>100°"))
            self.assertEqual(status["service_code"], 122)
            session.calls.clear()
            app._refresh_display_status(session, fault, 112.0, self.AT, False)
            self.assertEqual(session.calls, [])

            # Failed read: unavailable, no retry storm, never the old value.
            session.fail.add(0x1BCC)
            session.calls.clear()
            status = app._refresh_display_status(session, payloads, 120.0, self.AT, True)
            self.assertFalse(status["line1_available"])
            self.assertIsNone(status["line1"])
            self.assertIn("Timeout", status["error"])
            self.assertTrue(status["line2_available"])
            session.calls.clear()
            status = app._refresh_display_status(session, payloads, 121.0, self.AT, False)
            self.assertEqual(session.calls, [])
            self.assertFalse(status["line1_available"])

            # Missing fast block in this cycle: line unavailable.
            session.fail.clear()
            partial = {block: data for block, data in payloads.items() if block != 26}
            app._update_display_slow_payloads(payloads, set(), 130.0)
            status = app._refresh_display_status(session, partial, 130.0, self.AT, True)
            self.assertFalse(status["line2_available"])
            self.assertIn("Block 26", status["error"])
            self.assertTrue(status["line1_available"])

            # Stale slow block payload (block 50 not read for too long).
            status = app._refresh_display_status(session, payloads, 200.0, self.AT, True)
            self.assertFalse(status["line2_available"])
            self.assertIn("Block 50", status["error"])
            # A failed slow read drops the cache immediately.
            app._update_display_slow_payloads(payloads, set(), 200.0)
            app._update_display_slow_payloads({}, {104}, 201.0)
            status = app._refresh_display_status(session, payloads, 201.0, self.AT, True)
            self.assertFalse(status["line2_available"])
            self.assertIn("Block 104", status["error"])

            # After a long gap even a fast cycle refreshes the RAM bytes once
            # instead of using values older than one slow interval.
            app._update_display_slow_payloads(payloads, set(), 300.0)
            session.calls.clear()
            status = app._refresh_display_status(session, payloads, 300.0, self.AT, False)
            self.assertEqual(len(session.calls), 3)
            self.assertTrue(status["available"])
            # An old RAM sample is replaced by a new read, never reused.
            app._display_ram[0x1BCC]["attempt_mono"] = 250.0
            app._display_ram[0x1BCC]["value"] = 1
            session.calls.clear()
            status = app._refresh_display_status(session, payloads, 300.0, self.AT, False)
            self.assertEqual([call[0] for call in session.calls], [b"\x02\x1b\xcc\x01"])
            self.assertEqual(status["line1"], "Der Dachs läuft ...")
            # If the re-read fails, the old value is dropped as well.
            app._display_ram[0x1BCC]["attempt_mono"] = 250.0
            session.fail.add(0x1BCC)
            status = app._refresh_display_status(session, payloads, 300.0, self.AT, False)
            self.assertFalse(status["line1_available"])
            self.assertIsNone(status["line1"])

    def test_live_api_events_and_staleness(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            initial = app.live()["display_status"]
            self.assertFalse(initial["available"])
            self.assertIsNone(initial["line1"])
            self.assertIsNone(initial["line2"])
            for key in ("line1", "line2", "line1_index", "line2_index", "service_code", "inputs",
                        "available", "recorded_at", "error"):
                self.assertIn(key, initial)

            running = compute_display_status(base_ram(x0ed4=0x22), recorded_at="2026-10-24T18:00:00+00:00")
            regen = compute_display_status(base_ram(x0ed4=0x22, x0db3=1), recorded_at="2026-10-24T18:00:10+00:00")
            gap = compute_display_status({}, recorded_at="2026-10-24T18:00:20+00:00")
            fault = compute_display_status(base_ram(x0e91=0x1F), recorded_at="2026-10-24T18:00:30+00:00")
            now = time.monotonic()
            for status in (running, running, regen, gap, regen, fault):
                app._publish_display_status(status, now)
            live = app.live()["display_status"]
            self.assertEqual(live["line2"], "131 Drehzahl<100U/min")
            self.assertEqual(live["service_code"], 131)
            json.dumps(live)
            events = app.store.display_status_events()
            self.assertEqual([item["line2"] for item in events],
                             ["131 Drehzahl<100U/min", "Rußfilterregeneration", "Dachs HR 5.3"])
            self.assertEqual(events[0]["service_code"], 131)
            self.assertEqual(events[-1]["recorded_at"], "2026-10-24T18:00:00+00:00")
            self.assertEqual(len(app.store.display_status_events(limit=1)), 1)

            # After a restart the last stored text is not recorded again.
            restarted = DachsWebApp(data_dir=directory, interval=60)
            restarted._publish_display_status(fault, time.monotonic())
            self.assertEqual(len(restarted.store.display_status_events()), 3)

            # No new computation for too long: unavailable, never the old text.
            with app.state_lock:
                app._display_status_mono = time.monotonic() - 3600
            stale = app.live()["display_status"]
            self.assertFalse(stale["available"])
            self.assertIsNone(stale["line2"])
            self.assertIn("veraltet", stale["error"])

    def test_events_route_with_base_path(self):
        with tempfile.TemporaryDirectory() as directory:
            init_users(directory, admin_password="AdminPasswort123", guest_password="GastPasswort123")
            app = DachsWebApp(data_dir=directory, interval=60)
            app._publish_display_status(
                compute_display_status(base_ram(), recorded_at="2026-10-24T18:00:00+00:00"), time.monotonic()
            )
            guest_token = app.login("gast", "GastPasswort123")[0]
            server = DachsHTTPServer(("127.0.0.1", 0), app, base_path="/dachs")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def request(path, token=None):
                connection = http.client.HTTPConnection(*server.server_address, timeout=2)
                headers = {"Cookie": f"open_dachs_session={token}"} if token else {}
                connection.request("GET", path, headers=headers)
                response = connection.getresponse()
                body = response.read().decode("utf-8")
                connection.close()
                return response.status, body

            try:
                status, _body = request("/dachs/api/display-status/events")
                self.assertEqual(status, 401)
                status, body = request("/dachs/api/display-status/events?limit=50", guest_token)
                self.assertEqual(status, 200)
                data = json.loads(body)
                self.assertEqual(data["schema"], "open-dachs-display-status-events/v1")
                self.assertEqual(data["items"][0]["line2"], "Dachs HR 5.3")
                status, body = request("/dachs/api/live", guest_token)
                self.assertEqual(json.loads(body)["display_status"]["line2"], "Dachs HR 5.3")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_dashboard_tile_is_optional_and_default_off(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            defaults = app.dashboard_settings()["cards"]
            tile = {"block": 22, "key": DASHBOARD_DISPLAY_STATUS_KEY}
            self.assertNotIn(tile, defaults)
            self.assertIn(DASHBOARD_DISPLAY_STATUS_KEY, app._dashboard_available_keys(22))
            derived = {item["source"]: item for item in app.dashboard_settings()["derived_fields"]}
            self.assertEqual(derived["display_status"]["label"], "Display-Status")
            saved = app.set_dashboard_settings([tile])
            self.assertEqual(saved["cards"], [tile])
            # Existing selections stay untouched on restart.
            path = Path(directory) / "dashboard_settings.json"
            before = path.read_bytes()
            restarted = DachsWebApp(data_dir=directory, interval=60)
            self.assertEqual(restarted.dashboard_settings()["cards"], [tile])
            self.assertEqual(path.read_bytes(), before)


class DisplayStatusMonitorCycleTests(unittest.TestCase):
    def test_one_monitor_cycle_publishes_status_and_reads_0db3_once(self):
        from contextlib import contextmanager
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            ram = base_ram(x0ed4=0x22, x0f0a=0x08, x0db3=1)
            payloads = payloads_from_ram(ram)
            session = FakeRamSession({0x0DB3: 1})
            blocks_read = []

            class Service:
                @contextmanager
                def session(self):
                    yield session

                def decoded_block(self, _session, block):
                    blocks_read.append(block)
                    payload = payloads.get(block, bytes(70))
                    result = SimpleNamespace(
                        ok=True, payload=payload, status=0, response=SimpleNamespace(elapsed_ms=1.0)
                    )
                    return result, app.pack.display_fields(block, payload)

            app.service = Service()
            app.serial_enabled = True
            app.monitor_enabled = True
            # The soot reader gates on the type code from the previous cycle.
            app.live_values[(24, "Hka_Mw1.bKraftstofftyp")] = {"block": 24, "key": "Hka_Mw1.bKraftstofftyp", "raw": 0x08}
            original = app._publish_display_status

            def publish_and_stop(status, now_mono):
                original(status, now_mono)
                app.stop_event.set()

            app._publish_display_status = publish_and_stop
            app._monitor_loop()
            self.assertEqual(blocks_read[:7], [22, 24, 26, 20, 38, 50, 104])
            requests = [call[0] for call in session.calls]
            self.assertEqual(requests.count(b"\x02\x0d\xb3\x01"), 1)
            self.assertIn(b"\x02\x0f\x96\x01", requests)
            self.assertIn(b"\x02\x1b\xcc\x01", requests)
            for request in requests:
                self.assertEqual(request[0], 0x02)
            live = app.live()["display_status"]
            self.assertEqual((live["line1"], live["line2"]), ("Der Dachs läuft ...", "Rußfilterregeneration"))
            self.assertTrue(live["available"])
            self.assertEqual(app.live()["soot_filter_regeneration"]["state"], 1)
            self.assertEqual(len(app.store.display_status_events()), 1)


class DisplayStatusMigrationTests(unittest.TestCase):
    def test_existing_database_gains_event_table_without_data_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dachs-web.db"
            db = sqlite3.connect(path)
            db.executescript(
                """
                CREATE TABLE motor_status_points (
                    recorded_at TEXT PRIMARY KEY,
                    status INTEGER CHECK(status IS NULL OR (status>=0 AND status<=255))
                );
                INSERT INTO motor_status_points VALUES('2026-10-24T10:00:00+00:00', 34);
                """
            )
            db.commit()
            db.close()
            store = DachsStore(path)
            with store.database() as check:
                tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn("display_status_events", tables)
                self.assertEqual(
                    check.execute("SELECT status FROM motor_status_points").fetchone()[0], 34
                )
                self.assertEqual(check.execute("PRAGMA quick_check").fetchone()[0], "ok")
            # Idempotent reopen.
            store = DachsStore(path)
            status = compute_display_status(base_ram(), recorded_at="2020-01-01T00:00:00+00:00")
            self.assertTrue(store.record_display_status_change("2020-01-01T00:00:00+00:00", status))
            self.assertFalse(store.record_display_status_change("2020-01-01T00:00:01+00:00", status))
            changed = compute_display_status(base_ram(x0db3=1))
            self.assertTrue(store.record_display_status_change("2020-01-02T00:00:00+00:00", changed))
            self.assertFalse(store.record_display_status_change("2020-01-03T00:00:00+00:00", compute_display_status({})))
            # Retention keeps the newest change even when it is old.
            store.purge(30)
            events = store.display_status_events()
            self.assertEqual([item["line2"] for item in events], ["Rußfilterregeneration"])


if __name__ == "__main__":
    unittest.main()
