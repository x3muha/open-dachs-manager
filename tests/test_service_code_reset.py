import http.client
import json
import math
import os
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from open_dachs_manager.transport import TransportError
from open_dachs_manager.web import (
    APIRequestConflictError,
    DachsHTTPServer,
    DachsWebApp,
    SERVICE_RESET_REQUEST,
    SERVICE_RESET_RESPONSE,
    SERVICE_RESET_TEMPERATURE_LIMITS,
    build_parser,
    init_users,
)


def wire_response(payload, positive=True):
    return SimpleNamespace(
        ack=SimpleNamespace(positive=positive),
        data=SimpleNamespace(payload=bytes(payload)),
        elapsed_ms=0.5,
    )


def assert_secret_redacted(testcase, payload):
    serialized = json.dumps(payload, ensure_ascii=False).lower()
    for marker in ('"pw4"', '"pass4"', '"password"', "secret 4567"):
        testcase.assertNotIn(marker, serialized)


class ResetSession:
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
        if payload != SERVICE_RESET_REQUEST:
            raise AssertionError("unexpected request " + payload.hex())
        controller.write_count += 1
        if controller.write_count > 1:
            raise AssertionError("service reset was written more than once")
        if controller.write_mode == "transport-error":
            raise TransportError("simulated ambiguous write")
        if controller.write_mode == "negative-ack":
            return wire_response(SERVICE_RESET_RESPONSE, positive=False)
        if controller.write_mode != "readback-stuck":
            controller.service_code = 0
        if controller.write_mode == "bad-echo":
            return wire_response(bytes([0x83, 0x1C, 0x9E, 0x02]))
        return wire_response(SERVICE_RESET_RESPONSE)


class ResetController:
    def __init__(self):
        self.serial_socket = "/run/open-dachs-manager/test-serial.sock"
        self.service_code = 23
        self.fault_count = 7
        self.fault_runtime_seconds = 12345
        self.motor_status = 16
        self.temperatures = {
            "Hka_Mw1.Temp.sbMotor": 65.0,
            "Hka_Mw1.Temp.sbGen": 60.0,
            "Hka_Mw1.Temp.sAbgasMotor": 35.5,
            "Hka_Mw1.Temp.sAbgasHKA": 30.0,
            "Hka_Mw1.Temp.sKapsel": 29.4,
        }
        self.temperature_sequences = {}
        self.requests = []
        self.auth_calls = []
        self.deauth_calls = []
        self.decoded_reads = []
        self.sessions = 0
        self.write_count = 0
        self.write_mode = "ok"
        self.auth_ok = True
        self.granted_level = 3
        self.auth_raises = False
        self.deauth_raises = False
        self.motor_after_auth = None
        self.service_after_auth = None
        self.fault_count_after_auth = None
        self.fault_runtime_after_auth = None
        self.service_sequence = []
        self.motor_sequence = []

    @contextmanager
    def session(self):
        self.sessions += 1
        yield ResetSession(self)

    def decoded_block(self, _session, block, cpu=0):
        self.decoded_reads.append(block)
        if block == 22:
            if self.service_sequence:
                self.service_code = self.service_sequence.pop(0)
            raw = self.service_code
            value = "Kein aktiver Servicecode" if raw == 0 else f"SC {raw + 100} · Teststörung"
            fields = [
                SimpleNamespace(key="Hka_Bd.bStoerung", raw=raw, value=value),
                SimpleNamespace(
                    key="Hka_Bd.usAnzahlStoerungenHka",
                    raw=self.fault_count,
                    value=self.fault_count,
                ),
                SimpleNamespace(
                    key="Hka_Bd.ulBetriebssekundenLetzteStoerung",
                    raw=self.fault_runtime_seconds,
                    value=self.fault_runtime_seconds,
                ),
            ]
        elif block == 24:
            if self.motor_sequence:
                self.motor_status = self.motor_sequence.pop(0)
            fields = [SimpleNamespace(
                key="Hka_Mw1.bMotorStatus",
                raw=self.motor_status,
                value=f"{self.motor_status} (Teststatus)",
            )]
            for key, _label, _minimum, _maximum in SERVICE_RESET_TEMPERATURE_LIMITS:
                sequence = self.temperature_sequences.get(key) or []
                value = sequence.pop(0) if sequence else self.temperatures[key]
                if math.isfinite(float(value)):
                    raw = int(round(value * 10)) if key in {
                        "Hka_Mw1.Temp.sAbgasMotor",
                        "Hka_Mw1.Temp.sAbgasHKA",
                        "Hka_Mw1.Temp.sKapsel",
                    } else int(round(value))
                else:
                    raw = 0
                fields.append(SimpleNamespace(key=key, raw=raw, value=value))
        else:
            raise AssertionError(f"unexpected decoded block {block}")
        return SimpleNamespace(ok=True, payload=b"", status=0), fields

    def authenticate(self, _session, level, pass4=None):
        self.auth_calls.append((level, pass4))
        if self.auth_raises:
            raise TransportError("simulated auth failure with secret 4567")
        if self.motor_after_auth is not None:
            self.motor_status = self.motor_after_auth
        if self.service_after_auth is not None:
            self.service_code = self.service_after_auth
        if self.fault_count_after_auth is not None:
            self.fault_count = self.fault_count_after_auth
        if self.fault_runtime_after_auth is not None:
            self.fault_runtime_seconds = self.fault_runtime_after_auth
        return SimpleNamespace(
            ok=self.auth_ok,
            granted_level=self.granted_level,
            pw4="4567",
        )

    def authentication_inputs(self, _session):
        # 818 + 2749 + (2000 / 2) = 4567
        return SimpleNamespace(serial_number="818", operating_hours=2000)

    def deauthenticate(self, _session, pass4):
        self.deauth_calls.append(pass4)
        if self.deauth_raises:
            raise TransportError("simulated cleanup failure with secret 4567")


class ServiceResetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.app = DachsWebApp(
            data_dir=self.temporary.name,
            interval=60,
            service_reset_temperature_policy_approved=True,
        )
        self.controller = ResetController()
        self.app.service = self.controller
        self.app.serial_enabled = True
        self.app._service_reset_wait = lambda _seconds: False

    def tearDown(self):
        self.temporary.cleanup()

    def preview(self):
        return self.app.service_reset_preview("admin")

    def reset(self, request_token=None):
        if request_token is None:
            request_token = self.preview()["request_token"]
        return self.app.reset_service_code(
            "admin", 123, True, request_token
        )

    def test_preview_is_fresh_double_read_without_authentication(self):
        preview = self.preview()
        self.assertTrue(preview["eligible"])
        self.assertEqual(preview["service_code"], 123)
        self.assertEqual(
            preview["fault_event"], {"count": 7, "runtime_seconds": 12345}
        )
        self.assertTrue(preview["temperature_policy_approved"])
        self.assertEqual(preview["motor_status"], 16)
        self.assertEqual(preview["validated_samples"], 2)
        self.assertTrue(preview["request_token"])
        self.assertEqual(self.controller.decoded_reads, [22, 24, 22, 24])
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(self.controller.requests, [])

    def test_temperature_policy_is_fail_closed_until_explicitly_approved(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            controller = ResetController()
            app.service = controller
            app.serial_enabled = True
            preview = app.service_reset_preview("admin")
        self.assertFalse(preview["temperature_policy_approved"])
        self.assertFalse(preview["eligible"])
        self.assertIsNone(preview["request_token"])
        self.assertTrue(
            any("fachlich freigegeben" in item for item in preview["blockers"])
        )
        self.assertEqual(controller.auth_calls, [])
        self.assertEqual(controller.requests, [])

    def test_temperature_policy_process_option_is_explicit_opt_in(self):
        variable = "OPEN_DACHS_SERVICE_RESET_TEMPERATURE_POLICY_APPROVED"
        with patch.dict(os.environ, {variable: "0"}):
            self.assertFalse(
                build_parser().parse_args([]).service_reset_temperature_policy_approved
            )
        with patch.dict(os.environ, {variable: "1"}):
            self.assertTrue(
                build_parser().parse_args([]).service_reset_temperature_policy_approved
            )
        with patch.dict(os.environ, {variable: "0"}):
            self.assertTrue(
                build_parser().parse_args([
                    "--service-reset-temperature-policy-approved"
                ]).service_reset_temperature_policy_approved
            )

    def test_direct_tty_mode_is_rejected_before_opening_a_session(self):
        self.controller.serial_socket = ""
        with self.assertRaises(TransportError) as caught:
            self.preview()
        self.assertIn("Serialworker", str(caught.exception))
        self.assertEqual(self.controller.sessions, 0)
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(self.controller.requests, [])

    def test_serial_disable_while_preview_waits_blocks_session_open(self):
        cleared = threading.Event()
        errors = []
        original_clear = self.app._clear_service_reset_token

        def observed_clear(username):
            original_clear(username)
            cleared.set()

        self.app._clear_service_reset_token = observed_clear
        self.app.serial_lock.acquire()

        def run_preview():
            try:
                self.preview()
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        thread = threading.Thread(target=run_preview)
        try:
            thread.start()
            self.assertTrue(cleared.wait(timeout=2))
            with self.app.state_lock:
                self.app.serial_enabled = False
        finally:
            self.app.serial_lock.release()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], TransportError)
        self.assertIn("getrennt", str(errors[0]))
        self.assertEqual(self.controller.sessions, 0)
        self.assertIsNone(self.app._service_reset_token)

    def test_serial_disable_while_post_waits_blocks_auth_and_write(self):
        preview = self.preview()
        consumed = threading.Event()
        results = []
        errors = []
        original_consume = self.app._consume_service_reset_token

        def observed_consume(username, token, expected_raw):
            preimage = original_consume(username, token, expected_raw)
            consumed.set()
            return preimage

        self.app._consume_service_reset_token = observed_consume
        self.app.serial_lock.acquire()

        def run_reset():
            try:
                results.append(self.reset(preview["request_token"]))
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        thread = threading.Thread(target=run_reset)
        try:
            thread.start()
            self.assertTrue(consumed.wait(timeout=2))
            with self.app.state_lock:
                self.app.serial_enabled = False
        finally:
            self.app.serial_lock.release()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], TransportError)
        self.assertIn("getrennt", str(errors[0]))
        self.assertEqual(self.controller.sessions, 1)
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(self.controller.requests, [])

    def test_both_temperature_samples_must_be_inside_the_safe_range(self):
        key = "Hka_Mw1.Temp.sbMotor"
        self.controller.temperature_sequences[key] = [-0.5, 0.5]
        preview = self.preview()
        self.assertFalse(preview["eligible"])
        self.assertIsNone(preview["request_token"])
        self.assertTrue(any("-0.5/0.5" in item for item in preview["blockers"]))
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(self.controller.requests, [])

    def test_non_finite_temperature_blocks_before_token_auth_or_write(self):
        key = "Hka_Mw1.Temp.sbMotor"
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.controller = ResetController()
                self.controller.temperatures[key] = value
                self.app.service = self.controller
                with self.assertRaises(TransportError) as caught:
                    self.preview()
                self.assertIn("nicht endlich", str(caught.exception))
                self.assertIsNone(self.app._service_reset_token)
                self.assertEqual(self.controller.auth_calls, [])
                self.assertEqual(self.controller.requests, [])

    def test_success_writes_once_requires_exact_echo_readback_deauth_and_audit(self):
        result = self.reset()
        self.assertTrue(result["ok"])
        self.assertTrue(result["write_confirmed"])
        self.assertTrue(result["readback_ok"])
        self.assertTrue(result["deauthenticated"])
        self.assertTrue(result["write_intent_persisted"])
        self.assertTrue(result["write_intent_resolved"])
        self.assertEqual(result["write_intent_resolution"], "write-confirmed")
        self.assertFalse(result["restart_guard_pending"])
        self.assertTrue(result["audit_persisted"])
        self.assertIsNone(self.app.store.pending_service_reset_write_intent())
        with self.app.store.database() as db:
            intent = db.execute(
                "SELECT status,resolution,details_json FROM "
                "service_reset_write_intents WHERE intent_id=?",
                (result["write_intent_id"],),
            ).fetchone()
        self.assertEqual(intent["status"], "resolved")
        self.assertEqual(intent["resolution"], "write-confirmed")
        intent_details = json.loads(intent["details_json"])
        self.assertTrue(intent_details["write_confirmed"])
        self.assertTrue(intent_details["deauthenticated"])
        self.assertFalse(intent_details["cleanup_uncertain"])
        self.assertEqual(result["service_code_before"], 123)
        self.assertEqual(result["service_code_after"], 0)
        self.assertEqual(result["readback_codes_raw"], [0, 0])
        self.assertEqual(self.controller.requests, [SERVICE_RESET_REQUEST])
        self.assertEqual(self.controller.write_count, 1)
        self.assertEqual(self.controller.auth_calls, [(3, "4567")])
        self.assertEqual(self.controller.deauth_calls, ["4567"])
        audit = self.app.store.audits(limit=1)[0]["audit"]
        self.assertEqual(audit["operation"], "manual-servicecode-reset")
        self.assertEqual(audit["address"], "0x001c9e")
        self.assertTrue(audit["volatile_ram_only"])
        self.assertFalse(audit["nvm_write"])
        assert_secret_redacted(self, audit)

    def test_atomic_intent_and_audit_failure_rolls_back_and_keeps_guard(self):
        with self.app.store.database() as db:
            db.execute(
                "CREATE TRIGGER fail_service_reset_audit BEFORE INSERT ON write_audit "
                "BEGIN SELECT RAISE(ABORT, 'simulated audit failure'); END"
            )
        self.controller.deauth_raises = True
        result = self.reset()
        self.assertFalse(result["ok"])
        self.assertIn("Auth-0-Cleanup", result["error"])
        self.assertIn("Pflichtaudit", result["error"])
        self.assertTrue(result["write_confirmed"])
        self.assertTrue(result["restart_guard_pending"])
        self.assertFalse(result["write_intent_resolved"])
        self.assertFalse(result["audit_persisted"])
        self.assertTrue(result["uncertain"])
        self.assertEqual(self.controller.write_count, 1)
        pending = self.app.store.pending_service_reset_write_intent()
        self.assertIsNotNone(pending)
        self.assertEqual(self.app.store.audits(limit=10), [])
        assert_secret_redacted(self, result)

    def test_missing_fault_is_idempotent_without_auth_or_write(self):
        request_token = self.preview()["request_token"]
        self.controller.service_code = 0
        result = self.reset(request_token)
        self.assertTrue(result["ok"] and result["unchanged"])
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(self.controller.requests, [])
        self.assertEqual(self.controller.deauth_calls, [])

    def test_running_motor_and_unsafe_temperature_block_before_auth(self):
        for mutate, expected in (
            (lambda: setattr(self.controller, "motor_status", 31), "Motorstatus 31"),
            (lambda: self.controller.temperatures.__setitem__("Hka_Mw1.Temp.sAbgasHKA", 171.0), "Dachs-Abgas"),
        ):
            with self.subTest(expected=expected):
                self.controller = ResetController()
                self.app.service = self.controller
                request_token = self.preview()["request_token"]
                mutate()
                with self.assertRaises(APIRequestConflictError) as caught:
                    self.reset(request_token)
                self.assertIn(expected, str(caught.exception))
                self.assertEqual(self.controller.auth_calls, [])
                self.assertEqual(self.controller.requests, [])

    def test_stale_preview_is_never_used_for_post(self):
        preview = self.preview()
        self.assertTrue(preview["eligible"])
        # Cached UI state remains safe, but the direct POST re-read sees that
        # the motor has started and therefore cannot authenticate or write.
        self.controller.motor_status = 31
        with self.assertRaises(APIRequestConflictError):
            self.reset(preview["request_token"])
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(self.controller.write_count, 0)

    def test_same_service_code_with_new_fault_event_invalidates_preview(self):
        preview = self.preview()
        self.controller.fault_count += 1
        with self.assertRaises(APIRequestConflictError) as caught:
            self.reset(preview["request_token"])
        self.assertIn("Störereignis änderte sich", str(caught.exception))
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(self.controller.write_count, 0)

    def test_incoherent_direct_read_blocks_as_unfresh(self):
        self.controller.motor_sequence = [16, 31]
        with self.assertRaises(TransportError) as caught:
            self.preview()
        self.assertIn("änderte sich", str(caught.exception))
        self.assertEqual(self.controller.auth_calls, [])

    def test_slow_double_read_is_not_treated_as_fresh(self):
        with self.controller.session() as session:
            with patch(
                "open_dachs_manager.web.time.monotonic", side_effect=[10.0, 20.001]
            ):
                with self.assertRaises(TransportError) as caught:
                    self.app._service_reset_state(session)
        self.assertIn("dauerte zu lange", str(caught.exception))
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(self.controller.requests, [])

    def test_token_expiring_while_waiting_for_serial_lock_blocks_before_auth(self):
        preview = self.preview()
        consumed = threading.Event()
        errors = []
        original_consume = self.app._consume_service_reset_token

        def consume_with_short_deadline(*args, **kwargs):
            preimage = original_consume(*args, **kwargs)
            preimage["expires_at"] = time.monotonic() + 0.05
            consumed.set()
            return preimage

        def worker():
            try:
                self.reset(preview["request_token"])
            except Exception as exc:
                errors.append(exc)

        with patch.object(
            self.app, "_consume_service_reset_token", consume_with_short_deadline
        ):
            with self.app.serial_lock:
                thread = threading.Thread(target=worker)
                thread.start()
                self.assertTrue(consumed.wait(timeout=2))
                time.sleep(0.08)
            thread.join(timeout=2)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], APIRequestConflictError)
        self.assertIn("Serialworker", str(errors[0]))
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(self.controller.requests, [])

    def test_token_expiring_during_intent_commit_aborts_before_auth(self):
        preview = self.preview()
        issued_expires = float(self.app._service_reset_token["expires_at"])
        clock = {"now": issued_expires - 10.0}
        original_consume = self.app._consume_service_reset_token
        original_begin = self.app.store.begin_service_reset_write_intent

        def consume_with_deadline(*args, **kwargs):
            preimage = original_consume(*args, **kwargs)
            preimage["expires_at"] = clock["now"] + 1.0
            return preimage

        def begin_then_expire(*args, **kwargs):
            original_begin(*args, **kwargs)
            clock["now"] += 2.0

        with patch(
            "open_dachs_manager.web.time.monotonic",
            side_effect=lambda: clock["now"],
        ), patch.object(
            self.app,
            "_consume_service_reset_token",
            side_effect=consume_with_deadline,
        ), patch.object(
            self.app.store,
            "begin_service_reset_write_intent",
            side_effect=begin_then_expire,
        ):
            result = self.reset(preview["request_token"])
        self.assertFalse(result["ok"])
        self.assertIn("vor der Authentifizierung", result["error"])
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(self.controller.write_count, 0)
        self.assertTrue(result["write_intent_resolved"])
        self.assertEqual(result["write_intent_resolution"], "aborted-before-write")
        self.assertFalse(result["restart_guard_pending"])
        self.assertTrue(result["audit_persisted"])
        self.assertIsNone(self.app.store.pending_service_reset_write_intent())

    def test_state_change_during_intent_commit_is_rechecked_before_write(self):
        original_begin = self.app.store.begin_service_reset_write_intent

        def begin_then_start_motor(*args, **kwargs):
            original_begin(*args, **kwargs)
            self.controller.motor_status = 31

        with patch.object(
            self.app.store,
            "begin_service_reset_write_intent",
            side_effect=begin_then_start_motor,
        ):
            result = self.reset()
        self.assertFalse(result["ok"])
        self.assertIn("Motorstatus 31", result["error"])
        self.assertEqual(self.controller.write_count, 0)
        self.assertTrue(result["write_intent_resolved"])
        self.assertEqual(result["write_intent_resolution"], "aborted-before-write")
        self.assertFalse(result["restart_guard_pending"])
        self.assertTrue(result["audit_persisted"])
        self.assertIsNone(self.app.store.pending_service_reset_write_intent())
        self.assertEqual(self.controller.deauth_calls, ["4567"])

    def test_expired_confirmation_immediately_before_write_sends_no_frame(self):
        original = self.app._require_service_reset_token_fresh

        def expire_only_before_write(preimage, stage):
            if stage == "unmittelbar vor dem Write":
                raise APIRequestConflictError("simulated token expiry before write")
            return original(preimage, stage)

        with patch.object(
            self.app,
            "_require_service_reset_token_fresh",
            side_effect=expire_only_before_write,
        ):
            result = self.reset()
        self.assertFalse(result["ok"])
        self.assertIn("token expiry", result["error"])
        self.assertEqual(self.controller.write_count, 0)
        self.assertEqual(self.controller.auth_calls, [(3, "4567")])
        self.assertEqual(self.controller.deauth_calls, ["4567"])
        self.assertTrue(result["write_intent_resolved"])
        self.assertFalse(result["restart_guard_pending"])

    def test_prewrite_age_boundary_is_one_second(self):
        with patch("open_dachs_manager.web.time.monotonic", return_value=11.0):
            self.assertEqual(
                self.app._require_service_reset_prewrite_fresh(10.0), 1.0
            )
        with patch("open_dachs_manager.web.time.monotonic", return_value=11.0001):
            with self.assertRaises(APIRequestConflictError):
                self.app._require_service_reset_prewrite_fresh(10.0)

    def test_expired_prewrite_age_sends_no_frame(self):
        with patch.object(
            self.app,
            "_require_service_reset_prewrite_fresh",
            side_effect=APIRequestConflictError("simulated stale prewrite"),
        ):
            result = self.reset()
        self.assertFalse(result["ok"])
        self.assertIn("stale prewrite", result["error"])
        self.assertEqual(self.controller.write_count, 0)
        self.assertTrue(result["write_intent_resolved"])
        self.assertFalse(result["restart_guard_pending"])

    def test_expired_one_shot_token_blocks_before_auth_or_write(self):
        preview = self.preview()
        self.app._service_reset_token["expires_at"] = time.monotonic() - 1.0
        with self.assertRaises(APIRequestConflictError):
            self.reset(preview["request_token"])
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(self.controller.requests, [])

    def test_token_issued_before_another_write_attempt_cannot_repeat_it(self):
        # Models the narrow race in which a second preview finishes after the
        # first POST consumed its token but before that POST reaches the write.
        preview = self.preview()
        self.app._record_service_reset_attempt(23)
        with self.assertRaises(APIRequestConflictError) as caught:
            self.reset(preview["request_token"])
        self.assertIn("nicht automatisch wiederholt", str(caught.exception))
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(self.controller.requests, [])

    def test_stale_token_does_not_consume_a_newer_admin_preview(self):
        first = self.preview()
        second = self.app.service_reset_preview("second-admin")
        with self.assertRaises(APIRequestConflictError):
            self.reset(first["request_token"])
        result = self.app.reset_service_code(
            "second-admin", 123, True, second["request_token"]
        )
        self.assertTrue(result["ok"])
        self.assertEqual(self.controller.write_count, 1)

    def test_overlapping_previews_leave_only_the_newest_token_valid(self):
        first_after_serial = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        call_lock = threading.Lock()
        calls = 0
        previews = {}

        def guarded_remaining(_service_code_raw):
            nonlocal calls
            with call_lock:
                calls += 1
                current = calls
            if current == 1:
                first_after_serial.set()
                self.assertTrue(release_first.wait(timeout=2))
            return 0.0

        self.app._service_reset_retry_guard_remaining = guarded_remaining

        first_thread = threading.Thread(
            target=lambda: previews.__setitem__("first", self.preview())
        )

        def second_preview():
            second_started.set()
            previews["second"] = self.preview()

        second_thread = threading.Thread(target=second_preview)
        first_thread.start()
        self.assertTrue(first_after_serial.wait(timeout=2))
        second_thread.start()
        self.assertTrue(second_started.wait(timeout=2))
        release_first.set()
        first_thread.join(timeout=2)
        second_thread.join(timeout=2)
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        first_token = previews["first"]["request_token"]
        second_token = previews["second"]["request_token"]
        self.assertNotEqual(first_token, second_token)
        self.assertEqual(self.app._service_reset_token["token"], second_token)
        with self.assertRaises(APIRequestConflictError):
            self.reset(first_token)
        result = self.reset(second_token)
        self.assertTrue(result["ok"])
        self.assertEqual(self.controller.write_count, 1)

    def test_denied_auth_is_cleaned_up_and_never_writes(self):
        self.controller.auth_ok = False
        self.controller.granted_level = 2
        result = self.reset()
        self.assertFalse(result["ok"])
        self.assertIn("nicht exakt", result["error"])
        self.assertEqual(self.controller.requests, [])
        self.assertEqual(self.controller.deauth_calls, ["4567"])
        self.assertFalse(result["write_uncertain"])

    def test_ambiguous_auth_response_still_gets_exact_auth_zero_cleanup(self):
        self.controller.auth_raises = True
        result = self.reset()
        self.assertFalse(result["ok"])
        self.assertTrue(result["auth_result_uncertain"])
        self.assertTrue(result["deauthenticated"])
        self.assertFalse(result["cleanup_uncertain"])
        self.assertFalse(result["uncertain"])
        self.assertEqual(self.controller.auth_calls, [(3, "4567")])
        self.assertEqual(self.controller.deauth_calls, ["4567"])
        self.assertEqual(self.controller.write_count, 0)
        self.assertEqual(self.controller.requests, [])
        assert_secret_redacted(self, result)

    def test_motor_change_after_auth_blocks_the_write_and_still_deauthenticates(self):
        self.controller.motor_after_auth = 31
        result = self.reset()
        self.assertFalse(result["ok"])
        self.assertTrue(result["conflict"])
        self.assertEqual(result["prewrite_preconditions"]["motor_status"], 31)
        self.assertEqual(self.controller.write_count, 0)
        self.assertEqual(self.controller.requests, [])
        self.assertEqual(self.controller.deauth_calls, ["4567"])
        self.assertTrue(result["deauthenticated"])

    def test_fault_event_change_after_auth_blocks_write_and_deauthenticates(self):
        self.controller.fault_runtime_after_auth = 12346
        result = self.reset()
        self.assertFalse(result["ok"])
        self.assertTrue(result["conflict"])
        self.assertIn("Störereignis änderte sich", result["error"])
        self.assertEqual(self.controller.write_count, 0)
        self.assertEqual(self.controller.requests, [])
        self.assertEqual(self.controller.deauth_calls, ["4567"])
        self.assertTrue(result["deauthenticated"])

    def test_fault_auto_clear_after_auth_is_idempotent_without_write(self):
        self.controller.service_after_auth = 0
        result = self.reset()
        self.assertTrue(result["ok"])
        self.assertTrue(result["unchanged"])
        self.assertTrue(result["service_code_cleared"])
        self.assertFalse(result["changed"])
        self.assertEqual(self.controller.write_count, 0)
        self.assertEqual(self.controller.requests, [])
        self.assertEqual(self.controller.deauth_calls, ["4567"])

    def test_wrong_echo_and_missing_readback_never_retry_write(self):
        for mode in ("bad-echo", "readback-stuck", "negative-ack"):
            with self.subTest(mode=mode):
                self.controller = ResetController()
                self.controller.write_mode = mode
                self.app.service = self.controller
                # Each subtest represents an independent process lifetime;
                # the production retry guard is exercised separately below.
                self.app._service_reset_last_attempt = None
                result = self.reset()
                self.assertFalse(result["ok"])
                self.assertEqual(self.controller.write_count, 1)
                self.assertEqual(self.controller.requests, [SERVICE_RESET_REQUEST])
                self.assertEqual(self.controller.deauth_calls, ["4567"])
                self.assertTrue(result["write_uncertain"])
                self.assertTrue(result["restart_guard_pending"])
                pending = self.app.store.pending_service_reset_write_intent()
                self.assertEqual(pending["intent_id"], result["write_intent_id"])
                # Each subtest models a separately reviewed incident. Resolve
                # only in the test database before starting the next one.
                self.app.store.resolve_service_reset_write_intent(
                    pending["intent_id"], "test-reviewed", "test-reviewed", {}
                )

    def test_unexpected_readback_exception_still_deauthenticates(self):
        def fail_readback(_session):
            raise RuntimeError("simulated unexpected readback failure")

        self.app._service_reset_readback = fail_readback
        result = self.reset()
        self.assertFalse(result["ok"])
        self.assertTrue(result["write_uncertain"])
        self.assertFalse(result["readback_ok"])
        self.assertEqual(result["readback_errors"], ["RuntimeError"])
        self.assertTrue(result["deauthenticated"])
        self.assertEqual(self.controller.deauth_calls, ["4567"])
        self.assertEqual(self.controller.write_count, 1)

    def test_uncertain_write_blocks_new_preview_after_process_restart(self):
        self.controller.write_mode = "transport-error"
        result = self.reset()
        self.assertTrue(result["write_uncertain"])
        self.assertTrue(result["restart_guard_pending"])

        restarted = DachsWebApp(
            data_dir=self.temporary.name,
            interval=60,
            service_reset_temperature_policy_approved=True,
        )
        restarted_controller = ResetController()
        restarted.service = restarted_controller
        restarted.serial_enabled = True
        preview = restarted.service_reset_preview("admin")
        self.assertFalse(preview["eligible"])
        self.assertIsNone(preview["request_token"])
        self.assertEqual(
            preview["unresolved_write_intent"]["intent_id"],
            result["write_intent_id"],
        )
        self.assertTrue(
            any("Nicht aufgelöster" in item for item in preview["blockers"])
        )
        self.assertEqual(restarted_controller.auth_calls, [])
        self.assertEqual(restarted_controller.requests, [])

    def test_deauth_failure_preserves_confirmed_clear_and_redacts_secret(self):
        self.controller.deauth_raises = True
        result = self.reset()
        self.assertFalse(result["ok"])
        self.assertTrue(result["write_confirmed"])
        self.assertTrue(result["service_code_cleared"])
        self.assertFalse(result["deauthenticated"])
        self.assertTrue(result["cleanup_uncertain"])
        self.assertTrue(result["post_cleanup_readback_ok"])
        self.assertEqual(result["post_cleanup_readback_codes_raw"], [0, 0])
        self.assertTrue(result["write_intent_resolved"])
        self.assertTrue(result["audit_persisted"])
        self.assertEqual(self.controller.write_count, 1)
        assert_secret_redacted(self, result)
        stored = self.app.store.audits(limit=1)[0]["audit"]
        self.assertFalse(stored["deauthenticated"])
        self.assertTrue(stored["post_cleanup_readback_ok"])
        assert_secret_redacted(self, stored)

    def test_concurrent_double_click_produces_only_one_write(self):
        barrier = threading.Barrier(3)
        results = []
        errors = []
        request_token = self.preview()["request_token"]

        def worker():
            barrier.wait()
            try:
                results.append(self.reset(request_token))
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], APIRequestConflictError)
        self.assertTrue(results[0]["write_confirmed"])
        self.assertEqual(self.controller.write_count, 1)
        self.assertEqual(self.controller.auth_calls, [(3, "4567")])


class ServiceResetHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        init_users(
            self.temporary.name,
            admin_password="AdminPasswort123",
            guest_password="GastPasswort123",
        )
        self.app = DachsWebApp(
            data_dir=self.temporary.name,
            interval=60,
            service_reset_temperature_policy_approved=True,
        )
        self.controller = ResetController()
        self.app.service = self.controller
        self.app.serial_enabled = True
        self.app._service_reset_wait = lambda _seconds: False
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

    def test_admin_only_base_path_contract_and_forbidden_client_fields(self):
        status, _ = self.request("GET", "/dachs/api/service-reset/preview", self.guest)
        self.assertEqual(status, 403)
        status, preview = self.request("GET", "/dachs/api/service-reset/preview", self.admin)
        self.assertEqual(status, 200)
        self.assertTrue(preview["eligible"])
        sessions = self.controller.sessions
        status, body = self.request(
            "POST", "/dachs/api/service-reset", self.admin, ["invalid"],
        )
        self.assertEqual(status, 400)
        self.assertIn("object", body["error"].lower())
        status, body = self.request(
            "POST", "/dachs/api/service-reset", self.admin,
            {"confirm": True, "expected_service_code": 123,
             "request_token": preview["request_token"], "pass4": "1234"},
        )
        self.assertEqual(status, 400)
        self.assertIn("pass4", body["error"])
        self.assertEqual(self.controller.sessions, sessions)
        status, result = self.request(
            "POST", "/dachs/api/service-reset", self.admin,
            {"confirm": True, "expected_service_code": 123,
             "request_token": preview["request_token"]},
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(self.controller.write_count, 1)

    def test_operational_failure_returns_structured_502_without_retry(self):
        self.controller.deauth_raises = True
        _, preview = self.request("GET", "/dachs/api/service-reset/preview", self.admin)
        status, result = self.request(
            "POST", "/dachs/api/service-reset", self.admin,
            {"confirm": True, "expected_service_code": 123,
             "request_token": preview["request_token"]},
        )
        self.assertEqual(status, 502)
        self.assertTrue(result["write_confirmed"])
        self.assertFalse(result["deauthenticated"])
        self.assertEqual(self.controller.write_count, 1)

    def test_prewrite_state_conflict_returns_409_without_write(self):
        self.controller.motor_after_auth = 31
        _, preview = self.request("GET", "/dachs/api/service-reset/preview", self.admin)
        status, result = self.request(
            "POST", "/dachs/api/service-reset", self.admin,
            {"confirm": True, "expected_service_code": 123,
             "request_token": preview["request_token"]},
        )
        self.assertEqual(status, 409)
        self.assertTrue(result["conflict"])
        self.assertEqual(self.controller.write_count, 0)
        self.assertEqual(self.controller.deauth_calls, ["4567"])

    def test_duplicate_uncertain_request_is_consumed_and_never_writes_twice(self):
        self.controller.write_mode = "readback-stuck"
        _, preview = self.request("GET", "/dachs/api/service-reset/preview", self.admin)
        body = {
            "confirm": True,
            "expected_service_code": 123,
            "request_token": preview["request_token"],
        }
        first_status, first = self.request(
            "POST", "/dachs/api/service-reset", self.admin, body,
        )
        second_status, second = self.request(
            "POST", "/dachs/api/service-reset", self.admin, body,
        )
        self.assertEqual(first_status, 502)
        self.assertTrue(first["write_uncertain"])
        self.assertEqual(second_status, 409)
        self.assertIn("bereits verbraucht", second["error"])
        self.assertEqual(self.controller.write_count, 1)
        preview_status, guarded = self.request(
            "GET", "/dachs/api/service-reset/preview", self.admin,
        )
        self.assertEqual(preview_status, 200)
        self.assertFalse(guarded["eligible"])
        self.assertIsNone(guarded["request_token"])
        self.assertGreater(guarded["retry_guard_remaining_seconds"], 0)
        self.assertTrue(any("nicht automatisch wiederholt" in item
                            for item in guarded["blockers"]))


class ServiceResetUITests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_button_modal_guarded_endpoints_and_no_tty_access_are_present(self):
        index = (self.ROOT / "src/open_dachs_manager/web/index.html").read_text(encoding="utf-8")
        app = (self.ROOT / "src/open_dachs_manager/web/app.js").read_text(encoding="utf-8")
        for marker in (
            'id="hmiResetServiceCode"', 'id="serviceResetModal"',
            'id="serviceResetConfirm"', 'id="serviceResetSubmit"',
        ):
            self.assertIn(marker, index)
        self.assertIn('api("/api/service-reset/preview")', app)
        self.assertIn('api("/api/service-reset"', app)
        self.assertIn("expected_service_code", app)
        self.assertIn("request_token", app)
        self.assertIn("state.serviceReset.busy", app)
        self.assertIn("Störung war bei der Prüfung nach Auth 3 bereits gelöscht", app)
        self.assertIn("Störung war vor dem Schreibvorgang bereits gelöscht", app)
        submit_start = app.index("async function submitServiceReset()")
        submit_end = app.index("\nfunction dashboardField", submit_start)
        submit = app[submit_start:submit_end]
        self.assertIn("const preview = state.serviceReset.preview;", submit)
        self.assertNotIn('api("/api/service-reset/preview")', submit)
        self.assertIn("result.write_uncertain === true", submit)
        self.assertIn("result.restart_guard_pending === true", submit)
        self.assertIn("result.audit_persisted === false", submit)
        self.assertIn("Auth-Level 0 wurde nicht bestätigt", submit)
        self.assertIn("result.post_cleanup_readback_ok === true", submit)
        self.assertIn("persistente Neustartschutz blieb offen", submit)
        self.assertIn("Nicht wiederholen; Audit und Reglerzustand prüfen", submit)
        self.assertIn("NEUSTARTSCHUTZ OFFEN · KEIN RETRY", app)
        self.assertIn("fachlich freigegebene Temperatur-/Wiederanlaufpolitik", index)
        self.assertIn(
            'state.user?.role !== "admin" || state.serviceReset.busy', app
        )
        self.assertNotIn("Keine aktive Störung mehr: kein Write und keine Authentifizierung", app)
        self.assertNotIn("/dev/ttyUSB0", app)

    def test_submit_uses_confirmed_preview_in_executable_js_harness(self):
        app = (self.ROOT / "src/open_dachs_manager/web/app.js").read_text(
            encoding="utf-8"
        )
        submit_start = app.index("async function submitServiceReset()")
        submit_end = app.index("\nfunction dashboardField", submit_start)
        submit = app[submit_start:submit_end]
        harness = (
            "const state={serviceReset:{busy:false,preview:{eligible:true,"
            "request_token:'token-A',service_code:123}}};\n"
            "const elements={serviceResetConfirm:{checked:true},"
            "serviceResetStatus:{className:'',textContent:''}};\n"
            "const $=(id)=>elements[id]||{};\n"
            "const calls=[];\n"
            "function setServiceResetBusy(value){state.serviceReset.busy=value;}\n"
            "function updateServiceResetSubmit(){}\n"
            "function toast(){}\n"
            "async function refreshLive(){}\n"
            "async function api(path,options){calls.push({path,options});"
            "state.serviceReset.preview={eligible:true,request_token:'token-B',"
            "service_code:124};return {unchanged:false,service_code_before:123,"
            "deauthenticated:true};}\n"
            + submit
            + "\n(async()=>{await submitServiceReset();"
            "if(calls.length!==1||calls[0].path!=='/api/service-reset')"
            "throw new Error('unexpected API calls');"
            "const body=JSON.parse(calls[0].options.body);"
            "if(body.expected_service_code!==123||body.request_token!=='token-A')"
            "throw new Error('confirmed preview changed');"
            "if(elements.serviceResetConfirm.checked!==false)"
            "throw new Error('confirmation was not cleared');"
            "})().catch((error)=>{console.error(error);process.exit(1);});\n"
        )
        subprocess.run(["node", "-e", harness], check=True, capture_output=True)
