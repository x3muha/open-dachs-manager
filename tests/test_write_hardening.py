import http.client
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from types import SimpleNamespace

from open_dachs_manager.service import DachsService
from open_dachs_manager.web import DachsHTTPServer, DachsWebApp, init_users


# Artificial profile-1 config image; no controller bytes are stored in this test.
SYNTHETIC_NETWORK_CONFIG_PAYLOAD = bytes([1]) + bytes(58)


class HardeningSession:
    def __init__(self, service):
        self.service = service
        self.closed = False

    def write_block(self, block, target, *, packet=None, timeout=None, cpu=0):
        if self.closed:
            raise AssertionError("write on closed fake session")
        target_key = self.service.target_key(cpu, block)
        target = bytes(target)
        self.service.wire_targets.append((cpu, block, target))
        mode = self.service.failure_modes.get(target_key)
        if mode == "negative-ack":
            return SimpleNamespace(ack=SimpleNamespace(positive=False))
        if mode != "readback-mismatch":
            self.service.device_payloads[target_key] = target
        return SimpleNamespace(ack=SimpleNamespace(positive=True))


class HardeningService(DachsService):
    """Hardware-free service that exercises the real write/audit machinery."""

    def __init__(self, pack):
        super().__init__(
            "/definitely-not-a-serial-device",
            19200,
            0.1,
            pack,
            readback_attempts=1,
            readback_delay=0,
        )
        self.device_payloads = {
            24: bytes(70),
            50: bytes(70),
            (1, 20): SYNTHETIC_NETWORK_CONFIG_PAYLOAD,
        }
        self.failure_modes = {}
        self.volatile_time_targets = set()
        self.reset_telemetry()

    @staticmethod
    def target_key(cpu, block):
        return int(block) if int(cpu) == 0 else (int(cpu), int(block))

    def reset_telemetry(self):
        self.session_entries = 0
        self.auth_calls = []
        self.wire_targets = []
        self.read_targets = []
        self.read_counts = {}

    @contextmanager
    def session(self):
        self.session_entries += 1
        session = HardeningSession(self)
        try:
            yield session
        finally:
            session.closed = True

    def read_block(self, session, block, cpu=0):
        if not isinstance(session, HardeningSession) or session.closed:
            raise AssertionError("read outside the fake service session")
        target_key = self.target_key(cpu, block)
        self.read_targets.append((cpu, block))
        self.read_counts[target_key] = self.read_counts.get(target_key, 0) + 1
        payload = self.device_payloads[target_key]
        if target_key in self.volatile_time_targets:
            payload = bytearray(payload)
            payload[36:40] = self.read_counts[target_key].to_bytes(4, "little")
            payload = bytes(payload)
        return SimpleNamespace(
            ok=True,
            status=0x90 | int(block),
            payload=payload,
            response=SimpleNamespace(
                elapsed_ms=0.1,
                crc_errors=0,
                protocol_errors=0,
            ),
        )

    def authenticate(self, session, level, pass4=None):
        if not isinstance(session, HardeningSession) or session.closed:
            raise AssertionError("authentication outside the fake service session")
        self.auth_calls.append((level, pass4))
        return SimpleNamespace(ok=True, granted_level=level)


class WriteHardeningHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        init_users(
            self.temporary.name,
            admin_password="AdminPasswort123",
            guest_password="GastPasswort123",
        )
        self.app = DachsWebApp(data_dir=self.temporary.name, interval=60)
        self.service = HardeningService(self.app.pack)
        self.app.service = self.service
        self.admin_token = self.app.login("admin", "AdminPasswort123")[0]
        self.server = DachsHTTPServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, path, body, *, session=True, api_token=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        headers = {"Content-Type": "application/json"}
        if session:
            headers["Cookie"] = f"open_dachs_session={self.admin_token}"
        if api_token is not None:
            headers["Authorization"] = f"Bearer {api_token}"
        connection.request("POST", path, body=json.dumps(body), headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        status = response.status
        connection.close()
        return status, payload

    def test_string_false_is_rejected_before_state_auth_or_serial(self):
        # Deliberately make the later transport-state guard fail as well. The
        # malformed boolean must still win before that state is consulted.
        with self.app.state_lock:
            self.app.serial_enabled = False
        requests = (
            (
                "/api/block/50",
                {"key": "Hka_Ew.usAufstellhoehe", "value": 0},
            ),
            (
                "/api/network-protection/1/20",
                {"key": "NetzKonfig1.ubSchutzart", "value": 1},
            ),
        )
        for path, change in requests:
            with self.subTest(path=path):
                self.service.reset_telemetry()
                status, result = self.request(
                    path,
                    {
                        "changes": [change],
                        "auth_level": 4,
                        "write_enabled": "false",
                    },
                )
                self.assertEqual(status, 400)
                self.assertIn("true oder false", result["error"])
                self.assertEqual(self.service.session_entries, 0)
                self.assertEqual(self.service.auth_calls, [])
                self.assertEqual(self.service.wire_targets, [])
        self.assertEqual(self.app.store.audits(), [])

    def test_live_noops_skip_controller_auth_and_wire_and_api_reports_ok(self):
        session_requests = (
            (
                "/api/block/50",
                {"key": "Hka_Ew.usAufstellhoehe", "value": 0},
            ),
            (
                "/api/network-protection/1/20",
                {"key": "NetzKonfig1.ubSchutzart", "value": 1},
            ),
        )
        for path, change in session_requests:
            with self.subTest(path=path):
                status, result = self.request(
                    path,
                    {
                        "changes": [change],
                        "auth_level": 4,
                        "pass4": "1234",
                        "write_enabled": True,
                    },
                )
                self.assertEqual(status, 200)
                self.assertFalse(result["dry_run"])
                self.assertFalse(result["written"])
                self.assertFalse(result["write_attempted"])
                self.assertTrue(result["readback_ok"])
                self.assertIsNone(result["error"])

        api_access = self.app.store.create_api_token(
            "admin", "No-op hardening", ["write"]
        )
        self.app.set_api_settings({"write_enabled": True, "auth_level": 4})
        status, result = self.request(
            "/api/v1/actions/set-value",
            {
                "block": 50,
                "key": "Hka_Ew.usAufstellhoehe",
                "value": 0,
                "request_id": "noop-hardening-1",
            },
            session=False,
            api_token=api_access["token"],
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertFalse(result["audit"]["written"])
        self.assertFalse(result["audit"]["write_attempted"])
        self.assertTrue(result["audit"]["readback_ok"])
        self.assertEqual(self.service.auth_calls, [])
        self.assertEqual(self.service.wire_targets, [])
        self.assertEqual(len(self.app.store.audits()), 3)

    def test_overview_power_ignores_browser_auth_fields_and_uses_fresh_pw4(self):
        status, result = self.request(
            "/api/overview/power-target",
            {
                "value": "5.2",
                "auth_level": 0,
                "pass4": "9999",
                "write_enabled": False,
                "block": 20,
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(result["written"])
        self.assertEqual(self.service.auth_calls, [(4, None)])
        self.assertEqual([item[:2] for item in self.service.wire_targets], [(0, 50)])
        self.assertIn((0, 24), self.service.read_targets)
        self.assertEqual(result["auth_level_requested"], 4)
        self.assertEqual(result["auth_level_granted"], 4)

        self.service.failure_modes[50] = "negative-ack"
        status, failed = self.request(
            "/api/overview/power-target",
            {"value": "5.1", "pass4": "1111", "auth_level": 1},
        )
        self.assertEqual(status, 502)
        self.assertFalse(failed["written"])
        self.assertFalse(failed["ack_positive"])
        self.assertIn("positive ACK", failed["error"])

    def test_overview_power_rebases_only_system_time_and_keeps_ack_readback_guards(self):
        self.service.volatile_time_targets.add(50)
        status, result = self.request(
            "/api/overview/power-target",
            {"value": "5.2"},
        )

        self.assertEqual(status, 200)
        self.assertTrue(result["written"])
        self.assertEqual(result["prewrite_scope"], "stable-fields")
        self.assertEqual(
            result["rebased_volatile_keys"],
            ["Hka_Ew.ulSystemTime"],
        )
        self.assertEqual(result["readback_scope"], "changed-fields")
        self.assertEqual(len(self.service.wire_targets), 1)
        wire = self.service.wire_targets[0][2]
        self.assertEqual(wire[8:10], (5200).to_bytes(2, "little"))
        self.assertEqual(wire[36:40], (2).to_bytes(4, "little"))
        self.assertEqual(bytes.fromhex(result["before_hex"])[36:40], wire[36:40])
        self.assertEqual(bytes.fromhex(result["after_hex"]), wire)
        stored = self.app.store.audits(limit=1)[0]["audit"]
        self.assertEqual(stored["prewrite_scope"], "stable-fields")
        self.assertEqual(
            stored["rebased_volatile_keys"],
            ["Hka_Ew.ulSystemTime"],
        )
        self.assertEqual(stored["before_hex"], result["before_hex"])
        self.assertEqual(stored["after_hex"], result["after_hex"])

        self.service.reset_telemetry()
        self.service.failure_modes[50] = "negative-ack"
        status, negative = self.request(
            "/api/overview/power-target",
            {"value": "5.1"},
        )
        self.assertEqual(status, 502)
        self.assertTrue(negative["write_attempted"])
        self.assertFalse(negative["ack_positive"])
        self.assertEqual(negative["prewrite_scope"], "stable-fields")
        self.assertEqual(self.service.read_counts[50], 2)

        self.service.reset_telemetry()
        self.service.failure_modes[50] = "readback-mismatch"
        status, mismatch = self.request(
            "/api/overview/power-target",
            {"value": "5.0"},
        )
        self.assertEqual(status, 502)
        self.assertTrue(mismatch["write_attempted"])
        self.assertTrue(mismatch["ack_positive"])
        self.assertFalse(mismatch["readback_ok"])
        self.assertEqual(mismatch["prewrite_scope"], "stable-fields")
        self.assertIn("readback mismatch", mismatch["error"])

    def test_negative_ack_and_readback_mismatch_are_audited_http_failures(self):
        self.service.failure_modes[(1, 20)] = "negative-ack"
        status, negative = self.request(
            "/api/network-protection/1/20",
            {
                "changes": [
                    {"key": "NetzKonfig1.ubSchutzart", "value": 2}
                ],
                "auth_level": 4,
                "write_enabled": True,
            },
        )
        self.assertEqual(status, 502)
        self.assertFalse(negative["written"])
        self.assertTrue(negative["write_attempted"])
        self.assertFalse(negative["ack_positive"])
        self.assertFalse(negative["readback_ok"])
        self.assertIn("positive ACK", negative["error"])

        self.service.failure_modes[50] = "readback-mismatch"
        status, mismatch = self.request(
            "/api/block/50",
            {
                "changes": [
                    {"key": "Hka_Ew.usAufstellhoehe", "value": 100}
                ],
                "auth_level": 4,
                "write_enabled": True,
            },
        )
        self.assertEqual(status, 502)
        self.assertFalse(mismatch["written"])
        self.assertTrue(mismatch["write_attempted"])
        self.assertTrue(mismatch["ack_positive"])
        self.assertFalse(mismatch["readback_ok"])
        self.assertIn("readback mismatch", mismatch["error"])

        audits = self.app.store.audits()
        self.assertEqual(len(audits), 2)
        self.assertEqual(
            {item["block"]: item["audit"]["error"] for item in audits},
            {20: negative["error"], 50: mismatch["error"]},
        )

    def test_failed_api_write_is_502_idempotent_and_keeps_its_audit(self):
        api_access = self.app.store.create_api_token(
            "admin", "Failure hardening", ["write"]
        )
        self.app.set_api_settings({"write_enabled": True, "auth_level": 4})
        self.service.failure_modes[50] = "readback-mismatch"
        action = {
            "block": 50,
            "key": "Hka_Ew.usAufstellhoehe",
            "value": 100,
            "request_id": "failed-hardening-1",
        }

        status, failed = self.request(
            "/api/v1/actions/set-value",
            action,
            session=False,
            api_token=api_access["token"],
        )
        self.assertEqual(status, 502)
        self.assertFalse(failed["ok"])
        self.assertFalse(failed["audit"]["written"])
        self.assertTrue(failed["audit"]["write_attempted"])
        self.assertFalse(failed["audit"]["readback_ok"])
        self.assertIn("readback mismatch", failed["audit"]["error"])
        wire_count = len(self.service.wire_targets)

        status, replay = self.request(
            "/api/v1/actions/set-value",
            action,
            session=False,
            api_token=api_access["token"],
        )
        self.assertEqual(status, 502)
        self.assertFalse(replay["ok"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(self.service.wire_targets), wire_count)
        self.assertEqual(len(self.app.store.audits()), 1)


if __name__ == "__main__":
    unittest.main()
