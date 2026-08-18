import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from open_dachs_manager.auth import AuthInputs
from open_dachs_manager.mapping import PackRepository
from open_dachs_manager.service import DachsService, _image_sha256, write_json_atomic
from open_dachs_manager.transport import BlockResult, Frame, Response


def block_result(block, payload, *, ok=True, cpu=0):
    data = Frame("data", 1, b"", payload=b"\x00" + bytes(payload)) if ok else None
    return BlockResult(
        block,
        1,
        Response(b"", None, data, 1.25),
        0 if ok else None,
        bytes(payload),
        cpu,
    )


def positive_write_response():
    return Response(b"", Frame("ack", 2, b"", positive=True), None, 1.0)


def canonical_image_digest(image):
    raw_pack = image.get("pack")
    pack = None if raw_pack is None else {
        "name": " ".join(raw_pack.get("name", "").split()),
        "schema": " ".join(raw_pack.get("schema", "").split()),
        "revision": " ".join(raw_pack.get("revision", "").split()),
    }
    raw_controller = image.get("controller")
    controller = None
    if raw_controller is not None:
        controller = {"available": raw_controller.get("available")}
        if raw_controller.get("available") is True:
            controller.update({
                "serial_number": " ".join(
                    raw_controller.get("serial_number", "").split()
                ),
                "operating_hours": raw_controller.get("operating_hours"),
            })
        elif raw_controller.get("error") is not None:
            controller["error"] = " ".join(raw_controller["error"].split())
    records = []
    derived_ids = []
    for record in image.get("blocks", []):
        derived_ids.append(record.get("block"))
        payload_digest = record.get("payload_sha256")
        records.append({
            "block": record.get("block"),
            "ok": record.get("ok"),
            "status": record.get("status"),
            "payload_len": record.get("payload_len"),
            "payload_sha256": (
                payload_digest.lower()
                if isinstance(payload_digest, str)
                else payload_digest
            ),
            "error": (
                " ".join(record["error"].split())
                if isinstance(record.get("error"), str)
                else record.get("error")
            ),
        })
    semantic = {
        "schema": image.get("schema"),
        "schema_version": image.get("schema_version"),
        "pack": pack,
        "controller": controller,
        "requested_blocks": image.get("requested_block_ids", derived_ids),
        "records": records,
    }
    encoded = json.dumps(
        semantic,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class BackupSession:
    def __init__(self, payloads=None, failures=None):
        self.payloads = dict(payloads or {})
        self.failures = set(failures or ())
        self.reads = []

    @staticmethod
    def _key(cpu, block):
        return block if cpu == 0 else (cpu, block)

    def read_block(self, block, packet=None, timeout=0.9, cpu=0):
        key = self._key(cpu, block)
        self.reads.append((cpu, block))
        if key in self.failures:
            raise RuntimeError(f"synthetic read failure for CPU {cpu}, block {block}")
        return block_result(
            block,
            self.payloads.get(key, bytes([block & 0xFF, 0xA5])),
            cpu=cpu,
        )

    def write_block(self, *args, **kwargs):
        raise AssertionError("backup must never write")


class RestoreSession:
    def __init__(self, reads, *, ack_positive=True):
        self.reads = list(reads)
        self.ack_positive = ack_positive
        self.calls = []

    def read_block(self, block, packet=None, timeout=0.9, cpu=0):
        call = ("read", block) if cpu == 0 else ("read", cpu, block)
        self.calls.append(call)
        return block_result(block, self.reads.pop(0), cpu=cpu)

    def write_block(self, block, payload, packet=None, timeout=0.9, cpu=0):
        call = (
            ("write", block, bytes(payload))
            if cpu == 0
            else ("write", cpu, block, bytes(payload))
        )
        self.calls.append(call)
        ack = Frame("ack", 2, b"", positive=self.ack_positive)
        return Response(b"", ack, None, 1.0)


class BackupRestoreTests(unittest.TestCase):
    def setUp(self):
        self.pack = PackRepository()
        self.service = DachsService(
            "/dev/fake",
            19200,
            0.1,
            self.pack,
            readback_attempts=3,
            readback_delay=0,
        )

    def make_image(self, blocks=(20, 22), failures=(), include_identity=False):
        payloads = {
            block: bytes(((block + index) & 0xFF) for index in range(8))
            for block in blocks
        }
        session = BackupSession(payloads, failures)
        if include_identity:
            self.service.authentication_inputs = lambda _session: AuthInputs(
                "D1234567", 4321
            )
        return self.service.backup(
            session,
            list(blocks),
            decode=False,
            include_identity=include_identity,
        )

    def test_backup_contains_product_pack_names_and_two_levels_of_digest(self):
        image = self.make_image(include_identity=True)

        self.assertEqual(image["schema"], "dachs-msr2-backup/v3")
        self.assertEqual(image["schema_version"], 3)
        self.assertEqual(image["product"]["name"], "Open Dachs Manager")
        self.assertEqual(image["pack"]["revision"], self.pack.pack_rev)
        self.assertTrue(image["pack"]["name"])
        self.assertEqual(image["controller"]["serial_number"], "D1234567")
        self.assertEqual(image["controller"]["operating_hours"], 4321)
        self.assertEqual(image["image_sha256"], canonical_image_digest(image))
        for record in image["blocks"]:
            payload = bytes.fromhex(record["payload_hex"])
            self.assertEqual(
                record["payload_sha256"], hashlib.sha256(payload).hexdigest()
            )
            self.assertEqual(record["block_name"], self.pack.block_name(record["block"]))

        inspection = self.service.inspect_backup(image)
        self.assertTrue(inspection["digest_present"])
        self.assertTrue(inspection["digest_verified"])
        self.assertTrue(inspection["pack_compatible"])
        self.assertEqual(inspection["restorable_block_ids"], [20, 22])
        self.assertEqual(inspection["controller"]["serial_number"], "D1234567")

    def test_network_protection_targets_are_distinct_and_cpu_is_digest_bound(self):
        targets = [
            {"cpu": 1, "block": 16},
            {"cpu": 2, "block": 16},
        ]
        session = BackupSession({
            (1, 16): bytes(range(18)),
            (2, 16): bytes(reversed(range(18))),
        })

        image = self.service.backup(session, targets, decode=False)

        self.assertEqual(image["requested_targets"], targets)
        self.assertNotIn("requested_block_ids", image)
        self.assertEqual(
            [(record["cpu"], record["block"]) for record in image["blocks"]],
            [(1, 16), (2, 16)],
        )
        self.assertEqual(
            [record["block_name"] for record in image["blocks"]],
            [
                "Netzschutz · Überwachungs-CPU 1",
                "Netzschutz · Überwachungs-CPU 2",
            ],
        )
        self.assertEqual(session.reads, [(1, 16), (2, 16)])
        self.assertEqual(image["image_sha256"], _image_sha256(image))

        inspection = self.service.inspect_backup(image)
        self.assertTrue(inspection["digest_verified"])
        self.assertEqual(inspection["requested_targets"], targets)
        self.assertEqual(inspection["restorable_targets"], targets)
        self.assertNotIn("restorable_block_ids", inspection)

        cpu_tamper = copy.deepcopy(image)
        cpu_tamper["blocks"][0]["cpu"] = 2
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.service.inspect_backup(cpu_tamper)

        timestamp_tamper = copy.deepcopy(image)
        timestamp_tamper["created_utc"] = "2099-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.service.inspect_backup(timestamp_tamper)

        duplicate = copy.deepcopy(image)
        duplicate["blocks"][1]["cpu"] = 1
        duplicate["requested_targets"][1]["cpu"] = 1
        duplicate["image_sha256"] = _image_sha256(duplicate)
        with self.assertRaisesRegex(ValueError, "(?i)(duplicate|mehrfach).*(target|block)"):
            self.service.inspect_backup(duplicate)

    def test_sha256_bound_legacy_cpu0_v3_keeps_its_original_digest_contract(self):
        image = self.make_image(blocks=(20, 22))
        image.pop("requested_targets", None)
        for record in image["blocks"]:
            record.pop("cpu", None)
        image["image_sha256"] = canonical_image_digest(image)
        # created_utc was not part of the 1.2.0 CPU-0 digest.  Preserve that
        # exact contract so archived images remain inspectable.
        image["created_utc"] = "2099-01-01T00:00:00+00:00"

        inspection = self.service.inspect_backup(json.loads(json.dumps(image)))

        self.assertTrue(inspection["digest_present"])
        self.assertTrue(inspection["digest_verified"])
        self.assertEqual(inspection["requested_block_ids"], [20, 22])
        self.assertEqual(inspection["restorable_block_ids"], [20, 22])
        self.assertEqual(
            [(record["cpu"], record["block"]) for record in inspection["records"]],
            [(0, 20), (0, 22)],
        )

    def test_network_protection_requires_exactly_eighteen_payload_bytes(self):
        image = self.service.backup(
            BackupSession({(1, 16): bytes(range(18))}),
            [{"cpu": 1, "block": 16}],
            decode=False,
        )

        for length in (17, 19):
            with self.subTest(path="inspect", length=length):
                malformed = copy.deepcopy(image)
                candidate = bytes(range(length))
                record = malformed["blocks"][0]
                record["payload_hex"] = candidate.hex().upper()
                record["payload_len"] = length
                record["payload_sha256"] = hashlib.sha256(candidate).hexdigest()
                malformed["image_sha256"] = _image_sha256(malformed)
                with self.assertRaisesRegex(ValueError, "exactly 18 bytes"):
                    self.service.inspect_backup(malformed)

            with self.subTest(path="restore", length=length):
                untouched = RestoreSession([])
                candidate = bytes(range(length))
                audit = self.service.restore_payload(
                    untouched,
                    16,
                    candidate,
                    candidate,
                    dry_run=False,
                    cpu=2,
                )
                self.assertFalse(audit.write_attempted)
                self.assertFalse(audit.written)
                self.assertIn("exactly 18 bytes", audit.error)
                self.assertEqual(untouched.calls, [])

    def test_invalid_network_payload_is_a_visible_failed_partial_backup(self):
        targets = [
            {"cpu": 1, "block": 16},
            {"cpu": 2, "block": 16},
        ]
        session = BackupSession({
            (1, 16): bytes(range(17)),
            (2, 16): bytes(range(18)),
        })

        image = self.service.backup(session, targets, decode=True)
        inspection = self.service.inspect_backup(image)

        self.assertEqual(session.reads, [(1, 16), (2, 16)])
        self.assertEqual(image["successful_blocks"], 1)
        self.assertEqual(image["failed_blocks"], 1)
        failed = image["blocks"][0]
        self.assertFalse(failed["ok"])
        self.assertTrue(failed["critical"])
        self.assertEqual(failed["payload_len"], 17)
        self.assertIn("Netzschutz CPU 1", failed["error"])
        self.assertFalse(inspection["records"][0]["restorable"])
        self.assertTrue(inspection["records"][1]["restorable"])
        self.assertEqual(
            inspection["restorable_targets"],
            [{"cpu": 2, "block": 16}],
        )

    def test_identity_failure_is_recorded_without_losing_the_backup(self):
        session = BackupSession({20: b"\x01\x02"})
        self.service.authentication_inputs = lambda _session: (_ for _ in ()).throw(
            RuntimeError("identity unavailable")
        )

        image = self.service.backup(
            session, [20], decode=False, include_identity=True
        )
        inspection = self.service.inspect_backup(image)

        self.assertFalse(image["controller"]["available"])
        self.assertIn("identity unavailable", image["controller"]["error"])
        self.assertFalse(inspection["controller"]["available"])
        self.assertEqual(inspection["restorable_blocks"], 1)

    def test_inspection_rejects_top_level_or_payload_tampering(self):
        image = self.make_image(blocks=(20,))
        top_level_tamper = copy.deepcopy(image)
        top_level_tamper["pack"]["revision"] = "999"
        with self.assertRaisesRegex(ValueError, "image SHA-256 mismatch"):
            self.service.inspect_backup(top_level_tamper)

        stale_payload_digest = copy.deepcopy(image)
        stale_payload_digest["blocks"][0]["payload_hex"] = "00" * 8
        with self.assertRaisesRegex(ValueError, "payload SHA-256 mismatch"):
            self.service.inspect_backup(stale_payload_digest)

        rehashed_payload = copy.deepcopy(stale_payload_digest)
        rehashed_payload["blocks"][0]["payload_sha256"] = hashlib.sha256(
            bytes.fromhex(rehashed_payload["blocks"][0]["payload_hex"])
        ).hexdigest()
        with self.assertRaisesRegex(ValueError, "image SHA-256 mismatch"):
            self.service.inspect_backup(rehashed_payload)

    def test_decoded_float_lexical_change_survives_browser_style_roundtrip(self):
        image = self.make_image(blocks=(20,))
        image["blocks"][0]["values"] = [{
            "key": "synthetic.zero",
            "raw": 0.0,
            "value": 0.0,
            "unit": "",
        }]
        image["image_sha256"] = canonical_image_digest(image)

        browser_roundtrip = json.loads(json.dumps(image))
        browser_roundtrip["blocks"][0]["values"][0]["raw"] = 0
        browser_roundtrip["blocks"][0]["values"][0]["value"] = 0

        inspection = self.service.inspect_backup(browser_roundtrip)
        self.assertTrue(inspection["digest_verified"])

    def test_legacy_image_without_any_digest_remains_accepted(self):
        image = self.make_image(blocks=(20,))
        image.pop("image_sha256")
        image.pop("product")
        image.pop("pack")
        image["blocks"][0].pop("payload_sha256")
        image["blocks"][0].pop("block_name")

        inspection = self.service.inspect_backup(json.dumps(image))

        self.assertFalse(inspection["digest_present"])
        self.assertIsNone(inspection["digest_verified"])
        self.assertTrue(inspection["records"][0]["restorable"])
        self.assertFalse(inspection["records"][0]["payload_digest_present"])
        self.assertEqual(
            inspection["records"][0]["payload_sha256"],
            hashlib.sha256(bytes.fromhex(image["blocks"][0]["payload_hex"])).hexdigest(),
        )

    def test_failed_record_is_valid_but_never_restorable(self):
        image = self.make_image(blocks=(20, 38), failures=(38,))
        inspection = self.service.inspect_backup(image)

        self.assertEqual(inspection["successful_blocks"], 1)
        self.assertEqual(inspection["failed_blocks"], 1)
        self.assertEqual(inspection["restorable_block_ids"], [20])
        failed = next(record for record in inspection["records"] if record["block"] == 38)
        self.assertFalse(failed["restorable"])
        self.assertIn("synthetic read failure", failed["error"])

    def test_failed_wire_record_with_empty_payload_keeps_the_image_inspectable(self):
        class EmptyFailureSession:
            def read_block(self, block, packet=None, timeout=0.9):
                return block_result(block, b"", ok=False)

        image = self.service.backup(
            EmptyFailureSession(), [20], decode=False
        )
        inspection = self.service.inspect_backup(image)

        self.assertEqual(image["blocks"][0]["payload_hex"], "")
        self.assertEqual(image["blocks"][0]["payload_len"], 0)
        self.assertEqual(
            image["blocks"][0]["payload_sha256"], hashlib.sha256(b"").hexdigest()
        )
        self.assertEqual(inspection["failed_blocks"], 1)
        self.assertEqual(inspection["restorable_blocks"], 0)
        self.assertFalse(inspection["records"][0]["restorable"])

    def test_raw_restore_accepts_an_addressable_pack_block_without_fields(self):
        class FieldlessPack:
            data = {"schema": "synthetic-pack/v1"}
            pack_rev = "test"

            @staticmethod
            def addressable_blocks():
                return [21]

            @staticmethod
            def field_map(_block):
                return {}

            @staticmethod
            def block_name(block):
                return f"Synthetic {block}"

        service = DachsService(
            "/dev/fake", 19200, 0.1, FieldlessPack(), readback_delay=0
        )
        image = service.backup(
            BackupSession({21: b"\x01\x02"}), [21], decode=False
        )
        inspection = service.inspect_backup(image)
        self.assertEqual(inspection["restorable_block_ids"], [21])

        session = RestoreSession([b"\x01\x02", b"\x03\x04"])
        audit = service.restore_payload(
            session, 21, b"\x01\x02", b"\x03\x04", dry_run=False
        )
        self.assertTrue(audit.written)

    def test_inspection_rejects_duplicate_unmapped_and_malformed_records(self):
        floating_version = self.make_image(blocks=(20,))
        floating_version["schema_version"] = 3.0
        floating_version["image_sha256"] = canonical_image_digest(floating_version)
        with self.assertRaisesRegex(ValueError, "schema version"):
            self.service.inspect_backup(floating_version)

        duplicate = self.make_image(blocks=(20, 22))
        duplicate["blocks"][1]["block"] = 20
        duplicate["image_sha256"] = canonical_image_digest(duplicate)
        with self.assertRaisesRegex(ValueError, "duplicate backup (block|target)"):
            self.service.inspect_backup(duplicate)

        unmapped = self.make_image(blocks=(20,))
        unmapped["blocks"][0]["block"] = 17
        unmapped["image_sha256"] = canonical_image_digest(unmapped)
        with self.assertRaisesRegex(ValueError, "not mapped and writable"):
            self.service.inspect_backup(unmapped)

        malformed_hex = self.make_image(blocks=(20,))
        malformed_hex["blocks"][0]["payload_hex"] = "0XZ1"
        malformed_hex["image_sha256"] = canonical_image_digest(malformed_hex)
        with self.assertRaisesRegex(ValueError, "payload_hex is invalid"):
            self.service.inspect_backup(malformed_hex)

        wrong_length = self.make_image(blocks=(20,))
        wrong_length["blocks"][0]["payload_len"] += 1
        wrong_length["image_sha256"] = canonical_image_digest(wrong_length)
        with self.assertRaisesRegex(ValueError, "payload_len does not match"):
            self.service.inspect_backup(wrong_length)

    def test_sha256_bound_image_requires_digest_for_every_successful_payload(self):
        image = self.make_image(blocks=(20,))
        image["blocks"][0].pop("payload_sha256")
        image["image_sha256"] = canonical_image_digest(image)

        with self.assertRaisesRegex(ValueError, "payload_sha256 is required"):
            self.service.inspect_backup(image)

    def test_atomic_json_fsyncs_file_and_parent_and_propagates_parent_failure(self):
        original_fsync = os.fsync
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "restore-preimage.json"
            fsync_kinds = []

            def recording_fsync(fd):
                fsync_kinds.append(
                    "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
                )
                return original_fsync(fd)

            with patch("os.fsync", side_effect=recording_fsync):
                write_json_atomic(destination, {"ok": True})

            self.assertEqual(fsync_kinds, ["file", "directory"])
            self.assertEqual(
                json.loads(destination.read_text(encoding="utf-8")),
                {"ok": True},
            )

            def failing_directory_fsync(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError("synthetic parent fsync failure")
                return original_fsync(fd)

            with patch("os.fsync", side_effect=failing_directory_fsync):
                with self.assertRaisesRegex(OSError, "parent fsync failure"):
                    write_json_atomic(destination, {"ok": False})

    def test_restore_noop_and_dry_run_never_touch_the_session(self):
        class UntouchedSession:
            def read_block(self, *args, **kwargs):
                raise AssertionError("no read expected")

            def write_block(self, *args, **kwargs):
                raise AssertionError("no write expected")

        session = UntouchedSession()
        no_op = self.service.restore_payload(
            session, 20, b"\x01\x02", b"\x01\x02", dry_run=False
        )
        self.assertFalse(no_op.written)
        self.assertFalse(no_op.write_attempted)
        self.assertTrue(no_op.readback_ok)
        self.assertEqual(no_op.readback_scope, "block")

        dry_run = self.service.restore_payload(
            session, 20, b"\x01\x02", b"\x03\x04", dry_run=True
        )
        self.assertTrue(dry_run.dry_run)
        self.assertFalse(dry_run.written)
        self.assertFalse(dry_run.write_attempted)
        self.assertIsNone(dry_run.readback_ok)
        self.assertIsNone(dry_run.error)

        network_no_op = self.service.restore_payload(
            session,
            16,
            bytes(range(18)),
            bytes(range(18)),
            dry_run=False,
            cpu=1,
        )
        self.assertEqual(network_no_op.cpu, 1)
        self.assertFalse(network_no_op.write_attempted)
        self.assertTrue(network_no_op.readback_ok)

        network_dry_run = self.service.restore_payload(
            session,
            16,
            bytes(range(18)),
            bytes(reversed(range(18))),
            dry_run=True,
            cpu=2,
        )
        self.assertEqual(network_dry_run.cpu, 2)
        self.assertFalse(network_dry_run.write_attempted)
        self.assertIsNone(network_dry_run.readback_ok)

    def test_live_restore_requires_stability_positive_ack_and_exact_readback(self):
        before = b"\x01\x02\x03\x04"
        target = b"\x05\x06\x07\x08"
        session = RestoreSession([before, target])

        result = self.service.restore_payload(
            session, 20, before, target, dry_run=False
        )

        self.assertTrue(result.written)
        self.assertTrue(result.write_attempted)
        self.assertTrue(result.ack_positive)
        self.assertTrue(result.readback_ok)
        self.assertEqual(result.readback_scope, "block")
        self.assertEqual(result.readback_attempts, 1)
        self.assertEqual(
            session.calls,
            [("read", 20), ("write", 20, target), ("read", 20)],
        )
        self.assertEqual(
            result.changed_keys, ("backup.restore.block[20].full_payload",)
        )

    def test_network_live_restore_uses_addressed_cpu_ack_and_exact_readback(self):
        before = bytes(range(18))
        target = bytes(reversed(range(18)))
        session = RestoreSession([before, target])

        result = self.service.restore_payload(
            session,
            16,
            before,
            target,
            dry_run=False,
            cpu=1,
        )

        self.assertTrue(result.written)
        self.assertTrue(result.write_attempted)
        self.assertTrue(result.ack_positive)
        self.assertTrue(result.readback_ok)
        self.assertEqual(result.cpu, 1)
        self.assertEqual(result.readback_scope, "block")
        self.assertEqual(
            result.changed_keys,
            ("backup.restore.cpu[1].block[16].full_payload",),
        )
        self.assertEqual(
            session.calls,
            [
                ("read", 1, 16),
                ("write", 1, 16, target),
                ("read", 1, 16),
            ],
        )

        mismatch = target[:-1] + bytes([target[-1] ^ 0xFF])
        mismatching_session = RestoreSession(
            [before, mismatch, mismatch, mismatch]
        )
        failed = self.service.restore_payload(
            mismatching_session,
            16,
            before,
            target,
            dry_run=False,
            cpu=2,
        )
        self.assertTrue(failed.write_attempted)
        self.assertTrue(failed.ack_positive)
        self.assertFalse(failed.written)
        self.assertFalse(failed.readback_ok)
        self.assertEqual(failed.cpu, 2)
        self.assertEqual(failed.readback_attempts, 3)
        self.assertIn("full-block readback mismatch", failed.error)

    def test_live_restore_records_negative_ack_as_an_attempted_write(self):
        before = b"\x01\x02"
        target = b"\x03\x04"
        session = RestoreSession([before], ack_positive=False)

        result = self.service.restore_payload(
            session, 20, before, target, dry_run=False
        )

        self.assertFalse(result.written)
        self.assertTrue(result.write_attempted)
        self.assertFalse(result.ack_positive)
        self.assertFalse(result.readback_ok)
        self.assertIn("positive ACK", result.error)
        self.assertEqual(session.calls, [("read", 20), ("write", 20, target)])

    def test_restore_retries_but_never_accepts_a_partial_block_match(self):
        before = b"\x01\x02\x03\x04"
        target = b"\x05\x06\x07\x08"
        partial = b"\x05\x06\x07\x09"
        session = RestoreSession([before, partial, partial, partial])

        result = self.service.restore_payload(
            session, 20, before, target, dry_run=False
        )

        self.assertFalse(result.written)
        self.assertTrue(result.write_attempted)
        self.assertTrue(result.ack_positive)
        self.assertFalse(result.readback_ok)
        self.assertEqual(result.readback_attempts, 3)
        self.assertIn("full-block readback mismatch", result.error)
        self.assertIsNone(result.readback_scope)

    def test_restore_stability_and_validation_fail_before_write(self):
        changed = RestoreSession([b"\x09\x09"])
        result = self.service.restore_payload(
            changed, 20, b"\x01\x02", b"\x03\x04", dry_run=False
        )
        self.assertFalse(result.write_attempted)
        self.assertIn("changed since", result.error)
        self.assertEqual(changed.calls, [("read", 20)])

        untouched = RestoreSession([])
        wrong_length = self.service.restore_payload(
            untouched, 20, b"\x01", b"\x02\x03", dry_run=False
        )
        self.assertFalse(wrong_length.write_attempted)
        self.assertIn("payload length changed", wrong_length.error)

        unmapped = self.service.restore_payload(
            untouched, 17, b"\x01", b"\x02", dry_run=False
        )
        self.assertFalse(unmapped.write_attempted)
        self.assertIn("not mapped and writable", unmapped.error)
        self.assertEqual(untouched.calls, [])

        non_integer = self.service.restore_payload(
            untouched, 20.0, b"\x01", b"\x02", dry_run=False
        )
        self.assertFalse(non_integer.write_attempted)
        self.assertIn("block must be an integer", non_integer.error)
        self.assertEqual(untouched.calls, [])

    def test_inspect_and_restore_reject_payloads_larger_than_the_frame_limit(self):
        image = self.make_image(blocks=(20,))
        oversized = b"\xA5" * 4095
        image["blocks"][0]["payload_hex"] = oversized.hex().upper()
        image["blocks"][0]["payload_len"] = len(oversized)
        image["blocks"][0]["payload_sha256"] = hashlib.sha256(
            oversized
        ).hexdigest()
        image["image_sha256"] = canonical_image_digest(image)
        with self.assertRaisesRegex(ValueError, "4094-byte restore limit"):
            self.service.inspect_backup(image)

        untouched = RestoreSession([])
        audit = self.service.restore_payload(
            untouched, 20, b"\x01" * 4095, b"\x02" * 4095, dry_run=False
        )
        self.assertFalse(audit.write_attempted)
        self.assertIn("4094-byte restore limit", audit.error)
        self.assertEqual(untouched.calls, [])


if __name__ == "__main__":
    unittest.main()
