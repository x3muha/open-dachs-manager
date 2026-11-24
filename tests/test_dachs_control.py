import json
import http.client
import math
import shutil
import subprocess
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from open_dachs_manager.transport import TransportError
from open_dachs_manager.web import (
    APIRequestConflictError,
    DACHS_CONTROL_MANUAL_MODE_REQUEST,
    DACHS_CONTROL_MASK,
    DACHS_CONTROL_READ_REQUEST,
    DACHS_CONTROL_WRITE_PREFIX,
    DACHS_CONTROL_WRITE_RESPONSE,
    DachsHTTPServer,
    DachsWebApp,
    SERVICE_RESET_TEMPERATURE_LIMITS,
    init_users,
)


def wire_response(payload, positive=True):
    return SimpleNamespace(
        ack=SimpleNamespace(positive=positive),
        data=SimpleNamespace(payload=bytes(payload)),
        elapsed_ms=0.5,
    )


class ControlSession:
    def __init__(self, controller):
        self.controller = controller
        self.packet = 0

    def next_packet(self):
        self.packet += 1
        return self.packet

    def request(self, payload, packet=None, **kwargs):
        payload = bytes(payload)
        controller = self.controller
        controller.requests.append(payload)
        if payload == DACHS_CONTROL_READ_REQUEST:
            if controller.read_mode == "negative-ack":
                return wire_response(
                    bytes([0x82, 0x0E, 0xA1, 0x01, controller.control_byte]),
                    positive=False,
                )
            if controller.read_mode == "malformed":
                return wire_response(
                    bytes([0x82, 0x0E, 0xA1, 0x02, controller.control_byte])
                )
            value = controller.control_byte
            if controller.control_read_sequence:
                value = controller.control_read_sequence.pop(0)
            return wire_response(bytes([0x82, 0x0E, 0xA1, 0x01, value]))
        if payload == DACHS_CONTROL_MANUAL_MODE_REQUEST:
            return wire_response(bytes([0x82, 0x1B, 0x5A, 0x01, controller.manual_mode]))
        if payload.startswith(DACHS_CONTROL_WRITE_PREFIX) and len(payload) == 5:
            controller.write_count += 1
            if controller.write_count > 1:
                raise AssertionError("Dachs control wrote more than once")
            desired = payload[4]
            controller.last_desired = desired
            if controller.write_mode == "transport-error":
                raise TransportError("simulated ambiguous write")
            if controller.write_mode != "readback-stuck":
                controller.control_byte = desired
            if controller.write_mode == "negative-ack":
                return wire_response(DACHS_CONTROL_WRITE_RESPONSE, positive=False)
            if controller.write_mode == "bad-echo":
                return wire_response(bytes([0x83, 0x0E, 0xA1, 0x02]))
            return wire_response(DACHS_CONTROL_WRITE_RESPONSE)
        raise AssertionError("unexpected request " + payload.hex())


class ControlController:
    def __init__(self):
        self.serial_socket = "/run/open-dachs-manager/test-serial.sock"
        self.control_byte = 0xBF
        self.control_read_sequence = []
        self.manual_mode = 0
        self.service_code = 0
        self.motor_status = 16
        self.temperatures = {
            "Hka_Mw1.Temp.sbMotor": 65.0,
            "Hka_Mw1.Temp.sbGen": 60.0,
            "Hka_Mw1.Temp.sAbgasMotor": 35.5,
            "Hka_Mw1.Temp.sAbgasHKA": 30.0,
            "Hka_Mw1.Temp.sKapsel": 29.4,
        }
        self.requests = []
        self.decoded_reads = []
        self.auth_calls = []
        self.deauth_calls = []
        self.write_count = 0
        self.sessions = 0
        self.write_mode = "ok"
        self.read_mode = "ok"
        self.auth_ok = True
        self.granted_level = 3
        self.auth_raises = False
        self.deauth_raises = False
        self.control_after_auth = None
        self.last_desired = None

    @contextmanager
    def session(self):
        self.sessions += 1
        yield ControlSession(self)

    def decoded_block(self, _session, block, cpu=0):
        self.decoded_reads.append(block)
        if block == 22:
            release_word = ((self.control_byte & 0xFF) << 8) | 0xFF
            service_text = (
                "Kein aktiver Servicecode"
                if self.service_code == 0
                else f"SC {self.service_code + 100} · Teststörung"
            )
            fields = [
                SimpleNamespace(
                    key="Hka_Bd.bStoerung",
                    raw=self.service_code,
                    value=service_text,
                ),
                SimpleNamespace(
                    key="Hka_Bd.UHka_Frei.usFreigabe",
                    raw=release_word,
                    value="Ja" if self.control_byte & DACHS_CONTROL_MASK else "Nein",
                ),
            ]
        elif block == 24:
            fields = [
                SimpleNamespace(
                    key="Hka_Mw1.bMotorStatus",
                    raw=self.motor_status,
                    value=f"{self.motor_status} (Teststatus)",
                )
            ]
            for key, _label, _minimum, _maximum in SERVICE_RESET_TEMPERATURE_LIMITS:
                value = self.temperatures[key]
                raw = 0 if not math.isfinite(float(value)) else int(round(float(value) * 10))
                fields.append(SimpleNamespace(key=key, raw=raw, value=value))
        else:
            raise AssertionError(f"unexpected decoded block {block}")
        return SimpleNamespace(ok=True, payload=b"", status=0), fields

    def authentication_inputs(self, _session):
        return SimpleNamespace(serial_number="818", operating_hours=2000)

    def authenticate(self, _session, level, pass4=None):
        self.auth_calls.append((level, pass4))
        if self.auth_raises:
            raise TransportError("simulated auth failure with secret 4567")
        if self.control_after_auth is not None:
            self.control_byte = self.control_after_auth
        return SimpleNamespace(ok=self.auth_ok, granted_level=self.granted_level, pw4="4567")

    def deauthenticate(self, _session, pass4):
        self.deauth_calls.append(pass4)
        if self.deauth_raises:
            raise TransportError("simulated cleanup failure with secret 4567")


class DachsControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.app = DachsWebApp(data_dir=self.temporary.name, interval=60)
        self.controller = ControlController()
        self.app.service = self.controller
        self.app.serial_enabled = True

    def tearDown(self):
        self.temporary.cleanup()

    def preview(self, action="on"):
        return self.app.dachs_control_preview("admin", action)

    def apply(self, action="on", token=None):
        if token is None:
            token = self.preview(action)["request_token"]
        return self.app.set_dachs_control("admin", action, True, token)

    def test_preview_double_reads_blocks_and_fixed_ram_without_auth(self):
        preview = self.preview("on")
        self.assertTrue(preview["eligible"])
        self.assertFalse(preview["enabled"])
        self.assertTrue(preview["target_enabled"])
        self.assertEqual(preview["release_byte"], 0xBF)
        self.assertEqual(preview["validated_samples"], 2)
        self.assertTrue(preview["request_token"])
        self.assertEqual(self.controller.decoded_reads, [22, 24, 22, 24])
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(
            self.controller.requests,
            [
                DACHS_CONTROL_READ_REQUEST,
                DACHS_CONTROL_MANUAL_MODE_REQUEST,
                DACHS_CONTROL_READ_REQUEST,
                DACHS_CONTROL_MANUAL_MODE_REQUEST,
            ],
        )

    def test_on_preserves_unrelated_bits_writes_once_and_audits_without_pw4(self):
        self.controller.control_byte = 0x95
        result = self.apply("on")
        self.assertTrue(result["ok"])
        self.assertTrue(result["write_confirmed"])
        self.assertTrue(result["deauthenticated"])
        self.assertEqual(result["before_byte"], 0x95)
        self.assertEqual(result["desired_byte"], 0xD5)
        self.assertEqual(result["after_byte"], 0xD5)
        self.assertEqual(self.controller.write_count, 1)
        self.assertEqual(self.controller.last_desired, 0xD5)
        self.assertEqual(self.controller.auth_calls, [(3, "4567")])
        self.assertEqual(self.controller.deauth_calls, ["4567"])
        audit = self.app.store.audits(1)[0]["audit"]
        self.assertEqual(audit["operation"], "manual-dachs-enable")
        self.assertEqual(audit["changed_keys"], ["CPU0.RAM.0x000ea1.bit6"])
        self.assertNotIn("4567", json.dumps(audit))

    def test_off_is_controlled_stop_and_allowed_while_running_or_faulted(self):
        self.controller.control_byte = 0xFF
        self.controller.motor_status = 32
        self.controller.service_code = 23
        self.controller.temperatures["Hka_Mw1.Temp.sbMotor"] = 120.0
        preview = self.preview("off")
        self.assertTrue(preview["eligible"])
        self.assertIn("kontrollierte Stoppanforderung", preview["semantics"])
        result = self.apply("off", preview["request_token"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["desired_byte"], 0xBF)
        self.assertEqual(result["operation"], "manual-dachs-disable")

    def test_on_blocks_active_fault_running_motor_unsafe_sensor_and_manual_mode(self):
        self.controller.service_code = 23
        self.controller.motor_status = 32
        self.controller.manual_mode = 1
        self.controller.temperatures["Hka_Mw1.Temp.sbMotor"] = 120.0
        preview = self.preview("on")
        self.assertFalse(preview["eligible"])
        self.assertIsNone(preview["request_token"])
        text = " ".join(preview["blockers"])
        self.assertIn("Hand-/Aktortest", text)
        self.assertIn("SC 123", text)
        self.assertIn("Motorstatus 32", text)
        self.assertIn("Kühlwasser Motor", text)
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(self.controller.write_count, 0)

    def test_already_reached_is_noop_without_auth_or_write(self):
        self.controller.control_byte = 0xFF
        preview = self.preview("on")
        self.assertTrue(preview["eligible"])
        self.assertTrue(preview["unchanged"])
        result = self.apply("on", preview["request_token"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["unchanged"])
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(self.controller.write_count, 0)

    def test_block_and_raw_mismatch_fails_before_token_auth_or_write(self):
        self.controller.control_read_sequence = [0xBE]
        with self.assertRaises(TransportError) as caught:
            self.preview("on")
        self.assertIn("verschiedene Zustände", str(caught.exception))
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(self.controller.write_count, 0)

    def test_stale_preimage_and_wrong_action_token_are_rejected(self):
        preview = self.preview("on")
        with self.assertRaises(APIRequestConflictError):
            self.apply("off", preview["request_token"])
        self.assertEqual(self.controller.auth_calls, [])
        newer = self.preview("on")
        self.controller.control_byte = 0xBE
        with self.assertRaises(APIRequestConflictError) as caught:
            self.apply("on", newer["request_token"])
        self.assertIn("Ausgangswert", str(caught.exception))
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(self.controller.write_count, 0)

    def test_change_after_auth_blocks_write_and_still_deauthenticates(self):
        preview = self.preview("on")
        self.controller.control_after_auth = 0xBE
        result = self.apply("on", preview["request_token"])
        self.assertFalse(result["ok"])
        self.assertTrue(result["conflict"])
        self.assertEqual(self.controller.write_count, 0)
        self.assertEqual(self.controller.deauth_calls, ["4567"])

    def test_bad_echo_never_retries_even_when_target_is_reached(self):
        self.controller.write_mode = "bad-echo"
        result = self.apply("on")
        self.assertFalse(result["ok"])
        self.assertTrue(result["target_reached"])
        self.assertFalse(result["write_confirmed"])
        self.assertEqual(self.controller.write_count, 1)
        self.assertEqual(self.controller.deauth_calls, ["4567"])

    def test_readback_mismatch_never_retries(self):
        self.controller.write_mode = "readback-stuck"
        result = self.apply("on")
        self.assertFalse(result["ok"])
        self.assertFalse(result["readback_ok"])
        self.assertEqual(self.controller.write_count, 1)
        self.assertEqual(self.controller.deauth_calls, ["4567"])

    def test_transport_error_and_negative_ack_never_retry(self):
        for mode, target_reached in (("transport-error", False), ("negative-ack", True)):
            with self.subTest(mode=mode):
                self.controller = ControlController()
                self.app.service = self.controller
                self.app._dachs_control_last_attempt = None
                self.app._dachs_control_token = None
                self.controller.write_mode = mode
                result = self.apply("on")
                self.assertFalse(result["ok"])
                self.assertEqual(result["target_reached"], target_reached)
                self.assertEqual(self.controller.write_count, 1)
                self.assertEqual(self.controller.deauth_calls, ["4567"])

    def test_deauth_failure_preserves_confirmed_target_and_redacts_pw4(self):
        self.controller.deauth_raises = True
        result = self.apply("on")
        self.assertFalse(result["ok"])
        self.assertTrue(result["write_confirmed"])
        self.assertTrue(result["target_reached"])
        self.assertFalse(result["deauthenticated"])
        self.assertEqual(self.controller.write_count, 1)
        self.assertNotIn("4567", json.dumps(result))
        self.assertNotIn("4567", json.dumps(self.app.store.audits(1)[0]["audit"]))

    def test_negative_or_malformed_ram_read_fails_closed(self):
        for mode in ("negative-ack", "malformed"):
            with self.subTest(mode=mode):
                self.controller = ControlController()
                self.app.service = self.controller
                self.controller.read_mode = mode
                with self.assertRaises(TransportError):
                    self.preview("on")
                self.assertEqual(self.controller.auth_calls, [])
                self.assertEqual(self.controller.write_count, 0)

    def test_denied_or_ambiguous_auth_never_writes_and_is_cleaned_up(self):
        for mode in ("denied", "raises"):
            with self.subTest(mode=mode):
                self.controller = ControlController()
                self.app.service = self.controller
                if mode == "denied":
                    self.controller.auth_ok = False
                    self.controller.granted_level = 2
                else:
                    self.controller.auth_raises = True
                result = self.apply("on")
                self.assertFalse(result["ok"])
                self.assertEqual(self.controller.write_count, 0)
                self.assertEqual(self.controller.deauth_calls, ["4567"])
                self.assertNotIn("4567", json.dumps(result))

    def test_direct_tty_mode_is_rejected(self):
        self.controller.serial_socket = ""
        with self.assertRaises(TransportError):
            self.preview("on")
        self.assertEqual(self.controller.requests, [])

    def test_ui_and_api_keep_address_auth_and_raw_value_server_side(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "src/open_dachs_manager/web/index.html").read_text(encoding="utf-8")
        app = (root / "src/open_dachs_manager/web/app.js").read_text(encoding="utf-8")
        web = (root / "src/open_dachs_manager/web.py").read_text(encoding="utf-8")
        self.assertIn('id="hmiDachsOn"', html)
        self.assertIn('id="hmiDachsOff"', html)
        self.assertIn('id="dachsControlModal"', html)
        self.assertIn('id="dachsControlStatus" class="write-guard-status neutral" role="status" aria-live="polite"', html)
        self.assertIn('/api/dachs-control/preview?', app)
        self.assertIn('release && release.raw !== null && release.raw !== undefined', app)
        self.assertIn('liveRowMatchesMonitorCycle(release, monitor)', app)
        self.assertIn('Keine frische Block-22-Lesung · Bedienung bleibt gesperrt', app)
        self.assertIn('$("hmiDachsOn").disabled = !releaseKnown || dachsEnabled;', app)
        self.assertIn('$("hmiDachsOff").disabled = !releaseKnown || !dachsEnabled;', app)
        self.assertIn('function trapOpenModalFocus(event)', app)
        self.assertIn('document.querySelectorAll(".modal-backdrop")', app)
        self.assertIn('audit.unchanged && audit.deauthenticated === true', app)
        self.assertIn('set(payload).difference({"action", "confirm", "request_token"})', web)
        post = app[app.index("async function submitDachsControl()") : app.index("function updateServiceResetSubmit()")]
        self.assertNotIn("auth_level:", post)
        self.assertNotIn("0x0ea1", post.lower())
        self.assertNotIn("desired_byte", post)

    @unittest.skipUnless(shutil.which("node"), "Node.js ist für den JS-Regressionscheck nötig")
    def test_live_row_freshness_accepts_a_new_in_progress_cycle_without_accepting_stale_data(self):
        root = Path(__file__).resolve().parents[1]
        app = (root / "src/open_dachs_manager/web/app.js").read_text(encoding="utf-8")
        start = app.index("function liveRowMatchesMonitorCycle(")
        end = app.index("\n}", start) + 2
        helper = app[start:end]
        script = helper + r"""
const active = {enabled:true,running:true,serial_enabled:true,last_cycle:'2026-11-06T10:00:01.000Z'};
const equal = liveRowMatchesMonitorCycle({recorded_at:'2026-11-06T10:00:01.000Z'}, active);
const inProgress = liveRowMatchesMonitorCycle({recorded_at:'2026-11-06T10:00:02.000Z'}, active);
const stale = liveRowMatchesMonitorCycle({recorded_at:'2026-11-06T10:00:00.000Z'}, active);
const stopped = liveRowMatchesMonitorCycle({recorded_at:'2026-11-06T10:00:02.000Z'}, {...active,running:false});
const malformed = liveRowMatchesMonitorCycle({recorded_at:'invalid'}, active);
console.log(JSON.stringify({equal,inProgress,stale,stopped,malformed}));
"""
        completed = subprocess.run(
            ["node", "-e", script], check=True, capture_output=True, text=True
        )
        self.assertEqual(
            json.loads(completed.stdout),
            {"equal": True, "inProgress": True, "stale": False, "stopped": False, "malformed": False},
        )


class DachsControlHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        init_users(
            self.temporary.name,
            admin_password="AdminPasswort123",
            guest_password="GastPasswort123",
        )
        self.app = DachsWebApp(data_dir=self.temporary.name, interval=60)
        self.controller = ControlController()
        self.app.service = self.controller
        self.app.serial_enabled = True
        self.admin = self.app.login("admin", "AdminPasswort123")[0]
        self.guest = self.app.login("gast", "GastPasswort123")[0]
        self.server = DachsHTTPServer(("127.0.0.1", 0), self.app, base_path="/dachs")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, method, path, token, body=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        headers = {"Cookie": f"open_dachs_session={token}"}
        encoded = None
        if body is not None:
            encoded = json.dumps(body)
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=encoded, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        status = response.status
        connection.close()
        return status, payload

    def test_admin_only_base_path_and_fixed_server_side_contract(self):
        status, _body = self.request(
            "GET", "/dachs/api/dachs-control/preview?action=on", self.guest
        )
        self.assertEqual(status, 403)
        status, preview = self.request(
            "GET", "/dachs/api/dachs-control/preview?action=on", self.admin
        )
        self.assertEqual(status, 200)
        self.assertTrue(preview["eligible"])
        sessions = self.controller.sessions
        status, body = self.request(
            "POST",
            "/dachs/api/dachs-control",
            self.admin,
            {
                "action": "on",
                "confirm": True,
                "request_token": preview["request_token"],
                "address": "0x0ea1",
                "value": 0xFF,
                "auth_level": 3,
                "pass4": "1234",
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("address", body["error"])
        self.assertEqual(self.controller.sessions, sessions)
        self.assertEqual(self.controller.write_count, 0)
        status, result = self.request(
            "POST",
            "/dachs/api/dachs-control",
            self.admin,
            {
                "action": "on",
                "confirm": True,
                "request_token": preview["request_token"],
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(self.controller.write_count, 1)

    def test_prewrite_conflict_returns_409_and_deauthenticates_without_write(self):
        _status, preview = self.request(
            "GET", "/dachs/api/dachs-control/preview?action=on", self.admin
        )
        self.controller.control_after_auth = 0xBE
        status, result = self.request(
            "POST",
            "/dachs/api/dachs-control",
            self.admin,
            {
                "action": "on",
                "confirm": True,
                "request_token": preview["request_token"],
            },
        )
        self.assertEqual(status, 409)
        self.assertTrue(result["conflict"])
        self.assertEqual(self.controller.write_count, 0)
        self.assertEqual(self.controller.deauth_calls, ["4567"])

    def test_uncertain_write_token_is_consumed_and_retry_guard_blocks(self):
        self.controller.write_mode = "readback-stuck"
        _status, preview = self.request(
            "GET", "/dachs/api/dachs-control/preview?action=on", self.admin
        )
        body = {
            "action": "on",
            "confirm": True,
            "request_token": preview["request_token"],
        }
        first_status, first = self.request(
            "POST", "/dachs/api/dachs-control", self.admin, body
        )
        second_status, second = self.request(
            "POST", "/dachs/api/dachs-control", self.admin, body
        )
        self.assertEqual(first_status, 502)
        self.assertTrue(first["write_uncertain"])
        self.assertEqual(second_status, 409)
        self.assertIn("bereits verbraucht", second["error"])
        self.assertEqual(self.controller.write_count, 1)
        preview_status, guarded = self.request(
            "GET", "/dachs/api/dachs-control/preview?action=on", self.admin
        )
        self.assertEqual(preview_status, 200)
        self.assertFalse(guarded["eligible"])
        self.assertIsNone(guarded["request_token"])
        self.assertGreater(guarded["retry_guard_remaining_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
