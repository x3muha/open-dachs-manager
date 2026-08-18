import copy
import http.client
import json
import stat
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from open_dachs_manager.auth import AuthInputs
from open_dachs_manager.service import DachsService, _image_sha256
from open_dachs_manager.web import (
    BACKUP_RESTORE_CONFIRMATION,
    DachsHTTPServer,
    DachsWebApp,
    init_users,
)


PAYLOAD_LENGTH = 70
NETWORK_PAYLOAD_LENGTH = 18
ROOT = Path(__file__).resolve().parents[1]


def payload(value: int) -> bytes:
    return bytes([value]) * PAYLOAD_LENGTH


def network_payload(value: int) -> bytes:
    return bytes([value]) * NETWORK_PAYLOAD_LENGTH


# Explicitly artificial fixtures; neither byte string was captured from a Dachs.
SYNTHETIC_NETWORK_CONFIG_PAYLOAD = bytes([1]) + bytes(58)
SYNTHETIC_NETWORK_LIVE_PAYLOAD = bytes(range(56))


def normalize_target(value):
    if isinstance(value, dict):
        return int(value.get("cpu", 0)), int(value["block"])
    if isinstance(value, tuple):
        return int(value[0]), int(value[1])
    return 0, int(value)


def public_target(cpu, block):
    return {"cpu": int(cpu), "block": int(block)}


class RecordingSession:
    """In-memory serial session; a wire write only changes the fake device."""

    def __init__(self, service):
        self.service = service
        self.closed = False

    def write_block(self, block, target, *, packet=None, timeout=None, cpu=0):
        if self.closed:
            raise AssertionError("write on closed fake session")
        target = bytes(target)
        call = (
            ("wire-write", block, target)
            if cpu == 0
            else ("wire-write", cpu, block, target)
        )
        self.service.calls.append(call)
        self.service.wire_writes.append((block, target))
        self.service.wire_targets.append((cpu, block, target))
        if (cpu, block) in self.service.negative_ack_blocks or block in self.service.negative_ack_blocks:
            return SimpleNamespace(ack=SimpleNamespace(positive=False))
        self.service.device_payloads[self.service.device_key(cpu, block)] = target
        return SimpleNamespace(ack=SimpleNamespace(positive=True))


class RecordingBackupRestoreService(DachsService):
    """Real image/restore helpers around a deterministic, hardware-free session."""

    def __init__(self, pack, device_payloads):
        super().__init__(
            "/definitely-not-a-serial-device",
            19200,
            0.1,
            pack,
            readback_attempts=1,
            readback_delay=0,
        )
        self.device_payloads = {}
        for target, block_payload in device_payloads.items():
            cpu, block = normalize_target(target)
            self.device_payloads[self.device_key(cpu, block)] = bytes(block_payload)
        self.serial_number = "TEST-DACHS-123"
        self.operating_hours = 4567
        self.negative_ack_blocks = set()
        self.reset_telemetry()

    def reset_telemetry(self):
        self.calls = []
        self.read_blocks = []
        self.read_targets = []
        self.auth_calls = []
        self.identity_calls = 0
        self.restore_calls = []
        self.restore_targets = []
        self.wire_writes = []
        self.wire_targets = []
        self.session_entries = 0
        self.active_sessions = 0
        self.maximum_active_sessions = 0

    @contextmanager
    def session(self):
        self.session_entries += 1
        self.active_sessions += 1
        self.maximum_active_sessions = max(
            self.maximum_active_sessions, self.active_sessions
        )
        session = RecordingSession(self)
        self.calls.append(("session-enter",))
        try:
            yield session
        finally:
            session.closed = True
            self.calls.append(("session-exit",))
            self.active_sessions -= 1

    @staticmethod
    def device_key(cpu, block):
        return int(block) if int(cpu) == 0 else (int(cpu), int(block))

    def read_block(self, session, block, cpu=0):
        if not isinstance(session, RecordingSession) or session.closed:
            raise AssertionError("read outside the fake service session")
        if cpu not in (0, 1, 2) or cpu and block not in (16, 20, 21):
            raise AssertionError(f"unsupported fake target CPU {cpu}, block {block}")
        block = int(block)
        call = ("read", block) if cpu == 0 else ("read", cpu, block)
        self.calls.append(call)
        self.read_blocks.append(block)
        self.read_targets.append((cpu, block))
        block_payload = self.device_payloads[self.device_key(cpu, block)]
        return SimpleNamespace(
            ok=True,
            status=150,
            payload=block_payload,
            response=SimpleNamespace(
                elapsed_ms=0.1,
                crc_errors=0,
                protocol_errors=0,
            ),
        )

    def authentication_inputs(self, session):
        if not isinstance(session, RecordingSession) or session.closed:
            raise AssertionError("identity read outside the fake service session")
        self.calls.append(("identity",))
        self.identity_calls += 1
        return AuthInputs(self.serial_number, self.operating_hours)

    def authenticate(self, session, level, pass4=None):
        if not isinstance(session, RecordingSession) or session.closed:
            raise AssertionError("authentication outside the fake service session")
        self.calls.append(("auth", level, pass4))
        self.auth_calls.append((level, pass4))
        return SimpleNamespace(ok=True, granted_level=level)

    def restore_payload(self, session, block, before, target, dry_run, cpu=0):
        call = (
            ("restore", block, bool(dry_run))
            if cpu == 0
            else ("restore", cpu, block, bool(dry_run))
        )
        self.calls.append(call)
        self.restore_calls.append((block, bytes(before), bytes(target), bool(dry_run)))
        self.restore_targets.append(
            (cpu, block, bytes(before), bytes(target), bool(dry_run))
        )
        if cpu == 0:
            return super().restore_payload(
                session,
                block,
                before,
                target,
                dry_run,
            )
        return super().restore_payload(
            session,
            block,
            before,
            target,
            dry_run,
            cpu=cpu,
        )

    def image_for(self, targets):
        previous = self.device_payloads
        self.device_payloads = {}
        selection = []
        for raw_target, block_payload in targets.items():
            cpu, block = normalize_target(raw_target)
            self.device_payloads[self.device_key(cpu, block)] = bytes(block_payload)
            selection.append(block if cpu == 0 else public_target(cpu, block))
        try:
            with self.session() as session:
                image = self.backup(
                    session,
                    selection,
                    decode=False,
                    include_identity=True,
                )
        finally:
            self.device_payloads = previous
        self.reset_telemetry()
        return image


class WebBackupRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.app = DachsWebApp(data_dir=self.temporary.name, interval=60)
        self.service = RecordingBackupRestoreService(
            self.app.pack,
            {20: payload(0), 22: payload(0), 24: payload(0)},
        )
        self.app.service = self.service

    def tearDown(self):
        self.temporary.cleanup()

    def test_backup_tab_and_restore_controls_are_wired_through_base_path_api(self):
        index = (ROOT / "src/open_dachs_manager/web/index.html").read_text(
            encoding="utf-8"
        )
        app = (ROOT / "src/open_dachs_manager/web/app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('data-view="backupView"', index)
        self.assertIn('id="backupView" class="app-view" hidden', index)
        for element_id in (
            "backupBlockList",
            "backupSelectAll",
            "backupSelectNone",
            "backupCreate",
            "restoreFile",
            "restoreBlockList",
            "restoreSelectAll",
            "restoreSelectNone",
            "restoreWriteEnabled",
            "restoreConfirmation",
            "restoreSubmit",
            "restoreResults",
        ):
            self.assertIn(f'id="{element_id}"', index)
        self.assertIn('accept=".json,application/json"', index)
        self.assertIn('class="panel restore-panel backup-admin" hidden', index)
        self.assertIn('value="4"', index)

        self.assertIn(
            'backup: { image: null, inspection: null, busy: false, importGeneration: 0 }',
            app,
        )
        self.assertIn('api("/api/backup/create"', app)
        self.assertIn('api("/api/backup/inspect"', app)
        self.assertIn('api("/api/backup/restore"', app)
        self.assertIn("function backupSchemaBlocks()", app)
        self.assertIn("(state.schema?.network_protection || [])", app)
        self.assertIn(".filter((item) => item.backup_eligible !== false);", app)
        self.assertIn(
            "`/api/network-protection/${requestedCpu}/${requestedBlock}`",
            app,
        )
        self.assertIn("state.block.writable === false", app)
        self.assertIn('data-raw-mode-toggle', app)
        self.assertIn('value = `raw:${compareValue}`;', app)
        self.assertIn('if ($("writeEnabled")) $("writeEnabled").checked = false;', app)
        self.assertIn('if ($("pass4")) $("pass4").value = "";', app)
        self.assertIn('result.readback_ok && result.write_attempted === false', app)
        self.assertIn('ACHTUNG: Schreibtelegramm', app)
        self.assertNotIn('result.written ? `${target}', app)
        self.assertIn(
            '.map((input) => ({ cpu: Number(input.dataset.cpu ?? 0), block: Number(input.dataset.block ?? input.value) }))',
            app,
        )
        self.assertIn('data-target-key="${escapeHtml(targetKey)}"', app)
        self.assertIn("Netzschutz · Überwachungs-CPU ${cpu}", app)
        self.assertIn('RESTORE_CONFIRMATION = "SICHERUNG WIEDERHERSTELLEN"', app)
        self.assertIn('window.confirm(`LIVE-Wiederherstellung starten?', app)
        self.assertIn('state.backup.image = null;', app)
        self.assertNotIn('fetch("/api/backup', app)

    def test_layout4_config_is_writable_live_values_fail_closed_and_backup_stays_proven(self):
        complete_schema = self.app.schema()
        schemas = complete_schema["network_protection"]
        self.assertEqual(
            [(item["cpu"], item["block"]) for item in schemas],
            [(1, 16), (1, 20), (1, 21), (2, 16), (2, 20), (2, 21)],
        )
        self.assertEqual(
            [len(item["fields"]) for item in schemas],
            [18, 39, 28, 18, 39, 28],
        )
        backup_targets = [
            (0, item["block"])
            for item in complete_schema["blocks"]
        ] + [
            (item["cpu"], item["block"])
            for item in schemas
            if item["backup_eligible"]
        ]
        self.assertEqual(len(backup_targets), 38)
        self.assertEqual(
            [target for target in backup_targets if target[0]],
            [(1, 16), (2, 16)],
        )

        self.service.device_payloads[(1, 20)] = SYNTHETIC_NETWORK_CONFIG_PAYLOAD
        self.service.device_payloads[(1, 21)] = SYNTHETIC_NETWORK_LIVE_PAYLOAD
        config = self.app.read_network_protection(1, 20)
        live = self.app.read_network_protection(1, 21)

        self.assertTrue(config["writable"])
        self.assertFalse(live["writable"])
        self.assertEqual(config["payload_len"], 59)
        self.assertEqual(live["payload_len"], 56)
        self.assertEqual(config["fields"][0]["value"], "Benutzer")
        self.assertEqual(live["fields"][0]["value"], 2.56)
        self.assertEqual(self.service.read_targets, [(1, 20), (1, 21)])

        self.service.reset_telemetry()
        dry_run = self.app.write_network_protection(
            "admin",
            1,
            [{"key": "NetzKonfig1.usSpannung1Unten", "value": "184,1"}],
            4,
            "",
            False,
            block=20,
        )
        self.assertTrue(dry_run["dry_run"])
        self.assertFalse(dry_run["written"])
        self.assertEqual(self.service.session_entries, 1)
        self.assertEqual(self.service.auth_calls, [])
        self.assertEqual(self.service.wire_writes, [])

        self.service.reset_telemetry()
        with self.assertRaisesRegex(PermissionError, "nur lesbar"):
            self.app.write_network_protection(
                "admin",
                1,
                [{"key": "Netzwerte1.usMeanVoltageL1", "value": 230}],
                4,
                "",
                True,
                block=21,
            )
        self.assertEqual(self.service.session_entries, 0)
        self.assertEqual(self.service.auth_calls, [])
        self.assertEqual(self.service.wire_writes, [])

        for block in (20, 21):
            with self.subTest(backup_block=block):
                with self.assertRaises(ValueError):
                    self.app.create_backup([{"cpu": 1, "block": block}])
        self.assertEqual(self.service.session_entries, 0)

    def test_layout4_profile_is_encoded_before_dependent_times(self):
        self.service.device_payloads[(1, 20)] = SYNTHETIC_NETWORK_CONFIG_PAYLOAD

        result = self.app.write_network_protection(
            "admin",
            1,
            [
                {
                    "key": "NetzKonfig1.usAbschaltzeitU1Oben",
                    "value": "1,02",
                },
                {"key": "NetzKonfig1.ubSchutzart", "value": 5},
            ],
            4,
            "",
            False,
            block=20,
        )

        after = bytes.fromhex(result["after_hex"])
        self.assertEqual(after[0], 5)
        # Profile 5 uses +2: displayed 1.02 s therefore stores raw 100.
        self.assertEqual(after[5:7], b"d\x00")
        self.assertEqual(
            result["changed_keys"],
            [
                "NetzKonfig1.ubSchutzart",
                "NetzKonfig1.usAbschaltzeitU1Oben",
            ],
        )
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.service.auth_calls, [])
        self.assertEqual(self.service.wire_writes, [])

    def test_block_selection_rejects_bool_string_float_and_duplicate(self):
        invalid_selections = (
            ([True], "ganze Zahlen"),
            (["20"], "ganze Zahlen"),
            ([20.0], "ganze Zahlen"),
            ([20, 20], "mehrfach"),
        )
        for selection, message in invalid_selections:
            with self.subTest(selection=selection):
                with self.assertRaisesRegex(ValueError, message):
                    self.app.create_backup(selection)
        self.assertEqual(self.service.session_entries, 0)
        self.assertEqual(self.service.read_blocks, [])
        self.assertEqual(self.service.wire_writes, [])

        image = self.service.image_for({20: payload(20)})
        with self.assertRaisesRegex(ValueError, "mehrfach"):
            self.app.restore_backup(
                "admin",
                image,
                image["image_sha256"],
                [20, 20],
                4,
                "1234",
                False,
                "",
            )
        self.assertEqual(self.service.session_entries, 0)

    def test_create_reads_selected_blocks_in_one_session_without_write(self):
        created = self.app.create_backup([20, 24])

        self.assertTrue(created["ok"])
        self.assertEqual(created["summary"], {
            "requested_blocks": 2,
            "successful_blocks": 2,
            "failed_blocks": 0,
        })
        self.assertEqual(created["image"]["requested_block_ids"], [20, 24])
        self.assertTrue(created["inspection"]["digest_verified"])
        self.assertEqual(self.service.session_entries, 1)
        self.assertEqual(self.service.maximum_active_sessions, 1)
        self.assertEqual(self.service.read_blocks, [20, 24])
        self.assertEqual(self.service.identity_calls, 1)
        self.assertEqual(self.service.auth_calls, [])
        self.assertEqual(self.service.restore_calls, [])
        self.assertEqual(self.service.wire_writes, [])

    def test_network_targets_share_block_number_but_backup_as_distinct_cpus(self):
        selected = [
            public_target(0, 20),
            public_target(1, 16),
            public_target(2, 16),
        ]
        self.service.device_payloads[(1, 16)] = network_payload(1)
        self.service.device_payloads[(2, 16)] = network_payload(2)

        created = self.app.create_backup(selected)

        self.assertTrue(created["ok"])
        self.assertEqual(created["summary"], {
            "requested_blocks": 3,
            "successful_blocks": 3,
            "failed_blocks": 0,
        })
        self.assertEqual(created["image"]["requested_targets"], selected)
        self.assertEqual(
            [(item["cpu"], item["block"]) for item in created["image"]["blocks"]],
            [(0, 20), (1, 16), (2, 16)],
        )
        self.assertEqual(
            [item["target_key"] for item in created["inspection"]["blocks"]],
            ["0:20", "1:16", "2:16"],
        )
        self.assertEqual(
            [item["name"] for item in created["inspection"]["blocks"]],
            [
                self.app.pack.block_name(20),
                "Netzschutz · Überwachungs-CPU 1",
                "Netzschutz · Überwachungs-CPU 2",
            ],
        )
        self.assertEqual(self.service.session_entries, 1)
        self.assertEqual(
            self.service.read_targets,
            [(0, 20), (1, 16), (2, 16)],
        )
        self.assertEqual(self.service.auth_calls, [])
        self.assertEqual(self.service.restore_calls, [])
        self.assertEqual(self.service.wire_targets, [])

    def test_network_target_selection_uses_cpu_block_pair_for_duplicates(self):
        invalid = (
            [{"cpu": True, "block": 16}],
            [{"cpu": 1.0, "block": 16}],
            [{"cpu": 1, "block": 16.0}],
        )
        for selection in invalid:
            with self.subTest(selection=selection):
                with self.assertRaises(ValueError):
                    self.app.create_backup(selection)

        duplicate = [public_target(1, 16), public_target(1, 16)]
        with self.assertRaisesRegex(ValueError, "mehrfach"):
            self.app.create_backup(duplicate)

        self.service.device_payloads[(1, 16)] = network_payload(1)
        self.service.device_payloads[(2, 16)] = network_payload(2)
        result = self.app.create_backup(
            [public_target(1, 16), public_target(2, 16)]
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["image"]["requested_targets"],
            [public_target(1, 16), public_target(2, 16)],
        )

    def test_inspect_is_offline_and_reports_verified_restorable_blocks(self):
        image = self.service.image_for({20: payload(20), 24: payload(24)})

        inspection = self.app.inspect_backup(json.dumps(image))

        self.assertEqual(
            inspection["schema"],
            "open-dachs-manager-backup-inspection/v1",
        )
        self.assertEqual(inspection["image_sha256"], image["image_sha256"])
        self.assertTrue(inspection["digest_present"])
        self.assertTrue(inspection["digest_verified"])
        self.assertTrue(inspection["pack_compatible"])
        self.assertTrue(inspection["live_restore_compatible"])
        self.assertEqual(
            [(item["block"], item["restorable"]) for item in inspection["blocks"]],
            [(20, True), (24, True)],
        )
        self.assertEqual(self.service.session_entries, 0)
        self.assertEqual(self.service.read_blocks, [])
        self.assertEqual(self.service.auth_calls, [])
        self.assertEqual(self.service.wire_writes, [])

        tampered = copy.deepcopy(image)
        tampered["blocks"][0]["payload_hex"] = payload(99).hex().upper()
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.app.inspect_backup(tampered)
        self.assertEqual(self.service.session_entries, 0)

    def test_restore_dry_run_preflights_but_never_authenticates_or_writes(self):
        image = self.service.image_for({20: payload(20), 24: payload(24)})

        result = self.app.restore_backup(
            "admin",
            image,
            image["image_sha256"],
            [20, 24],
            4,
            "1234",
            False,
            "",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(result["differing_blocks"], 2)
        self.assertEqual(result["written_blocks"], 0)
        self.assertEqual(result["summary"], {
            "requested": 2,
            "planned": 2,
            "restored": 0,
            "unchanged": 0,
            "failed": 0,
            "not_attempted": 0,
            "write_attempted": 0,
            "uncertain": 0,
        })
        self.assertEqual(
            [(item["block"], item["status"]) for item in result["results"]],
            [(24, "planned"), (20, "planned")],
        )
        self.assertEqual(self.service.session_entries, 1)
        self.assertEqual(self.service.read_blocks, [20, 24])
        self.assertEqual(self.service.identity_calls, 0)
        self.assertEqual(self.service.auth_calls, [])
        self.assertEqual(
            [(block, dry_run) for block, _before, _target, dry_run in self.service.restore_calls],
            [(24, True), (20, True)],
        )
        self.assertEqual(self.service.wire_writes, [])
        audits = self.app.store.audits()
        self.assertEqual(len(audits), 2)
        self.assertTrue(all(item["audit"]["dry_run"] for item in audits))
        self.assertTrue(all(not item["audit"]["write_attempted"] for item in audits))
        self.assertFalse(
            (Path(self.temporary.name) / "restore-preimages").exists()
        )

    def test_network_restore_dry_run_and_live_noop_never_authenticate_or_write(self):
        targets = {
            (1, 16): network_payload(1),
            (2, 16): network_payload(2),
        }
        selection = [public_target(1, 16), public_target(2, 16)]
        image = self.service.image_for(targets)
        self.service.device_payloads[(1, 16)] = network_payload(0)
        self.service.device_payloads[(2, 16)] = network_payload(0)

        dry_run = self.app.restore_backup(
            "admin",
            image,
            image["image_sha256"],
            selection,
            4,
            "1234",
            False,
            "",
        )

        self.assertTrue(dry_run["ok"])
        self.assertEqual(dry_run["mode"], "dry-run")
        self.assertEqual(dry_run["summary"]["planned"], 2)
        self.assertEqual(
            [(item["target_key"], item["status"]) for item in dry_run["results"]],
            [("1:16", "planned"), ("2:16", "planned")],
        )
        self.assertEqual(self.service.auth_calls, [])
        self.assertEqual(self.service.wire_targets, [])
        self.assertEqual(
            [(cpu, block, is_dry) for cpu, block, _before, _after, is_dry in self.service.restore_targets],
            [(1, 16, True), (2, 16, True)],
        )

        self.service.reset_telemetry()
        self.service.device_payloads[(1, 16)] = network_payload(1)
        self.service.device_payloads[(2, 16)] = network_payload(2)
        no_op = self.app.restore_backup(
            "admin",
            image,
            image["image_sha256"],
            selection,
            4,
            "1234",
            True,
            BACKUP_RESTORE_CONFIRMATION,
        )

        self.assertTrue(no_op["ok"])
        self.assertEqual(no_op["mode"], "live")
        self.assertEqual(no_op["unchanged_blocks"], 2)
        self.assertEqual(
            [(item["target_key"], item["status"]) for item in no_op["results"]],
            [("1:16", "unchanged"), ("2:16", "unchanged")],
        )
        self.assertEqual(self.service.auth_calls, [])
        self.assertEqual(self.service.wire_targets, [])
        self.assertTrue(all(not item["write_attempted"] for item in no_op["results"]))
        self.assertFalse(
            (Path(self.temporary.name) / "restore-preimages").exists()
        )

    def test_live_noop_never_authenticates_or_writes(self):
        image = self.service.image_for({20: payload(0), 24: payload(0)})

        result = self.app.restore_backup(
            "admin",
            image,
            image["image_sha256"],
            [20, 24],
            4,
            "1234",
            True,
            BACKUP_RESTORE_CONFIRMATION,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "live")
        self.assertEqual(result["differing_blocks"], 0)
        self.assertEqual(result["written_blocks"], 0)
        self.assertEqual(result["unchanged_blocks"], 2)
        self.assertEqual(
            [(item["block"], item["status"]) for item in result["results"]],
            [(24, "unchanged"), (20, "unchanged")],
        )
        self.assertEqual(self.service.session_entries, 1)
        self.assertEqual(self.service.read_blocks, [20, 24])
        self.assertEqual(self.service.identity_calls, 0)
        self.assertEqual(self.service.auth_calls, [])
        self.assertEqual(self.service.wire_writes, [])
        self.assertTrue(all(item["readback_ok"] for item in result["results"]))
        self.assertTrue(all(not item["write_attempted"] for item in result["results"]))
        self.assertFalse(
            (Path(self.temporary.name) / "restore-preimages").exists()
        )

    def test_changed_live_batch_preflights_once_authenticates_once_and_writes_auth_blocks_last(self):
        targets = {20: payload(20), 22: payload(22), 24: payload(24)}
        image = self.service.image_for(targets)

        result = self.app.restore_backup(
            "admin",
            image,
            image["image_sha256"],
            [20, 22, 24],
            4,
            "1234",
            True,
            BACKUP_RESTORE_CONFIRMATION,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["differing_blocks"], 3)
        self.assertEqual(result["written_blocks"], 3)
        self.assertEqual(result["auth_level_granted"], 4)
        self.assertEqual(result["summary"], {
            "requested": 3,
            "planned": 0,
            "restored": 3,
            "unchanged": 0,
            "failed": 0,
            "not_attempted": 0,
            "write_attempted": 3,
            "uncertain": 0,
        })
        self.assertEqual(
            [(item["block"], item["status"]) for item in result["results"]],
            [(24, "restored"), (20, "restored"), (22, "restored")],
        )
        for item in result["results"]:
            self.assertTrue(item["written"])
            self.assertTrue(item["write_attempted"])
            self.assertTrue(item["ack_positive"])
            self.assertTrue(item["readback_ok"])
            self.assertEqual(item["readback_scope"], "block")
            self.assertEqual(item["changed_bytes"], PAYLOAD_LENGTH)

        self.assertEqual(self.service.session_entries, 1)
        self.assertEqual(self.service.maximum_active_sessions, 1)
        self.assertEqual(self.service.auth_calls, [(4, "1234")])
        self.assertEqual(self.service.identity_calls, 1)
        self.assertEqual(
            self.service.calls[:9],
            [
                ("session-enter",),
                ("read", 20),
                ("read", 22),
                ("read", 24),
                ("read", 20),
                ("read", 22),
                ("read", 24),
                ("identity",),
                ("auth", 4, "1234"),
            ],
        )
        self.assertEqual(
            [block for block, _before, _target, _dry in self.service.restore_calls],
            [24, 20, 22],
        )
        self.assertEqual(
            [block for block, _target in self.service.wire_writes],
            [24, 20, 22],
        )
        self.assertEqual(self.service.device_payloads, targets)

        preimage_metadata = result["preimage"]
        self.assertIsNotNone(preimage_metadata)
        preimage_path = (
            Path(self.temporary.name)
            / "restore-preimages"
            / preimage_metadata["filename"]
        )
        self.assertTrue(preimage_path.is_file())
        self.assertEqual(stat.S_IMODE(preimage_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(preimage_path.parent.stat().st_mode), 0o700)
        preimage = json.loads(preimage_path.read_text(encoding="utf-8"))
        preimage_inspection = self.service.inspect_backup(preimage)
        self.assertEqual(
            preimage_inspection["image_sha256"],
            preimage_metadata["image_sha256"],
        )
        self.assertEqual(preimage_inspection["restorable_block_ids"], [20, 22, 24])
        self.assertTrue(all(
            bytes.fromhex(item["payload_hex"]) == payload(0)
            for item in preimage_inspection["records"]
        ))

        audits = sorted(self.app.store.audits(), key=lambda item: item["id"])
        self.assertEqual([item["block"] for item in audits], [24, 20, 22])
        for item in audits:
            block = item["block"]
            audit = item["audit"]
            self.assertEqual(item["username"], "admin")
            self.assertEqual(audit["operation"], "backup-restore")
            self.assertEqual(audit["image_sha256"], image["image_sha256"])
            self.assertEqual(audit["auth_level_requested"], 4)
            self.assertEqual(audit["auth_level_granted"], 4)
            self.assertEqual(audit["before_hex"], payload(0).hex(" ").upper())
            self.assertEqual(audit["after_hex"], targets[block].hex(" ").upper())
            self.assertTrue(audit["written"])
            self.assertTrue(audit["write_attempted"])
            self.assertTrue(audit["ack_positive"])
            self.assertTrue(audit["readback_ok"])
            self.assertEqual(audit["readback_scope"], "block")
            self.assertEqual(audit["preimage"], preimage_metadata)

    def test_network_changed_live_batch_requires_ack_and_exact_cpu_readback(self):
        targets = {
            (1, 16): network_payload(1),
            (2, 16): network_payload(2),
        }
        selection = [public_target(1, 16), public_target(2, 16)]
        image = self.service.image_for(targets)
        self.service.device_payloads[(1, 16)] = network_payload(0)
        self.service.device_payloads[(2, 16)] = network_payload(0)

        result = self.app.restore_backup(
            "admin",
            image,
            image["image_sha256"],
            selection,
            4,
            "1234",
            True,
            BACKUP_RESTORE_CONFIRMATION,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["differing_blocks"], 2)
        self.assertEqual(result["written_blocks"], 2)
        self.assertEqual(result["auth_level_granted"], 4)
        self.assertEqual(result["summary"]["write_attempted"], 2)
        self.assertEqual(
            [(item["target_key"], item["status"]) for item in result["results"]],
            [("1:16", "restored"), ("2:16", "restored")],
        )
        for item in result["results"]:
            self.assertIn(item["cpu"], (1, 2))
            self.assertEqual(item["block"], 16)
            self.assertTrue(item["critical"])
            self.assertTrue(item["written"])
            self.assertTrue(item["write_attempted"])
            self.assertTrue(item["ack_positive"])
            self.assertTrue(item["readback_ok"])
            self.assertEqual(item["readback_scope"], "block")
            self.assertEqual(item["changed_bytes"], NETWORK_PAYLOAD_LENGTH)

        self.assertEqual(self.service.session_entries, 1)
        self.assertEqual(self.service.auth_calls, [(4, "1234")])
        self.assertEqual(
            [(cpu, block) for cpu, block, _payload in self.service.wire_targets],
            [(1, 16), (2, 16)],
        )
        self.assertEqual(
            self.service.device_payloads[(1, 16)],
            network_payload(1),
        )
        self.assertEqual(
            self.service.device_payloads[(2, 16)],
            network_payload(2),
        )

        preimage = json.loads(
            (
                Path(self.temporary.name)
                / "restore-preimages"
                / result["preimage"]["filename"]
            ).read_text(encoding="utf-8")
        )
        preimage_inspection = self.service.inspect_backup(preimage)
        self.assertEqual(
            preimage_inspection["restorable_targets"],
            selection,
        )

        audits = sorted(self.app.store.audits(), key=lambda item: item["id"])
        self.assertEqual(
            [(item["audit"]["cpu"], item["block"]) for item in audits],
            [(1, 16), (2, 16)],
        )
        self.assertTrue(all(item["audit"]["operation"] == "backup-restore" for item in audits))
        self.assertTrue(all(item["audit"]["critical"] for item in audits))
        self.assertTrue(all(item["audit"]["ack_positive"] for item in audits))
        self.assertTrue(all(item["audit"]["readback_ok"] for item in audits))

    def test_live_batch_fails_fast_and_audits_the_attempted_write(self):
        targets = {20: payload(20), 22: payload(22), 24: payload(24)}
        image = self.service.image_for(targets)
        self.service.negative_ack_blocks.add(24)

        result = self.app.restore_backup(
            "admin",
            image,
            image["image_sha256"],
            [20, 22, 24],
            4,
            "1234",
            True,
            BACKUP_RESTORE_CONFIRMATION,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["stopped_at_block"], 24)
        self.assertEqual(result["written_blocks"], 0)
        self.assertEqual(result["failed_blocks"], 1)
        self.assertEqual(result["write_attempted_blocks"], 1)
        self.assertEqual(result["uncertain_blocks"], 1)
        self.assertEqual(result["summary"]["not_attempted"], 2)
        self.assertEqual(
            [(item["block"], item["status"]) for item in result["results"]],
            [(24, "failed"), (20, "not-attempted"), (22, "not-attempted")],
        )
        self.assertEqual(self.service.read_blocks[:3], [20, 22, 24])
        self.assertEqual(self.service.auth_calls, [(4, "1234")])
        self.assertEqual(
            [block for block, _target in self.service.wire_writes],
            [24],
        )
        self.assertEqual(
            [block for block, _before, _target, _dry in self.service.restore_calls],
            [24],
        )
        audits = self.app.store.audits()
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["block"], 24)
        self.assertEqual(audits[0]["audit"]["operation"], "backup-restore")
        self.assertTrue(audits[0]["audit"]["write_attempted"])
        self.assertFalse(audits[0]["audit"]["ack_positive"])
        self.assertFalse(audits[0]["audit"]["written"])
        self.assertIn("positive ACK", audits[0]["audit"]["error"])

    def test_live_guards_fail_before_authentication_or_wire_write(self):
        targets = {20: payload(20), 24: payload(24)}

        image = self.service.image_for(targets)
        with self.assertRaisesRegex(ValueError, "nach der Prüfung verändert"):
            self.app.restore_backup(
                "admin", image, "0" * 64, [20, 24], 4, "1234", True,
                BACKUP_RESTORE_CONFIRMATION,
            )
        self.assertEqual(self.service.session_entries, 0)

        with self.assertRaisesRegex(ValueError, "Bestätigung muss exakt"):
            self.app.restore_backup(
                "admin", image, image["image_sha256"], [20, 24], 4, "1234",
                True, "WIEDERHERSTELLEN",
            )
        self.assertEqual(self.service.session_entries, 0)

        without_digest = copy.deepcopy(image)
        without_digest.pop("image_sha256")
        self.service.reset_telemetry()
        with self.assertRaisesRegex(ValueError, "Hash der geprüften Datei"):
            self.app.restore_backup(
                "admin", without_digest, None, [20, 24], 4, "1234", True,
                BACKUP_RESTORE_CONFIRMATION,
            )
        self.assertEqual(self.service.auth_calls, [])
        self.assertEqual(self.service.wire_writes, [])

        missing_pack = copy.deepcopy(image)
        missing_pack.pop("pack")
        missing_pack["image_sha256"] = _image_sha256(missing_pack)
        inspected_missing_pack = self.app.inspect_backup(missing_pack)
        self.assertFalse(inspected_missing_pack["pack_compatible"])
        self.assertFalse(inspected_missing_pack["live_restore_compatible"])
        self.service.reset_telemetry()
        with self.assertRaisesRegex(ValueError, "Packstand"):
            self.app.restore_backup(
                "admin", missing_pack, missing_pack["image_sha256"], [20, 24],
                4, "1234", True, BACKUP_RESTORE_CONFIRMATION,
            )
        self.assertEqual(self.service.auth_calls, [])
        self.assertEqual(self.service.wire_writes, [])

        wrong_pack = copy.deepcopy(image)
        wrong_pack["pack"]["revision"] = "999"
        wrong_pack["image_sha256"] = _image_sha256(wrong_pack)
        self.service.reset_telemetry()
        with self.assertRaisesRegex(ValueError, "Packstand"):
            self.app.restore_backup(
                "admin", wrong_pack, wrong_pack["image_sha256"], [20, 24], 4,
                "1234", True, BACKUP_RESTORE_CONFIRMATION,
            )
        self.assertEqual(self.service.auth_calls, [])
        self.assertEqual(self.service.wire_writes, [])

        self.service.reset_telemetry()
        self.service.serial_number = "OTHER-DACHS-999"
        with self.assertRaisesRegex(PermissionError, "anderen Regler"):
            self.app.restore_backup(
                "admin", image, image["image_sha256"], [20, 24], 4, "1234",
                True, BACKUP_RESTORE_CONFIRMATION,
            )
        self.assertEqual(self.service.auth_calls, [])
        self.assertEqual(self.service.wire_writes, [])

        self.service.serial_number = "TEST-DACHS-123"
        self.service.reset_telemetry()
        del self.service.device_payloads[24]
        with self.assertRaises(KeyError):
            self.app.restore_backup(
                "admin", image, image["image_sha256"], [20, 24], 4, "1234",
                True, BACKUP_RESTORE_CONFIRMATION,
            )
        self.assertEqual(self.service.auth_calls, [])
        self.assertEqual(self.service.wire_writes, [])


class BackupRestoreHTTPTests(unittest.TestCase):
    def test_network_routes_write_config_but_keep_live_values_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            init_users(
                directory,
                admin_password="AdminPasswort123",
                guest_password="GastPasswort123",
            )
            app = DachsWebApp(data_dir=directory, interval=60)
            service = RecordingBackupRestoreService(
                app.pack,
                {
                    (1, 16): network_payload(0),
                    (1, 20): SYNTHETIC_NETWORK_CONFIG_PAYLOAD,
                    (1, 21): SYNTHETIC_NETWORK_LIVE_PAYLOAD,
                },
            )
            app.service = service
            admin_token = app.login("admin", "AdminPasswort123")[0]
            guest_token = app.login("gast", "GastPasswort123")[0]
            server = DachsHTTPServer(("127.0.0.1", 0), app, base_path="/dachs")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def request(method, path, token=None, payload_data=None, api_token=None):
                connection = http.client.HTTPConnection(*server.server_address, timeout=2)
                headers = {}
                if token is not None:
                    headers["Cookie"] = f"open_dachs_session={token}"
                if api_token is not None:
                    headers["Authorization"] = f"Bearer {api_token}"
                body = None
                if payload_data is not None:
                    headers["Content-Type"] = "application/json"
                    body = json.dumps(payload_data)
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                raw = response.read().decode("utf-8")
                result = json.loads(raw) if raw else {}
                status = response.status
                connection.close()
                return status, result

            try:
                status, legacy = request(
                    "GET", "/dachs/api/network-protection/1", guest_token
                )
                self.assertEqual(status, 200)
                self.assertEqual(legacy["block"], 16)

                status, config = request(
                    "GET", "/dachs/api/network-protection/1/20", guest_token
                )
                self.assertEqual(status, 200)
                self.assertEqual(config["block"], 20)
                self.assertTrue(config["writable"])

                status, live = request(
                    "GET", "/dachs/api/network-protection/1/21", guest_token
                )
                self.assertEqual(status, 200)
                self.assertEqual(live["block"], 21)
                self.assertEqual(live["fields"][0]["value"], 2.56)

                service.reset_telemetry()
                status, written = request(
                    "POST",
                    "/dachs/api/network-protection/1/20",
                    admin_token,
                    {
                        "changes": [
                            {"key": "NetzKonfig1.ubSchutzart", "value": 1}
                        ],
                        "auth_level": 4,
                        "write_enabled": False,
                    },
                )
                self.assertEqual(status, 200)
                self.assertTrue(written["dry_run"])
                self.assertEqual(service.session_entries, 1)
                self.assertEqual(service.auth_calls, [])
                self.assertEqual(service.wire_writes, [])

                service.reset_telemetry()
                status, denied = request(
                    "POST",
                    "/dachs/api/network-protection/1/21",
                    admin_token,
                    {
                        "changes": [
                            {"key": "Netzwerte1.usMeanVoltageL1", "value": 230}
                        ],
                        "auth_level": 4,
                        "write_enabled": True,
                    },
                )
                self.assertEqual(status, 403)
                self.assertIn("nur lesbar", denied["error"])
                self.assertEqual(service.session_entries, 0)

                api_access = app.store.create_api_token(
                    "admin", "Netzschutz-Schreibschutztest", ["write"]
                )
                app.set_api_settings({"write_enabled": True, "auth_level": 4})
                status, api_written = request(
                    "POST",
                    "/dachs/api/v1/actions/set-value",
                    payload_data={
                        "cpu": 1,
                        "block": 20,
                        "key": "NetzKonfig1.usSpannung1Unten",
                        "value": "184,1",
                        "request_id": "writable-b20",
                    },
                    api_token=api_access["token"],
                )
                self.assertEqual(status, 200)
                self.assertTrue(api_written["ok"])
                self.assertEqual(service.auth_calls, [(4, None)])
                self.assertEqual(
                    [(cpu, block) for cpu, block, _payload in service.wire_targets],
                    [(1, 20)],
                )

                service.reset_telemetry()
                guarded_targets = (
                    {
                        "cpu": 1,
                        "block": 21,
                        "key": "Netzwerte1.usMeanVoltageL1",
                        "value": 230,
                        "request_id": "readonly-b21",
                    },
                )
                for guarded in guarded_targets:
                    with self.subTest(api_block=guarded["block"]):
                        status, denied = request(
                            "POST",
                            "/dachs/api/v1/actions/set-value",
                            payload_data=guarded,
                            api_token=api_access["token"],
                        )
                        self.assertEqual(status, 403)
                        self.assertIn("nur lesbar", denied["error"])

                malformed_targets = (
                    {"cpu": True, "block": 16, "request_id": "bool-cpu"},
                    {"cpu": 1.9, "block": 16, "request_id": "float-cpu"},
                    {"cpu": 1, "block": 16.9, "request_id": "float-block"},
                )
                for malformed in malformed_targets:
                    malformed.update({
                        "key": "UC1.SA1.ubLaendercode",
                        "value": 12,
                    })
                    with self.subTest(target=malformed):
                        status, _ = request(
                            "POST",
                            "/dachs/api/v1/actions/set-value",
                            payload_data=malformed,
                            api_token=api_access["token"],
                        )
                        self.assertEqual(status, 400)

                with app.store.database() as db:
                    self.assertEqual(
                        db.execute("SELECT COUNT(*) FROM api_requests").fetchone()[0],
                        1,
                    )
                self.assertEqual(service.session_entries, 0)
                self.assertEqual(service.auth_calls, [])
                self.assertEqual(service.wire_writes, [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_roles_and_base_path_for_create_inspect_and_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            init_users(
                directory,
                admin_password="AdminPasswort123",
                guest_password="GastPasswort123",
            )
            app = DachsWebApp(data_dir=directory, interval=60)
            service = RecordingBackupRestoreService(
                app.pack,
                {
                    20: payload(0),
                    24: payload(0),
                    (1, 16): network_payload(0),
                    (2, 16): network_payload(0),
                },
            )
            app.service = service
            admin_token = app.login("admin", "AdminPasswort123")[0]
            guest_token = app.login("gast", "GastPasswort123")[0]
            server = DachsHTTPServer(("127.0.0.1", 0), app, base_path="/dachs")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def request(path, payload_data, token=None):
                connection = http.client.HTTPConnection(
                    *server.server_address,
                    timeout=2,
                )
                headers = {"Content-Type": "application/json"}
                if token is not None:
                    headers["Cookie"] = f"open_dachs_session={token}"
                connection.request(
                    "POST",
                    path,
                    body=json.dumps(payload_data),
                    headers=headers,
                )
                response = connection.getresponse()
                body = json.loads(response.read().decode("utf-8"))
                status = response.status
                connection.close()
                return status, body

            try:
                status, _ = request(
                    "/dachs/api/backup/create",
                    {"blocks": [20, 24]},
                )
                self.assertEqual(status, 401)

                status, guest_created = request(
                    "/dachs/api/backup/create",
                    {"blocks": [20, 24]},
                    guest_token,
                )
                self.assertEqual(status, 200)
                self.assertTrue(guest_created["ok"])

                status, guest_inspection = request(
                    "/dachs/api/backup/inspect",
                    {"image": guest_created["image"]},
                    guest_token,
                )
                self.assertEqual(status, 200)
                self.assertTrue(guest_inspection["digest_verified"])

                network_selection = [
                    public_target(1, 16),
                    public_target(2, 16),
                ]
                status, network_created = request(
                    "/dachs/api/backup/create",
                    {"blocks": network_selection},
                    guest_token,
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    network_created["image"]["requested_targets"],
                    network_selection,
                )
                status, network_inspection = request(
                    "/dachs/api/backup/inspect",
                    {"image": network_created["image"]},
                    guest_token,
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    [item["target_key"] for item in network_inspection["blocks"]],
                    ["1:16", "2:16"],
                )

                status, _ = request(
                    "/dachs/api/backup/restore",
                    {
                        "image": guest_created["image"],
                        "image_sha256": guest_created["image"]["image_sha256"],
                        "blocks": [20, 24],
                        "auth_level": 4,
                        "write_enabled": False,
                    },
                    guest_token,
                )
                self.assertEqual(status, 403)

                status, admin_created = request(
                    "/dachs/api/backup/create",
                    {"blocks": [20, 24]},
                    admin_token,
                )
                self.assertEqual(status, 200)
                self.assertTrue(admin_created["ok"])

                status, admin_inspection = request(
                    "/dachs/api/backup/inspect",
                    {"image": admin_created["image"]},
                    admin_token,
                )
                self.assertEqual(status, 200)
                self.assertTrue(admin_inspection["digest_verified"])

                status, admin_restore = request(
                    "/dachs/api/backup/restore",
                    {
                        "image": admin_created["image"],
                        "image_sha256": admin_created["image"]["image_sha256"],
                        "blocks": [20, 24],
                        "auth_level": 4,
                        "pass4": "",
                        "write_enabled": False,
                        "confirmation": "",
                    },
                    admin_token,
                )
                self.assertEqual(status, 200)
                self.assertEqual(admin_restore["mode"], "dry-run")
                self.assertEqual(admin_restore["unchanged_blocks"], 2)

                status, network_restore = request(
                    "/dachs/api/backup/restore",
                    {
                        "image": network_created["image"],
                        "image_sha256": network_created["image"]["image_sha256"],
                        "blocks": network_selection,
                        "auth_level": 4,
                        "pass4": "",
                        "write_enabled": False,
                        "confirmation": "",
                    },
                    admin_token,
                )
                self.assertEqual(status, 200)
                self.assertEqual(network_restore["mode"], "dry-run")
                self.assertEqual(network_restore["unchanged_blocks"], 2)
                self.assertEqual(
                    [item["target_key"] for item in network_restore["results"]],
                    ["1:16", "2:16"],
                )

                status, _ = request(
                    "/api/backup/create",
                    {"blocks": [20]},
                    admin_token,
                )
                self.assertEqual(status, 404)
                self.assertEqual(service.auth_calls, [])
                self.assertEqual(service.wire_writes, [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
