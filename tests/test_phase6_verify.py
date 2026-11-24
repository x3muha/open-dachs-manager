import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from open_dachs_manager import phase6_verify as p6
from open_dachs_manager.phase6_verify import (
    PHASE6_BLOCK_CELLS,
    PHASE6_VERIFY_FIELDS,
    Phase6GuardError,
    Phase6ReadError,
    Phase6Verifier,
    VerifyField,
    check_verify_payload,
    compare_base,
    guarded_request,
    night_window_delta,
    validate_verify_response,
    verify_read_request,
)
from open_dachs_manager.transport import Frame, Response


T0 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


class FakeSession:
    """Serves service-0x02 reads from a per-CPU RAM image; never writes."""

    def __init__(self, ram):
        self.ram = ram
        self.calls = []
        self.packet = 0
        self.bad_echo = False
        self.negative_ack = False

    def next_packet(self):
        self.packet += 1
        return self.packet

    def request(self, payload, packet, timeout, cpu):
        payload = bytes(payload)
        self.calls.append((cpu, payload))
        if payload[0] != 0x02:
            raise AssertionError("non-read service reached the session")
        addr = payload[1] << 8 | payload[2]
        length = payload[3]
        echo = bytes([0x82, payload[1] ^ (0xFF if self.bad_echo else 0), payload[2], length])
        body = echo + bytes(self.ram[cpu][addr:addr + length])
        return Response(
            tx=b"",
            ack=Frame("ack", packet, b"", positive=not self.negative_ack),
            data=Frame("data", packet, b"", body),
            elapsed_ms=60.0,
        )

    def write_block(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("write_block must not be used")


class FakeService:
    def __init__(self, ram):
        self.ram = ram
        self.block_reads = []

    def read_block(self, session, block, cpu=0):
        self.block_reads.append((cpu, block))
        payload = bytearray(59)
        payload[11:13] = self.ram[cpu][0x0772:0x0774]
        return SimpleNamespace(ok=True, payload=bytes(payload))


def make_ram(status=16):
    ram = {cpu: bytearray(0x10000) for cpu in (0, 1, 2)}
    ram[0][0x0ED4] = status
    ram[0][0x0EA0:0x0EA2] = (0xFFFF).to_bytes(2, "little")
    ram[0][0x0E7E:0x0E82] = (5).to_bytes(4, "little")
    for cpu in (1, 2):
        ram[cpu][0x0772:0x0774] = bytes([0x52, 0x67])
    return ram


def b20(regler=(0, 0, 0, 4, 43), ueberw=(0, 0, 3, 50)):
    payload = bytearray(70)
    payload[26:30] = bytes(ueberw)
    payload[30:34] = bytes([0, 0, 2, 5])
    payload[34:39] = bytes(regler)
    return bytes(payload)


def payloads_from(ram, block20=None):
    payloads = {
        block: bytes(ram[0][base:base + 70]) for block, base in p6.BLOCK_BASES.items()
    }
    payloads[20] = block20 if block20 is not None else b20()
    return payloads


class Harness:
    def __init__(self, directory, *, config=True, end=None, status=16, clock_start=T0):
        self.dir = Path(directory)
        self.now = clock_start
        self.mono = 1000.0
        self.ram = make_ram(status)
        self.session = FakeSession(self.ram)
        self.service = FakeService(self.ram)
        if config:
            self.write_config(end=end)
        self.verifier = self.new_verifier()

    def new_verifier(self):
        return Phase6Verifier(self.dir, version="test", write_audit_count=lambda: 436,
                              clock=lambda: self.now)

    def write_config(self, end=None, enabled=True, run_id="run-1"):
        end = end or (self.now + timedelta(days=7))
        (self.dir / "phase6_verify.json").write_text(json.dumps({
            "enabled": enabled, "end_at": end.isoformat(), "run_id": run_id,
            "approval": "Bober 2026-10-25 Variante A, P0–P7, CPU 1/2, 7 Tage",
        }))

    def step(self, seconds=0.75, slow=None, block20=None, **cycle):
        self.mono += seconds
        self.now += timedelta(seconds=seconds)
        slow = slow if slow is not None else int(self.mono / 10) != int((self.mono - seconds) / 10)
        payloads = payloads_from(self.ram, block20)
        if not slow:
            payloads.pop(20)
            payloads.pop(50)
        info = {"slow": slow, "display_available": True,
                "ram_telemetry_available": True if slow else None, **cycle}
        before = len(self.session.calls)
        self.verifier.cycle(self.session, self.service, payloads, self.mono, info)
        return self.session.calls[before:]

    def run_until(self, state, limit=200):
        for _ in range(limit):
            self.step()
            if self.verifier.state == state:
                return
        raise AssertionError(f"state {self.verifier.state} ({self.verifier.reason}) != {state}")

    def log_lines(self):
        lines = []
        for path in sorted((self.dir / "phase6-verify").glob("*.jsonl")):
            lines.extend(json.loads(line) for line in path.read_text().splitlines())
        return lines


class AllowlistAndFramingTests(unittest.TestCase):
    def test_allowlist_matches_plan_counts(self):
        self.assertEqual(len(PHASE6_VERIFY_FIELDS), 47)
        by_cpu = {cpu: sum(1 for f in PHASE6_VERIFY_FIELDS if f.cpu == cpu) for cpu in (0, 1, 2)}
        self.assertEqual(by_cpu, {0: 25, 1: 10, 2: 12})
        keys = [(f.cpu, f.addr, f.length) for f in PHASE6_VERIFY_FIELDS]
        self.assertEqual(len(keys), len(set(keys)))
        pakete = {f.id.split(".")[0] for f in PHASE6_VERIFY_FIELDS}
        self.assertEqual(pakete, {"P0", "P1", "P2", "P4", "P5", "P6", "P7"})

    def test_plan_positions_spot_check(self):
        rows = {(f.id, f.cpu, f.addr): f for f in PHASE6_VERIFY_FIELDS}
        self.assertEqual(rows[("P2.1", 0, 0x222A)].length, 19)
        self.assertEqual(rows[("P1.4", 0, 0x0D78)].length, 10)
        self.assertEqual(rows[("P6.4", 1, 0x0E7A)].length, 52)
        self.assertEqual(rows[("P7.4", 2, 0x0E70)].length, 56)
        self.assertEqual(rows[("P0.5", 2, 0x0772)].length, 2)
        self.assertTrue(rows[("P2.1", 0, 0x222A)].fast)
        self.assertTrue(rows[("P1.4", 0, 0x0D78)].fast)
        self.assertEqual({f.id for f in PHASE6_VERIFY_FIELDS if f.fast}, {"P2.1", "P1.4"})

    def test_every_entry_frames_exact_read(self):
        for field in PHASE6_VERIFY_FIELDS:
            payload = verify_read_request(field.cpu, field.addr, field.length)
            self.assertEqual(payload, bytes([0x02, field.addr >> 8, field.addr & 0xFF, field.length]))
            self.assertGreaterEqual(field.addr, 0x0400)
            self.assertLessEqual(field.addr + field.length - 1, 0xFFFF)
            self.assertTrue(1 <= field.length <= 64)

    def test_block_cells_are_not_read_twice(self):
        polled = [(0x0E7A, 0x0E7A + 74), (0x0ED4, 0x0ED4 + 70), (0x0F1A, 0x0F1A + 70), (0x1ACC, 0x1ACC + 70)]
        for field in PHASE6_VERIFY_FIELDS:
            if field.cpu != 0 or field.id.startswith("P0"):
                continue
            for start, end in polled:
                self.assertFalse(start <= field.addr < end, field)
        for _cell_id, addr, width, block in PHASE6_BLOCK_CELLS:
            offset = addr - p6.BLOCK_BASES[block]
            self.assertTrue(0 <= offset and offset + width <= 70)


class GuardTests(unittest.TestCase):
    def test_rejections_before_sending(self):
        cases = [
            lambda: check_verify_payload(0, b"\x03\x22\x2a\x13"),
            lambda: check_verify_payload(0, b"\x05\x22\x2a\x13"),
            lambda: check_verify_payload(0, b"\x7e\x22\x2a\x13"),
            lambda: check_verify_payload(0, b"\x02\x22\x2a\x13\x00"),
            lambda: verify_read_request(0, 0x03FF, 1),
            lambda: verify_read_request(0, 0x0100, 4),
            lambda: verify_read_request(0, 0x222A, 0),
            lambda: verify_read_request(0, 0x222A, 65),
            lambda: verify_read_request(0, 0xFFF0, 64),
            lambda: verify_read_request(3, 0x222A, 19),
            lambda: verify_read_request(-1, 0x222A, 19),
            lambda: verify_read_request(True, 0x222A, 19),
            lambda: verify_read_request(1, 0x222A, 19),
            lambda: verify_read_request(0, 0x0F0E, 4),
            lambda: verify_read_request(0, 0x222A, 18),
            lambda: verify_read_request(0, "0x222a", 19),
        ]
        for case in cases:
            with self.assertRaises(Phase6GuardError):
                case()

    def test_guarded_request_never_reaches_session_on_violation(self):
        session = FakeSession(make_ram())
        with self.assertRaises(Phase6GuardError):
            guarded_request(session, 0, b"\x03\x0f\x0e\x04")
        with self.assertRaises(Phase6GuardError):
            guarded_request(session, 4, b"\x02\x22\x2a\x13")
        self.assertEqual(session.calls, [])
        guarded_request(session, 0, b"\x02\x22\x2a\x13")
        self.assertEqual(session.calls, [(0, b"\x02\x22\x2a\x13")])

    def test_response_validation_fails_closed(self):
        payload = verify_read_request(0, 0x222A, 19)
        good = SimpleNamespace(ack=SimpleNamespace(positive=True),
                               data=SimpleNamespace(payload=b"\x82\x22\x2a\x13" + bytes(19)))
        self.assertEqual(validate_verify_response(0, payload, good), bytes(19))
        bad = [
            SimpleNamespace(ack=SimpleNamespace(positive=False), data=good.data),
            SimpleNamespace(ack=None, data=good.data),
            SimpleNamespace(ack=good.ack, data=None),
            SimpleNamespace(ack=good.ack, data=SimpleNamespace(payload=b"\x82\x22\x2b\x13" + bytes(19))),
            SimpleNamespace(ack=good.ack, data=SimpleNamespace(payload=b"\x83\x22\x2a\x13" + bytes(19))),
            SimpleNamespace(ack=good.ack, data=SimpleNamespace(payload=b"\x82\x22\x2a\x13" + bytes(18))),
            SimpleNamespace(ack=good.ack, data=SimpleNamespace(payload=b"\x82\x22\x2a\x13" + bytes(20))),
        ]
        for response in bad:
            with self.assertRaises(Phase6ReadError):
                validate_verify_response(0, payload, response)

    def test_compare_base_and_night_formula(self):
        block = bytes(range(64))
        self.assertTrue(compare_base(block, block, block)[0])
        self.assertFalse(compare_base(block, bytes(64), bytes(64))[0])
        volatile = bytearray(block)
        volatile[4] = 200
        second = bytearray(block)
        second[4] = 201
        self.assertTrue(compare_base(block, bytes(volatile), bytes(second))[0])
        # Live case 2026-10-27 12:20:51Z (engine at 2440 rpm): block 26 with ten
        # volatile measurement bytes, zero unexplained -> must pass (1.8.9).
        live_first = bytes.fromhex(
            "2b5c465b665cb602c202ae0200000000000052c3ffffff7ffbf8b2feffffff7ffb3893fe"
            "03001b83270633061f06000000000101ffffff14ffffff1400000000")
        live_second = bytes.fromhex(
            "285c5a5b635cbe02c702b90200000000000049c3ffffff7ffbf8b2feffffff7ffb3893fe"
            "03001b83350619062606000000000101ffffff14ffffff1400000000")
        # The cycle block payload was read earlier: every moving byte differs
        # from both RAM reads (54 of 64 equal, as logged live).
        live_block = bytes((b + 50) % 256 if x != y else b
                           for b, x, y in zip(live_first, live_first, live_second))
        ok, detail = compare_base(live_block, live_first, live_second)
        self.assertTrue(ok)
        self.assertEqual(detail["unexplained"], [])
        self.assertEqual(detail["equal"], 54)
        self.assertEqual(detail["volatile"], 10)
        # Many volatile bytes alone never fail; five stable but wrong bytes do.
        noisy_first = bytes(range(64))
        noisy_second = bytes((b + 7) % 256 for b in noisy_first)
        self.assertTrue(compare_base(block, noisy_first, noisy_second)[0])
        wrong = bytearray(block)
        for index in (10, 20, 30, 40, 50):
            wrong[index] = (wrong[index] + 9) % 256
        ok, detail = compare_base(block, bytes(wrong), bytes(wrong))
        self.assertFalse(ok)
        self.assertEqual(detail["unexplained"], [10, 20, 30, 40, 50])
        self.assertEqual(night_window_delta(10800 + 300, 5), 0)
        self.assertEqual(night_window_delta(86400 * 9000 + 10800 + 300 - 120, 105), -120)
        self.assertEqual(night_window_delta(10800, 0), 0)


class LifecycleTests(unittest.TestCase):
    def test_default_off_without_config(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, config=False)
            for _ in range(30):
                self.assertEqual(h.step(), [])
            status = h.verifier.status()
            self.assertEqual((status["enabled"], status["state"]), (False, "aus"))
            self.assertFalse((Path(directory) / "phase6-verify").exists())

    def test_disabled_and_invalid_configs_do_not_read(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, config=False)
            (Path(directory) / "phase6_verify.json").write_text('{"enabled": true}')
            self.assertEqual(h.step(), [])
            self.assertEqual(h.verifier.state, "konfigfehler")
            (Path(directory) / "phase6_verify.json").write_text(
                '{"enabled": true, "end_at": "2026-11-01T00:00:00", "run_id": "x"}')
            h.verifier._config_checked = float("-inf")
            self.assertEqual(h.step(), [])
            self.assertEqual(h.verifier.state, "konfigfehler")
            (Path(directory) / "phase6_verify.json").write_text('{"enabled": false, "end_at": "x"}')
            h.verifier._config_checked = float("-inf")
            self.assertEqual(h.step(), [])
            self.assertEqual(h.verifier.state, "aus")

    def test_full_start_sequence_runs_p0_then_reads_only_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            h.run_until("läuft")
            for _ in range(200):
                h.step()
            allowed = {(f.cpu, verify_read_request(f.cpu, f.addr, f.length)) for f in PHASE6_VERIFY_FIELDS}
            self.assertTrue(h.session.calls)
            for call in h.session.calls:
                self.assertIn(call, allowed)
            self.assertEqual(sorted(h.service.block_reads), [(1, 20), (2, 20)])
            lines = h.log_lines()
            self.assertEqual(lines[0]["type"], "manifest")
            self.assertEqual(lines[0]["allowlist_requests"], 47)
            self.assertEqual(lines[0]["write_audit_max_id"], 436)
            p0 = [line for line in lines if line["type"] == "p0"]
            self.assertEqual([line["paket"] for line in p0][:5], ["P0.1", "P0.2", "P0.3", "P0.5", "P0.5"])
            self.assertTrue(all(line["ok"] for line in p0 if "ok" in line))
            reads = [line for line in lines if line["type"] == "read"]
            sample = reads[-1]
            for key in ("schema", "seq", "ts_utc", "ts_mono", "paket", "cpu", "addr", "len", "raw_hex",
                        "ack", "rtt_ms", "crc_errors", "protocol_errors", "coherent", "ctx", "error"):
                self.assertIn(key, sample)
            self.assertEqual(sample["schema"], "dachs-phase6-verify/v1")
            self.assertEqual([r["seq"] for r in reads], list(range(1, len(reads) + 1)))
            self.assertTrue(any(line["type"] == "kontext" for line in lines))
            state = json.loads((Path(directory) / "phase6-verify" / "state.json").read_text())
            self.assertEqual(state["state"], "läuft")

    def test_firmware_mismatch_blocks_without_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            for _ in range(40):
                h.step(block20=b20(regler=(0, 0, 0, 3, 10)))
            self.assertEqual(h.session.calls, [])
            self.assertEqual(h.verifier.state, "blockiert")
            self.assertIn("R 000.003.010", h.verifier.reason)
            h2 = Harness(tempfile.mkdtemp(dir=directory))
            for _ in range(40):
                h2.step(block20=b20(ueberw=(0, 0, 2, 4)))
            self.assertEqual(h2.session.calls, [])
            self.assertIn("U 00.002.004", h2.verifier.reason)

    def test_service_code_blocks_without_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            h.ram[0][0x0E91] = 0x16
            for _ in range(40):
                h.step()
            self.assertEqual(h.session.calls, [])
            self.assertEqual(h.verifier.state, "blockiert")

    def test_manual_mode_blocks_start_and_only_reads_1b5a(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            h.ram[0][0x1B5A] = 1
            for _ in range(200):
                h.step()
            self.assertEqual(h.verifier.state, "blockiert")
            self.assertTrue(h.session.calls)
            self.assertEqual({call for call in h.session.calls}, {(0, b"\x02\x1b\x5a\x01")})
            self.assertLessEqual(len(h.session.calls), 3)

    def test_p0_waits_for_s0_or_s2(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, status=22)
            for _ in range(40):
                h.step()
            self.assertEqual(h.verifier.state, "vorbedingungen")
            self.assertEqual({call[1][1:3] for call in h.session.calls}, {b"\x1b\x5a"})

    def test_p0_mismatch_aborts(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            original = payloads_from

            def shifted(ram, block20=None):
                payloads = original(ram, block20)
                shifted22 = bytearray(range(1, 71))
                shifted22[22:26] = bytes(4)  # no warning/service code
                payloads[22] = bytes(shifted22)
                return payloads

            with mock.patch(f"{__name__}.payloads_from", shifted):
                for _ in range(60):
                    h.step()
                    if h.verifier.state == "abgebrochen":
                        break
            self.assertEqual(h.verifier.state, "abgebrochen")
            self.assertIn("P0.1", h.verifier.reason)
            calls = len(h.session.calls)
            for _ in range(20):
                h.step()
            self.assertEqual(len(h.session.calls), calls)

    def test_p0_5_mismatch_aborts(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)

            def read_block(session, block, cpu=0):
                return SimpleNamespace(ok=True, payload=bytes(59))

            h.service.read_block = read_block
            for _ in range(60):
                h.step()
            self.assertEqual(h.verifier.state, "abgebrochen")
            self.assertIn("P0.5", h.verifier.reason)


class SchedulingTests(unittest.TestCase):
    def test_rate_limits_and_cycle_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            h.run_until("läuft")
            start = len(h.session.calls)
            minutes = 40
            per_cycle = []
            for _ in range(int(minutes * 60 / 0.75)):
                per_cycle.append(h.step())
            calls = h.session.calls[start:]
            for cpu in (1, 2):
                frames = sum(1 for c in calls if c[0] == cpu)
                self.assertLessEqual(frames, 2 * minutes + 2, cpu)
                self.assertGreater(frames, minutes)
            for cycle in per_cycle:
                self.assertLessEqual(len(cycle), p6.MAX_FRAMES_PER_CYCLE + 1)
                net = {c[0] for c in cycle if c[0]}
                self.assertLessEqual(len(net), 1)
                for cpu in net:
                    self.assertLessEqual(sum(1 for c in cycle if c[0] == cpu), 2)
            cpu0 = sum(1 for c in calls if c[0] == 0)
            self.assertLess(cpu0 / (minutes * 60), 0.8)
            self.assertIn((1, verify_read_request(1, 0x0E1A, 26)), calls)
            self.assertIn((2, verify_read_request(2, 0x0E40, 48)), calls)

    def test_event_window_reads_fast_cells_every_cycle_and_is_limited(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            h.run_until("läuft")
            for _ in range(30):
                h.step()
            h.ram[0][0x0ED4] = 20
            h.step()
            self.assertEqual(h.verifier.counters["f_windows"], 1)
            p21 = verify_read_request(0, 0x222A, 19)
            p14 = verify_read_request(0, 0x0D78, 10)
            for _ in range(int(170 / 0.75)):
                cycle = h.step()
                self.assertIn((0, p21), cycle)
                self.assertIn((0, p14), cycle)
                self.assertLessEqual(len(cycle), p6.MAX_FRAMES_PER_CYCLE)
            for _ in range(int(20 / 0.75)):
                h.step()
            self.assertFalse(h.verifier.in_f_window(h.mono))
            # Many state changes within one hour: at most six windows.
            for index in range(20):
                h.ram[0][0x0ED4] = 35 if index % 2 else 16
                for _ in range(int(200 / 0.75)):
                    h.step()
            self.assertLessEqual(h.verifier.counters["f_windows"], 6 + 1 + 6)
            opens = [line for line in h.log_lines() if line["type"] == "fenster" and line["fenster"] != "N"]
            self.assertTrue(any("unterdrückt" in line for line in opens))

    def test_release_drop_opens_window(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            h.run_until("läuft")
            h.step()
            h.ram[0][0x0EA0:0x0EA2] = (0xFFFB).to_bytes(2, "little")
            h.step()
            self.assertEqual(h.verifier.f_window_label, "F:freigabe")

    def test_night_window(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, status=35)
            h.ram[0][0x0F0E:0x0F12] = (21600).to_bytes(4, "little")
            reglerzeit = 86400 * 9000 + 10800 + 5 * 60 - 115
            h.ram[0][0x163E:0x1642] = reglerzeit.to_bytes(4, "little")
            h.run_until("läuft")
            for _ in range(200):
                h.step()
                if h.verifier.counters["night_windows"]:
                    break
            self.assertEqual(h.verifier.counters["night_windows"], 1)
            self.assertTrue(h.verifier.in_f_window(h.mono))
            for _ in range(40):
                h.step()
            self.assertEqual(h.verifier.counters["night_windows"], 1)

    def test_night_window_needs_six_hours_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, status=35)
            h.ram[0][0x0F0E:0x0F12] = (3600).to_bytes(4, "little")
            h.ram[0][0x163E:0x1642] = (86400 * 9000 + 10800 + 300 - 60).to_bytes(4, "little")
            h.run_until("läuft")
            for _ in range(200):
                h.step()
            self.assertEqual(h.verifier.counters["night_windows"], 0)

    def test_manual_mode_during_run_pauses_p2_only(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            h.run_until("läuft")
            h.ram[0][0x1B5A] = 1
            h.verifier.hand_checked = float("-inf")
            h.verifier.next_due[(0, 0x1B5A)] = 0
            for _ in range(20):
                h.step()
            self.assertIs(h.verifier.hand_mode, True)
            start = len(h.session.calls)
            for _ in range(int(120 / 0.75)):
                h.step()
            calls = h.session.calls[start:]
            p2 = {verify_read_request(0, f.addr, f.length) for f in PHASE6_VERIFY_FIELDS if f.paket == "P2"}
            self.assertFalse(any(c[1] in p2 for c in calls))
            self.assertTrue(any(c[1] == verify_read_request(0, 0x0D78, 10) for c in calls))
            self.assertEqual(h.verifier.state, "läuft")


class EndAndAbortTests(unittest.TestCase):
    def test_end_date_turns_logger_off(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, end=T0 + timedelta(minutes=5))
            h.run_until("läuft")
            h.now = T0 + timedelta(minutes=6)
            calls = len(h.session.calls)
            h.step()
            self.assertEqual(h.verifier.state, "beendet")
            self.assertEqual(len(h.session.calls), calls)
            self.assertEqual(h.log_lines()[-1]["type"], "ende")

    def test_end_date_in_past_never_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, end=T0 - timedelta(days=1))
            for _ in range(20):
                h.step()
            self.assertEqual(h.session.calls, [])
            self.assertEqual(h.verifier.state, "beendet")

    def test_fourteen_day_hard_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, end=T0 + timedelta(days=60))
            h.run_until("läuft")
            self.assertEqual(h.verifier.status()["end_at"],
                             (T0 + timedelta(days=14, seconds=0.75)).isoformat().replace("+00:00", "Z"))
            h.now = T0 + timedelta(days=13, hours=23)
            h.step()
            self.assertEqual(h.verifier.state, "läuft")
            h.now = T0 + timedelta(days=14, seconds=2)
            h.step()
            self.assertEqual(h.verifier.state, "beendet")
            # Editing the end date of the same run cannot extend it.
            h.write_config(end=T0 + timedelta(days=90))
            h.verifier._config_checked = float("-inf")
            calls = len(h.session.calls)
            h.step()
            self.assertEqual(h.verifier.state, "beendet")
            self.assertEqual(len(h.session.calls), calls)

    def test_disable_by_config_ends_run(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory)
            h.run_until("läuft")
            h.write_config(enabled=False)
            h.verifier._config_checked = float("-inf")
            calls = len(h.session.calls)
            h.step()
            self.assertEqual(h.verifier.state, "beendet")
            self.assertEqual(len(h.session.calls), calls)

    def _running(self, directory):
        h = Harness(directory)
        h.run_until("läuft")
        return h

    def _assert_stops(self, h, state, text):
        for _ in range(40):
            h.step()
            if h.verifier.state == state:
                break
        self.assertEqual(h.verifier.state, state, h.verifier.reason)
        self.assertIn(text, h.verifier.reason)
        calls = len(h.session.calls)
        for _ in range(20):
            h.step()
        self.assertEqual(len(h.session.calls), calls)
        persisted = json.loads((h.dir / "phase6-verify" / "state.json").read_text())
        self.assertEqual(persisted["state"], state)

    def test_wrong_echo_more_than_three_times_aborts(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self._running(directory)
            h.session.bad_echo = True
            self._assert_stops(h, "abgebrochen", "§2.5/1")
            failed = [line for line in h.log_lines() if line["type"] == "read" and line["error"]]
            self.assertGreaterEqual(len(failed), 4)
            self.assertTrue(all(line["raw_hex"] is None and line["ack"] is False for line in failed))

    def test_negative_ack_aborts(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self._running(directory)
            h.session.negative_ack = True
            self._assert_stops(h, "abgebrochen", "§2.5/1")

    def test_link_error_ratio_aborts(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self._running(directory)
            h.verifier.counters["frames"] = 200
            h.verifier.counters["crc_errors"] = 3
            self._assert_stops(h, "abgebrochen", "CRC")

    def test_version_change_aborts(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self._running(directory)
            h.step(slow=True, block20=b20(regler=(0, 0, 0, 4, 44)))
            self.assertEqual(h.verifier.state, "abgebrochen")
            self.assertIn("§2.5/2", h.verifier.reason)

    def test_service_code_or_new_warning_halts(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self._running(directory)
            h.ram[0][0x0E91] = 0x16
            self._assert_stops(h, "angehalten", "§2.5/3")
        with tempfile.TemporaryDirectory() as directory:
            h = self._running(directory)
            h.ram[0][0x0E92] = 1
            h.ram[0][0x0E90] = 7
            self._assert_stops(h, "angehalten", "neue Warnung")
        with tempfile.TemporaryDirectory() as directory:
            h = self._running(directory)
            h.ram[0][0x0E90] = 9
            self._assert_stops(h, "angehalten", "Code 9")

    def test_web_restart_aborts_run(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self._running(directory)
            state_path = h.dir / "phase6-verify" / "state.json"
            state = json.loads(state_path.read_text())
            state["pid"] = os.getpid() + 1
            state_path.write_text(json.dumps(state))
            h.verifier = h.new_verifier()
            h.step()
            self.assertEqual(h.verifier.state, "abgebrochen")
            self.assertIn("Neustart", h.verifier.reason)
            # A later restart keeps the terminal state.
            h.verifier = h.new_verifier()
            calls = len(h.session.calls)
            for _ in range(10):
                h.step()
            self.assertEqual(h.verifier.state, "abgebrochen")
            self.assertEqual(len(h.session.calls), calls)

    def test_new_run_id_starts_fresh_after_abort(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self._running(directory)
            h.verifier._abort("Test")
            h.write_config(run_id="run-2")
            h.verifier._config_checked = float("-inf")
            h.run_until("läuft")

    def test_serial_worker_restart_aborts(self):
        with tempfile.TemporaryDirectory() as directory:
            sock = Path(directory) / "serial.sock"
            sock.write_text("a")
            h = Harness(directory)
            h.verifier.serial_socket = str(sock)
            h.run_until("läuft")
            sock.unlink()
            sock.write_text("b")
            os.utime(sock, ns=(1, 1))
            sock.chmod(0o600)
            self._assert_stops(h, "abgebrochen", "Serialworker")

    def _pause(self, h, elapsed=1.6):
        for _ in range(p6.SLOW_FAST_CYCLE_LIMIT):
            h.step(slow=False, previous_elapsed=elapsed, previous_slow=False,
                   previous_serial_elapsed=0.2)

    def test_slow_fast_cycles_pause_reads_instead_of_abort(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self._running(directory)
            self._pause(h)
            self.assertEqual(h.verifier.state, "pausiert")
            self.assertIn("1,5 s", h.verifier.reason)
            self.assertEqual(h.verifier.counters["slow_pauses"], 1)
            pause = [line for line in h.log_lines() if line["type"] == "pause"]
            self.assertEqual(len(pause), 1)
            self.assertEqual(pause[0]["zyklen"], [{"elapsed_s": 1.6, "serial_s": 0.2}] * 3)
            # While paused not a single Phase-6 frame is sent, slow polls included.
            calls = len(h.session.calls)
            for _ in range(10):
                h.step(slow=False, previous_elapsed=2.0)
            h.step(slow=True, previous_elapsed=2.0)
            self.assertEqual(len(h.session.calls), calls)
            self.assertEqual(h.verifier.status()["slow_pause"]["healthy_cycles"], 0)

    def test_pause_resumes_after_healthy_fast_cycles(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self._running(directory)
            self._pause(h)
            for index in range(p6.SLOW_PAUSE_RESUME_CYCLES - 1):
                h.step(slow=index % 12 == 5, previous_elapsed=0.7)
            self.assertEqual(h.verifier.state, "pausiert")
            # One more slow fast cycle restarts the healthy count.
            h.step(slow=False, previous_elapsed=1.7)
            for _ in range(p6.SLOW_PAUSE_RESUME_CYCLES - 1):
                h.step(slow=False, previous_elapsed=0.7)
            self.assertEqual(h.verifier.state, "pausiert")
            calls = len(h.session.calls)
            h.step(slow=True, previous_elapsed=0.7)
            self.assertEqual(h.verifier.state, "läuft", h.verifier.reason)
            self.assertGreater(len(h.session.calls), calls)
            resumed = [line for line in h.log_lines() if line["type"] == "fortsetzung"]
            self.assertEqual(len(resumed), 1)
            self.assertEqual(resumed[0]["max_elapsed_s"], 1.7)
            self.assertIsNone(h.verifier.status()["slow_pause"])

    def test_pause_longer_than_ten_minutes_aborts(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self._running(directory)
            self._pause(h)
            h.step(seconds=p6.SLOW_PAUSE_ABORT_SECONDS, slow=True, previous_elapsed=2.0)
            self.assertEqual(h.verifier.state, "abgebrochen")
            self.assertIn("10 min", h.verifier.reason)

    def test_seventh_pause_within_one_hour_aborts(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self._running(directory)
            for _ in range(p6.SLOW_PAUSES_PER_HOUR_LIMIT):
                self._pause(h)
                self.assertEqual(h.verifier.state, "pausiert")
                for _ in range(p6.SLOW_PAUSE_RESUME_CYCLES):
                    h.step(previous_elapsed=0.7)
                self.assertEqual(h.verifier.state, "läuft", h.verifier.reason)
            self._pause(h)
            self.assertEqual(h.verifier.state, "abgebrochen")
            self.assertIn("wiederkehrend", h.verifier.reason)
            self.assertEqual(h.verifier.counters["slow_pauses"], p6.SLOW_PAUSES_PER_HOUR_LIMIT)

    def test_housekeeping_cycle_is_not_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self._running(directory)
            h.step(slow=False, previous_elapsed=1.6)
            h.step(slow=False, previous_elapsed=1.6)
            for _ in range(3):
                h.step(slow=False, previous_elapsed=3.0, previous_housekeeping=True)
            self.assertEqual(h.verifier.state, "läuft")
            # Housekeeping neither counts nor resets: the third real one pauses.
            h.step(slow=False, previous_elapsed=1.6)
            self.assertEqual(h.verifier.state, "pausiert")

    def test_slow_cycle_long_duration_is_not_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self._running(directory)
            for _ in range(5):
                h.step(slow=False, previous_elapsed=2.0, previous_slow=True)
            self.assertEqual(h.verifier.state, "läuft")

    def test_display_status_unavailable_repeatedly_aborts(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self._running(directory)
            for _ in range(3):
                h.step(slow=True, display_available=False)
            self.assertEqual(h.verifier.state, "abgebrochen")
            self.assertIn("display_status", h.verifier.reason)

    def test_ram_telemetry_unavailable_repeatedly_aborts(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self._running(directory)
            for _ in range(3):
                h.step(slow=True, ram_telemetry_available=False)
            self.assertEqual(h.verifier.state, "abgebrochen")

    def test_guard_refusal_aborts(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self._running(directory)
            bad = VerifyField(id="P9.9", paket="P9", cpu=0, addr=0x0F0E, length=4, takt="S")
            h.verifier._plan_cycle = lambda payloads, now: [[bad]]
            calls = len(h.session.calls)
            h.step()
            self.assertEqual(h.verifier.state, "abgebrochen")
            self.assertIn("§2.5/6", h.verifier.reason)
            self.assertEqual(len(h.session.calls), calls)

    def test_full_day_log_pauses_reads_until_next_day(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self._running(directory)
            size = h.verifier._log_path().stat().st_size
            with mock.patch.object(p6, "PHASE6_LOG_MAX_BYTES_PER_FILE", size + p6.PHASE6_LOG_MARGIN_BYTES):
                calls = len(h.session.calls)
                for _ in range(30):
                    h.step()
                self.assertEqual(h.verifier.state, "pausiert")
                self.assertIn("50 MB", h.verifier.reason)
                self.assertEqual(len(h.session.calls), calls)
                h.now += timedelta(days=1)
                for _ in range(30):
                    h.step()
                    if h.verifier.state == "läuft":
                        break
                self.assertEqual(h.verifier.state, "läuft")
                h.step()
                self.assertGreater(len(h.session.calls), calls)

    def test_total_log_limit_aborts(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self._running(directory)
            with mock.patch.object(p6, "PHASE6_LOG_MAX_BYTES_TOTAL", 1):
                calls = len(h.session.calls)
                h.step()
            self.assertEqual(h.verifier.state, "abgebrochen")
            self.assertIn("Gesamtobergrenze", h.verifier.reason)
            self.assertEqual(len(h.session.calls), calls)

    def test_unloggable_read_aborts(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self._running(directory)
            h.verifier._log = lambda record: False
            for _ in range(30):
                h.step()
                if h.verifier.state != "läuft":
                    break
            self.assertEqual(h.verifier.state, "abgebrochen")
            self.assertIn("Log-Obergrenze", h.verifier.reason)

    def test_daily_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(directory, clock_start=datetime(2026, 10, 25, 23, 59, tzinfo=timezone.utc))
            h.run_until("läuft")
            for _ in range(200):
                h.step()
            names = sorted(path.name for path in (h.dir / "phase6-verify").glob("*.jsonl"))
            self.assertEqual(names, ["20261025.jsonl", "20261026.jsonl"])


class WebIntegrationTests(unittest.TestCase):
    def test_live_reports_phase6_default_off(self):
        from open_dachs_manager.web import DachsWebApp

        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            status = app.live()["phase6_verify"]
            self.assertEqual(status["state"], "aus")
            self.assertFalse(status["enabled"])
            self.assertIsNone(status["end_at"])

    def test_monitor_cycle_runs_logger_inside_lease_and_fails_closed(self):
        from contextlib import contextmanager

        from open_dachs_manager.web import DachsWebApp

        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "phase6_verify.json").write_text(json.dumps({
                "enabled": True, "end_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
                "run_id": "monitor-test",
            }))
            app = DachsWebApp(data_dir=directory, interval=60)
            ram = make_ram()
            session = FakeSession(ram)
            leases = []

            class Service:
                @contextmanager
                def session(self):
                    leases.append("open")
                    yield session
                    leases.append("close")

                def decoded_block(self, _session, block):
                    # Block 20 carries a foreign firmware version.
                    payload = b20(regler=(0, 0, 0, 3, 10)) if block == 20 else bytes(70)
                    result = SimpleNamespace(
                        ok=True, payload=payload, status=0, response=SimpleNamespace(elapsed_ms=1.0)
                    )
                    return result, app.pack.display_fields(block, payload)

            app.service = Service()
            app.serial_enabled = True
            app.monitor_enabled = True
            original = app._publish_display_status

            def publish_and_stop(status, now_mono):
                original(status, now_mono)
                app.stop_event.set()

            app._publish_display_status = publish_and_stop
            app._monitor_loop()
            self.assertEqual(leases, ["open", "close"])
            status = app.live()["phase6_verify"]
            self.assertEqual(status["state"], "blockiert")
            self.assertIn("R 000.003.010", status["reason"])
            phase6_calls = [c for c in session.calls if c[1] in {
                verify_read_request(f.cpu, f.addr, f.length) for f in PHASE6_VERIFY_FIELDS}]
            self.assertEqual(phase6_calls, [])
            self.assertTrue(all(c[1][0] == 0x02 for c in session.calls))


    def test_history_reads_do_not_take_the_store_writer_lock(self):
        import threading

        from open_dachs_manager.web import DachsWebApp

        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            store = app.store
            now = datetime.now(timezone.utc)
            store.record_adaptive_snapshot(now.isoformat(), [{
                "block": 24, "key": "Hka_Mw1.Temp.sbMotor", "label": "Motor",
                "raw": 40, "value": 40, "unit": "°C",
            }])
            results = {}

            def read_all():
                start = now - timedelta(hours=1)
                end = now + timedelta(minutes=1)
                results["adaptive"] = store.adaptive_history_between(
                    24, "Hka_Mw1.Temp.sbMotor", start, end, 100)
                results["status"] = store.adaptive_status()
                results["catalog"] = store.adaptive_catalog()
                results["measurements"] = store.measurements_between(
                    24, "Hka_Mw1.Temp.sbMotor", start, end, 100)

            # A long history query used to hold this lock for 0.4-23 s on ODM
            # and stalled the monitor's commits; reads must now pass it by.
            with store.lock:
                worker = threading.Thread(target=read_all, daemon=True)
                worker.start()
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive(), "Lesepfad wartet auf den Schreib-Lock")
            self.assertEqual(len(results["adaptive"]), 1)
            self.assertEqual(results["status"]["raw"]["count"], 1)
            self.assertTrue(results["catalog"])

    def test_monitor_cycle_timing_marks_kind_and_splits_serial_part(self):
        from open_dachs_manager.web import DachsWebApp

        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            app._record_cycle_timing(0.6, 0.3, False, False)
            app._record_cycle_timing(1.9, 1.2, True, False)
            app._record_cycle_timing(2.5, 0.4, False, True)
            app._record_cycle_timing(1.7, 0.2, False, False)
            timing = app.live()["monitor"]["cycle_timing"]
            self.assertEqual(timing["kind"], "fast")
            self.assertEqual(timing["serial_s"], 0.2)
            self.assertEqual(timing["store_s"], 1.5)
            self.assertEqual(timing["fast_over_limit"], 1)
            self.assertEqual(timing["fast_max_s"], 1.7)
            self.assertEqual(timing["fast_limit_s"], p6.SLOW_FAST_CYCLE_SECONDS)


if __name__ == "__main__":
    unittest.main()
