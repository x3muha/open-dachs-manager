import json
import http.client
import os
import tempfile
import threading
import unittest
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from open_dachs_manager.mapping import PackRepository, WriteAllowlist
from open_dachs_manager.auth import authenticate, calculate_pw4
from open_dachs_manager.service import DachsService, write_json_atomic
from open_dachs_manager.maintenance import (
    MAINTENANCE_CONFIRMATION,
    MAINTENANCE_DEMO_CONFIRMATION,
    checklist_definition,
    maintenance_status,
    report_comparison,
    report_html,
    report_pdf,
    supplemental_definition,
    validate_protocol,
)
from open_dachs_manager.web import (
    DEFAULT_DASHBOARD_SERIES,
    DEFAULT_SLOW_MONITOR_BLOCKS,
    DachsHTTPServer,
    DachsWebApp,
    DachsStore,
    init_users,
    normalize_base_path,
    soot_filter_estimate,
    web_monitor_field_visible,
)
from open_dachs_manager.transport import (
    BlockResult,
    Frame,
    Response,
    SerialSession,
    encode_ack,
    encode_data,
    parse_frame,
)


class FakeSerial:
    def __init__(self):
        self.is_open = True
        self.writes = []
        self.read_queue = deque()

    def write(self, data):
        self.writes.append(bytes(data))
        if not data or data[0] != 0x02:
            return len(data)
        request = parse_frame(bytes(data))
        self.read_queue.append(encode_ack(request))
        if request.payload:
            # Status 0 followed by a small deterministic block payload.
            self.read_queue.append(encode_data(b"\x00\xAA", request.packet))
        return len(data)

    def read(self, _size):
        return self.read_queue.popleft() if self.read_queue else b""

    def close(self):
        self.is_open = False


class NoisySerial(FakeSerial):
    def write(self, data):
        self.writes.append(bytes(data))
        if not data or data[0] != 0x02:
            return len(data)
        request = parse_frame(bytes(data))
        self.read_queue.append(encode_ack(request))
        if request.payload:
            invalid = bytearray(encode_data(b"\x00\xAA", request.packet))
            invalid[-1] ^= 1
            self.read_queue.append(bytes(invalid))
            self.read_queue.append(encode_data(b"\x00\xAA", request.packet))
        return len(data)


class CoreTests(unittest.TestCase):
    def test_generator_target_is_a_slow_polled_writable_startpage_value(self):
        pack = PackRepository(service_codes_file="")
        payload = bytearray(70)
        pack.encode_value(payload, "Hka_Ew.usSollGenerator", "5.2", block=50)

        self.assertEqual(payload[8:10], (5200).to_bytes(2, "little"))
        values = {field.key: field.value for field in pack.decode(50, bytes(payload))}
        self.assertEqual(values["Hka_Ew.usSollGenerator"], 5.2)
        self.assertIn(50, DEFAULT_SLOW_MONITOR_BLOCKS)
        self.assertIn(
            ("wirkleistung_soll", "Wirkleistung Soll", 50, "Hka_Ew.usSollGenerator", "kW", "#d97706"),
            DEFAULT_DASHBOARD_SERIES,
        )
        self.assertTrue(web_monitor_field_visible(50, "Hka_Ew.usSollGenerator"))
        self.assertFalse(web_monitor_field_visible(50, "Hka_Ew.Res[0]"))

    def test_dashboard_generator_target_always_uses_checked_live_write(self):
        class RecordingApp:
            def write_block(self, *args):
                self.args = args
                return {"written": True}

        app = RecordingApp()
        result = DachsWebApp.write_power_target(app, "admin", "5.2", 4, "1234")

        self.assertTrue(result["written"])
        self.assertEqual(app.args, (
            "admin",
            50,
            [{"key": "Hka_Ew.usSollGenerator", "value": "5.2"}],
            4,
            "1234",
            True,
        ))

    def test_only_admin_can_change_guest_password_and_guest_sessions_end(self):
        with tempfile.TemporaryDirectory() as directory:
            init_users(
                directory,
                admin_password="AdminPasswort123",
                guest_password="GastPasswort123",
            )
            app = DachsWebApp(data_dir=directory, interval=60)
            admin_token = app.login("admin", "AdminPasswort123")[0]
            guest_token = app.login("gast", "GastPasswort123")[0]

            with self.assertRaises(PermissionError):
                app.change_password("gast", "GastPasswort123", "EigenesPasswort456")
            with self.assertRaises(PermissionError):
                app.change_guest_password("admin", "falsch", "NeuesGastPasswort456")

            app.change_guest_password("admin", "AdminPasswort123", "NeuesGastPasswort456")

            self.assertIsNone(app.login("gast", "GastPasswort123"))
            self.assertIsNotNone(app.login("gast", "NeuesGastPasswort456"))
            self.assertIsNone(app.session_user(guest_token))
            self.assertEqual(app.session_user(admin_token)["role"], "admin")

    def test_maintenance_test_mode_is_safe_and_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60, maintenance_live_writes=False)
            self.assertEqual(app.maintenance_settings(), {
                "test_mode": True,
                "maintenance_live_writes_enabled": False,
            })

            enabled = app.set_maintenance_test_mode(False)
            self.assertFalse(enabled["test_mode"])
            self.assertTrue(enabled["maintenance_live_writes_enabled"])
            saved = json.loads((Path(directory) / "maintenance_settings.json").read_text())
            self.assertEqual(saved, {"version": 1, "test_mode": False})

            restarted = DachsWebApp(data_dir=directory, interval=60, maintenance_live_writes=False)
            self.assertFalse(restarted.maintenance_settings()["test_mode"])
            restarted.set_maintenance_test_mode(True)

            safe_restart = DachsWebApp(data_dir=directory, interval=60, maintenance_live_writes=True)
            self.assertTrue(safe_restart.maintenance_settings()["test_mode"])
            self.assertFalse(safe_restart.maintenance_settings()["maintenance_live_writes_enabled"])
            with self.assertRaises(ValueError):
                safe_restart.set_maintenance_test_mode("false")

    def test_dashboard_cards_are_validated_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            defaults = app.dashboard_settings()["cards"]
            self.assertGreater(len(defaults), 10)
            self.assertIn({"block": 24, "key": "Hka_Mw1.Temp.sbMotor"}, defaults)

            cards = [
                {"block": 24, "key": "Hka_Mw1.sWirkleistung"},
                {"block": 110, "key": next(iter(app._dashboard_available_keys(110)))},
            ]
            saved = app.set_dashboard_settings(cards)
            self.assertEqual(saved["cards"], cards)
            self.assertEqual(
                json.loads((Path(directory) / "dashboard_settings.json").read_text())["cards"],
                cards,
            )
            restarted = DachsWebApp(data_dir=directory, interval=60)
            self.assertEqual(restarted.dashboard_settings()["cards"], cards)
            with self.assertRaises(KeyError):
                app.set_dashboard_settings([{"block": 24, "key": "Gibt.Es.Nicht"}])

    def test_soot_filter_estimate_uses_requested_curve_and_colours(self):
        self.assertEqual(soot_filter_estimate(419)["percent"], 0)
        self.assertEqual(soot_filter_estimate(479)["level"], "green")
        self.assertEqual(soot_filter_estimate(480), {
            "available": True,
            "percent": 60,
            "level": "orange",
            "source_temperature_c": 480.0,
            "zero_temperature_c": 420.0,
            "full_temperature_c": 520.0,
        })
        self.assertEqual(soot_filter_estimate(509)["level"], "orange")
        self.assertEqual(soot_filter_estimate(510)["level"], "red")
        self.assertEqual(soot_filter_estimate(521)["percent"], 100)
        self.assertFalse(soot_filter_estimate(None)["available"])

    def test_soot_filter_settings_are_safe_persistent_and_feed_live_estimate(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            self.assertEqual(app.soot_filter_settings()["zero_temperature_c"], 420)
            self.assertEqual(app.soot_filter_settings()["full_temperature_c"], 520)
            app.live_values[(24, "Hka_Mw1.Temp.sAbgasMotor")] = {
                "block": 24,
                "key": "Hka_Mw1.Temp.sAbgasMotor",
                "value": 470,
            }
            self.assertEqual(app.live()["soot_filter"]["percent"], 50)

            saved = app.set_soot_filter_settings({
                "zero_temperature_c": 400,
                "full_temperature_c": 500,
            })
            self.assertEqual(saved["source"], "Motorabgastemperatur")
            self.assertEqual(app.live()["soot_filter"]["percent"], 70)
            self.assertEqual(app.live()["soot_filter"]["level"], "orange")
            self.assertEqual(
                json.loads((Path(directory) / "soot_filter_settings.json").read_text()),
                {"version": 1, "zero_temperature_c": 400, "full_temperature_c": 500},
            )
            restarted = DachsWebApp(data_dir=directory, interval=60)
            self.assertEqual(restarted.soot_filter_settings()["full_temperature_c"], 500)
            with self.assertRaisesRegex(ValueError, "mindestens 10"):
                restarted.set_soot_filter_settings({
                    "zero_temperature_c": 500,
                    "full_temperature_c": 505,
                })

    def test_external_service_catalog_resolves_code_163_with_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            catalogue = Path(directory) / "Servicecodes_de.properties"
            catalogue.write_bytes(
                "sc.163=Leistung zu klein\n"
                "sc.163.uc=1028\n"
                "uc.1028=Generatorleistung pruefen\n"
                "uc.1028.mc=2001\n"
                "mc.2001=Messung kontrollieren\n".encode("latin-1")
            )
            pack = PackRepository(service_codes_file=catalogue)
            result = pack.service_catalog("163")
            self.assertTrue(result["available"])
            self.assertGreaterEqual(result["count"], 222)
            self.assertTrue(result["details_available"])
            self.assertEqual(result["items"][0]["text"], "Leistung zu klein")
            self.assertEqual(result["items"][0]["causes"][0]["code"], "1028")
            self.assertEqual(result["items"][0]["measures"][0]["code"], "2001")

            payload = bytearray(70)
            pack.encode_value(payload, "Hka_Bd.bStoerung", "163", block=22)
            field = next(item for item in pack.decode(22, bytes(payload)) if item.key == "Hka_Bd.bStoerung")
            self.assertIn("Leistung zu klein", str(field.value))

    def test_bundled_fault_catalog_resolves_live_error_and_warning(self):
        pack = PackRepository(service_codes_file="")
        result = pack.service_catalog("163")
        self.assertTrue(result["available"])
        self.assertEqual(result["schema"], "open-dachs-manager/fault-catalog/v1")
        self.assertEqual(result["count"], 222)
        self.assertFalse(result["details_available"])
        self.assertEqual(result["items"], [{
            "code": 163,
            "text": "Leistung zu klein",
            "causes": [],
            "measures": [],
        }])

        payload = bytearray(70)
        pack.encode_value(payload, "Hka_Bd.bStoerung", "163", block=22)
        fields = {item.key: item for item in pack.decode(22, bytes(payload))}
        self.assertEqual(fields["Hka_Bd.bStoerung"].value, "SC 163 · Leistung zu klein")

        pack.encode_value(payload, "Hka_Bd.bWarnung", "610", block=22)
        fields = {item.key: item for item in pack.decode(22, bytes(payload))}
        self.assertEqual(fields["Hka_Bd.bWarnung"].value, "WARN 610 · Zusatzbrenner startet nicht")

    def test_configurable_base_path_routes_health_and_static_files(self):
        self.assertEqual(normalize_base_path("dachs/"), "/dachs")
        self.assertEqual(normalize_base_path("/"), "")
        with self.assertRaises(ValueError):
            normalize_base_path("/dachs/../admin")

        app = SimpleNamespace()
        server = DachsHTTPServer(("127.0.0.1", 0), app, base_path="/dachs")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            connection.request("GET", "/dachs/healthz")
            response = connection.getresponse()
            health = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 200)
            self.assertEqual(health["base_path"], "/dachs")
            connection.close()

            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            connection.request("GET", "/dachs/")
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn('content="/dachs"', body)
            self.assertIn('href="static/style.css', body)
            connection.close()

            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 404)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_block18_history_decodes_packed_entries_and_message_id(self):
        pack = PackRepository()
        payload = bytearray(70)
        payload[0] = 1
        payload[10] = 5
        payload[11] = 0x01  # type 0 (Störung), module 1 (Dachs)
        payload[12:16] = (86400).to_bytes(4, "little")
        payload[16] = 7
        payload[17] = 0x20  # type 2 (Wartung nach Betriebsstunden), kein Modul
        payload[18:22] = (172800).to_bytes(4, "little")

        history = pack.meldehist(bytes(payload))
        first, second = history["entries"][:2]

        self.assertEqual(history["current_ring"], 1)
        self.assertTrue(first["active"] is False)
        self.assertTrue(second["active"])
        self.assertEqual(first["timestamp_text"], "02.01.2000 00:00:00 (!)")
        self.assertFalse(first["timestamp_plausible"])
        self.assertEqual(first["type_label"], "Störung")
        self.assertEqual(first["module_label"], "Dachs")
        self.assertEqual(first["message_id"], 105)
        self.assertEqual(first["message"], "Vorlauftemperaturfühler fehlerhaft")
        self.assertIsNone(first["raw_value_label"])
        self.assertEqual(second["message_id"], 14)
        self.assertEqual(pack.field_map(18)["MeldeHIST.bWert[1]"]["offset"], 16)
        self.assertEqual(pack.field_map(18)["MeldeHIST.bMeldecodeTypModul[1]"]["offset"], 17)
        self.assertEqual(pack.field_map(18)["MeldeHIST.ulZeitstempel[1]"]["offset"], 18)

    def test_block18_history_accepts_msr_and_unix_epoch_entries(self):
        pack = PackRepository()
        payload = bytearray(70)
        payload[10] = 120
        payload[11] = 0xA1
        payload[12:16] = (1732429589).to_bytes(4, "little")
        payload[16] = 120
        payload[17] = 0xA1
        payload[18:22] = (787899872).to_bytes(4, "little")

        first, second = pack.meldehist(bytes(payload))["entries"][:2]
        self.assertEqual(first["timestamp_text"], "24.11.2024 06:26:29")
        self.assertEqual(second["timestamp_text"], "19.12.2024 05:04:32")
        self.assertTrue(first["timestamp_plausible"])
        self.assertTrue(second["timestamp_plausible"])

    def test_service_and_warning_histories_keep_status_bytes(self):
        pack = PackRepository()
        block80 = bytes.fromhex(
            "0200000400007400BB6331310002740035B432310000"
            "0200FB274531FF02740020DF0D310002740013B419310002"
            "7400F6541C3100027400E4FC1E3100027400058529310002"
        )
        block82 = bytes.fromhex(
            "7400D8D22A310002FF00139A2B310000FF00C49A2B310000"
            "74000F0D2F31000274001C1930310002E5414C45BE2EE541"
            "BA45BE2ED2419583D52ED241FA16F02ED241E065F62E"
        )
        values80 = {item.key: item.raw for item in pack.decode(80, block80)}
        values82 = {item.key: item.raw for item in pack.decode(82, block82)}
        self.assertEqual(values80["Hka_BZbeiSC_Hist_1L.bStoercode"], 116)
        self.assertEqual(values80["Hka_BZbeiSC_Hist_2L.bStoercode"], 116)
        self.assertEqual(values80["Hka_BZbeiSC_Hist_2L.ulZeitstempel"], 825406517)
        self.assertEqual(values80["Hka_BZbeiSC_Hist_3L.bStoercode"], 2)
        self.assertEqual(values82["Hka_BZbeiSC_Hist_10L.bStoercode"], 255)
        self.assertEqual(values80["Hka_BZbeiSC_Hist_1L.bStatusFlags"], 2)
        self.assertEqual(values82["Hka_BzbeiWarnHist_1L.bWarncode"], 229)
        self.assertEqual(values82["Hka_BzbeiWarnHist_1L.bWarntypModul"], 0x41)
        self.assertEqual(values82["Hka_BzbeiWarnHist_1L.ulZeitstempel"], 784221516)
        self.assertEqual(pack.field_map(80)["Hka_BZbeiSC_Hist_2L.bStoercode"]["offset"], 14)
        self.assertEqual(pack.field_map(82)["Hka_BzbeiWarnHist_1L.bWarncode"]["offset"], 40)
        self.assertEqual(pack.field_map(82)["Hka_BzbeiWarnHist_1L.ulZeitstempel"]["offset"], 42)

        changed80 = bytearray(block80)
        pack.encode_value(changed80, "Hka_BZbeiSC_Hist_2L.bStoercode", "7", block=80)
        self.assertEqual(changed80[14], 7)
        changed82 = bytearray(block82)
        pack.encode_value(changed82, "Hka_BzbeiWarnHist_1L.bWarncode", "9", block=82)
        self.assertEqual(changed82[40], 9)
        pack.encode_value(changed82, "Hka_BzbeiWarnHist_1L.bWarntypModul", "33", block=82, raw_mode=True)
        self.assertEqual(changed82[41], 0x21)

        history = pack.service_history({80: block80, 82: block82})
        self.assertEqual(history["service_ring"], 2)
        self.assertEqual(history["snapshot_ring"], 0)
        self.assertEqual(history["warning_ring"], 0)
        newest = history["services"][0]
        self.assertEqual((newest["slot"], newest["recency"], newest["code"]), (1, 1, 216))
        self.assertEqual(newest["text"], "Spannung über 280 V")
        self.assertTrue(newest["active"])
        self.assertTrue(newest["auto_reset"])
        warning = next(entry for entry in history["warnings"] if entry["slot"] == 1)
        self.assertEqual(warning["code"], 829)
        self.assertEqual(warning["module_label"], "Dachs")
        self.assertEqual(warning["type_label"], "Wartung ausgeführt")

    def test_motor_service_snapshot_uses_sections_and_ring_context(self):
        pack = PackRepository()
        payload = bytearray(70)
        pack.encode_value(payload, "Hka_BZbeiSC_Mw1_1L.bMotorStatus", "32", raw_mode=True, block=84)
        pack.encode_value(payload, "Hka_BZbeiSC_Mw1_1L.usDrehzahl", "1500", raw_mode=True, block=84)
        pack.encode_value(payload, "Hka_BZbeiSC_Mw1_1L.sWirkleistung", "5.3", block=84)
        pack.encode_value(payload, "Hka_BZbeiSC_Mw1_1L.Temp.sbMotor", "80", block=84)
        pack.encode_value(payload, "Hka_BZbeiSC_Mw1_1L.Temp.sbGen", "70", block=84)
        history = {
            "snapshot_ring": 2,
            "services": [{"recency": 1, "slot": 7, "code": 216, "text": "Spannung > 280V", "timestamp_text": "01.08.2026 12:00:00"}],
        }

        snapshot = pack.motor_snapshot(84, bytes(payload), history)
        self.assertEqual(snapshot["slot"], 1)
        self.assertEqual(snapshot["paired_mc_block"], 86)
        self.assertEqual(snapshot["service_context"]["code"], 216)
        by_label = {
            item["label"]: item
            for section in snapshot["sections"]
            for item in section["items"]
        }
        self.assertEqual(by_label["Motordrehzahl"]["value"], 1500)
        self.assertEqual(by_label["Elektrische Generatorleistung"]["value"], 5.3)
        self.assertEqual(by_label["Dachs-Austritt (aus Kühlwasser Motor + 3 K)"]["value"], 83)
        self.assertTrue(by_label["Dachs-Austritt (aus Kühlwasser Motor + 3 K)"]["derived"])

    def test_version_fields_are_grouped_but_expand_back_to_writable_bytes(self):
        pack = PackRepository()
        payload = bytearray(70)
        payload[25:29] = bytes((1, 2, 3, 4))

        fields = pack.display_fields(20, bytes(payload))
        version = next(field for field in fields if field.key == "Hka_Bd_Stat.bSoftwareVersionUeberw")
        self.assertEqual(version.value, "U 12.003.004")
        self.assertEqual(version.metadata["components"], [
            "Hka_Bd_Stat.bSoftwareVersionUeberw[0]",
            "Hka_Bd_Stat.bSoftwareVersionUeberw[1]",
            "Hka_Bd_Stat.bSoftwareVersionUeberw[2]",
            "Hka_Bd_Stat.bSoftwareVersionUeberw[3]",
        ])

        pack.encode_value(payload, version.key, "U 56.007.008", block=20)
        self.assertEqual(bytes(payload[25:29]), bytes((5, 6, 7, 8)))
        pack.encode_value(payload, version.key, "9.10.11.12", block=20)
        self.assertEqual(bytes(payload[25:29]), bytes((9, 10, 11, 12)))
        block, key, metadata = pack.resolve_key("Hka_Bd_Stat.bSoftwareVersionUeberw", 20)
        self.assertEqual((block, key), (20, "Hka_Bd_Stat.bSoftwareVersionUeberw"))
        self.assertEqual(metadata["type"], "version")

    def test_all_version_styles_are_consistent_and_remain_writable(self):
        pack = PackRepository()
        payload = bytearray(70)
        payload[25:29] = bytes((0, 0, 0, 3))
        payload[29:33] = bytes((50, 0, 0, 2))
        payload[33:38] = bytes((5, 0, 48, 48, 4))

        fields = {field.key: field for field in pack.display_fields(20, bytes(payload))}
        self.assertEqual(fields["Hka_Bd_Stat.bSoftwareVersionUeberw"].value, "U 00.000.003")
        self.assertEqual(fields["Hka_Bd_Stat.bSoftwareVersionMessen"].value, "M 0.000.002")
        self.assertEqual(fields["Hka_Bd_Stat.bSoftwareVersionRegler"].value, "R 500.048.004")

        pack.encode_value(payload, "Hka_Bd_Stat.bSoftwareVersionRegler", "R 501.049.005", block=20)
        self.assertEqual(bytes(payload[33:38]), bytes((5, 0, 49, 49, 5)))
        pack.encode_value(payload, "Hka_Bd_Stat.bSoftwareVersionMessen", "M 1.001.003", block=20)
        self.assertEqual(bytes(payload[29:33]), bytes((50, 1, 1, 3)))

    def test_hydraulic_code_is_one_grouped_writable_field(self):
        pack = PackRepository()
        payload = bytearray(70)
        payload[14:18] = bytes((5, 1, 2, 0))

        fields = {field.key: field for field in pack.display_fields(50, bytes(payload))}
        hydraulic = fields["Hka_Ew.HydraulikNr"]
        self.assertEqual(hydraulic.value, "5.1.2.0")
        self.assertEqual(hydraulic.metadata["type"], "hydraulik-code")
        self.assertNotIn("Hka_Ew.HydraulikNr.bSpeicherArt", fields)

        pack.encode_value(payload, hydraulic.key, "1.2.3.4", block=50)
        self.assertEqual(bytes(payload[14:18]), bytes((1, 2, 3, 4)))
        with self.assertRaisesRegex(ValueError, "vier Ziffern"):
            pack.encode_value(payload, hydraulic.key, "1.2.3", block=50)
        with self.assertRaisesRegex(ValueError, "zwischen 0 und 8"):
            pack.encode_value(payload, hydraulic.key, "1.2.3.9", block=50)

    def test_block50_choices_keep_manual_raw_writes_available(self):
        pack = PackRepository()
        payload = bytearray(75)
        payload[2] = 65
        payload[4] = 1
        payload[6] = 2
        payload[12:14] = (1).to_bytes(2, "little")
        payload[48] = 1

        fields = {field.key: field for field in pack.display_fields(50, bytes(payload))}
        self.assertEqual(
            fields["Hka_Ew.uchProgrammwahl"].metadata["choices"],
            [
                {"value": 65, "label": "A"},
                {"value": 66, "label": "B"},
                {"value": 83, "label": "S"},
                {"value": 69, "label": "E"},
            ],
        )
        self.assertEqual(fields["Hka_Ew.uchProgrammwahl"].value, "65 (A)")
        self.assertEqual(fields["Hka_Ew.bMaxlaufzeit"].value, "1 (Ja)")
        self.assertEqual(fields["Hka_Ew.bFuehrungHKA"].value, "2 (Wärme und Strom)")
        self.assertEqual(fields["Hka_Ew.usSprache"].value, "1 (englisch)")
        self.assertIn("SW15", fields["Hka_Ew.bFuehlerAbgasMotorTyp"].value)
        self.assertNotIn("choices", fields["Hka_Ew.bLaenderkonfiguration"].metadata)
        self.assertEqual(fields["Hka_Ew.bMindestlaufzeit"].metadata["min"], 30)
        self.assertEqual(fields["Hka_Ew.bMaxRuecklaufTempHKA"].metadata["max"], 73)
        self.assertEqual(fields["Hka_Ew.usAufstellhoehe"].metadata["step"], 100)
        field_map = pack.field_map(50)
        self.assertEqual(field_map["Hka_Ew.Reserve_2"]["offset"], 31)
        self.assertEqual(field_map["Hka_Ew.ulSystemTime"]["offset"], 36)
        self.assertEqual(field_map["Hka_Ew.bStartverzoegerung"]["offset"], 47)
        self.assertEqual(field_map["Hka_Ew.bFuehlerAbgasMotorTyp"]["offset"], 48)
        self.assertEqual(field_map["Hka_Ew.usFuehlerAbgasMotorBasisRegelung"]["offset"], 50)
        self.assertEqual(field_map["Hka_Ew.Res[15]"]["offset"], 69)

        # Dropdown selections and the manual raw fallback use the same safe
        # numeric write path; no special or restricted enum encoder exists.
        pack.encode_value(payload, "Hka_Ew.uchProgrammwahl", "83", block=50)
        pack.encode_value(payload, "Hka_Ew.bFuehrungHKA", "9", block=50)
        self.assertEqual(payload[2], 83)
        self.assertEqual(payload[6], 9)

    def test_generated_dropdowns_use_the_normal_raw_capable_write_path(self):
        pack = PackRepository()

        heating = bytearray(70)
        key = "Hk_Ew.Heizkreis1.bVorlaufanstieg"
        offset = pack.field_map(70)[key]["offset"]
        pack.encode_value(heating, key, "4", block=70)
        self.assertEqual(heating[offset], 4)
        pack.encode_value(heating, key, "9", block=70)
        self.assertEqual(heating[offset], 9)

        information = bytearray(70)
        part_key = "Hka_Bd_Stat.uchTeilenummer"
        part_offset = pack.field_map(20)[part_key]["offset"]
        pack.encode_value(information, part_key, "4700107000", block=20)
        self.assertEqual(bytes(information[part_offset:part_offset + 10]), b"4700107000")

    def test_mc_layout_and_status_bits_use_reviewed_physical_offsets(self):
        pack = PackRepository()
        payload = bytearray(70)
        payload[20] = 0b00000001
        payload[28] = 0b00000010
        payload[38] = 0b00000011
        payload[46:52] = bytes((7, 8, 9, 10, 1, 2))

        for block in (26, 86, 90, 94):
            fields = pack.field_map(block)
            prefix = "Hka_Mw2" if block == 26 else f"Hka_BZbeiSC_Mw2_{(block - 82) // 4}L"
            self.assertEqual(fields[f"{prefix}.Hka_UC.FlagsMc1[0]"]["offset"], 20)
            self.assertEqual(fields[f"{prefix}.Hka_UC.FlagsMc2[7]"]["offset"], 35)
            self.assertEqual(fields[f"{prefix}.Hka_UC.ubFehlerGrundMc1"]["offset"], 46)
            self.assertEqual(fields[f"{prefix}.Hka_UC.ubSchutzartMc2"]["offset"], 51)

        status = pack.mc_status(bytes(payload))
        self.assertEqual(status["controllers"][0]["error_reason"], 7)
        self.assertEqual(status["controllers"][1]["error_code"], 10)
        self.assertEqual(status["flags"][0]["mc1"]["text"], "OK")
        self.assertEqual(status["flags"][0]["mc2"]["text"], "Fehler")
        self.assertTrue(status["controllers"][0]["actors"][0]["active"])

    def test_motor_measurement_layout_includes_packed_status_bytes(self):
        pack = PackRepository()
        payload = bytearray(70)
        payload[54] = 8
        payload[55] = 17
        payload[56] = 5
        payload[58:62] = (3600).to_bytes(4, "little")

        decoded = {field.key: field for field in pack.decode(24, bytes(payload))}
        self.assertEqual(pack.field_map(24)["Hka_Mw1.bKraftstofftyp"]["offset"], 54)
        self.assertEqual(decoded["Hka_Mw1.bKraftstofftyp"].raw, 8)
        self.assertEqual(decoded["Hka_Mw1.bCodierstecker"].raw, 17)
        self.assertEqual(decoded["Hka_Mw1.bZusatzplatinen"].raw, 5)
        self.assertEqual(decoded["Hka_Mw1.ulMotorlaufsekunden"].raw, 3600)

    def test_oil_refill_history_is_an_array_of_ten_byte_records(self):
        pack = PackRepository()
        payload = bytearray(70)
        for index, (timestamp, seconds, amount) in enumerate(((86400, 3600, 2), (172800, 7200, 3), (259200, 10800, 4))):
            offset = index * 10
            payload[offset:offset + 4] = timestamp.to_bytes(4, "little")
            payload[offset + 4:offset + 8] = seconds.to_bytes(4, "little")
            payload[offset + 8] = amount
        payload[30] = 3

        field_map = pack.field_map(102)
        self.assertEqual(field_map["Wartung_Ew2.aNachfuellOel.ulZeitstempel[2]"]["offset"], 20)
        self.assertEqual(field_map["Wartung_Ew2.aNachfuellOel.ulBetriebssekundenBei[1]"]["offset"], 14)
        self.assertEqual(field_map["Wartung_Ew2.aNachfuellOel.bMenge[2]"]["offset"], 28)
        history = pack.oil_refill_history(bytes(payload))
        self.assertEqual(history["counter"], 3)
        self.assertEqual(history["entries"][1]["operating_hours"], 2.0)
        self.assertEqual(history["entries"][2]["amount"], 4)

        pack.encode_value(payload, "Wartung_Ew2.aNachfuellOel.bMenge[1]", "7", raw_mode=True, block=102)
        self.assertEqual(payload[18], 7)

    def test_run_history_combines_all_four_blocks(self):
        pack = PackRepository()
        p28, p30, p31, p32 = (bytearray(70), bytearray(70), bytearray(14), bytearray(70))
        p28[0] = 0
        p28[9] = 3  # starts for physical ring slot 7
        p30[12] = 0b11000000  # slot 7, 00:00 and 00:15 active (MSB first)
        p30[26:28] = (12).to_bytes(2, "little")
        p30[28:32] = (787899872).to_bytes(4, "little")
        for offset, value in ((0, 7200), (4, 8), (8, 123000), (12, 456000), (16, 789000), (20, 1_500_000)):
            p32[offset:offset + 4] = value.to_bytes(4, "little")

        history = pack.run_history({28: bytes(p28), 30: bytes(p30), 31: bytes(p31), 32: bytes(p32)})
        self.assertTrue(history["available"])
        self.assertEqual(history["days"][1]["ring_slot"], 7)
        self.assertEqual(history["days"][1]["runtime_hours"], 0.5)
        self.assertEqual(history["days"][1]["starts"], 3)
        self.assertEqual(history["summary"]["electric_work_kwh"], 123.0)
        self.assertEqual(history["summary"]["thermal_work_condenser_kwh"], 789.0)
        self.assertEqual(history["shutdowns"][0]["code"], 12)

    def test_service_history_codes_use_bundled_fault_catalogue(self):
        pack = PackRepository()
        payload = bytearray(70)
        payload[0] = 5
        payload[2:6] = (86400).to_bytes(4, "little")

        fields = {field.key: field for field in pack.decode(82, bytes(payload))}
        self.assertEqual(
            fields["Hka_BZbeiSC_Hist_9L.bStoercode"].value,
            "SC 105 · Vorlauftemperaturfühler fehlerhaft",
        )

        pack.encode_value(payload, "Hka_BZbeiSC_Hist_9L.bStoercode", "7", block=82)
        self.assertEqual(payload[0], 7)
        pack.encode_value(payload, "Hka_Bd.bStoerung", "105", block=22)
        self.assertEqual(payload[23], 5)

    def test_multi_module_codes_use_reviewed_offsets(self):
        pack = PackRepository()
        payload = bytearray(70)
        payload[20] = 5
        payload[30] = 10

        fields = {field.key: field for field in pack.decode(34, bytes(payload))}
        self.assertEqual(fields["Mm.ModulDaten.bStoerung[0]"].value, "SC 105 · Vorlauftemperaturfühler fehlerhaft")
        self.assertEqual(fields["Mm.ModulDaten.bWarnung[0]"].value, "WARN 610 · Zusatzbrenner startet nicht")

    def test_decoder_uses_explicit_offsets_for_block_22_variants(self):
        pack = PackRepository()
        values = {item.key: item.raw for item in pack.decode(22, bytes(range(74)))}

        self.assertEqual(values["Hka_Bd.Anforderung.ModulAnzahl"], 44)
        self.assertEqual(values["Hka_Bd.Anforderung.bDauer"], 45)
        self.assertEqual(values["Hka_Bd.Anforderung.Reserve"], 46)
        self.assertEqual(values["Hka_Bd.UStromF_Frei.bFreigabe"], 47)
        self.assertEqual(values["Hka_Bd.sbMittelTempAussen"], 51)

    def test_decoder_and_write_map_use_explicit_offsets_for_block_114(self):
        pack = PackRepository()
        payload = bytearray(range(70))
        values = {item.key: item.raw for item in pack.decode(114, bytes(payload))}

        self.assertEqual(values["Adresse3.aModemTelLegacy"], bytes(range(17)).decode("latin1"))
        self.assertEqual(values["Adresse3.aServiceTel1"], bytes(range(17, 34)).decode("latin1"))
        self.assertEqual(values["Adresse3.aPLZ"], bytes(range(51, 61)).decode("latin1"))
        self.assertEqual(pack.field_map(114)["Adresse3.aModemTelLegacy"]["offset"], 0)

        pack.encode_value(payload, "Adresse3.aModemTelLegacy", "MODEM", block=114)
        self.assertEqual(bytes(payload[:5]), b"MODEM")
        self.assertEqual(bytes(payload[17:34]), bytes(range(17, 34)))

    def test_motor_diagnostic_pad_is_preserved_with_explicit_offsets(self):
        pack = PackRepository()
        payload = bytearray(64)
        payload[0] = 32
        payload[1] = 0xEE  # unmapped DIAGFLAGS byte
        payload[2:4] = (2448).to_bytes(2, "little")
        payload[4:6] = (5200).to_bytes(2, "little", signed=True)

        values = {item.key: item.raw for item in pack.decode(24, bytes(payload))}
        self.assertEqual(values["Hka_Mw1.bMotorStatus"], 32)
        self.assertEqual(values["Hka_Mw1.usDrehzahl"], 2448)
        self.assertEqual(values["Hka_Mw1.sWirkleistung"], 5200)
        self.assertEqual(pack.field_map(24)["Hka_Mw1.usDrehzahl"]["offset"], 2)

    def test_session_advances_packet_numbers_and_does_not_wait_for_sync_timeout(self):
        fake = FakeSerial()
        session = SerialSession(read_timeout=0.001)
        session._serial = fake

        first = session.read_block(20, packet=None, timeout=0.05)
        second = session.read_block(22, packet=None, timeout=0.05)

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(first.packet, 1)
        self.assertEqual(second.packet, 3)
        frames = [parse_frame(item) for item in fake.writes if item and item[0] == 0x02]
        self.assertEqual([frame.packet for frame in frames], [0, 1, 2, 3])

    def test_session_discards_bad_crc_and_accepts_following_valid_frame(self):
        session = SerialSession(read_timeout=0.001)
        session._serial = NoisySerial()
        result = session.read_block(20, packet=None, timeout=0.05)
        self.assertTrue(result.ok)
        self.assertEqual(result.response.crc_errors, 1)

    def test_encode_value_honours_explicit_addressable_legacy_block(self):
        pack = PackRepository()
        key = "Motbel250[4].bQuotientBh_Start"
        block_38 = bytearray(57)

        pack.encode_value(block_38, key, "7", raw_mode=True, block=38)

        self.assertEqual(block_38[56], 7)

    def test_write_service_dry_run_and_allowlist_are_auditable(self):
        service = DachsService("/dev/null", 19200, 0.1, PackRepository())
        before = b"\x01\x02"
        after = b"\x01\x03"
        path = _write_allowlist("field.a")
        try:
            allowlist = WriteAllowlist(path)
            audit = service.write_payload(object(), 20, before, after, ["field.a", "field.b"], allowlist, True)
            self.assertTrue(audit.dry_run)
            self.assertFalse(audit.written)
            self.assertEqual(audit.as_dict()["changed_keys"], ["field.a", "field.b"])

            denied = service.write_payload(object(), 20, before, after, ["field.b"], allowlist, True)
            self.assertIn("not allowlisted", denied.error)
        finally:
            os.unlink(path)

    def test_auth_uses_dynamic_pw4_and_next_packet(self):
        pack = PackRepository()
        block20_payload = bytearray(10)
        block20_payload[:10] = b"1234567890"
        block22_payload = bytearray((123456 * 3600).to_bytes(4, "little"))
        response = Response(
            b"",
            None,
            Frame("data", 4, encode_data(b"\xFE\x05", 4), payload=b"\xFE\x05"),
            1.0,
        )

        class AuthSession:
            def __init__(self):
                self.calls = []

            def read_block(self, block, packet=None, timeout=0.9):
                payload = block20_payload if block == 20 else block22_payload
                frame = Frame("data", 1, encode_data(b"\x00" + bytes(payload), 1), payload=b"\x00" + bytes(payload))
                return BlockResult(block, 1, Response(b"", None, frame, 1.0), 0, bytes(payload))

            def next_packet(self):
                return 4

            def request(self, payload, packet, timeout):
                self.calls.append((payload, packet))
                return response

        session = AuthSession()
        result = authenticate(session, pack, 5)
        self.assertEqual(result.granted_level, 5)
        self.assertEqual(result.pw4, calculate_pw4("1234567890", 123456))
        self.assertEqual(session.calls[0][0], bytes([0x7E]) + result.pw4.encode() + b"\x05")
        self.assertEqual(session.calls[0][1], 4)

    def test_live_write_requires_stable_before_and_confirms_readback(self):
        service = DachsService("/dev/null", 19200, 0.1, PackRepository())
        before = b"\x01\x02"
        after = b"\x01\x03"
        frame = Frame("data", 1, encode_data(b"\x00" + before, 1), payload=b"\x00" + before)
        after_frame = Frame("data", 3, encode_data(b"\x00" + after, 3), payload=b"\x00" + after)
        reads = [
            BlockResult(20, 1, Response(b"", None, frame, 1.0), 0, before),
            BlockResult(20, 3, Response(b"", None, after_frame, 1.0), 0, after),
        ]
        ack = Frame("ack", 2, b"", positive=True)

        class WriteSession:
            def __init__(self):
                self.written = None

            def read_block(self, block, packet=None, timeout=0.9):
                return reads.pop(0)

            def write_block(self, block, payload, packet=None, timeout=0.9):
                self.written = bytes(payload)
                return Response(b"", ack, None, 1.0)

        session = WriteSession()
        audit = service.write_payload(session, 20, before, after, ["field.a"], WriteAllowlist(), False)
        self.assertTrue(audit.written)
        self.assertTrue(audit.readback_ok)
        self.assertTrue(audit.ack_positive)
        self.assertEqual(audit.readback_scope, "block")
        self.assertEqual(audit.readback_attempts, 1)
        self.assertEqual(session.written, after)

    def test_live_write_verifies_changed_field_when_an_unrelated_counter_moves(self):
        pack = PackRepository()
        service = DachsService(
            "/dev/null", 19200, 0.1, pack, readback_attempts=3, readback_delay=0
        )
        before = bytearray(70)
        pack.encode_value(before, "Hka_Ew.usSollGenerator", "5.3", block=50)
        after = bytearray(before)
        pack.encode_value(after, "Hka_Ew.usSollGenerator", "4.7", block=50)
        readback = bytearray(after)
        readback[36:38] = (1234).to_bytes(2, "little")
        ack = Frame("ack", 2, b"", positive=True)
        reads = deque([bytes(before), bytes(readback)])

        class WriteSession:
            def read_block(self, block, packet=None, timeout=0.9):
                payload = reads.popleft()
                frame = Frame("data", 1, b"", payload=b"\x00" + payload)
                return BlockResult(block, 1, Response(b"", None, frame, 1.0), 0, payload)

            def write_block(self, block, payload, packet=None, timeout=0.9):
                return Response(b"", ack, None, 1.0)

        audit = service.write_payload(
            WriteSession(), 50, bytes(before), bytes(after),
            ["Hka_Ew.usSollGenerator"], WriteAllowlist(), False,
        )

        self.assertTrue(audit.written)
        self.assertTrue(audit.readback_ok)
        self.assertTrue(audit.ack_positive)
        self.assertEqual(audit.readback_scope, "changed-fields")
        self.assertEqual(audit.readback_attempts, 1)

    def test_live_write_retries_a_stale_changed_field_readback(self):
        pack = PackRepository()
        service = DachsService(
            "/dev/null", 19200, 0.1, pack, readback_attempts=3, readback_delay=0
        )
        before = bytearray(70)
        pack.encode_value(before, "Hka_Ew.usSollGenerator", "5.3", block=50)
        after = bytearray(before)
        pack.encode_value(after, "Hka_Ew.usSollGenerator", "4.7", block=50)
        ack = Frame("ack", 2, b"", positive=True)
        reads = deque([bytes(before), bytes(before), bytes(after)])

        class WriteSession:
            def read_block(self, block, packet=None, timeout=0.9):
                payload = reads.popleft()
                frame = Frame("data", 1, b"", payload=b"\x00" + payload)
                return BlockResult(block, 1, Response(b"", None, frame, 1.0), 0, payload)

            def write_block(self, block, payload, packet=None, timeout=0.9):
                return Response(b"", ack, None, 1.0)

        audit = service.write_payload(
            WriteSession(), 50, bytes(before), bytes(after),
            ["Hka_Ew.usSollGenerator"], WriteAllowlist(), False,
        )

        self.assertTrue(audit.written)
        self.assertEqual(audit.readback_scope, "block")
        self.assertEqual(audit.readback_attempts, 2)

    def test_live_write_stops_when_block_changed(self):
        service = DachsService("/dev/null", 19200, 0.1, PackRepository())
        frame = Frame("data", 1, encode_data(b"\x00\x09", 1), payload=b"\x00\x09")

        class ChangedSession:
            def read_block(self, block, packet=None, timeout=0.9):
                return BlockResult(20, 1, Response(b"", None, frame, 1.0), 0, b"\x09")

            def write_block(self, *args, **kwargs):
                raise AssertionError("write must not happen after a changed-block check")

        audit = service.write_payload(ChangedSession(), 20, b"\x01", b"\x02", ["field.a"], WriteAllowlist(), False)
        self.assertFalse(audit.written)
        self.assertIn("changed since", audit.error)

    def test_atomic_json_write_replaces_destination_and_leaves_no_tmp(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "nested" / "backup.json"
            write_json_atomic(destination, {"ok": True})
            self.assertEqual(json.loads(destination.read_text()), {"ok": True})
            self.assertEqual(list(destination.parent.glob("*.tmp")), [])

    def test_history_batch_preserves_series_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DachsStore(Path(directory) / "history.db")
            now = datetime.now(timezone.utc)
            store.record(now.isoformat(), [
                {"block": 24, "key": "field.a", "label": "A", "raw": 1, "value": 1, "unit": "°C", "rtt_ms": 1},
                {"block": 24, "key": "field.b", "label": "B", "raw": 2, "value": 2, "unit": "°C", "rtt_ms": 1},
            ])

            rows = store.measurements_batch(
                [("first", 24, "field.a"), ("second", 24, "field.b")],
                now - timedelta(minutes=1),
                now + timedelta(minutes=1),
                100,
            )

            self.assertEqual(rows["first"][0]["field_key"], "field.a")
            self.assertEqual(rows["second"][0]["field_key"], "field.b")

    def test_maintenance_traffic_light_uses_remaining_values(self):
        self.assertEqual(maintenance_status({"Wartung_Cache.sDeltaBh": 800, "Wartung_Cache.sDeltaTage": 90})["level"], "green")
        self.assertEqual(maintenance_status({"Wartung_Cache.sDeltaBh": 199, "Wartung_Cache.sDeltaTage": 60})["level"], "yellow")
        self.assertEqual(maintenance_status({"Wartung_Cache.sDeltaBh": -1, "Wartung_Cache.sDeltaTage": 60})["level"], "red")
        self.assertEqual(maintenance_status({"Wartung_Cache.fStehtAn": True})["level"], "red")
        self.assertIn(104, DEFAULT_SLOW_MONITOR_BLOCKS)
        self.assertTrue(web_monitor_field_visible(104, "Wartung_Cache.sDeltaBh"))
        self.assertFalse(web_monitor_field_visible(104, "Wartung_Cache.Reserve[0]"))

    def test_maintenance_protocol_requires_every_checklist_item_on_completion(self):
        gas_items = checklist_definition("gas")
        protocol = {
            "fuel_type": "gas", "technician": "Service", "notes": "",
            "checklist": {item["id"]: "yes" for item in gas_items}, "measurements": {},
        }
        self.assertEqual(validate_protocol(protocol, complete=True)["technician"], "Service")
        protocol["checklist"].pop(gas_items[0]["id"])
        with self.assertRaisesRegex(ValueError, "nicht bewertete"):
            validate_protocol(protocol, complete=True)

    def test_maintenance_status_choices_match_field_widths(self):
        gas_items = checklist_definition("gas")
        by_id = {item["id"]: item for item in gas_items}
        self.assertEqual(by_id["geraeusch"]["allowed_status"], ["yes", "no", "corrected"])
        self.assertEqual(by_id["kabelbaum"]["allowed_status"], ["yes", "no"])
        protocol = {
            "fuel_type": "gas", "technician": "Service", "notes": "",
            "checklist": {item["id"]: "yes" for item in gas_items}, "measurements": {},
        }
        protocol["checklist"]["geraeusch"] = "corrected"
        self.assertEqual(validate_protocol(protocol, complete=True)["checklist"]["geraeusch"], "corrected")
        protocol["checklist"]["kabelbaum"] = "corrected"
        with self.assertRaisesRegex(ValueError, "unzulässiger Wartungsstatus"):
            validate_protocol(protocol, complete=True)

    def test_packed_maintenance_fields_preserve_neighbor_bits_for_gas_and_oil(self):
        pack = PackRepository()
        packed = bytearray(70)
        packed[32] = 0b10100101
        pack.encode_value(
            packed, "Wartung_Ew1.Dicht_Wart.bKraftstoff_Abgas", "2", raw_mode=True, block=100
        )
        self.assertEqual(packed[32], 0b10101001)
        decoded = {field.key: field.raw for field in pack.decode(100, bytes(packed))}
        self.assertEqual(decoded["Wartung_Ew1.Dicht_Wart.bKraftstoff_Abgas"], 2)
        self.assertEqual(decoded["Wartung_Ew1.Dicht_Wart.bHeizwasser"], 2)
        with self.assertRaisesRegex(ValueError, "does not fit 1-bit"):
            pack.encode_value(
                packed, "Wartung_Ew1.Flags_Allg1.fIsoIntKabelbaum", "2", raw_mode=True, block=100
            )

        oil = bytearray(70)
        oil[36] = 0xA5
        oil[37] = 0x80
        for item in checklist_definition("oil"):
            pack.encode_value(oil, item["controller_key"], "1", raw_mode=True, block=100)
        self.assertEqual(oil[36], 0xA5)
        self.assertEqual(oil[37], 0xFF)

    def test_maintenance_archive_and_pdf_export(self):
        report = {"generated_at": "2026-08-02T12:00:00+00:00", "generated_by": "admin", "blocks": {}, "maintenance_status": {}}
        protocol = {"fuel_type": "gas", "technician": "Firma", "notes": "ok", "checklist": {}, "measurements": {}}
        with tempfile.TemporaryDirectory() as directory:
            store = DachsStore(Path(directory) / "history.db")
            created = store.create_maintenance_report("admin", "gas", report, protocol)
            self.assertEqual(created["status"], "draft")
            self.assertEqual(store.maintenance_reports()[0]["technician"], "Firma")
        pdf = report_pdf(report, protocol)
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertIn(b"xref", pdf)

    def test_open_and_completed_maintenance_reports_can_be_deleted(self):
        report = {"generated_at": "2026-08-08T12:00:00+00:00", "blocks": {}}
        protocol = {
            "fuel_type": "gas", "technician": "Firma", "notes": "ok",
            "checklist": {}, "supplemental": {}, "measurements": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            store = DachsStore(Path(directory) / "history.db")
            draft = store.create_maintenance_report("admin", "gas", report, protocol)
            completed = store.create_maintenance_report("admin", "gas", report, protocol)
            store.complete_maintenance_report(completed["id"], {
                "mode": "demo", "controller_written": False,
            })

            self.assertEqual(store.delete_maintenance_report(draft["id"]), {
                "id": draft["id"], "status": "draft",
            })
            self.assertEqual(store.delete_maintenance_report(completed["id"]), {
                "id": completed["id"], "status": "completed",
            })
            self.assertEqual(store.maintenance_reports(), [])
            with self.assertRaisesRegex(KeyError, "nicht gefunden"):
                store.delete_maintenance_report(completed["id"])

    def test_maintenance_delete_http_endpoint_requires_admin(self):
        class App:
            def __init__(self):
                self.deleted = []

            @staticmethod
            def session_user(token):
                return {
                    "admin-token": {"username": "admin", "role": "admin"},
                    "guest-token": {"username": "gast", "role": "guest"},
                }.get(token)

            def delete_maintenance_report(self, report_id):
                self.deleted.append(report_id)
                return {"id": report_id, "status": "completed"}

        app = App()
        server = DachsHTTPServer(("127.0.0.1", 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(token=None):
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            headers = {"Cookie": f"open_dachs_session={token}"} if token else {}
            connection.request("DELETE", "/api/maintenance/reports/7", headers=headers)
            response = connection.getresponse()
            body = json.loads(response.read().decode("utf-8"))
            connection.close()
            return response.status, body

        try:
            self.assertEqual(request()[0], 401)
            self.assertEqual(request("guest-token")[0], 403)
            status, body = request("admin-token")
            self.assertEqual(status, 200)
            self.assertEqual(body["deleted"], {"id": 7, "status": "completed"})
            self.assertEqual(app.deleted, [7])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_compact_report_uses_three_pages_and_marks_demo_as_non_writing(self):
        report = {
            "generated_at": "2026-08-03T12:00:00+00:00",
            "generated_by": "admin",
            "pack_rev": "50",
            "blocks": {},
            "snapshot": {"attempted_blocks": [20, 22], "captured_blocks": [20, 22], "failed_blocks": []},
            "maintenance_status": {"title": "Wartung im Plan"},
        }
        protocol = {
            "fuel_type": "gas", "technician": "Firma", "notes": "Demotest",
            "checklist": {}, "supplemental": {}, "measurements": {},
        }
        completion = {
            "mode": "demo", "controller_written": False,
            "completed_at": "2026-08-03T12:30:00+00:00", "username": "admin",
        }
        html = report_html(report, protocol, completion)
        self.assertEqual(html.count(b"<section class='page'>"), 3)
        self.assertIn("DEMOLAUF · KEIN REGLERWRITE".encode(), html)
        self.assertIn("Keine Reglerdaten geschrieben".encode(), html)
        pdf = report_pdf(report, protocol, completion)
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertEqual(pdf.count(b"/Type /Page\n"), 3)

    def test_maintenance_start_archives_every_addressable_block_in_one_session(self):
        pack = PackRepository()

        class Service:
            def __init__(self, failed=()):
                self.blocks = []
                self.failed = set(failed)
                self.sessions = 0

            @contextmanager
            def session(self):
                self.sessions += 1
                yield object()

            def decoded_block(self, session, block):
                self.blocks.append(block)
                payload = bytearray(70)
                if block == 24:
                    key = "Hka_Mw1.bKraftstofftyp"
                    payload[pack.field_map(24)[key]["offset"]] = 8
                ok = block not in self.failed
                result = SimpleNamespace(
                    ok=ok,
                    payload=bytes(payload) if ok else b"",
                    status=0 if ok else None,
                    response=SimpleNamespace(elapsed_ms=1.25),
                )
                fields = pack.display_fields(block, bytes(payload)) if ok else []
                return result, fields

        with tempfile.TemporaryDirectory() as directory:
            store = DachsStore(Path(directory) / "history.db")
            service = Service(failed={38})
            app = SimpleNamespace(
                state_lock=threading.Lock(), serial_lock=threading.Lock(), serial_enabled=True,
                service=service, store=store, pack=pack,
            )
            app._report_field = DachsWebApp._report_field
            app.maintenance_report = lambda item_id: DachsWebApp.maintenance_report(app, item_id)

            item = DachsWebApp.create_maintenance_report(app, "admin")

            self.assertEqual(service.sessions, 1)
            self.assertEqual(service.blocks, pack.addressable_blocks())
            self.assertEqual(item["status"], "draft")
            self.assertEqual(item["fuel_type"], "oil")
            self.assertEqual(item["protocol"]["fuel_type"], "oil")
            self.assertEqual(len(item["report"]["blocks"]), len(pack.addressable_blocks()))
            self.assertEqual(item["snapshot"]["captured_blocks"], [block for block in pack.addressable_blocks() if block != 38])
            self.assertEqual(item["snapshot"]["failed_blocks"][0]["block"], 38)
            self.assertFalse(item["snapshot"]["complete"])
            self.assertFalse(item["report"]["blocks"]["38"]["ok"])
            self.assertEqual(store.maintenance_reports()[0]["snapshot"]["attempted_blocks"], pack.addressable_blocks())

    def test_maintenance_start_requires_core_snapshot_blocks(self):
        pack = PackRepository()

        class Service:
            @contextmanager
            def session(self):
                yield object()

            def decoded_block(self, session, block):
                ok = block != 100
                payload = bytes(70) if ok else b""
                return SimpleNamespace(
                    ok=ok,
                    payload=payload,
                    status=0 if ok else None,
                    response=SimpleNamespace(elapsed_ms=1.0),
                ), (pack.display_fields(block, payload) if ok else [])

        with tempfile.TemporaryDirectory() as directory:
            store = DachsStore(Path(directory) / "history.db")
            app = SimpleNamespace(
                state_lock=threading.Lock(), serial_lock=threading.Lock(), serial_enabled=True,
                service=Service(), store=store, pack=pack,
            )
            app._report_field = DachsWebApp._report_field
            with self.assertRaisesRegex(RuntimeError, "Pflichtblöcke fehlen: 100"):
                DachsWebApp.create_maintenance_report(app, "admin")
            self.assertEqual(store.maintenance_reports(), [])

    def test_maintenance_report_comparison_calculates_counter_deltas(self):
        def report(hours, starts):
            return {"blocks": {
                "20": {"fields": []},
                "22": {"fields": [
                    {"key": "Hka_Bd.ulBetriebssekunden", "value": hours},
                    {"key": "Hka_Bd.ulAnzahlStarts", "value": starts},
                ]},
                "104": {"fields": []},
            }}
        comparison = report_comparison(report(120, 10), report(100, 7))
        rows = {row["key"]: row for row in comparison["rows"]}
        self.assertEqual(rows["operating_hours"]["delta"], 20)
        self.assertEqual(rows["starts"]["delta"], 3)

    def test_maintenance_completion_writes_values_then_only_confirmation_bit(self):
        class Audit:
            def __init__(self, block, after, keys):
                self.block, self.after, self.keys = block, after, keys

            def as_dict(self):
                return {"block": self.block, "written": True, "readback_ok": True, "ack_positive": True, "changed_keys": self.keys}

        class Service:
            def __init__(self):
                self.writes = []

            @contextmanager
            def session(self):
                yield object()

            def authenticate(self, session, level, pass4):
                return SimpleNamespace(ok=True, granted_level=level)

            def read_block(self, session, block):
                payload = bytearray(70)
                if block == 100:
                    payload[34] = 0xA8
                    payload[36] = 0xA1
                    payload[37] = 0x5A
                if block == 104:
                    payload[0] = 0xA1
                return SimpleNamespace(ok=True, payload=bytes(payload))

            def write_payload(self, session, block, before, after, changed_keys, allowlist, dry_run):
                self.writes.append((block, bytes(before), bytes(after), tuple(changed_keys), dry_run))
                return Audit(block, after, list(changed_keys))

        with tempfile.TemporaryDirectory() as directory:
            store = DachsStore(Path(directory) / "history.db")
            report = {"blocks": {}, "maintenance_status": {}}
            protocol = {
                "fuel_type": "gas", "technician": "Service", "notes": "fertig",
                "checklist": {item["id"]: "yes" for item in checklist_definition("gas")},
                "measurements": {"Wartung_Ew1.Vorher.bOelstand": "5.0"},
            }
            protocol["checklist"]["geraeusch"] = "corrected"
            protocol["checklist"]["kabelbaum"] = "no"
            report_id = store.create_maintenance_report("admin", "gas", report, protocol)["id"]
            app = SimpleNamespace(
                state_lock=threading.Lock(), serial_lock=threading.Lock(), serial_enabled=True,
                service=Service(), store=store, pack=PackRepository(),
            )
            app.maintenance_report = lambda item_id: DachsWebApp.maintenance_report(app, item_id)
            result = DachsWebApp.complete_maintenance(
                app, "admin", report_id, protocol, 4, "1234", MAINTENANCE_CONFIRMATION,
            )
            self.assertEqual(result["status"], "completed")
            self.assertEqual([item[0] for item in app.service.writes], [100, 104])
            before100, after100, changed100 = app.service.writes[0][1:4]
            self.assertEqual(after100[:7], b"Service")
            self.assertEqual(after100[20], 50)
            self.assertEqual(after100[32], 0x56)
            self.assertEqual(after100[33], 0xFE)
            self.assertEqual(after100[34], 0xAF)
            self.assertEqual(after100[36], 0xAF)
            self.assertEqual(after100[37], 0x5A)
            self.assertEqual(before100[37], after100[37])
            self.assertIn("Wartung_Ew1.Flags_Gas.fZuendkerze", changed100)
            self.assertNotIn("Wartung_Ew1.Flags_Oel.fKraftstofffilter", changed100)
            before104, after104 = app.service.writes[1][1:3]
            self.assertEqual(before104[0], 0xA1)
            self.assertEqual(after104[0], 0xA3)
            self.assertEqual(after104[1:], before104[1:])

    def test_maintenance_demo_completion_never_opens_serial_or_writes(self):
        class NoHardwareService:
            def session(self):
                raise AssertionError("demo completion must not open the serial worker")

        with tempfile.TemporaryDirectory() as directory:
            store = DachsStore(Path(directory) / "history.db")
            protocol = {
                "fuel_type": "gas", "technician": "Demo Service", "notes": "Test",
                "checklist": {item["id"]: "yes" for item in checklist_definition("gas")},
                "supplemental": {item["id"]: "done" for item in supplemental_definition()},
                "measurements": {},
            }
            report_id = store.create_maintenance_report(
                "admin", "gas", {"blocks": {}, "maintenance_status": {}}, protocol,
            )["id"]
            app = SimpleNamespace(store=store, service=NoHardwareService(), pack=PackRepository())
            app.maintenance_report = lambda item_id: DachsWebApp.maintenance_report(app, item_id)
            result = DachsWebApp.complete_maintenance(
                app, "admin", report_id, protocol, -1, "", MAINTENANCE_DEMO_CONFIRMATION,
                demo=True,
            )
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["completion_mode"], "demo")
            self.assertFalse(result["completion"]["controller_written"])
            self.assertFalse(result["completion"]["confirmation_bit_set"])
            self.assertEqual(result["completion"]["audits"], [])


def _write_allowlist(*keys):
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"mode": "explicit", "keys": list(keys)}, handle)
    handle.close()
    # The caller only needs the file during construction of WriteAllowlist.
    return handle.name


if __name__ == "__main__":
    unittest.main()
