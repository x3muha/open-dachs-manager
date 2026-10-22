import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from open_dachs_manager.diagnosis_250h import (
    DIAGNOSIS_CACHE_SCHEMA,
    Diagnosis250hCache,
    MotorStopDetector,
    build_250h_diagnosis,
    next_local_midnight,
)
from open_dachs_manager.web import (
    HISTORY_SERIES,
    DachsHTTPServer,
    DachsStore,
    DachsWebApp,
    init_users,
)

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "open_dachs_manager" / "web"


def diagnosis_result(marker="fresh"):
    return {
        "schema": "open-dachs-manager-250h-diagnosis/v1",
        "available": False,
        "reason": marker,
        "source": "test history",
        "intervals": [],
        "metrics": {},
        "notices": [],
        "hint_count": 0,
        "action_count": 0,
    }


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class Diagnosis250hTests(unittest.TestCase):
    def samples(self, hours=260, *, gap_at=None):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        result = []
        starts = 1000
        for quarter in range(hours * 4 + 1):
            if gap_at is not None and gap_at < quarter < gap_at + 8:
                continue
            operating_hours = 1000 + quarter / 4
            if quarter and quarter % 8 == 0:
                starts += 1
            target = 5.0 if operating_hours < 1125 else 5.3
            result.append({
                "recorded_at": (start + timedelta(minutes=15 * quarter)).isoformat(),
                "operating_seconds": int(operating_hours * 3600),
                "starts": starts,
                "motor_exhaust": 460,
                "dachs_exhaust": 150,
                "coolant": 80 if operating_hours < 1250 else 90,
                "inlet": 58,
                "controller": 32,
                "power": target + 0.10,
                "power_target": target,
                "warning_code": 0,
                "service_code": 0,
            })
        return result

    def test_completed_and_running_intervals_are_separate_and_power_uses_dynamic_target(self):
        samples = self.samples()
        result = build_250h_diagnosis(samples, history_complete=True)

        self.assertTrue(result["available"])
        self.assertEqual(len(result["intervals"]), 2)
        completed, current = result["intervals"]
        self.assertTrue(completed["complete"])
        self.assertFalse(completed["current"])
        self.assertFalse(current["complete"])
        self.assertTrue(current["current"])
        self.assertAlmostEqual(completed["coverage_percent"], 100)
        self.assertIsNone(completed["failed_starts"])
        self.assertIsNone(completed["failed_start_percent"])
        self.assertFalse(completed["failed_start_reliable"])
        self.assertAlmostEqual(completed["power_in_band_percent"], 100)
        self.assertEqual(
            [segment["target_kw"] for segment in completed["target_segments"]],
            [5.0, 5.3],
        )
        self.assertTrue(any(
            notice["category"] == "hint" and "Kühlwasser" in notice["title"]
            for notice in result["notices"]
        ))
        self.assertFalse(result["condenser_available"])
        self.assertIn("keine MSR2-Diagnoseblöcke", result["source"])

    def test_status_14_is_not_counted_as_failed_start_and_factor_is_unavailable(self):
        samples = self.samples()
        result = build_250h_diagnosis(
            samples,
            status_events=[
                {"recorded_at": samples[10]["recorded_at"], "status": 14},
                {"recorded_at": samples[11]["recorded_at"], "status": 20},
            ],
            history_complete=True,
        )
        completed = result["intervals"][0]
        self.assertIsNone(completed["failed_starts"])
        self.assertIsNone(completed["failed_start_percent"])
        self.assertFalse(completed["failed_start_reliable"])
        self.assertFalse(result["failed_start_available"])
        self.assertIn("keinen belegten Fehlstartzähler", result["failed_start_basis"])
        self.assertIn("nicht als Fehlstart gezählt", result["failed_start_basis"])
        self.assertFalse(any("Fehlstartfaktor" in notice["title"] for notice in result["notices"]))

    def test_full_motor_stop_requires_run_zero_rpm_and_stable_off_status(self):
        detector = MotorStopDetector(confirm_seconds=5)
        start = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)

        self.assertFalse(detector.observe(start, status=16, rpm=0))
        self.assertFalse(detector.observe(start + timedelta(seconds=1), status=35, rpm=2400))
        self.assertFalse(detector.observe(start + timedelta(seconds=2), status=14, rpm=0))
        self.assertFalse(detector.observe(start + timedelta(seconds=3), status=16, rpm=0))
        self.assertFalse(detector.observe(start + timedelta(seconds=7), status=15, rpm=0))
        self.assertTrue(detector.observe(start + timedelta(seconds=8), status=16, rpm=0))
        self.assertFalse(detector.observe(start + timedelta(seconds=20), status=15, rpm=0))

    def test_motor_stop_flapping_is_debounced_until_a_new_run(self):
        detector = MotorStopDetector(confirm_seconds=2)
        start = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)
        events = [
            (0, 30, 1200),
            (1, 16, 0),
            (2, 14, 0),
            (3, 16, 0),
            (5, 16, 0),
            (6, 14, 0),
            (7, 16, 0),
        ]
        triggers = [
            detector.observe(start + timedelta(seconds=offset), status=status, rpm=rpm)
            for offset, status, rpm in events
        ]
        self.assertEqual(sum(triggers), 1)

    def test_next_midnight_uses_berlin_dst_day_lengths(self):
        berlin = ZoneInfo("Europe/Berlin")
        spring = next_local_midnight(datetime(2026, 3, 28, 23, 30, tzinfo=berlin))
        autumn = next_local_midnight(datetime(2026, 10, 24, 23, 30, tzinfo=berlin))
        self.assertEqual(spring.isoformat(), "2026-03-29T00:00:00+01:00")
        self.assertEqual(autumn.isoformat(), "2026-10-25T00:00:00+02:00")
        self.assertEqual(
            (next_local_midnight(spring).astimezone(UTC) - spring.astimezone(UTC)).total_seconds(),
            23 * 3600,
        )
        self.assertEqual(
            (next_local_midnight(autumn).astimezone(UTC) - autumn.astimezone(UTC)).total_seconds(),
            25 * 3600,
        )

    def test_midnight_due_is_coalesced_and_advances_to_next_local_day(self):
        with tempfile.TemporaryDirectory() as directory:
            now = datetime(2026, 3, 29, 0, 0, tzinfo=ZoneInfo("Europe/Berlin"))
            cache = Diagnosis250hCache(Path(directory) / "cache.json", diagnosis_result)
            cache.next_midnight = now
            self.assertTrue(cache._schedule_midnight_if_due(now))
            self.assertFalse(cache._schedule_midnight_if_due(now))
            self.assertEqual(cache.pending_reasons, {"midnight"})
            self.assertEqual(cache.next_midnight.isoformat(), "2026-03-30T00:00:00+02:00")

    def test_cache_is_atomic_private_and_reloads_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "diagnosis_250h_cache.json"
            cache = Diagnosis250hCache(path, lambda: diagnosis_result("first"))
            cache.start()
            self.assertTrue(wait_until(lambda: cache.get()["cache_available"]))
            cache.stop()
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["schema"], DIAGNOSIS_CACHE_SCHEMA)
            self.assertEqual(stat_mode := os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(stat_mode, 0o600)

            restarted = Diagnosis250hCache(path, lambda: diagnosis_result("second"))
            immediate = restarted.get()
            self.assertEqual(immediate["reason"], "first")
            self.assertTrue(immediate["cache_available"])
            self.assertTrue(immediate["running"])
            restarted.stop()

    def test_failed_atomic_replace_keeps_previous_cache_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "diagnosis_250h_cache.json"
            cache = Diagnosis250hCache(path, diagnosis_result)
            original = {
                "schema": DIAGNOSIS_CACHE_SCHEMA,
                "generated_at": datetime.now(UTC).isoformat(),
                "trigger_reason": "test",
                "result": diagnosis_result("old"),
            }
            cache._write(original)
            before = path.read_bytes()
            with (
                patch("open_dachs_manager.diagnosis_250h.os.replace", side_effect=OSError("disk")),
                self.assertRaisesRegex(OSError, "disk"),
            ):
                cache._write({**original, "result": diagnosis_result("new")})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_cache_never_runs_parallel_and_coalesces_duplicate_reasons(self):
        with tempfile.TemporaryDirectory() as directory:
            release = threading.Event()
            state = {"active": 0, "maximum": 0, "calls": 0}

            def calculate():
                state["calls"] += 1
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
                release.wait(1)
                state["active"] -= 1
                return diagnosis_result(str(state["calls"]))

            cache = Diagnosis250hCache(Path(directory) / "cache.json", calculate)
            cache.start()
            self.assertTrue(wait_until(lambda: state["active"] == 1))
            for _ in range(20):
                cache.request_refresh("motor_stop")
            release.set()
            self.assertTrue(wait_until(lambda: state["calls"] >= 2 and not cache.get()["running"]))
            cache.stop()
            self.assertEqual(state["calls"], 2)
            self.assertEqual(state["maximum"], 1)

    def test_status_10_remains_separate_protection_context(self):
        samples = self.samples()
        result = build_250h_diagnosis(
            samples,
            status_events=[{"recorded_at": samples[-2]["recorded_at"], "status": 10}],
            history_complete=True,
        )
        current = result["intervals"][-1]
        self.assertEqual(current["protection_shutdowns"], 1)
        self.assertTrue(any(
            notice["category"] == "protection" and notice["metric"] == "protection_shutdown"
            for notice in result["notices"]
        ))

    def test_exact_interval_boundary_starts_a_separate_running_interval(self):
        result = build_250h_diagnosis(self.samples(hours=250))
        self.assertEqual(len(result["intervals"]), 2)
        self.assertTrue(result["intervals"][0]["complete"])
        self.assertTrue(result["intervals"][1]["current"])
        self.assertEqual(result["intervals"][1]["expected_hours"], 0)

    def test_large_calendar_gap_is_visible_and_not_credited_as_coverage(self):
        result = build_250h_diagnosis(
            self.samples(gap_at=200),
            history_complete=True,
        )
        completed = result["intervals"][0]
        self.assertGreater(completed["gap_count"], 0)
        self.assertFalse(completed["sufficient"])
        self.assertLess(completed["coverage_percent"], 100)
        self.assertFalse(completed["failed_start_reliable"])
        self.assertIsNone(completed["failed_starts"])

    def test_too_few_coherent_points_is_explicitly_unavailable(self):
        result = build_250h_diagnosis([])
        self.assertFalse(result["available"])
        self.assertEqual(result["hint_count"], 0)
        self.assertEqual(result["notices"][0]["category"], "data")

    def test_store_reconstructs_from_compact_snapshots_without_diagnosis_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DachsStore(Path(directory) / "history.db")
            indexes = {item[0]: index for index, item in enumerate(HISTORY_SERIES)}
            now = datetime.now(UTC).replace(second=0, microsecond=0)
            rows = []
            for offset, seconds in ((-30, 4_500_000), (-15, 4_500_900), (0, 4_501_800)):
                values = [None] * len(HISTORY_SERIES)
                for series_id, raw, value in (
                    ("betriebsstunden_gesamt", seconds, seconds / 3600),
                    ("starts", 1000, 1000),
                    ("abgas_motor", 460, 460),
                    ("abgas_hka", 150, 150),
                    ("kuehlwasser", 80, 80),
                    ("dachs_eintritt", 58, 58),
                    ("regler", 32, 32),
                    ("wirkleistung", 5100, 5.1),
                    ("warncode", 0, "Keine aktive Warnung"),
                    ("servicecode", 0, "Kein aktiver Servicecode"),
                ):
                    values[indexes[series_id]] = [raw, value, 1]
                rows.append(((now + timedelta(minutes=offset)).isoformat(), json.dumps({"version": 1, "values": values})))
            with store.database() as db:
                db.executemany(
                    "INSERT INTO history_samples(recorded_at,sample_json) VALUES(?,?)",
                    rows,
                )
            for recorded_at, _payload in rows:
                store.record_adaptive_snapshot(recorded_at, [{
                    "block": 50,
                    "key": "Hka_Ew.usSollGenerator",
                    "label": "Generatornennleistung",
                    "raw": 5000,
                    "value": 5.0,
                    "unit": "kW",
                    "type": "u16",
                }])
            result = store.diagnosis_250h()
            self.assertTrue(result["available"])
            self.assertEqual(len(result["intervals"]), 1)
            self.assertAlmostEqual(result["intervals"][0]["metrics"]["power"], 5.1)
            self.assertEqual(result["intervals"][0]["target_segments"][0]["target_kw"], 5.0)
            self.assertIsNone(result["intervals"][0]["failed_starts"])

    def test_authenticated_api_route_is_base_path_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            init_users(
                directory,
                admin_password="AdminPasswort123",
                guest_password="GastPasswort123",
            )
            app = DachsWebApp(data_dir=directory, interval=60)
            token = app.login("gast", "GastPasswort123")[0]
            app.diagnosis_250h_cache = Diagnosis250hCache(
                Path(directory) / "test-cache.json",
                diagnosis_result,
            )
            server = DachsHTTPServer(("127.0.0.1", 0), app, base_path="/dachs")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection(*server.server_address, timeout=2)
                connection.request(
                    "GET",
                    "/dachs/api/diagnosis/250h",
                    headers={"Cookie": f"open_dachs_session={token}"},
                )
                response = connection.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["schema"], "open-dachs-manager-250h-diagnosis/v1")

                connection = http.client.HTTPConnection(*server.server_address, timeout=2)
                connection.request("GET", "/api/diagnosis/250h")
                response = connection.getresponse()
                response.read()
                connection.close()
                self.assertEqual(response.status, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                app.diagnosis_250h_cache.stop()

    def test_api_returns_pending_immediately_keeps_last_result_on_error_and_never_reads_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            init_users(
                directory,
                admin_password="AdminPasswort123",
                guest_password="GastPasswort123",
            )
            app = DachsWebApp(data_dir=directory, interval=60)
            token = app.login("gast", "GastPasswort123")[0]
            release = threading.Event()
            serial_session = Mock(side_effect=AssertionError("unexpected serial read"))
            app.service.session = serial_session

            def slow_calculation():
                release.wait(1)
                return diagnosis_result("cached result")

            app.diagnosis_250h_cache = Diagnosis250hCache(
                Path(directory) / "api-cache.json",
                slow_calculation,
            )
            server = DachsHTTPServer(("127.0.0.1", 0), app)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def request(path):
                connection = http.client.HTTPConnection(*server.server_address, timeout=2)
                started = time.monotonic()
                connection.request(
                    "GET",
                    path,
                    headers={"Cookie": f"open_dachs_session={token}"},
                )
                response = connection.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
                elapsed = time.monotonic() - started
                connection.close()
                return response.status, payload, elapsed

            try:
                status, pending, elapsed = request("/api/diagnosis/250h")
                self.assertEqual(status, 200)
                self.assertLess(elapsed, 0.25)
                self.assertTrue(pending["pending"])
                self.assertTrue(pending["running"])
                release.set()
                self.assertTrue(wait_until(lambda: app.diagnosis_250h_cache.get()["cache_available"]))

                app.diagnosis_250h_cache.calculate = Mock(
                    side_effect=RuntimeError("history unavailable")
                )
                status, refreshing, elapsed = request("/api/diagnosis/250h?refresh=1")
                self.assertEqual(status, 200)
                self.assertLess(elapsed, 0.25)
                self.assertEqual(refreshing["reason"], "cached result")
                self.assertTrue(wait_until(lambda: app.diagnosis_250h_cache.get()["last_error"] is not None))
                failed = app.diagnosis_250h_cache.get()
                self.assertEqual(failed["reason"], "cached result")
                self.assertTrue(failed["stale"])
                self.assertIn("history unavailable", failed["last_error"])
                serial_session.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                app.diagnosis_250h_cache.stop()

    def test_ui_route_badges_and_source_separation_are_wired(self):
        index = (WEB / "index.html").read_text(encoding="utf-8")
        app = (WEB / "app.js").read_text(encoding="utf-8")
        style = (WEB / "style.css").read_text(encoding="utf-8")
        backend = (ROOT / "src" / "open_dachs_manager" / "web.py").read_text(encoding="utf-8")
        self.assertIn('data-view="diagnosis250View">250-h-Diagnose', index)
        self.assertIn('id="diagnosis250Badge"', index)
        self.assertIn('<section id="diagnosis250View"', index)
        self.assertIn('api(`/api/diagnosis/250h${force ? "?refresh=1" : ""}`)', app)
        self.assertIn('path == "/api/diagnosis/250h"', backend)
        self.assertIn("diagnosis-notice-groups", style)
        self.assertIn("Herstellerhinweise", app)
        self.assertIn("Schutz / Störung", app)
        self.assertIn("Datenqualität", app)
        self.assertIn("dynamischer Sollwert ±150 W", index)
        self.assertIn("Kondenser-Abgas wird nicht angezeigt", index)
        self.assertIn("diagnosis-metric-badge", app)
        self.assertIn("action_count", app)
        self.assertIn("metricNoticeCounts", app)
        self.assertIn("— · nicht verfügbar", app)

    def test_existing_soot_filter_contract_and_placement_stay_unchanged(self):
        index = (WEB / "index.html").read_text(encoding="utf-8")
        app = (WEB / "app.js").read_text(encoding="utf-8")
        backend = (ROOT / "src" / "open_dachs_manager" / "web.py").read_text(encoding="utf-8")
        self.assertEqual(index.count('id="v98-tech-soot-fill"'), 1)
        self.assertEqual(index.count('id="sootFilterSettingsForm"'), 1)
        self.assertLess(index.index('id="sootFilterControls"'), index.index('id="settingsTabs"'))
        self.assertIn("DEFAULT_SOOT_FILTER_ZERO_C = 420.0", backend)
        self.assertIn("DEFAULT_SOOT_FILTER_FULL_C = 520.0", backend)
        self.assertIn('state.sootFilterSettings = await api("/api/settings/soot-filter")', app)
        diagnosis = index[index.index('id="diagnosis250View"'):index.index('id="maintenanceView"')]
        self.assertNotIn('id="v98-tech-soot-fill"', diagnosis)
        self.assertNotIn('id="sootFilterSettingsForm"', diagnosis)

    def test_action_badge_counts_hints_warnings_and_protection_not_data(self):
        samples = self.samples()
        result = build_250h_diagnosis(
            samples,
            status_events=[{"recorded_at": samples[-2]["recorded_at"], "status": 10}],
            history_complete=True,
        )
        categories = [notice["category"] for notice in result["notices"]]
        self.assertIn("hint", categories)
        self.assertIn("protection", categories)
        self.assertTrue(all("metric" in notice for notice in result["notices"]))
        self.assertEqual(
            result["action_count"],
            sum(1 for category in categories if category in {"hint", "warning", "protection"}),
        )
        self.assertEqual(result["hint_count"], result["action_count"])
        self.assertFalse(any(notice["metric"] == "data_quality" for notice in result["notices"] if notice["category"] != "data"))


if __name__ == "__main__":
    unittest.main()
