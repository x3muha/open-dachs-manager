import json
import http.client
import subprocess
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from open_dachs_manager.service import DachsService
from open_dachs_manager.transport import TransportError
from open_dachs_manager.web import (
    APIRequestConflictError,
    HISTORY_SERIES,
    RUNTIME_24H_REQUEST,
    RUNTIME_24H_WRITE_PREFIX,
    DachsHTTPServer,
    DachsWebApp,
    init_users,
    runtime_24h_write_seconds,
)


def response(payload, positive=True):
    return SimpleNamespace(
        ack=SimpleNamespace(positive=positive),
        data=SimpleNamespace(payload=bytes(payload)),
        elapsed_ms=0.5,
    )


class RuntimeSession:
    def __init__(self, service):
        self.service = service
        self.packet = 0

    def next_packet(self):
        self.packet += 1
        return self.packet

    def request(self, payload, packet=None, **_kwargs):
        payload = bytes(payload)
        self.service.requests.append(payload)
        if payload == RUNTIME_24H_REQUEST:
            value = self.service.current
            if self.service.readback_advance and self.service.written:
                value += self.service.readback_advance
            return response(bytes([0x82, 0x0F, 0x0E, 0x04]) + value.to_bytes(4, "little"))
        if payload.startswith(RUNTIME_24H_WRITE_PREFIX):
            self.service.written = True
            target = int.from_bytes(payload[4:], "little")
            if self.service.write_mode == "negative-ack":
                return response(bytes([0x83, 0x0F, 0x0E, 0x04]), positive=False)
            if self.service.write_mode == "malformed":
                return response(bytes([0x83, 0x0F, 0x0E, 0x05]))
            self.service.current = target
            return response(bytes([0x83, 0x0F, 0x0E, 0x04]))
        raise AssertionError("unexpected request " + payload.hex())


class RuntimeService:
    def __init__(self, current=100):
        self.current = current
        self.requests = []
        self.auth_calls = []
        self.deauth_calls = []
        self.sessions = 0
        self.write_mode = "ok"
        self.readback_advance = 0
        self.written = False
        self.auth_ok = True
        self.granted_level = 3

    @contextmanager
    def session(self):
        self.sessions += 1
        yield RuntimeSession(self)

    def authenticate(self, _session, level, pass4=None):
        self.auth_calls.append((level, pass4))
        return SimpleNamespace(ok=self.auth_ok, granted_level=self.granted_level, pw4=pass4 or "4567")

    def deauthenticate(self, _session, pass4):
        self.deauth_calls.append(pass4)


class RuntimeCounterWriteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.app = DachsWebApp(data_dir=self.temporary.name, interval=60)
        self.service = RuntimeService()
        self.app.service = self.service
        self.app.serial_enabled = True

    def tearDown(self):
        self.temporary.cleanup()

    def write_counter(self, **overrides):
        arguments = dict(
            username="admin", value="00:03:20", expected_counter=100,
            write_enabled=True,
        )
        arguments.update(overrides)
        return self.app.set_runtime_24h_counter(**arguments)

    def test_happy_path_guards_exact_write_readback_deauth_and_audit(self):
        result = self.write_counter()
        self.assertTrue(result["written"] and result["readback_ok"] and result["deauthenticated"])
        self.assertEqual(result["counter_seconds"], 200)
        self.assertEqual(self.service.auth_calls, [(3, None)])
        self.assertEqual(self.service.deauth_calls, ["4567"])
        self.assertEqual(self.service.requests, [
            RUNTIME_24H_REQUEST, RUNTIME_24H_REQUEST,
            RUNTIME_24H_WRITE_PREFIX + (200).to_bytes(4, "little"),
            RUNTIME_24H_REQUEST,
        ])
        self.assertFalse(any(item[:1] == bytes([0x05]) for item in self.service.requests))
        self.assertFalse(any(bytes([0x0E, 0xA0]) in item for item in self.service.requests))
        audit = self.app.store.audits(limit=1)[0]["audit"]
        self.assertEqual(audit["address"], "0x000f0e")
        self.assertFalse(audit["nvm_write"] or audit["flag_write"])
        self.assertNotIn("4567", json.dumps(audit))

    def test_range_expected_value_and_toggle_fail_before_serial(self):
        invalid = (
            {"value": 86370}, {"value": "24:00:00"}, {"value": -1},
            {"expected_counter": "100"}, {"expected_counter": True},
            {"write_enabled": False},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.write_counter(**overrides)
        self.assertEqual(self.service.sessions, 0)
        self.assertEqual(self.app.store.audits(), [])
        self.assertEqual(runtime_24h_write_seconds("23:59:29"), 86369)

    def test_cas_conflict_stops_before_auth_and_write(self):
        self.service.current = 131
        with self.assertRaises(APIRequestConflictError):
            self.write_counter()
        self.assertEqual(self.service.requests, [RUNTIME_24H_REQUEST, RUNTIME_24H_REQUEST])
        self.assertEqual(self.service.auth_calls, [])
        audit = self.app.store.audits(limit=1)[0]["audit"]
        self.assertFalse(audit["write_attempted"])
        self.assertIn("geändert", audit["error"])

    def test_failed_auth_is_deauthenticated_and_never_writes(self):
        self.service.auth_ok = False
        self.service.granted_level = 2
        with self.assertRaises(PermissionError):
            self.write_counter()
        self.assertEqual(self.service.deauth_calls, ["4567"])
        self.assertFalse(any(item.startswith(RUNTIME_24H_WRITE_PREFIX) for item in self.service.requests))

    def test_bad_ack_response_and_readback_all_fail_closed_after_deauth(self):
        for mode in ("negative-ack", "malformed", "readback"):
            with self.subTest(mode=mode):
                self.service = RuntimeService()
                self.app.service = self.service
                if mode == "readback":
                    self.service.readback_advance = 2
                else:
                    self.service.write_mode = mode
                with self.assertRaises(Exception):
                    self.write_counter()
                self.assertEqual(self.service.deauth_calls, ["4567"])
                self.assertTrue(self.app.store.audits(limit=1)[0]["audit"]["error"])

    def test_thirty_second_prewrite_progress_and_one_second_readback_are_accepted(self):
        self.service.current = 130
        self.service.readback_advance = 1
        result = self.write_counter()
        self.assertTrue(result["readback_ok"])
        self.assertEqual(result["counter_seconds"], 201)
        self.assertEqual(result["readback_advanced_seconds"], 1)
        self.assertEqual(result["prewrite_tolerance_seconds"], 30)
        self.assertEqual(result["readback_tolerance_seconds"], 1)

    def test_thirty_one_seconds_and_backward_preimage_are_conflicts_before_auth(self):
        for current in (131, 99):
            with self.subTest(current=current):
                self.service = RuntimeService(current=current)
                self.app.service = self.service
                with self.assertRaises(APIRequestConflictError):
                    self.write_counter()
                self.assertEqual(self.service.auth_calls, [])
                self.assertFalse(any(
                    item.startswith(RUNTIME_24H_WRITE_PREFIX)
                    for item in self.service.requests
                ))
                self.assertFalse(self.app.store.audits(limit=1)[0]["audit"]["write_attempted"])

    def test_target_is_absolute_for_forward_and_backward_writes(self):
        for current, target in ((120, 200), (120, 10)):
            with self.subTest(current=current, target=target):
                self.service = RuntimeService(current=current)
                self.app.service = self.service
                result = self.write_counter(
                    value=target,
                    expected_counter=current,
                )
                self.assertEqual(result["counter_seconds"], target)
                write = next(
                    item for item in self.service.requests
                    if item.startswith(RUNTIME_24H_WRITE_PREFIX)
                )
                self.assertEqual(
                    write,
                    RUNTIME_24H_WRITE_PREFIX + target.to_bytes(4, "little"),
                )


class RuntimeDeauthWireTests(unittest.TestCase):
    def test_deauth_is_level_zero_and_requires_exact_response(self):
        class Session:
            def next_packet(self): return 9
            def request(self, payload, **kwargs):
                self.payload = bytes(payload)
                return response(bytes([0xFE, 0x00]))
        session = Session()
        service = object.__new__(DachsService)
        service.timeout = 0.9
        service.deauthenticate(session, "1234")
        self.assertEqual(session.payload, bytes([0x7E]) + b"1234" + bytes([0]))

    def test_deauth_accepts_exact_password_echo_but_rejects_everything_else_redacted(self):
        class Session:
            def __init__(self, payload): self.response_payload = payload
            def next_packet(self): return 9
            def request(self, payload, **kwargs):
                self.payload = bytes(payload)
                return response(self.response_payload)
        service = object.__new__(DachsService)
        service.timeout = 0.9
        echoed = Session(bytes([0xFE, 0x00]) + b"1234")
        service.deauthenticate(echoed, "1234")
        self.assertEqual(echoed.payload, bytes([0x7E]) + b"1234" + bytes([0]))
        for invalid in (bytes([0xFE, 0x00]) + b"9999", bytes([0xFE, 0x03]) + b"1234", b""):
            with self.subTest(invalid=invalid):
                with self.assertRaises(TransportError) as caught:
                    service.deauthenticate(Session(invalid), "1234")
                message = str(caught.exception)
                self.assertNotIn("1234", message)
                self.assertNotIn("9999", message)


class RuntimeCounterHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        init_users(
            self.temporary.name,
            admin_password="AdminPasswort123",
            guest_password="GastPasswort123",
        )
        self.app = DachsWebApp(data_dir=self.temporary.name, interval=60)
        self.service = RuntimeService()
        self.app.service = self.service
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
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        headers = {"Cookie": f"open_dachs_session={token}"}
        encoded = None
        if body is not None:
            encoded = json.dumps(body)
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=encoded, headers=headers)
        response_ = connection.getresponse()
        result = json.loads(response_.read().decode("utf-8"))
        status = response_.status
        connection.close()
        return status, result

    def test_admin_only_preview_and_write_work_under_configured_base_path(self):
        status, _ = self.request("GET", "/dachs/api/runtime-24h/write-preview", self.guest)
        self.assertEqual(status, 403)
        status, preview = self.request("GET", "/dachs/api/runtime-24h/write-preview", self.admin)
        self.assertEqual(status, 200)
        self.assertEqual(preview["counter_seconds"], 100)
        status, result = self.request(
            "POST", "/dachs/api/runtime-24h/counter", self.admin,
            {
                "value": "00:03:20", "expected_counter": 100,
                "write_enabled": True,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["written"] and result["readback_ok"])

    def test_deprecated_client_auth_fields_are_rejected_before_serial_access(self):
        for field, value in (
            ("auth_level", 3), ("pass4", "1234"), ("confirmation", "text")
        ):
            with self.subTest(field=field):
                before_sessions = self.service.sessions
                status, result = self.request(
                    "POST", "/dachs/api/runtime-24h/counter", self.admin,
                    {
                        "value": 10,
                        "expected_counter": 100,
                        "write_enabled": True,
                        field: value,
                    },
                )
                self.assertEqual(status, 400)
                self.assertIn(field, result["error"])
                self.assertEqual(self.service.sessions, before_sessions)

    def test_removed_automatic_reset_endpoint_and_state_are_gone(self):
        for method, body in (("GET", None), ("POST", {"enabled": False})):
            with self.subTest(method=method):
                status, _ = self.request(
                    method, "/dachs/api/settings/runtime-24h-auto", self.admin, body
                )
                self.assertEqual(status, 404)
        live = self.app.live()
        self.assertNotIn("auto_reset", live["runtime_24h"])
        self.assertEqual(self.service.sessions, 0)


class RemovedRuntimeAutoResetTests(unittest.TestCase):
    def test_leftover_state_file_and_legacy_table_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "runtime_24h_auto_state.json"
            state_text = json.dumps({"version": 1, "enabled": True, "phase": "counting"})
            state_path.write_text(state_text, encoding="utf-8")
            app = DachsWebApp(data_dir=directory, interval=60)
            with app.store.database() as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS runtime_24h_auto_setting_tx(change_id TEXT)"
                )
            app = DachsWebApp(data_dir=directory, interval=60)
            self.assertFalse(hasattr(app, "runtime_24h_auto"))
            self.assertEqual(app.store.audits(), [])
            self.assertEqual(state_path.read_text(encoding="utf-8"), state_text)


class FrequencyHistoryAndUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.app_js = (cls.root / "src/open_dachs_manager/web/app.js").read_text(encoding="utf-8")
        cls.index = (cls.root / "src/open_dachs_manager/web/index.html").read_text(encoding="utf-8")
        cls.web_py = (cls.root / "src/open_dachs_manager/web.py").read_text(encoding="utf-8")

    def test_frequency_field_decode_and_history_use_fast_poll(self):
        frequency = next(item for item in HISTORY_SERIES if item[0] == "frequenz")
        self.assertEqual(frequency[2:5], (26, "Hka_Mw2.Hka_UC.usFrequency1", "Hz"))
        self.assertIn("DEFAULT_FAST_MONITOR_BLOCKS = (22, 24, 26)", self.web_py)
        self.assertIn("DEFAULT_SLOW_MONITOR_BLOCKS = (20, 38, 50, 104)", self.web_py)
        payload = bytearray(70)
        payload[18:20] = (50013).to_bytes(2, "little")
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            field = next(item for item in app.pack.display_fields(26, bytes(payload)) if item.key == frequency[3])
        self.assertEqual((field.raw, field.value, field.unit), (50013, 50.013, "Hz"))

    def test_modal_api_base_path_and_dedicated_frequency_markup(self):
        self.assertIn('id="runtime24hModal"', self.index)
        self.assertIn('data-open-runtime24h', self.app_js)
        self.assertIn('api("/api/runtime-24h/write-preview")', self.app_js)
        self.assertIn('api("/api/runtime-24h/counter"', self.app_js)
        self.assertIn("fetch(appUrl(path)", self.app_js)
        modal = self.index[
            self.index.index('id="runtime24hModal"'):
            self.index.index('id="changelogModal"')
        ]
        self.assertNotIn('runtime24hAuthLevel', modal)
        self.assertNotIn('runtime24hPass4', modal)
        self.assertNotIn('runtime24hConfirmation', modal)
        self.assertNotIn('10-s-Lesezyklus', modal)
        self.assertNotIn('runtime24hExplanation', modal)
        self.assertNotIn('Sicherheitsrelevantes Schreiben', modal)
        self.assertNotIn('0x000f0e', modal)
        self.assertIn('Zielwert wird unverändert', modal)
        write = self.app_js[
            self.app_js.index("async function submitRuntime24hCounter"):
            self.app_js.index("function dashboardCardHtml")
        ]
        self.assertNotIn("auth_level", write)
        self.assertNotIn("pass4", write)
        self.assertNotIn("confirmation", write)
        self.assertIn('id="frequencyChart"', self.index)
        self.assertIn('frequency: ["frequenz"]', self.app_js)
        self.assertIn('group === "frequency" ? frequencyScale(numbers)', self.app_js)
        self.assertIn('? frequencyTicks(leftScale)', self.app_js)
        self.assertIn('BLOCK 26 · 0,75 s', self.index)
        self.assertLess(self.index.index('id="exhaustChart"'), self.index.index('id="frequencyChart"'))

    def test_frequency_scale_ticks_align_with_curve_axis(self):
        self.assertNotIn('preferredMax: 6, includeNegative: true', self.app_js)
        self.assertIn('label: "Wirkleistung (kW)", mobileLabel: "Leistung", preferredMax: 6 }', self.app_js)
        script = r"""
const vm=require('vm'), fs=require('fs');
const sandbox={console,URLSearchParams,Intl,Date,Number,Math,JSON,Set,Map,WeakMap,
 setTimeout:()=>0,clearTimeout:()=>{},setInterval:()=>0,clearInterval:()=>{},
 document:{documentElement:{},getElementById:()=>({}),querySelector:()=>({content:''}),querySelectorAll:()=>[],addEventListener:()=>{}},
 window:{devicePixelRatio:1,matchMedia:()=>({matches:false}),addEventListener:()=>{}},
 getComputedStyle:()=>({getPropertyValue:()=>''})};
vm.createContext(sandbox); vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);
console.log(vm.runInContext('JSON.stringify({frequency:frequencyScale([50.013,50.018]),frequencyTicks:frequencyTicks(frequencyScale([50.013,50.018])),live:frequencyScale([49.925,50.067]),liveTicks:frequencyTicks(frequencyScale([49.925,50.067])),emptyTicks:frequencyTicks(frequencyScale([])),outlier:frequencyScale([49.1,50.013,51.2]),tick:formatAxisTick(50.013,.05,"frequency"),value:chartValueText(50.013,"Hz"),negativeInput:lowerBoundScale([-1.77,4.61],6,false),positive:lowerBoundScale([0,4.61],6,false)})',sandbox));
"""
        completed = subprocess.run(
            ["node", "-e", script, str(self.root / "src/open_dachs_manager/web/app.js")],
            check=True, capture_output=True, text=True,
        )
        result = json.loads(completed.stdout)
        scale = result["frequency"]
        # Keep at least ±0.100 Hz around the data, but align limits and labels
        # to the same exact tick step used by the curve renderer.
        self.assertEqual(scale, {"min": 49.9, "max": 50.15, "step": 0.05})
        self.assertEqual(
            result["frequencyTicks"],
            [50.15, 50.1, 50.05, 50.0, 49.95, 49.9],
        )
        self.assertLessEqual(scale["min"], 50.013 - 0.1)
        self.assertGreaterEqual(scale["max"], 50.018 + 0.1)
        self.assertEqual(result["live"], {"min": 49.8, "max": 50.2, "step": 0.1})
        self.assertEqual(result["liveTicks"], [50.2, 50.1, 50.0, 49.9, 49.8])
        self.assertEqual(result["emptyTicks"], [50.1, 50.05, 50.0, 49.95, 49.9])
        self.assertEqual(result["outlier"], {"min": 49.0, "max": 51.5, "step": 0.5})
        self.assertEqual(result["tick"], "50.013")
        self.assertRegex(result["value"], r"50[,.]013 Hz")
        self.assertEqual(result["negativeInput"]["min"], 0)
        self.assertEqual(result["positive"]["min"], 0)


if __name__ == "__main__":
    unittest.main()
