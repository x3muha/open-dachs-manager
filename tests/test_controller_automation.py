import http.client
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import quote
from zoneinfo import ZoneInfo

from open_dachs_manager.controller_automation import (
    clock_sync_due,
    clock_sync_payload,
    clock_sync_window_reason,
    controller_naive,
    controller_raw,
    next_clock_sync,
    night_shutdown_second,
    normalize_clock_sync_settings,
    normalize_runtime_auto_rule,
    real_runtime_status,
    runtime_auto_eligibility,
    runtime_auto_min_target,
    runtime_auto_precheck,
)
from pathlib import Path

from open_dachs_manager.web import (
    DASHBOARD_RUNTIME_24H_KEY,
    HISTORY_REAL_RUNTIME_SERIES,
    HISTORY_SERIES,
    HISTORY_SERIES_INDEX,
    MOTOR_STATUS_KEY,
    RUNTIME_24H_REQUEST,
    RUNTIME_24H_WRITE_PREFIX,
    DachsHTTPServer,
    DachsStore,
    DachsWebApp,
    init_users,
)

BERLIN = ZoneInfo("Europe/Berlin")


def berlin(*args):
    return datetime(*args, tzinfo=BERLIN).astimezone(timezone.utc)


def response(payload, positive=True):
    return SimpleNamespace(
        ack=SimpleNamespace(positive=positive),
        data=SimpleNamespace(payload=bytes(payload)),
        elapsed_ms=0.5,
    )


RULE = {"enabled": True, "threshold_seconds": 20000, "target_seconds": 10800, "max_resets_per_run": 0}


class RuleTests(unittest.TestCase):
    def test_runtime_rule_hard_limits(self):
        self.assertEqual(normalize_runtime_auto_rule(RULE, None)["target_seconds"], 10800)
        # Unknown bMindestlaufzeit -> universal 3 h minimum.
        with self.assertRaises(ValueError):
            normalize_runtime_auto_rule({**RULE, "target_seconds": 3600, "threshold_seconds": 9000}, None)
        # 60 min known -> 3600 allowed, 3599 rejected.
        self.assertEqual(
            normalize_runtime_auto_rule({**RULE, "target_seconds": 3600, "threshold_seconds": 9000}, 60)["target_seconds"],
            3600,
        )
        with self.assertRaises(ValueError):
            normalize_runtime_auto_rule({**RULE, "target_seconds": 3599, "threshold_seconds": 9000}, 60)
        # Lower bound max(1800, 360, 60*L).
        self.assertEqual(runtime_auto_min_target(0), 1800)
        self.assertEqual(runtime_auto_min_target(30), 1800)
        self.assertEqual(runtime_auto_min_target(180), 10800)
        with self.assertRaises(ValueError):
            normalize_runtime_auto_rule({**RULE, "target_seconds": 1799, "threshold_seconds": 9000}, 0)
        # X > Y + 600
        with self.assertRaises(ValueError):
            normalize_runtime_auto_rule({**RULE, "threshold_seconds": 11400}, None)
        self.assertEqual(normalize_runtime_auto_rule({**RULE, "threshold_seconds": 11401}, None)["threshold_seconds"], 11401)
        # X <= 86370 - 900
        self.assertEqual(normalize_runtime_auto_rule({**RULE, "threshold_seconds": 85470}, None)["threshold_seconds"], 85470)
        with self.assertRaises(ValueError):
            normalize_runtime_auto_rule({**RULE, "threshold_seconds": 85471}, None)
        # Z 0..999, strict integers.
        self.assertEqual(normalize_runtime_auto_rule({**RULE, "max_resets_per_run": 999}, None)["max_resets_per_run"], 999)
        for bad in ({"max_resets_per_run": 1000}, {"max_resets_per_run": -1}, {"target_seconds": True},
                    {"threshold_seconds": 2.5}, {"enabled": "yes"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_runtime_auto_rule({**RULE, **bad}, None)

    def test_clock_settings_limits(self):
        self.assertEqual(normalize_clock_sync_settings({})["time"], "12:00")
        self.assertFalse(normalize_clock_sync_settings({})["enabled"])
        self.assertEqual(normalize_clock_sync_settings({"time": "6:00"})["time"], "06:00")
        for bad in ({"time": "05:59"}, {"time": "22:01"}, {"time": "12"}, {"threshold_seconds": 9},
                    {"threshold_seconds": 3601}, {"max_offset_seconds": 30}, {"enabled": 1},
                    {"threshold_seconds": 600, "max_offset_seconds": 600}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_clock_sync_settings(bad)

    def test_window_exclusions_night_second_midnight_dst(self):
        now = berlin(2026, 10, 26, 12, 0, 0)
        controller_after = datetime(2026, 10, 26, 12, 0, 0)
        # Normal midday correction is allowed.
        self.assertIsNone(clock_sync_window_reason(
            controller_after + timedelta(seconds=354), controller_after, now, 38))
        # Night-shutdown second 03:38:00 for 38 starts (±15 min, also starts+1).
        self.assertEqual(night_shutdown_second(38), 3 * 3600 + 38 * 60)
        self.assertEqual(night_shutdown_second(138), 3 * 3600 + 38 * 60)
        reason = clock_sync_window_reason(
            datetime(2026, 10, 26, 3, 25), datetime(2026, 10, 26, 3, 23, 30), now, 38)
        self.assertIn("03:38:00", reason)
        reason = clock_sync_window_reason(
            datetime(2026, 10, 26, 3, 54), datetime(2026, 10, 26, 3, 55), now, 38)
        self.assertIn("03:39:00", reason)  # next start count
        self.assertIsNone(clock_sync_window_reason(
            datetime(2026, 10, 26, 4, 10), datetime(2026, 10, 26, 4, 11), now, 38))
        # Midnight in or within 15 min of the interval.
        self.assertIn("Mitternacht", clock_sync_window_reason(
            datetime(2026, 10, 26, 23, 50), datetime(2026, 10, 26, 23, 51), now, 38))
        self.assertIn("Mitternacht", clock_sync_window_reason(
            datetime(2026, 10, 27, 0, 10), datetime(2026, 10, 27, 0, 11), now, 38))
        # A backward step that crosses the night second is also rejected.
        self.assertIsNotNone(clock_sync_window_reason(
            datetime(2026, 10, 26, 4, 0), datetime(2026, 10, 26, 3, 30), now, 38))
        # DST switch 2026-10-25 01:00 UTC (03:00 CEST -> 02:00 CET).
        dst_now = datetime(2026, 10, 25, 0, 50, tzinfo=timezone.utc)
        self.assertIn("Sommer", clock_sync_window_reason(
            datetime(2026, 10, 25, 2, 50), datetime(2026, 10, 25, 2, 51), dst_now, 80))
        self.assertIn("Starts unbekannt", clock_sync_window_reason(
            controller_after, controller_after, now, None))

    def test_daily_due_window(self):
        settings = {"enabled": True, "time": "12:00"}
        self.assertEqual(clock_sync_due(settings, berlin(2026, 10, 26, 11, 59, 59), None), (False, "2026-10-26"))
        self.assertEqual(clock_sync_due(settings, berlin(2026, 10, 26, 12, 0, 0), None), (True, "2026-10-26"))
        self.assertEqual(clock_sync_due(settings, berlin(2026, 10, 26, 12, 59, 0), None), (True, "2026-10-26"))
        self.assertEqual(clock_sync_due(settings, berlin(2026, 10, 26, 13, 0, 0), None), (False, "2026-10-26"))
        self.assertFalse(clock_sync_due(settings, berlin(2026, 10, 26, 12, 30), "2026-10-26")[0])
        self.assertFalse(clock_sync_due({**settings, "enabled": False}, berlin(2026, 10, 26, 12, 0), None)[0])
        self.assertTrue(next_clock_sync(settings, berlin(2026, 10, 26, 12, 30), "2026-10-26").startswith("2026-10-27T12:00"))

    def test_precheck_rate_limit_and_per_run_counting(self):
        now = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(runtime_auto_precheck({**RULE, "enabled": False}, {}, 30000, "a", now)[0], "disabled")
        self.assertEqual(runtime_auto_precheck(RULE, {}, None, "a", now)[0], "blocked")
        self.assertEqual(runtime_auto_precheck(RULE, {}, 19999, "a", now)[0], "watching")
        self.assertEqual(runtime_auto_precheck(RULE, {}, 20000, "a", now)[0], "due")
        last = {"last_write_at": (now - timedelta(seconds=599)).isoformat()}
        phase, reason = runtime_auto_precheck(RULE, last, 20000, "a", now)
        self.assertEqual(phase, "blocked")
        self.assertIn("Rate-Limit", reason)
        last = {"last_write_at": (now - timedelta(seconds=600)).isoformat()}
        self.assertEqual(runtime_auto_precheck(RULE, last, 20000, "a", now)[0], "due")
        future = {"last_write_at": (now + timedelta(seconds=5)).isoformat()}
        self.assertEqual(runtime_auto_precheck(RULE, future, 20000, "a", now)[0], "blocked")
        limited = {**RULE, "max_resets_per_run": 2}
        state = {"run_key": "a", "resets_in_run": 2}
        self.assertEqual(runtime_auto_precheck(limited, state, 20000, "a", now)[0], "limit")
        self.assertEqual(runtime_auto_precheck(limited, {"run_key": "a", "resets_in_run": 1}, 20000, "a", now)[0], "due")
        # A new motor run (new key) starts counting from zero again.
        self.assertEqual(runtime_auto_precheck(limited, state, 20000, "b", now)[0], "due")
        self.assertEqual(runtime_auto_precheck(limited, {}, 20000, None, now)[0], "blocked")
        # Z = 0: unlimited, run key not required.
        self.assertEqual(runtime_auto_precheck(RULE, {"run_key": "a", "resets_in_run": 500}, 20000, None, now)[0], "due")

    def test_fresh_eligibility(self):
        base = dict(motor_status=31, service_code=0, manual_mode=0, min_runtime_minutes=60, counter_seconds=20001)
        self.assertIsNone(runtime_auto_eligibility(RULE, **base))
        for change, text in (
            ({"motor_status": 29}, "läuft nicht"), ({"motor_status": None}, "Motorstatus"),
            ({"service_code": 131}, "Servicecode 131"), ({"manual_mode": 1}, "Handbetrieb"),
            ({"manual_mode": None}, "0x1b5a"), ({"min_runtime_minutes": None}, "Mindestlaufzeit"),
            ({"min_runtime_minutes": 200}, "Mindestwert 12000"), ({"counter_seconds": 19999}, "unter Schwelle"),
            ({"counter_seconds": 86370}, "24-h-Schwelle"),
        ):
            with self.subTest(change=change):
                self.assertIn(text, runtime_auto_eligibility(RULE, **{**base, **change}))

    def test_real_runtime_reconstruction(self):
        now = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
        history = {"standstill_at": now - timedelta(hours=5, seconds=1), "start_at": now - timedelta(hours=5),
                   "gap": False}
        result = real_runtime_status(now_utc=now, motor_status=31, history=history,
                                     operating_seconds_now=None, counter_0f0e=18000)
        self.assertTrue(result["running"] and result["available"] and result["since_exact"])
        self.assertEqual(result["seconds"], 18000)
        self.assertFalse(result["counter_reset_detected"])
        reset = real_runtime_status(now_utc=now, motor_status=31, history=history,
                                    operating_seconds_now=None, counter_0f0e=10800)
        self.assertTrue(reset["counter_reset_detected"])
        self.assertEqual(reset["counter_deviation_seconds"], 7200)
        gap = {**history, "gap": True, "operating_seconds_at_standstill": 1000}
        estimated = real_runtime_status(now_utc=now, motor_status=31, history=gap,
                                        operating_seconds_now=8200, counter_0f0e=7200)
        self.assertEqual(estimated["seconds"], 7200)
        self.assertFalse(estimated["since_exact"])
        self.assertIn("betriebssekunden", estimated["source"])
        unknown = real_runtime_status(now_utc=now, motor_status=31, history={**gap, "operating_seconds_at_standstill": None},
                                      operating_seconds_now=8200, counter_0f0e=7200)
        self.assertFalse(unknown["available"])
        self.assertIsNone(unknown["seconds"])
        stopped = real_runtime_status(now_utc=now, motor_status=16, history={"stopped_since": now - timedelta(hours=2)},
                                      operating_seconds_now=None, counter_0f0e=0)
        self.assertEqual(stopped["state"], "steht")
        self.assertFalse(stopped["running"])
        self.assertEqual(stopped["since"], (now - timedelta(hours=2)).isoformat())
        self.assertEqual(real_runtime_status(now_utc=now, motor_status=22, history={}, operating_seconds_now=None,
                                             counter_0f0e=0)["state"], "startet")
        self.assertFalse(real_runtime_status(now_utc=now, motor_status=None, history={}, operating_seconds_now=None,
                                             counter_0f0e=None)["available"])


class StoreReconstructionTests(unittest.TestCase):
    def test_history_reconstruction_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DachsStore(DachsWebApp(data_dir=directory, interval=60).store.path)
            base = datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc)
            points = [(0, 16), (60, 20), (70, 22), (80, 30), (110, 31), (3600, 31)]
            index = HISTORY_SERIES_INDEX[(22, "Hka_Bd.ulBetriebssekunden")]
            with store.database() as db:
                for offset, status in points:
                    db.execute("INSERT INTO motor_status_points(recorded_at,status) VALUES(?,?)",
                               ((base + timedelta(seconds=offset)).isoformat(), status))
                values = [None] * len(HISTORY_SERIES)
                values[index] = [5000, 1.4, 0.0]
                db.execute("INSERT INTO history_samples(recorded_at,sample_json) VALUES(?,?)",
                           ((base + timedelta(seconds=69)).isoformat(), json.dumps({"version": 1, "values": values})))
            history = store.real_runtime_history()
            self.assertEqual(history["standstill_at"], base + timedelta(seconds=70))
            self.assertEqual(history["start_at"], base + timedelta(seconds=80))
            self.assertFalse(history["gap"])
            restarted = DachsStore(store.path)
            self.assertEqual(restarted.real_runtime_history()["start_at"], base + timedelta(seconds=80))
            # An outage after the standstill: start time no longer trusted,
            # operating seconds at the standstill are used instead.
            with store.database() as db:
                db.execute("INSERT INTO motor_status_points(recorded_at,status) VALUES(?,NULL)",
                           ((base + timedelta(seconds=200)).isoformat(),))
            gap = store.real_runtime_history()
            self.assertTrue(gap["gap"])
            self.assertEqual(gap["operating_seconds_at_standstill"], 5000)
            # Stop: "steht seit" is the first point below 30 after the last run point.
            with store.database() as db:
                db.execute("INSERT INTO motor_status_points(recorded_at,status) VALUES(?,?)",
                           ((base + timedelta(seconds=4000)).isoformat(), 12))
            self.assertEqual(store.real_runtime_history()["stopped_since"], base + timedelta(seconds=4000))


class FakeClock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now


class Controller:
    """Hardware double for block 50/22/24, RAM 0x0f0e/0x1b5a, auth and writes."""

    def __init__(self, clock, offset_seconds=354, starts=38):
        self.clock = clock
        self.offset = offset_seconds
        self.starts = starts
        self.block50 = bytearray(70)
        self.block50[3] = 60
        self.mode = "ok"
        self.auth_ok = True
        self.granted = 3
        self.requests = []
        self.writes = []
        self.auth_calls = []
        self.deauth_calls = []
        self.sessions = 0
        self.motor_status = [31]
        self.service_code = 0
        self.manual = 0
        self.counter = 20005
        self.readback_advance = 0
        self.timeout = 0.9

    def controller_raw(self):
        local = self.clock().astimezone(BERLIN).replace(tzinfo=None) + timedelta(seconds=self.offset)
        return controller_raw(local)

    def block50_payload(self):
        payload = bytearray(self.block50)
        payload[36:40] = self.controller_raw().to_bytes(4, "little")
        return bytes(payload)

    @contextmanager
    def session(self):
        self.sessions += 1
        yield Session(self)

    def read_block(self, _session, block, cpu=0):
        if block != 50:
            raise AssertionError(f"unexpected block {block}")
        return SimpleNamespace(ok=True, payload=self.block50_payload(), status=0)

    def decoded_block(self, _session, block, cpu=0):
        if block == 22:
            fields = [SimpleNamespace(key="Hka_Bd.ulAnzahlStarts", raw=self.starts),
                      SimpleNamespace(key="Hka_Bd.bStoerung", raw=self.service_code)]
        elif block == 24:
            status = self.motor_status.pop(0) if len(self.motor_status) > 1 else self.motor_status[0]
            fields = [SimpleNamespace(key=MOTOR_STATUS_KEY, raw=status)]
        else:
            raise AssertionError(f"unexpected block {block}")
        return SimpleNamespace(ok=True, payload=b"", status=0), fields

    def authenticate(self, _session, level, pass4=None):
        self.auth_calls.append((level, pass4))
        return SimpleNamespace(ok=self.auth_ok, granted_level=self.granted, pw4="4567")

    def deauthenticate(self, _session, pass4):
        self.deauth_calls.append(pass4)


class Session:
    def __init__(self, controller):
        self.controller = controller
        self.packet = 0

    def next_packet(self):
        self.packet += 1
        return self.packet

    def write_block(self, block, payload, packet=None, timeout=0.9):
        c = self.controller
        c.writes.append((block, bytes(payload)))
        if c.mode == "nack":
            return response(b"", positive=False)
        payload = bytearray(payload)
        if payload[0] & 0x20 and c.mode != "refuse":
            target = int.from_bytes(payload[36:40], "little")
            c.offset = target - c.controller_raw() + c.offset
            payload[0] &= ~0x20
        c.block50 = payload
        return response(b"")

    def request(self, payload, packet=None, **_kwargs):
        c = self.controller
        payload = bytes(payload)
        c.requests.append(payload)
        if payload == RUNTIME_24H_REQUEST:
            value = c.counter + (c.readback_advance if c.writes else 0)
            return response(bytes([0x82, 0x0F, 0x0E, 0x04]) + value.to_bytes(4, "little"))
        if payload == bytes([0x02, 0x1B, 0x5A, 0x01]):
            return response(bytes([0x82, 0x1B, 0x5A, 0x01, c.manual]))
        if payload.startswith(RUNTIME_24H_WRITE_PREFIX):
            c.writes.append(("ram", payload))
            c.counter = int.from_bytes(payload[4:], "little")
            return response(bytes([0x83, 0x0F, 0x0E, 0x04]))
        raise AssertionError("unexpected request " + payload.hex())


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.app = DachsWebApp(data_dir=self.temporary.name, interval=60)
        self.clock = FakeClock(berlin(2026, 10, 26, 12, 0, 5))
        self.controller = Controller(self.clock)
        self.app.service = self.controller
        self.app.serial_enabled = True
        self.app._utcnow = self.clock
        self.app._ntp_status = lambda: (True, "NTPSynchronized=yes")

    def tearDown(self):
        self.temporary.cleanup()


class ClockSyncTests(AppTestCase):
    def test_manual_sync_rmw_auth_readback_deauth_audit(self):
        result = self.app.sync_controller_clock("admin", "manual")
        self.assertEqual(result["result"], "synchronisiert", result)
        self.assertEqual(result["offset_before_seconds"], 354)
        self.assertLessEqual(abs(result["offset_after_seconds"]), 3)
        self.assertEqual(self.controller.auth_calls, [(3, None)])
        self.assertEqual(self.controller.deauth_calls, ["4567"])
        (block, written), = self.controller.writes
        self.assertEqual(block, 50)
        before = self.controller.block50_payload()
        changed = [index for index in range(70) if written[index] != before[index]]
        # Only the command bit in byte 0 and ulSystemTime may differ.
        self.assertTrue(set(changed) <= {0, 36, 37, 38, 39})
        self.assertEqual(written[0], 0x20)
        audit = self.app.store.audits(limit=1)[0]["audit"]
        self.assertEqual(audit["operation"], "controller-clock-sync")
        self.assertEqual(audit["offset_before_seconds"], 354)
        self.assertTrue(audit["readback_ok"] and audit["deauthenticated"])
        self.assertNotIn("4567", json.dumps(audit))
        self.assertEqual(self.app.controller_clock_sync_status()["last_run"]["result"], "synchronisiert")

    def test_skips_without_auth(self):
        cases = []
        controller = Controller(self.clock, offset_seconds=20)
        cases.append(("Schwelle", controller, None))
        controller = Controller(self.clock)
        controller.block50[54] = 4
        cases.append(("0x202f", controller, None))
        controller = Controller(self.clock, offset_seconds=5000)
        cases.append(("Maximum", controller, None))
        controller = Controller(self.clock)
        controller.block50[0] = 0x20
        cases.append(("bereits gesetzt", controller, None))
        for text, controller, _ in cases:
            with self.subTest(text=text):
                self.app.service = controller
                result = self.app.sync_controller_clock("admin", "manual")
                self.assertIn(text, result["reason"])
                self.assertEqual(controller.auth_calls, [])
                self.assertEqual(controller.writes, [])
        self.assertEqual(self.app.store.audits(), [])

    def test_ntp_unsynced_never_opens_serial(self):
        self.app._ntp_status = lambda: (False, "NTPSynchronized=no")
        result = self.app.sync_controller_clock("admin", "manual")
        self.assertEqual(result["result"], "übersprungen")
        self.assertIn("NTP", result["reason"])
        self.assertEqual(self.controller.sessions, 0)

    def test_window_exclusion_near_midnight(self):
        self.clock.now = berlin(2026, 10, 26, 23, 55, 0)
        result = self.app.sync_controller_clock("admin", "manual")
        self.assertIn("Mitternacht", result["reason"])
        self.assertEqual(self.controller.auth_calls, [])

    def test_firmware_refusal_is_audited_deauthed_and_disables_automation(self):
        self.app.set_controller_clock_sync_settings("admin", {"enabled": True, "acknowledge": True})
        self.controller.mode = "refuse"
        result = self.app.sync_controller_clock("system-automation", "auto")
        self.assertEqual(result["result"], "fehlgeschlagen")
        self.assertTrue(result["uncertain"])
        self.assertEqual(self.controller.deauth_calls, ["4567"])
        audit = self.app.store.audits(limit=1)[0]["audit"]
        self.assertIn("Bit 5", audit["error"])
        self.assertFalse(self.app.controller_clock_sync_status()["settings"]["enabled"])

    def test_denied_auth_never_writes(self):
        self.controller.auth_ok = False
        self.controller.granted = 2
        result = self.app.sync_controller_clock("admin", "manual")
        self.assertEqual(result["result"], "fehlgeschlagen")
        self.assertEqual(self.controller.writes, [])
        self.assertEqual(self.controller.deauth_calls, ["4567"])
        self.assertFalse(result["uncertain"])

    def test_negative_ack_fails_closed(self):
        self.controller.mode = "nack"
        result = self.app.sync_controller_clock("admin", "manual")
        self.assertEqual(result["result"], "fehlgeschlagen")
        self.assertEqual(self.controller.deauth_calls, ["4567"])

    def test_daily_tick_runs_once_and_persists_marker_first(self):
        with self.assertRaises(ValueError):
            self.app.set_controller_clock_sync_settings("admin", {"enabled": True})
        self.app.set_controller_clock_sync_settings("admin", {"enabled": True, "acknowledge": True, "time": "12:00"})
        self.clock.now = berlin(2026, 10, 26, 11, 59, 0)
        self.assertIsNone(self.app._clock_sync_tick())
        self.clock.now = berlin(2026, 10, 26, 12, 0, 1)
        self.assertEqual(self.app._clock_sync_tick()["result"], "synchronisiert")
        self.assertIsNone(self.app._clock_sync_tick())
        self.assertEqual(len(self.controller.writes), 1)
        state = json.loads(self.app.clock_sync_state_path.read_text())
        self.assertEqual(state["last_auto_date"], "2026-10-26")
        restarted = DachsWebApp(data_dir=self.temporary.name, interval=60)
        restarted.service = self.controller
        restarted._utcnow = self.clock
        restarted._ntp_status = self.app._ntp_status
        self.assertIsNone(restarted._clock_sync_tick())
        self.assertTrue(restarted.controller_clock_sync_status()["settings"]["enabled"])
        self.assertIn("synchronisiert", restarted.live()["controller_clock_sync"]["last_result"])

    def test_disabled_tick_does_nothing(self):
        self.assertIsNone(self.app._clock_sync_tick())
        self.assertEqual(self.controller.sessions, 0)


class RuntimeAutoResetTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.run_key = "after:A"
        self.app.real_runtime = lambda: {"running": True, "run_key": self.run_key}
        self.set_counter(20005)

    def set_counter(self, value):
        self.controller.counter = value
        self.app.runtime_24h = {
            **self.app.runtime_24h, "available": True, "counter_seconds": value,
            "recorded_at": self.clock.now.isoformat(),
        }

    def enable(self, **rule):
        payload = {**RULE, **rule, "acknowledge": True}
        return self.app.set_runtime_auto_reset_settings("admin", payload)

    def test_enable_requires_ack_and_disabled_never_reads(self):
        with self.assertRaises(ValueError):
            self.app.set_runtime_auto_reset_settings("admin", dict(RULE))
        self.assertEqual(self.app._runtime_auto_reset_tick()["phase"], "disabled")
        self.assertEqual(self.controller.sessions, 0)
        status = self.app.runtime_auto_reset_status()
        self.assertFalse(status["settings"]["enabled"])
        self.assertEqual(status["defaults"]["threshold_seconds"], 20000)
        self.assertEqual(status["limits"]["max_threshold_seconds"], 85470)

    def test_settings_use_cached_min_runtime(self):
        payload = bytearray(70)
        payload[3] = 30
        import time
        self.app._display_slow_payloads[50] = (bytes(payload), time.monotonic())
        status = self.enable(target_seconds=1800, threshold_seconds=2401)
        self.assertEqual(status["settings"]["target_seconds"], 1800)
        payload[3] = 180
        self.app._display_slow_payloads[50] = (bytes(payload), time.monotonic())
        with self.assertRaises(ValueError):
            self.enable(target_seconds=10799, threshold_seconds=20000)

    def test_happy_path_writes_target_and_audits_rule(self):
        self.enable()
        live = self.app._runtime_auto_reset_tick()
        self.assertEqual(live["phase"], "confirmed", live)
        self.assertEqual(self.controller.counter, 10800)
        self.assertEqual(self.controller.auth_calls, [(3, None)])
        self.assertEqual(self.controller.deauth_calls, ["4567"])
        ram_writes = [item for item in self.controller.writes if item[0] == "ram"]
        self.assertEqual(ram_writes, [("ram", RUNTIME_24H_WRITE_PREFIX + (10800).to_bytes(4, "little"))])
        row = self.app.store.audits(limit=1)[0]
        self.assertEqual(row["username"], "system-automation")
        audit = row["audit"]
        self.assertEqual(audit["operation"], "runtime-auto-reset")
        self.assertEqual(audit["rule"], {"threshold_seconds": 20000, "target_seconds": 10800, "max_resets_per_run": 0})
        self.assertEqual(audit["prewrite_counter"], 20005)
        self.assertTrue(audit["readback_ok"] and audit["deauthenticated"])
        self.assertEqual(audit["eligibility"]["min_runtime_minutes"], 60)
        state = json.loads(self.app.runtime_auto_state_path.read_text())
        self.assertEqual(state["resets_in_run"], 1)
        self.assertEqual(state["last_result"]["result"], "gesetzt")

    def test_rate_limit_one_write_per_ten_minutes(self):
        self.enable(threshold_seconds=11401)
        self.app._runtime_auto_reset_tick()
        self.set_counter(11500)
        self.clock.now += timedelta(seconds=599)
        self.set_counter(11500)
        live = self.app._runtime_auto_reset_tick()
        self.assertEqual(live["phase"], "blocked")
        self.assertIn("Rate-Limit", live["reason"])
        self.assertEqual(len(self.controller.auth_calls), 1)
        self.clock.now += timedelta(seconds=1)
        self.set_counter(11500)
        self.assertEqual(self.app._runtime_auto_reset_tick()["phase"], "confirmed")
        self.assertEqual(len(self.controller.auth_calls), 2)

    def test_z_limit_per_motor_run_resets_with_new_start(self):
        self.enable(max_resets_per_run=1)
        self.assertEqual(self.app._runtime_auto_reset_tick()["phase"], "confirmed")
        self.clock.now += timedelta(seconds=700)
        self.set_counter(20010)
        live = self.app._runtime_auto_reset_tick()
        self.assertEqual(live["phase"], "limit")
        self.assertEqual(len(self.controller.auth_calls), 1)
        self.run_key = "after:B"  # new start (status 22) -> new run
        self.assertEqual(self.app._runtime_auto_reset_tick()["phase"], "confirmed")
        self.assertEqual(len(self.controller.auth_calls), 2)
        self.run_key = None
        self.clock.now += timedelta(seconds=700)
        self.set_counter(20010)
        self.assertIn("Motorlauf", self.app._runtime_auto_reset_tick()["reason"])

    def test_fresh_checks_block_without_auth(self):
        cases = (
            ("motor_status", [25], "läuft nicht"),
            ("motor_status", [31, 16], "läuft nicht"),  # stopped right before auth
            ("service_code", 131, "Servicecode"),
            ("manual", 1, "Handbetrieb"),
        )
        for attribute, value, text in cases:
            with self.subTest(attribute=attribute, value=value):
                self.controller = Controller(self.clock)
                self.app.service = self.controller
                self.app._runtime_auto_backoff_until = 0.0
                setattr(self.controller, attribute, value)
                self.enable()
                self.set_counter(20005)
                live = self.app._runtime_auto_reset_tick()
                self.assertEqual(live["phase"], "blocked")
                self.assertIn(text, live["reason"])
                self.assertEqual(self.controller.auth_calls, [])
                # Backoff: the next tick does not touch the serial line.
                sessions = self.controller.sessions
                self.app._runtime_auto_reset_tick()
                self.assertEqual(self.controller.sessions, sessions)
        self.assertEqual(self.app.store.audits(), [])

    def test_min_runtime_from_fresh_block50(self):
        self.enable()
        self.controller.block50[3] = 200  # 200 min -> 12000 s > Y
        live = self.app._runtime_auto_reset_tick()
        self.assertIn("Mindestwert 12000", live["reason"])
        self.assertEqual(self.controller.auth_calls, [])

    def test_uncertain_readback_disables_automation(self):
        self.enable()
        self.controller.readback_advance = 5
        live = self.app._runtime_auto_reset_tick()
        self.assertEqual(live["phase"], "blocked")
        self.assertIn("unsicher", live["reason"])
        self.assertFalse(self.app.runtime_auto_reset_status()["settings"]["enabled"])
        self.assertIn("unsicher", self.app.runtime_auto_reset_status()["settings"]["disabled_reason"])
        audit = self.app.store.audits(limit=1)[0]["audit"]
        self.assertEqual(audit["operation"], "runtime-auto-reset")
        self.assertTrue(audit["error"])
        self.assertEqual(self.controller.deauth_calls, ["4567"])

    def test_stale_counter_is_not_used(self):
        self.enable()
        self.app.runtime_24h["recorded_at"] = (self.clock.now - timedelta(seconds=31)).isoformat()
        self.assertEqual(self.app._runtime_auto_reset_tick()["phase"], "blocked")
        self.assertEqual(self.controller.sessions, 0)


class RealRuntimeAppTests(AppTestCase):
    def test_live_contains_real_runtime_from_history(self):
        start = self.clock.now - timedelta(hours=3)
        with self.app.store.database() as db:
            db.execute("INSERT INTO motor_status_points(recorded_at,status) VALUES(?,?)",
                       ((start - timedelta(seconds=10)).isoformat(), 22))
            db.execute("INSERT INTO motor_status_points(recorded_at,status) VALUES(?,?)",
                       (start.isoformat(), 30))
        now = self.clock.now.isoformat()
        self.app.live_values[(24, MOTOR_STATUS_KEY)] = {"block": 24, "key": MOTOR_STATUS_KEY, "raw": 31, "recorded_at": now}
        self.app.live_values[(24, "Hka_Mw1.ulMotorlaufsekunden")] = {
            "block": 24, "key": "Hka_Mw1.ulMotorlaufsekunden", "raw": 10800, "recorded_at": now}
        real = self.app.live()["real_runtime"]
        self.assertTrue(real["running"])
        self.assertEqual(real["seconds"], 3 * 3600)
        self.assertEqual(real["since"], start.isoformat())
        self.assertEqual(real["source"], "historie")
        self.assertEqual(real["counter_0f0e"], 10800)
        self.assertFalse(real["counter_reset_detected"])
        self.app.live_values[(24, "Hka_Mw1.ulMotorlaufsekunden")]["raw"] = 600
        self.assertTrue(self.app.real_runtime()["counter_reset_detected"])
        # Stale motor status -> no fabricated value.
        self.app.live_values[(24, MOTOR_STATUS_KEY)]["recorded_at"] = (self.clock.now - timedelta(seconds=30)).isoformat()
        self.assertFalse(self.app.real_runtime()["available"])


class HttpAdminOnlyTests(unittest.TestCase):
    def test_endpoints_are_admin_only_and_require_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            init_users(directory, admin_password="AdminPasswort123", guest_password="GastPasswort123")
            app = DachsWebApp(data_dir=directory, interval=60)
            controller = Controller(lambda: datetime.now(timezone.utc))
            app.service = controller
            admin = app.login("admin", "AdminPasswort123")[0]
            guest = app.login("gast", "GastPasswort123")[0]
            server = DachsHTTPServer(("127.0.0.1", 0), app, base_path="/dachs")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def call(method, path, token, payload=None):
                connection = http.client.HTTPConnection(*server.server_address, timeout=5)
                body = json.dumps(payload) if payload is not None else None
                connection.request(method, "/dachs" + path, body=body, headers={
                    "Cookie": f"open_dachs_session={token}", "Content-Type": "application/json"})
                reply = connection.getresponse()
                data = json.loads(reply.read().decode("utf-8") or "{}")
                connection.close()
                return reply.status, data

            try:
                for method, path, payload in (
                    ("GET", "/api/controller-clock/sync", None),
                    ("GET", "/api/runtime-auto-reset", None),
                    ("POST", "/api/controller-clock/sync", {"confirm": True}),
                    ("POST", "/api/controller-clock/sync/settings", {"enabled": False}),
                    ("POST", "/api/runtime-auto-reset", dict(RULE, acknowledge=True)),
                ):
                    with self.subTest(path=path, method=method):
                        self.assertEqual(call(method, path, guest, payload)[0], 403)
                status, body = call("GET", "/api/controller-clock/sync", admin)
                self.assertEqual(status, 200)
                self.assertFalse(body["settings"]["enabled"])
                self.assertEqual(call("POST", "/api/controller-clock/sync", admin, {})[0], 400)
                self.assertEqual(call("POST", "/api/controller-clock/sync", admin,
                                      {"confirm": True, "pass4": "1234"})[0], 400)
                self.assertEqual(call("POST", "/api/runtime-auto-reset", admin,
                                      dict(RULE, target_seconds=1000, acknowledge=True))[0], 400)
                status, body = call("POST", "/api/runtime-auto-reset", admin, dict(RULE, acknowledge=True))
                self.assertEqual(status, 200)
                self.assertTrue(body["settings"]["enabled"])
                self.assertEqual(controller.sessions, 0)
                self.assertEqual(app.store.audits(), [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class RealRuntimeDisplayTests(unittest.TestCase):
    """"Laufzeit seit Start" shows the real runtime; 0x0f0e is a sub line."""

    ROOT = Path(__file__).resolve().parents[1]

    def test_history_series_renamed_without_index_shift(self):
        ids = [series_id for series_id, *_rest in HISTORY_SERIES]
        # Snapshot of the stored sample layout: positions must never move.
        self.assertEqual(ids[:13], [
            "dachs_eintritt", "vorlauf", "ruecklauf", "kuehlwasser", "regler",
            "abgas_motor", "kapsel", "abgas_hka", "drehzahl", "wirkleistung",
            "betriebsstunden", "motorstatus", "betriebsstunden_gesamt",
        ])
        index = HISTORY_SERIES_INDEX[(24, "Hka_Mw1.ulMotorlaufsekunden")]
        self.assertEqual(index, 10)
        self.assertEqual(HISTORY_SERIES[index][:2], ("betriebsstunden", "Reglerzähler (0x0f0e)"))
        self.assertNotIn("laufzeit_real", ids)
        self.assertNotIn(HISTORY_REAL_RUNTIME_SERIES["key"], [item[3] for item in HISTORY_SERIES])

    def test_retired_and_unknown_dashboard_tiles_keep_the_user_selection(self):
        runtime = {"block": 22, "key": DASHBOARD_RUNTIME_24H_KEY}
        power = {"block": 24, "key": "Hka_Mw1.sWirkleistung"}
        counter = {"block": 24, "key": "Hka_Mw1.ulMotorlaufsekunden"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dashboard_settings.json"
            stored = [power, {"block": 24, "key": "derived.real_runtime"}, counter,
                      {"block": 24, "key": "Gibt.Es.Nicht"}, "kaputt", runtime]
            path.write_text(json.dumps({"version": 2, "cards": stored}))
            app = DachsWebApp(data_dir=directory, interval=60)
            # Retired tile merges into the runtime tile (deduplicated), the
            # unknown key is dropped, everything else keeps its order.
            self.assertEqual(app.dashboard_settings()["cards"], [power, runtime, counter])
            self.assertEqual(json.loads(path.read_text())["cards"], [power, runtime, counter])
            self.assertNotIn({"block": 24, "key": "derived.real_runtime"},
                             app.dashboard_settings()["default_cards"])
            self.assertNotIn("real_runtime", [f["source"] for f in app.dashboard_settings()["derived_fields"]])
            label = {f["source"]: f["label"] for f in app.dashboard_settings()["derived_fields"]}
            self.assertEqual(label["runtime_24h_shutdown"], "Laufzeit seit Start")

            path.write_text(json.dumps({"version": 2, "cards": [{"block": 24, "key": "derived.real_runtime"}, power]}))
            self.assertEqual(DachsWebApp(data_dir=directory, interval=60).dashboard_settings()["cards"],
                             [runtime, power])
            # A current, valid selection is read without being rewritten.
            text = json.dumps({"version": 2, "cards": [power]})
            path.write_text(text)
            self.assertEqual(DachsWebApp(data_dir=directory, interval=60).dashboard_settings()["cards"], [power])
            self.assertEqual(path.read_text(), text)

    def test_real_runtime_series_is_reconstructed_from_status_points(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            store = app.store
            base = datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc)
            rows = [(0, 16)]
            rows += [(60 + 30 * i, 31) for i in range(0, 121)]          # run 1: 60 s .. 3660 s
            rows += [(3690, 12), (3720, 12)]                           # stop
            rows += [(3750, 31), (3780, 31)]                           # run 2 (start known)
            rows += [(3900, 31), (3930, 31), (3960, 12)]              # 120-s gap: start unknown
            rows += [(3990, 31), (4020, 31)]                           # run 3 after standstill
            with store.database() as db:
                for offset, status in rows:
                    db.execute("INSERT INTO motor_status_points(recorded_at,status) VALUES(?,?)",
                               ((base + timedelta(seconds=offset)).isoformat(), status))
            points = store.real_runtime_series(base, base + timedelta(seconds=4020))
            pairs = [(p["recorded_at"], p["raw"]) for p in points]
            at = lambda seconds: (base + timedelta(seconds=seconds)).isoformat()  # noqa: E731
            self.assertEqual(pairs[:3], [(at(60), 0), (at(3690), 3630), (at(3690), 0)])
            # Run 2 is cut by the gap and ends at its last observed point.
            self.assertEqual(pairs[3:5], [(at(3750), 0), (at(3780), 30)])
            # The run after the gap is never guessed; run 3 starts fresh at 0.
            self.assertNotIn(at(3900), [p[0] for p in pairs])
            self.assertEqual(pairs[5:], [(at(3990), 0), (at(4020), 30)])
            self.assertTrue(all(p["series_id"] == "laufzeit_real" and p["unit"] == "h" for p in points))
            self.assertEqual(points[1]["value"], round(3630 / 3600, 3))
            # A window starting inside run 1 continues the run, not 0x0f0e.
            inside = store.real_runtime_series(base + timedelta(seconds=1860), base + timedelta(seconds=2000))
            self.assertEqual(inside[0]["raw"], 1800)
            self.assertEqual(store.real_runtime_series(base, base), [])
            self.assertEqual(app.schema()["derived_series"], [HISTORY_REAL_RUNTIME_SERIES])
            self.assertEqual(app.schema()["derived_series"][0]["title"], "Laufzeit seit Start (real)")
            series = {item["id"]: item["title"] for item in app.schema()["series"]}
            self.assertEqual(series["betriebsstunden"], "Reglerzähler (0x0f0e)")

    def test_history_batch_serves_the_derived_series(self):
        with tempfile.TemporaryDirectory() as directory:
            init_users(directory, admin_password="AdminPasswort123", guest_password="GastPasswort123")
            app = DachsWebApp(data_dir=directory, interval=60)
            guest = app.login("gast", "GastPasswort123")[0]
            now = datetime.now(timezone.utc).replace(microsecond=0)
            with app.store.database() as db:
                for offset, status in ((-360, 12), (-330, 12), (-300, 31), (-270, 31), (-240, 31)):
                    db.execute("INSERT INTO motor_status_points(recorded_at,status) VALUES(?,?)",
                               ((now + timedelta(seconds=offset)).isoformat(), status))
            server = DachsHTTPServer(("127.0.0.1", 0), app)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                series = json.dumps([
                    {"id": "laufzeit_real", "block": 24, "key": HISTORY_REAL_RUNTIME_SERIES["key"]},
                    {"id": "betriebsstunden", "block": 24, "key": "Hka_Mw1.ulMotorlaufsekunden"},
                ])
                connection = http.client.HTTPConnection(*server.server_address, timeout=5)
                connection.request("GET", "/api/history-batch?hours=1&series=" + quote(series),
                                   headers={"Cookie": f"open_dachs_session={guest}"})
                reply = connection.getresponse()
                body = json.loads(reply.read().decode("utf-8"))
                connection.close()
                self.assertEqual(reply.status, 200)
                self.assertEqual(body["series"]["betriebsstunden"], [])
                self.assertEqual([p["raw"] for p in body["series"]["laufzeit_real"]], [0, 60])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_frontend_maps_runtime_views_to_real_runtime(self):
        app = (self.ROOT / "src/open_dachs_manager/web/app.js").read_text(encoding="utf-8")
        index = (self.ROOT / "src/open_dachs_manager/web/index.html").read_text(encoding="utf-8")
        # No view reads 0x0f0e directly as "Laufzeit seit Start" any more.
        for element in ("board-value-betriebsstunden", "v95-overview-betriebsstunden", "v95-tech-betriebsstunden"):
            with self.subTest(element=element):
                self.assertNotIn(f'"{element}":"betriebsstunden"', app)
        render = app[app.index("function renderRealRuntimeValues()"):app.index("function realRuntimeDetail()")]
        for element in ("v95-overview-betriebsstunden", "v95-tech-betriebsstunden", "compact-value-betriebsstunden"):
            self.assertIn(f'"{element}"', render)
        self.assertIn("real.short", render)
        self.assertIn("realRuntimeCounterText()", render)
        self.assertIn('"board-value-betriebsstunden"', render)
        self.assertIn("renderRealRuntimeValues();", app[app.index("function renderOverview()"):])
        display = app[app.index("function realRuntimeDisplay()"):app.index("function renderRealRuntimeValues()")]
        self.assertIn("state.live?.real_runtime", display)
        self.assertIn('value: "—"', display)
        self.assertNotIn("counter_0f0e", display)
        counter = app[app.index("function realRuntimeCounterText("):app.index("function realRuntimeDisplay()")]
        self.assertIn('"Reglerzähler: "', counter)
        self.assertIn("zurückgesetzt", counter)
        self.assertNotIn("function realRuntimeCard()", app)
        self.assertNotIn('field?.source === "real_runtime"', app)
        self.assertNotIn("Laufzeit (real)", app)
        self.assertIn('<h3 id="runtime24hTitle">Reglerzähler (0x0f0e) setzen</h3>', index)
        self.assertIn('<tr><th>Laufzeit seit Start</th><td id="board-value-betriebsstunden">', index)


if __name__ == "__main__":
    unittest.main()
