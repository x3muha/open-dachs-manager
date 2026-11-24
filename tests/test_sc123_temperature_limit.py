import http.client
import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from open_dachs_manager.mapping import PackRepository
from open_dachs_manager.sc123_limit import (
    SC123_LIMIT_CAPABILITY_BLOCK,
    SC123_LIMIT_HEARTBEAT_OFFSET,
    SC123_LIMIT_MAGIC,
    SC123_LIMIT_NVM_POLL_ATTEMPTS,
    SC123_LIMIT_NVM_READ_PREFIX,
    SC123_LIMIT_NVM_READ_REQUEST,
    SC123_LIMIT_PAYLOAD_LENGTH,
    SC123_LIMIT_RECORD_LENGTH,
    SC123_LIMIT_RECORD_OFFSET,
    SC123_LIMIT_RETRY_GUARD_SECONDS,
    SC123_LIMIT_STORAGE_BLOCK,
    SC123_LIMIT_THRESHOLD_OFFSET,
    parse_sc123_limit_c,
    sc123_limit_capability,
    sc123_limit_nvm_record,
    sc123_limit_payload,
    sc123_limit_readback,
    sc123_limit_record,
    sc123_limit_record_bytes,
    stable_sc123_block50_equal,
    stable_sc123_block60_equal,
)
from open_dachs_manager.transport import TransportError
from open_dachs_manager.web import (
    APIRequestConflictError,
    DachsHTTPServer,
    DachsWebApp,
    init_users,
)


def block50(heartbeat=0):
    payload = bytearray(SC123_LIMIT_PAYLOAD_LENGTH)
    payload[SC123_LIMIT_HEARTBEAT_OFFSET] = heartbeat
    return bytes(payload)


def block60(threshold=200, *, valid=True):
    payload = bytes(SC123_LIMIT_PAYLOAD_LENGTH)
    if not valid:
        return payload
    return sc123_limit_payload(payload, threshold)


def record_from(payload):
    return bytes(payload)[
        SC123_LIMIT_RECORD_OFFSET:
        SC123_LIMIT_RECORD_OFFSET + SC123_LIMIT_RECORD_LENGTH
    ]


def nvm_response(record, *, positive=True, body=None):
    payload = (
        SC123_LIMIT_NVM_READ_PREFIX + bytes(record)
        if body is None
        else bytes(body)
    )
    return SimpleNamespace(
        ack=SimpleNamespace(positive=positive),
        data=SimpleNamespace(payload=payload),
    )


class Sc123LimitPureTests(unittest.TestCase):
    def test_strict_whole_degree_parser_and_range(self):
        self.assertEqual(parse_sc123_limit_c(180), 180)
        self.assertEqual(parse_sc123_limit_c(240), 240)
        self.assertEqual(parse_sc123_limit_c(" 200 "), 200)
        for value in (True, False, 179, 241, 200.0, "200.0", "", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_sc123_limit_c(value)

    def test_capability_requires_heartbeat_and_two_stable_blocks(self):
        first = block50(9)
        second = block50(10)
        storage = block60(200)
        state = sc123_limit_capability(first, second, storage, storage)
        self.assertTrue(state["available"] and state["firmware_capability"])
        self.assertTrue(state["storage_stable_reads"])
        self.assertEqual(state["threshold_c"], 200)

        wrapped = sc123_limit_capability(
            block50(255), block50(0), storage, storage
        )
        self.assertTrue(wrapped["available"])

        stock = sc123_limit_capability(
            block50(7), block50(7), storage, storage
        )
        self.assertFalse(stock["available"])
        self.assertIn(
            "kein dynamischer Firmware-Heartbeat", " ".join(stock["blockers"])
        )

        drifted50 = bytearray(second)
        drifted50[12] ^= 1
        self.assertFalse(
            sc123_limit_capability(first, bytes(drifted50), storage, storage)[
                "available"
            ]
        )
        drifted60 = bytearray(storage)
        drifted60[12] ^= 1
        state = sc123_limit_capability(
            first, second, storage, bytes(drifted60)
        )
        self.assertTrue(state["firmware_capability"])
        self.assertFalse(state["available"])
        self.assertFalse(state["storage_stable_reads"])

    def test_record_requires_magic_range_and_complement(self):
        valid = block60(220)
        decoded = sc123_limit_record(valid)
        self.assertTrue(decoded["configured"])
        self.assertEqual(decoded["threshold_c"], 220)
        self.assertEqual(record_from(valid), SC123_LIMIT_MAGIC + bytes([220, 35]))

        corruptions = tuple((offset, valid[offset] ^ 0x01) for offset in range(40, 44)) + (
            (44, 179),
            (44, 241),
            (45, 0),
        )
        for offset, value in corruptions:
            with self.subTest(offset=offset, value=value):
                invalid = bytearray(valid)
                invalid[offset] = value
                decoded = sc123_limit_record(bytes(invalid))
                self.assertFalse(decoded["configured"])
                self.assertTrue(decoded["fallback_active"])
                self.assertEqual(decoded["threshold_c"], 180)

    def test_invalid_record_uses_fallback_but_capability_remains_writable(self):
        invalid = block60(valid=False)
        state = sc123_limit_capability(block50(4), block50(5), invalid, invalid)
        self.assertTrue(state["available"])
        self.assertFalse(state["configured"])
        self.assertEqual(state["threshold_c"], 180)

    def test_payload_changes_only_versioned_record(self):
        before = bytearray(block60(valid=False))
        before[12] = 0xA5
        after = sc123_limit_payload(bytes(before), 240)
        changed = [
            index
            for index, pair in enumerate(zip(before, after))
            if pair[0] != pair[1]
        ]
        self.assertEqual(changed, list(range(40, 46)))
        self.assertEqual(record_from(after), sc123_limit_record_bytes(240))
        self.assertEqual(after[12], 0xA5)
        self.assertEqual(sc123_limit_payload(after, 240), after)

    def test_ram_readback_is_full_block_exact(self):
        expected = block60(220)
        self.assertTrue(sc123_limit_readback(expected, expected)["ok"])
        wrong_record = bytearray(expected)
        wrong_record[SC123_LIMIT_THRESHOLD_OFFSET] = 219
        self.assertFalse(sc123_limit_readback(expected, bytes(wrong_record))["ok"])
        wrong_other = bytearray(expected)
        wrong_other[12] ^= 1
        self.assertFalse(sc123_limit_readback(expected, bytes(wrong_other))["ok"])
        self.assertTrue(stable_sc123_block60_equal(expected, expected))
        self.assertTrue(stable_sc123_block50_equal(block50(1), block50(2)))

    def test_generic_mapping_cannot_write_firmware_record(self):
        fields = PackRepository().field_map(SC123_LIMIT_STORAGE_BLOCK)
        for index in range(SC123_LIMIT_RECORD_LENGTH):
            field = fields[f"Waermef_Ew.Reserve[{index}]"]
            self.assertEqual(field["offset"], SC123_LIMIT_RECORD_OFFSET + index)
            self.assertIs(field["write"], False)

    def test_service_04_response_is_byte_exact(self):
        record = sc123_limit_record_bytes(200)
        self.assertEqual(sc123_limit_nvm_record(nvm_response(record)), record)
        for response in (
            nvm_response(record, positive=False),
            nvm_response(record, body=b""),
            nvm_response(record, body=b"\x84\x01\xb5\x06" + record),
            nvm_response(record, body=SC123_LIMIT_NVM_READ_PREFIX + record[:-1]),
        ):
            with self.subTest(response=response), self.assertRaises(ValueError):
                sc123_limit_nvm_record(response)


class Sc123Session:
    def __init__(self, controller):
        self.controller = controller
        self.packet = 0

    def next_packet(self):
        self.packet = (self.packet + 1) & 0x0F
        return self.packet

    def write_block(self, block, payload, packet=None, timeout=None):
        controller = self.controller
        if block != SC123_LIMIT_STORAGE_BLOCK or len(payload) != SC123_LIMIT_PAYLOAD_LENGTH:
            raise AssertionError("unexpected SC123 write contract")
        controller.write_count += 1
        if controller.write_count > 1:
            raise AssertionError("SC123 path wrote more than once")
        controller.last_wire_payload = bytes(payload)
        if controller.write_mode == "transport-error":
            raise TransportError("simulated ambiguous write")
        if controller.write_mode != "readback-stuck":
            controller.storage[:] = payload
        if controller.nvm_mode not in {"stale", "delayed"}:
            controller.nvm_record = record_from(payload)
        positive = controller.write_mode != "negative-ack"
        return SimpleNamespace(ack=SimpleNamespace(positive=positive))

    def request(self, payload, packet, timeout=None, cpu=0):
        controller = self.controller
        if bytes(payload) != SC123_LIMIT_NVM_READ_REQUEST or cpu != 0:
            raise AssertionError("unexpected SC123 request")
        controller.nvm_read_count += 1
        if controller.nvm_mode == "malformed":
            return nvm_response(controller.nvm_record, body=b"\x84")
        if (
            controller.nvm_mode == "delayed"
            and controller.nvm_read_count >= controller.nvm_ready_after
        ):
            controller.nvm_record = record_from(controller.last_wire_payload)
        return nvm_response(controller.nvm_record)


class Sc123Controller:
    def __init__(self, threshold=200, *, patched=True, valid_record=True):
        self.serial_socket = "/run/open-dachs-manager/test-serial.sock"
        self.timeout = 0.5
        self.capability = bytearray(block50(0))
        self.storage = bytearray(block60(threshold, valid=valid_record))
        self.nvm_record = record_from(self.storage)
        self.patched = patched
        self.drift_block50_offset = None
        self.drift_block60_offset = None
        self.read_count = 0
        self.block_reads = {50: 0, 60: 0}
        self.read_trace = []
        self.ram_readback_count = 0
        self.nvm_read_count = 0
        self.write_count = 0
        self.sessions = 0
        self.last_wire_payload = None
        self.write_mode = "ok"
        self.nvm_mode = "ok"
        self.nvm_ready_after = 3
        self.auth_calls = []
        self.deauth_calls = []
        self.auth_ok = True
        self.granted_level = 3
        self.auth_raises = False
        self.deauth_raises = False
        self.after_auth = None

    @contextmanager
    def session(self):
        self.sessions += 1
        yield Sc123Session(self)

    def read_block(self, _session, block, cpu=0):
        if block not in (50, 60) or cpu != 0:
            raise AssertionError("unexpected block read")
        self.read_count += 1
        self.block_reads[block] += 1
        self.read_trace.append((id(_session), block))
        if block == 50:
            if self.patched:
                self.capability[SC123_LIMIT_HEARTBEAT_OFFSET] = (
                    self.capability[SC123_LIMIT_HEARTBEAT_OFFSET] + 1
                ) & 0xFF
            if self.drift_block50_offset is not None:
                offset = self.drift_block50_offset
                self.capability[offset] = (self.capability[offset] + 1) & 0xFF
            payload = bytes(self.capability)
        else:
            if self.drift_block60_offset is not None:
                offset = self.drift_block60_offset
                self.storage[offset] = (self.storage[offset] + 1) & 0xFF
            if self.write_count:
                self.ram_readback_count += 1
            payload = bytes(self.storage)
        return SimpleNamespace(ok=True, payload=payload)

    def authentication_inputs(self, _session):
        return SimpleNamespace(serial_number="818", operating_hours=2000)

    def authenticate(self, _session, level, pass4=None):
        self.auth_calls.append((level, pass4))
        if self.auth_raises:
            raise TransportError("simulated auth failure with secret 4567")
        if self.after_auth is not None:
            self.after_auth(self)
        return SimpleNamespace(ok=self.auth_ok, granted_level=self.granted_level)

    def deauthenticate(self, _session, pass4):
        self.deauth_calls.append(pass4)
        if self.deauth_raises:
            raise TransportError("simulated cleanup failure with secret 4567")


class Sc123LimitWebAppTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.app = DachsWebApp(data_dir=self.temporary.name, interval=60)
        self.controller = Sc123Controller()
        self.app.service = self.controller
        self.app.serial_enabled = True

    def tearDown(self):
        self.temporary.cleanup()

    def preview(self, value=220, username="admin"):
        return self.app.sc123_temperature_limit_preview(username, value)

    def apply(self, value=220, token=None, username="admin", **overrides):
        if token is None:
            token = self.preview(value, username)["request_token"]
        arguments = dict(
            username=username,
            value=value,
            confirm=True,
            write_enabled=True,
            request_token=token,
        )
        arguments.update(overrides)
        return self.app.set_sc123_temperature_limit(**arguments)

    def latest_audit(self):
        return self.app.store.audits(limit=1)[0]["audit"]

    def reset_controller(self, **kwargs):
        self.controller = Sc123Controller(**kwargs)
        self.app.service = self.controller

    def test_generic_web_write_rejects_firmware_owned_bytes_before_auth(self):
        cases = (
            (50, "Hka_Ew.Reserve_2", "99"),
            (60, "Waermef_Ew.Reserve[0]", "82"),
        )
        for block, key, value in cases:
            with self.subTest(block=block, key=key):
                self.reset_controller()
                with self.assertRaisesRegex(PermissionError, "dedizierten SC123-Pfad"):
                    self.app.write_block(
                        "admin",
                        block,
                        [{"key": key, "value": value}],
                        3,
                        "4567",
                        True,
                    )
                self.assertEqual(self.controller.auth_calls, [])
                self.assertEqual(self.controller.write_count, 0)

    def test_schema_marks_only_sc123_owned_bytes_read_only(self):
        blocks = {
            int(item["block"]): {field["key"]: field for field in item["fields"]}
            for item in self.app.schema()["blocks"]
        }
        self.assertFalse(blocks[50]["Hka_Ew.Reserve_2"]["write"])
        for index in range(6):
            self.assertFalse(
                blocks[60][f"Waermef_Ew.Reserve[{index}]"]["write"]
            )
        self.assertTrue(blocks[50]["Hka_Ew.usSollGenerator"]["write"])

    def test_bearer_api_set_value_cannot_bypass_guard(self):
        created = self.app.store.create_api_token(
            "admin", "SC123 guard", ["write"]
        )
        principal = self.app.store.authenticate_api_token(created["token"])
        self.app.set_api_settings({"write_enabled": True, "auth_level": 3})
        with self.assertRaisesRegex(PermissionError, "dedizierten SC123-Pfad"):
            self.app.execute_api_set_value(
                principal,
                {
                    "block": 60,
                    "key": "Waermef_Ew.Reserve[0]",
                    "value": 82,
                    "request_id": "sc123-protected-1",
                },
            )
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(self.controller.write_count, 0)
        with self.app.store.database() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM api_requests").fetchone()[0],
                0,
            )

    def test_preview_reads_both_blocks_twice_without_auth_or_write(self):
        preview = self.preview()
        self.assertTrue(preview["available"] and preview["eligible"])
        self.assertEqual(preview["threshold_c"], 200)
        self.assertEqual(preview["target_c"], 220)
        self.assertEqual(preview["block"], 60)
        self.assertEqual(preview["record_offset"], 40)
        self.assertTrue(preview["request_token"])
        self.assertEqual(self.controller.block_reads, {50: 2, 60: 2})
        self.assertEqual(
            [block for _session_id, block in self.controller.read_trace],
            [50, 50, 60, 60],
        )
        self.assertEqual(
            len({session_id for session_id, _block in self.controller.read_trace}),
            1,
        )
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(self.controller.write_count, 0)

    def test_success_writes_once_reads_ram_once_polls_nvm_and_deauths(self):
        preview = self.preview(220)
        result = self.apply(220, preview["request_token"])
        self.assertTrue(result["ok"] and result["write_confirmed"])
        self.assertTrue(result["ram_readback_ok"] and result["nvm_readback_ok"])
        self.assertEqual(result["write_count"], 1)
        self.assertEqual(result["ram_readback_attempts"], 1)
        self.assertEqual(result["nvm_readback_attempts"], 1)
        self.assertEqual(self.controller.write_count, 1)
        self.assertEqual(self.controller.ram_readback_count, 1)
        self.assertEqual(self.controller.nvm_read_count, 1)
        self.assertEqual(self.controller.auth_calls, [(3, "4567")])
        self.assertEqual(self.controller.deauth_calls, ["4567"])
        self.assertEqual(sc123_limit_record(self.controller.storage)["threshold_c"], 220)
        changed = [
            index
            for index, pair in enumerate(zip(block60(200), self.controller.last_wire_payload))
            if pair[0] != pair[1]
        ]
        self.assertTrue(set(changed) <= set(range(40, 46)))
        audit_text = json.dumps(self.latest_audit())
        self.assertNotIn("4567", audit_text)
        self.assertEqual(self.latest_audit()["operation"], "sc123-temperature-limit-write")

    def test_stock_or_rolled_back_firmware_fails_closed(self):
        self.controller.patched = False
        preview = self.preview()
        self.assertFalse(preview["available"] or preview["eligible"])
        self.assertIsNone(preview["request_token"])
        self.assertEqual(self.controller.auth_calls, [])

        self.controller.patched = True
        valid = self.preview()
        self.controller.patched = False
        with self.assertRaises(APIRequestConflictError):
            self.apply(220, valid["request_token"])
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(self.controller.write_count, 0)

    def test_invalid_record_fallback_can_be_persisted_as_180(self):
        self.reset_controller(valid_record=False)
        preview = self.preview(180)
        self.assertTrue(preview["available"] and preview["fallback_active"])
        self.assertTrue(preview["transition_required"])
        result = self.apply(180, preview["request_token"])
        self.assertTrue(result["ok"])
        decoded = sc123_limit_record(self.controller.storage)
        self.assertTrue(decoded["configured"])
        self.assertEqual(decoded["threshold_c"], 180)

    def test_command_and_either_block_drift_fail_before_token_or_auth(self):
        mutators = (
            lambda c: c.capability.__setitem__(0, 0x20),
            lambda c: setattr(c, "drift_block50_offset", 12),
            lambda c: setattr(c, "drift_block60_offset", 12),
        )
        for mutate in mutators:
            with self.subTest(mutate=mutate):
                self.reset_controller()
                mutate(self.controller)
                preview = self.preview()
                self.assertFalse(preview["eligible"])
                self.assertIsNone(preview["request_token"])
                self.assertEqual(self.controller.auth_calls, [])
                self.assertEqual(self.controller.write_count, 0)

    def test_confirmation_toggle_token_and_expiry_fail_before_auth(self):
        preview = self.preview()
        for overrides in ({"confirm": False}, {"write_enabled": False}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.apply(token=preview["request_token"], **overrides)
            self.assertEqual(self.controller.auth_calls, [])
        with self.assertRaises(APIRequestConflictError):
            self.apply(token="wrong-token")
        self.assertEqual(self.controller.auth_calls, [])

        preview = self.preview()
        self.app._sc123_limit_token["expires_at"] = time.monotonic() - 1
        with self.assertRaises(APIRequestConflictError):
            self.apply(token=preview["request_token"])
        self.assertEqual(self.controller.auth_calls, [])

    def test_token_binds_user_target_and_full_block60_preimage(self):
        preview = self.preview(220)
        with self.assertRaises(APIRequestConflictError):
            self.apply(221, preview["request_token"])
        self.assertEqual(self.controller.auth_calls, [])

        preview = self.preview(220, "first-admin")
        with self.assertRaises(APIRequestConflictError):
            self.apply(220, preview["request_token"], username="second-admin")
        self.assertEqual(self.controller.auth_calls, [])

        preview = self.preview(220)
        self.controller.storage[12] ^= 1
        with self.assertRaises(APIRequestConflictError):
            self.apply(220, preview["request_token"])
        self.assertEqual(self.controller.auth_calls, [])

    def test_unchanged_is_idempotent_without_auth_or_write(self):
        preview = self.preview(200)
        self.assertFalse(preview["transition_required"])
        result = self.apply(200, preview["request_token"])
        self.assertTrue(result["ok"] and result["unchanged"])
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(self.controller.write_count, 0)
        self.assertEqual(self.controller.nvm_read_count, 1)
        self.assertTrue(result["nvm_readback_ok"])
        self.assertFalse(result["auth_attempted"])
        self.assertIsNone(result["deauthenticated"])

    def test_same_target_after_stale_nvm_never_becomes_false_success(self):
        self.controller.nvm_mode = "stale"
        with patch("open_dachs_manager.web.time.sleep", return_value=None):
            with self.assertRaises(TransportError):
                self.apply(220)
        self.assertEqual(self.controller.write_count, 1)
        self.assertEqual(
            self.controller.nvm_read_count, SC123_LIMIT_NVM_POLL_ATTEMPTS
        )
        preview = self.preview(220)
        self.assertTrue(preview["eligible"])
        self.assertFalse(preview["transition_required"])
        with patch("open_dachs_manager.web.time.sleep", return_value=None):
            with self.assertRaises(TransportError):
                self.apply(220, preview["request_token"])
        self.assertEqual(self.controller.write_count, 1)
        self.assertEqual(
            self.controller.nvm_read_count,
            2 * SC123_LIMIT_NVM_POLL_ATTEMPTS,
        )
        self.assertEqual(len(self.controller.auth_calls), 1)
        audit = self.latest_audit()
        self.assertFalse(audit["write_attempted"])
        self.assertFalse(audit["nvm_readback_ok"])
        self.assertFalse(audit["auth_attempted"])
        self.assertIsNone(audit["deauthenticated"])

    def test_auth_denial_or_ambiguity_never_writes_and_always_cleans_up(self):
        for mode in ("denied", "raises"):
            with self.subTest(mode=mode):
                self.reset_controller()
                if mode == "denied":
                    self.controller.auth_ok = False
                    self.controller.granted_level = 2
                    expected = PermissionError
                else:
                    self.controller.auth_raises = True
                    expected = TransportError
                with self.assertRaises(expected) as caught:
                    self.apply()
                self.assertEqual(self.controller.write_count, 0)
                self.assertEqual(self.controller.deauth_calls, ["4567"])
                self.assertNotIn("4567", str(caught.exception))
                self.assertNotIn("4567", json.dumps(self.latest_audit()))

    def test_post_auth_preimage_race_deauths_without_write(self):
        preview = self.preview()
        self.controller.after_auth = lambda controller: controller.storage.__setitem__(12, 1)
        with self.assertRaises(APIRequestConflictError):
            self.apply(token=preview["request_token"])
        self.assertEqual(self.controller.write_count, 0)
        self.assertEqual(self.controller.deauth_calls, ["4567"])

    def test_post_auth_capability_or_pending_command_loss_blocks_write(self):
        mutators = (
            lambda controller: setattr(controller, "patched", False),
            lambda controller: controller.capability.__setitem__(0, 0x20),
        )
        for mutate in mutators:
            with self.subTest(mutate=mutate):
                self.reset_controller()
                preview = self.preview()
                self.controller.after_auth = mutate
                with self.assertRaises(APIRequestConflictError):
                    self.apply(token=preview["request_token"])
                self.assertEqual(self.controller.write_count, 0)
                self.assertEqual(self.controller.deauth_calls, ["4567"])

    def test_negative_ack_or_transport_error_never_retries_and_arms_guard(self):
        for mode in ("negative-ack", "transport-error"):
            with self.subTest(mode=mode):
                self.app._sc123_limit_last_attempt = None
                self.reset_controller()
                self.controller.write_mode = mode
                with self.assertRaises(TransportError):
                    self.apply()
                self.assertEqual(self.controller.write_count, 1)
                self.assertEqual(self.controller.ram_readback_count, 0)
                self.assertEqual(self.controller.nvm_read_count, 0)
                self.assertEqual(self.controller.deauth_calls, ["4567"])
                audit = self.latest_audit()
                self.assertEqual(audit["write_count"], 1)
                self.assertTrue(audit["write_uncertain"] and audit["uncertain"])
                blocked_target = 221 if mode == "negative-ack" else 220
                blocked = self.preview(blocked_target)
                self.assertFalse(blocked["eligible"])
                self.assertIsNone(blocked["request_token"])

    def test_retry_guard_boundary_is_exact_and_unchanged_target_is_allowed(self):
        attempted_at = 100.0
        self.app._sc123_limit_last_attempt = {
            "target_c": 220,
            "attempted_at": attempted_at,
        }
        cases = (
            (attempted_at + SC123_LIMIT_RETRY_GUARD_SECONDS - 0.001, False),
            (attempted_at + SC123_LIMIT_RETRY_GUARD_SECONDS, True),
            (attempted_at + SC123_LIMIT_RETRY_GUARD_SECONDS + 0.001, True),
        )
        for now, eligible in cases:
            with self.subTest(now=now), patch(
                "open_dachs_manager.web.time.monotonic", return_value=now
            ):
                preview = self.preview(220)
                self.assertIs(preview["eligible"], eligible)
                self.assertIs(preview["request_token"] is not None, eligible)

        with patch(
            "open_dachs_manager.web.time.monotonic",
            return_value=attempted_at + 1.0,
        ):
            unchanged = self.preview(200)
        self.assertTrue(unchanged["eligible"])
        self.assertFalse(unchanged["transition_required"])

    def test_ram_readback_failure_never_retries_or_polls_nvm(self):
        self.controller.write_mode = "readback-stuck"
        with self.assertRaises(TransportError):
            self.apply()
        self.assertEqual(self.controller.write_count, 1)
        self.assertEqual(self.controller.ram_readback_count, 1)
        self.assertEqual(self.controller.nvm_read_count, 0)
        audit = self.latest_audit()
        self.assertTrue(audit["write_uncertain"] and audit["uncertain"])
        self.assertEqual(audit["ram_readback_attempts"], 1)

    def test_nvm_poll_is_bounded_read_only_and_timeout_is_uncertain(self):
        self.controller.nvm_mode = "stale"
        with patch("open_dachs_manager.web.time.sleep", return_value=None):
            with self.assertRaises(TransportError):
                self.apply()
        self.assertEqual(self.controller.write_count, 1)
        self.assertEqual(self.controller.ram_readback_count, 1)
        self.assertEqual(self.controller.nvm_read_count, SC123_LIMIT_NVM_POLL_ATTEMPTS)
        audit = self.latest_audit()
        self.assertFalse(audit["nvm_readback_ok"])
        self.assertTrue(audit["write_uncertain"] and audit["uncertain"])
        self.assertEqual(self.controller.deauth_calls, ["4567"])

    def test_nvm_poll_accepts_delayed_exact_persistence(self):
        self.controller.nvm_mode = "delayed"
        self.controller.nvm_ready_after = 3
        with patch("open_dachs_manager.web.time.sleep", return_value=None):
            result = self.apply()
        self.assertTrue(result["ok"])
        self.assertEqual(result["nvm_readback_attempts"], 3)
        self.assertEqual(self.controller.write_count, 1)

    def test_malformed_nvm_response_is_bounded_and_uncertain(self):
        self.controller.nvm_mode = "malformed"
        with patch("open_dachs_manager.web.time.sleep", return_value=None):
            with self.assertRaises(TransportError):
                self.apply()
        self.assertEqual(self.controller.write_count, 1)
        self.assertEqual(self.controller.ram_readback_count, 1)
        self.assertEqual(
            self.controller.nvm_read_count, SC123_LIMIT_NVM_POLL_ATTEMPTS
        )
        self.assertEqual(self.controller.deauth_calls, ["4567"])
        audit = self.latest_audit()
        self.assertFalse(audit["nvm_readback_ok"])
        self.assertTrue(audit["write_uncertain"] and audit["uncertain"])

    def test_deauth_failure_keeps_confirmed_write_but_marks_uncertain(self):
        self.controller.deauth_raises = True
        with self.assertRaises(TransportError) as caught:
            self.apply()
        self.assertNotIn("4567", str(caught.exception))
        audit = self.latest_audit()
        self.assertTrue(audit["write_confirmed"])
        self.assertTrue(audit["cleanup_uncertain"] and audit["uncertain"])
        self.assertNotIn("4567", json.dumps(audit))

    def test_direct_tty_and_disabled_serial_fail_before_session(self):
        self.controller.serial_socket = ""
        with self.assertRaises(TransportError):
            self.preview()
        self.assertEqual(self.controller.sessions, 0)
        self.controller.serial_socket = "/run/open-dachs-manager/test.sock"
        self.app.serial_enabled = False
        with self.assertRaises(TransportError):
            self.preview()
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(self.controller.write_count, 0)

    def test_ui_and_client_keep_raw_contract_server_side(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "src/open_dachs_manager/web/index.html").read_text(encoding="utf-8")
        app = (root / "src/open_dachs_manager/web/app.js").read_text(encoding="utf-8")
        web = (root / "src/open_dachs_manager/web.py").read_text(encoding="utf-8")
        self.assertIn('id="sc123LimitPanel"', html)
        self.assertIn('min="180" max="240" step="1"', html)
        self.assertIn("201–240 °C", html)
        self.assertIn('/api/sc123-temperature-limit/preview', app)
        self.assertIn('difference({"value"})', web)
        self.assertIn('allowed = {"value", "confirm", "write_enabled", "request_token"}', web)
        post = app[app.index("async function applySc123Limit()") : app.index("async function refreshSystemView(")]
        self.assertNotIn("auth_level", post)
        self.assertNotIn("pass4", post)
        self.assertNotIn("offset", post)


class Sc123LimitHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        init_users(
            self.temporary.name,
            admin_password="AdminPasswort123",
            guest_password="GastPasswort123",
        )
        self.app = DachsWebApp(data_dir=self.temporary.name, interval=60)
        self.controller = Sc123Controller()
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
        result = json.loads(response.read().decode("utf-8"))
        status = response.status
        connection.close()
        return status, result

    def test_admin_only_base_path_strict_payload_and_happy_write(self):
        status, _ = self.request("GET", "/dachs/api/sc123-temperature-limit", self.guest)
        self.assertEqual(status, 403)
        status, preview = self.request(
            "POST", "/dachs/api/sc123-temperature-limit/preview", self.admin, {"value": 220}
        )
        self.assertEqual(status, 200)
        self.assertTrue(preview["eligible"])
        sessions = self.controller.sessions
        status, body = self.request(
            "POST",
            "/dachs/api/sc123-temperature-limit",
            self.admin,
            {
                "value": 220,
                "confirm": True,
                "write_enabled": True,
                "request_token": preview["request_token"],
                "auth_level": 3,
                "pass4": "1234",
                "offset": 44,
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("auth_level", body["error"])
        self.assertEqual(self.controller.sessions, sessions)
        self.assertEqual(self.controller.write_count, 0)

        status, preview = self.request(
            "POST", "/dachs/api/sc123-temperature-limit/preview", self.admin, {"value": 220}
        )
        self.assertEqual(status, 200)
        status, result = self.request(
            "POST",
            "/dachs/api/sc123-temperature-limit",
            self.admin,
            {
                "value": 220,
                "confirm": True,
                "write_enabled": True,
                "request_token": preview["request_token"],
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(self.controller.write_count, 1)

    def test_generic_http_write_cannot_bypass_sc123_guard(self):
        status, body = self.request(
            "POST",
            "/dachs/api/block/60",
            self.admin,
            {
                "changes": [{"key": "Waermef_Ew.Reserve[0]", "value": "82"}],
                "auth_level": 3,
                "pass4": "4567",
                "write_enabled": True,
            },
        )
        self.assertEqual(status, 403)
        self.assertIn("dedizierten SC123-Pfad", body["error"])
        self.assertEqual(self.controller.auth_calls, [])
        self.assertEqual(self.controller.write_count, 0)


if __name__ == "__main__":
    unittest.main()
