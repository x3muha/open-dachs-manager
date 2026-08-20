import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from open_dachs_manager.mapping import PackRepository
from open_dachs_manager.network_protection import (
    NETWORK_PROTECTION_CPUS,
    NETWORK_PROTECTION_RESTORE_BLOCKS,
)
from open_dachs_manager.service import (
    BACKUP_LEGACY_TARGETS,
    BACKUP_PAYLOAD_LENGTHS,
    BACKUP_RESTORE_TARGETS,
    BACKUP_TARGETS,
    DachsService,
    _image_sha256,
    write_json_exclusive,
)
from open_dachs_manager.web import DachsWebApp


class SyntheticMaintenanceService(DachsService):
    """Exact-length, hardware-free source for strict archive images."""

    def __init__(self):
        pack = PackRepository(pack_rev="50")
        super().__init__("/dev/not-used", 19200, 0.1, pack)
        self.payloads = {
            target: bytearray(BACKUP_PAYLOAD_LENGTHS[target])
            for target in BACKUP_TARGETS
        }
        self.payloads[(0, 20)][:10] = b"TEST000123"
        self.payloads[(0, 22)][:4] = (4321 * 3600).to_bytes(4, "little")
        fuel = self.pack.field_map(24)["Hka_Mw1.bKraftstofftyp"]
        self.payloads[(0, 24)][int(fuel["offset"])] = 8

    def read_block(self, _session, block, cpu=0):
        payload = bytes(self.payloads[(int(cpu), int(block))])
        return SimpleNamespace(
            ok=True,
            status=0,
            payload=payload,
            response=SimpleNamespace(
                elapsed_ms=0.1,
                crc_errors=0,
                protocol_errors=0,
            ),
        )


class BackupArchiveIdentityTests(unittest.TestCase):
    def setUp(self):
        self.service = SyntheticMaintenanceService()
        self.guard = SimpleNamespace(
            service=self.service,
            pack=self.service.pack,
        )
        self.image, _capture = self.service.maintenance_backup(
            object(), decode=False, created_by="admin"
        )
        inspection = DachsWebApp._strict_backup_archive_inspection(
            self.guard, self.image, require_current_pack=True
        )
        self.assertEqual(inspection["successful_blocks"], 42)
        self.assertEqual(inspection["restorable_blocks"], 38)
        self.assertEqual(
            [
                (item["cpu"], item["block"])
                for item in inspection["records"]
                if item["restorable"]
            ],
            list(BACKUP_RESTORE_TARGETS),
        )

    def _redigest(self, image):
        image["image_sha256"] = _image_sha256(image)
        return image

    def test_restore_contract_uses_the_network_schema_source_of_truth(self):
        network_targets = tuple(
            target for target in BACKUP_RESTORE_TARGETS if target[0] != 0
        )
        self.assertEqual(
            network_targets,
            tuple(
                (cpu, block)
                for cpu in NETWORK_PROTECTION_CPUS
                for block in NETWORK_PROTECTION_RESTORE_BLOCKS
            ),
        )

    def test_declared_serial_cannot_disagree_with_bound_block20_payload(self):
        tampered = copy.deepcopy(self.image)
        tampered["controller"]["serial_number"] = "OTHER00001"
        self._redigest(tampered)

        with self.assertRaisesRegex(ValueError, "widerspricht den B20/B22"):
            DachsWebApp._strict_backup_archive_inspection(self.guard, tampered)

    def test_declared_hours_cannot_disagree_with_bound_block22_payload(self):
        tampered = copy.deepcopy(self.image)
        tampered["controller"]["operating_hours"] = 4322
        self._redigest(tampered)

        with self.assertRaisesRegex(ValueError, "Betriebsstunden widersprechen"):
            DachsWebApp._strict_backup_archive_inspection(self.guard, tampered)

    def test_payload_identity_change_is_rejected_even_after_all_digests_are_updated(self):
        tampered = copy.deepcopy(self.image)
        record20 = next(
            item
            for item in tampered["blocks"]
            if item["cpu"] == 0 and item["block"] == 20
        )
        payload20 = bytearray.fromhex(record20["payload_hex"])
        payload20[:10] = b"OTHER00001"
        record20["payload_hex"] = bytes(payload20).hex().upper()
        record20["payload_sha256"] = hashlib.sha256(payload20).hexdigest()
        self._redigest(tampered)

        with self.assertRaisesRegex(ValueError, "widerspricht den B20/B22"):
            DachsWebApp._strict_backup_archive_inspection(self.guard, tampered)

    def test_old_pack_archive_remains_verifiable_but_not_current_pack_compatible(self):
        old_pack = copy.deepcopy(self.image)
        old_pack["pack"]["revision"] = "49"
        self._redigest(old_pack)

        inspection = DachsWebApp._strict_backup_archive_inspection(
            self.guard, old_pack, require_current_pack=False
        )
        self.assertTrue(inspection["digest_verified"])
        self.assertFalse(inspection["pack_compatible"])

        with self.assertRaisesRegex(ValueError, "aktuellen Pack"):
            DachsWebApp._strict_backup_archive_inspection(
                self.guard, old_pack, require_current_pack=True
            )

    def test_archive_context_is_type_strict_and_created_by_is_a_real_string(self):
        invalid_values = (
            ("version", True),
            ("version", 1.0),
            ("created_by", True),
            ("created_by", ["admin"]),
            ("created_by", "   "),
            ("created_by", "x" * 129),
        )
        for key, value in invalid_values:
            with self.subTest(key=key, value_type=type(value).__name__):
                tampered = copy.deepcopy(self.image)
                tampered["maintenance_archive"][key] = value
                self._redigest(tampered)
                with self.assertRaises(ValueError):
                    DachsWebApp._strict_backup_archive_inspection(
                        self.guard, tampered
                    )

    def test_legacy_38_target_archive_remains_strictly_compatible(self):
        legacy = copy.deepcopy(self.image)
        legacy["maintenance_archive"]["version"] = 1
        allowed = set(BACKUP_LEGACY_TARGETS)
        legacy["blocks"] = [
            item
            for item in legacy["blocks"]
            if (int(item["cpu"]), int(item["block"])) in allowed
        ]
        legacy["requested_targets"] = [
            {"cpu": cpu, "block": block} for cpu, block in BACKUP_LEGACY_TARGETS
        ]
        legacy["requested_blocks"] = len(BACKUP_LEGACY_TARGETS)
        legacy["successful_blocks"] = len(BACKUP_LEGACY_TARGETS)
        legacy["failed_blocks"] = 0
        self._redigest(legacy)

        inspection = DachsWebApp._strict_backup_archive_inspection(
            self.guard, legacy, require_current_pack=True
        )

        self.assertEqual(inspection["successful_blocks"], 38)
        self.assertEqual(inspection["restorable_blocks"], 38)


class BackupArchiveLifecycleTests(unittest.TestCase):
    @staticmethod
    def _app_and_image(directory):
        app = DachsWebApp(data_dir=directory, interval=60)
        service = SyntheticMaintenanceService()
        app.service = service
        app.pack = service.pack
        image, _capture = service.maintenance_backup(
            object(), decode=False, created_by="admin"
        )
        return app, image

    @staticmethod
    def _link_report(app, archive):
        return app.store.create_maintenance_report_with_archive(
            "admin",
            "oil",
            {
                "version": 3,
                "generated_at": "2026-08-18T12:00:00+00:00",
                "generated_by": "admin",
                "pack_rev": app.pack.pack_rev,
                "blocks": {},
                "snapshot": {"complete": True},
                "maintenance_status": {},
            },
            {
                "fuel_type": "oil",
                "technician": "",
                "notes": "",
                "checklist": {},
                "supplemental": {},
                "measurements": {},
            },
            archive,
        )

    def test_archive_file_is_private_downloaded_exactly_and_tamper_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            app, image = self._app_and_image(directory)
            archive = app._persist_maintenance_backup_archive(image)
            indexed = app.store.create_orphan_backup_archive(archive)
            path = Path(directory) / "backup-archive" / archive["filename"]

            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            raw, filename = app.backup_archive_download(indexed["id"])
            self.assertEqual(filename, archive["filename"])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), archive["file_sha256"])
            self.assertEqual(json.loads(raw)["image_sha256"], archive["image_sha256"])

            path.write_bytes(raw + b" ")
            path.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "Archivprüfung fehlgeschlagen"):
                app.backup_archive_download(indexed["id"])
            corrupted = app.store.backup_archive(indexed["id"])
            self.assertEqual(corrupted["status"], "corrupt")
            self.assertFalse(corrupted["verified"])

    def test_report_deletion_keeps_archive_file_and_nulls_foreign_key(self):
        with tempfile.TemporaryDirectory() as directory:
            app, image = self._app_and_image(directory)
            archive = app._persist_maintenance_backup_archive(image)
            report = self._link_report(app, archive)
            indexed = app.store.backup_archive_for_report(report["id"])
            path = Path(directory) / "backup-archive" / indexed["filename"]

            deleted = app.delete_maintenance_report(report["id"])

            self.assertEqual(deleted["status"], "draft")
            self.assertTrue(path.is_file())
            preserved = app.store.backup_archive(indexed["id"])
            self.assertIsNone(preserved["maintenance_report_id"])
            with app.store.database() as database:
                self.assertEqual(database.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_valid_crash_orphan_is_reindexed_but_symlink_is_never_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            app, image = self._app_and_image(directory)
            archive = app._persist_maintenance_backup_archive(image)
            self.assertEqual(app.store.backup_archives(), [])

            target = Path(directory) / "outside.json"
            target.write_text("{}\n", encoding="utf-8")
            target.chmod(0o600)
            link_name = "maintenance-20260818T120000.000000Z-0123456789abcdef.json"
            os.symlink(target, Path(directory) / "backup-archive" / link_name)

            restarted = DachsWebApp(data_dir=directory, interval=60)
            items = restarted.store.backup_archives()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["filename"], archive["filename"])
            self.assertIsNone(items[0]["maintenance_report_id"])
            self.assertTrue(any(link_name in error for error in restarted.backup_archive_errors))

    def test_exclusive_publication_never_replaces_an_existing_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "archive.json"
            first = write_json_exclusive(destination, {"version": 1})
            with self.assertRaises(FileExistsError):
                write_json_exclusive(destination, {"version": 2})
            self.assertEqual(destination.read_bytes(), first)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(directory).glob(".*.tmp")), [])

    def test_restart_turns_inflight_completion_uncertain_and_blocks_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DachsWebApp(data_dir=directory, interval=60)
            report = app.store.create_maintenance_report(
                "admin",
                "gas",
                {"blocks": {}, "maintenance_status": {}},
                {
                    "fuel_type": "gas",
                    "technician": "",
                    "notes": "",
                    "checklist": {},
                    "supplemental": {},
                    "measurements": {},
                },
            )
            with app.store.database() as database:
                database.execute(
                    "UPDATE maintenance_reports SET status='completing' WHERE id=?",
                    (report["id"],),
                )

            restarted = DachsWebApp(data_dir=directory, interval=60)
            recovered = restarted.store.maintenance_report(report["id"])
            self.assertEqual(recovered["status"], "uncertain")
            self.assertTrue(recovered["completion"]["uncertain"])
            with self.assertRaisesRegex(ValueError, "unklarem Abschlusszustand"):
                restarted.delete_maintenance_report(report["id"])


if __name__ == "__main__":
    unittest.main()
