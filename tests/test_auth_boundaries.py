import io
import unittest
from contextlib import contextmanager, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from open_dachs_manager import cli, tui
from open_dachs_manager.auth import validate_auth_level
from open_dachs_manager.mapping import PackRepository, WriteAllowlist
from open_dachs_manager.tui import run_tui


class NoSerialService:
    def __init__(self):
        self.session_entries = 0

    @contextmanager
    def session(self):
        self.session_entries += 1
        raise AssertionError("invalid auth level must fail before serial access")
        yield


class LevelFiveService:
    def __init__(self):
        self.pack = PackRepository()
        self.session_entries = 0
        self.auth_levels = []

    @contextmanager
    def session(self):
        self.session_entries += 1
        yield object()

    def authenticate(self, _session, level, _pass4=None):
        self.auth_levels.append(level)
        return SimpleNamespace(
            requested_level=level,
            granted_level=level,
            ok=True,
            as_dict=lambda _reveal=False: {
                "requested_level": level,
                "granted_level": level,
                "ok": True,
            },
        )

    def read_block(self, _session, block):
        return SimpleNamespace(ok=True, status=150, payload=bytes(70), block=block)


class ProtectedWriteService(LevelFiveService):
    def __init__(self):
        super().__init__()
        self.write_calls = 0

    def write_payload(self, *_args, **_kwargs):
        self.write_calls += 1
        raise AssertionError("protected SC123 payload reached generic write service")


class AuthSurfaceBoundaryTests(unittest.TestCase):
    def test_level_five_is_accepted_by_central_cli_and_tui_boundaries(self):
        self.assertEqual(validate_auth_level(5), 5)

        parser = cli.build_parser()
        cli_service = LevelFiveService()
        args = parser.parse_args(["auth", "--level", "5"])
        with patch.object(cli, "_service", return_value=cli_service):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli._run(args), 0)
        self.assertEqual(cli_service.auth_levels, [5])
        self.assertEqual(cli_service.session_entries, 1)

        tui_service = LevelFiveService()
        args = parser.parse_args(["tui", "--auth-level", "5"])
        with patch("open_dachs_manager.tui.curses.wrapper", return_value=0):
            self.assertEqual(run_tui(tui_service, args), 0)
        self.assertEqual(tui_service.auth_levels, [5])
        self.assertEqual(tui_service.session_entries, 2)

    def test_cli_auth_rejects_out_of_range_levels_before_session(self):
        parser = cli.build_parser()
        for level in (0, 6, 255, 99999):
            with self.subTest(level=level):
                service = NoSerialService()
                args = parser.parse_args(["auth", "--level", str(level)])
                with patch.object(cli, "_service", return_value=service):
                    with self.assertRaisesRegex(ValueError, "zwischen 1 und 5"):
                        cli._run(args)
                self.assertEqual(service.session_entries, 0)

    def test_cli_live_write_rejects_out_of_range_levels_before_session(self):
        parser = cli.build_parser()
        for level in (0, 6, 255, 99999):
            with self.subTest(level=level):
                service = NoSerialService()
                args = parser.parse_args([
                    "write",
                    "apply",
                    "--key",
                    "Hka_Bd_Stat.ubSoftwareVersion",
                    "--value",
                    "1",
                    "--write-enabled",
                    "--auth-level",
                    str(level),
                ])
                with patch.object(cli, "_service", return_value=service):
                    with self.assertRaisesRegex(ValueError, "zwischen 1 und 5"):
                        cli._run(args)
                self.assertEqual(service.session_entries, 0)

    def test_tui_rejects_out_of_range_levels_before_session(self):
        parser = cli.build_parser()
        for write_enabled in (False, True):
            for level in (0, 6, 255, 99999):
                with self.subTest(write_enabled=write_enabled, level=level):
                    service = NoSerialService()
                    arguments = ["tui", "--auth-level", str(level)]
                    if write_enabled:
                        arguments.append("--write-enabled")
                    args = parser.parse_args(arguments)
                    with self.assertRaisesRegex(ValueError, "zwischen 1 und 5"):
                        run_tui(service, args)
                    self.assertEqual(service.session_entries, 0)

    def test_cli_rejects_protected_sc123_byte_before_authentication(self):
        parser = cli.build_parser()
        service = ProtectedWriteService()
        args = parser.parse_args([
            "write", "apply",
            "--block", "60",
            "--key", "Waermef_Ew.Reserve[0]",
            "--value", "83",
            "--raw",
            "--write-enabled",
            "--auth-level", "3",
        ])
        with patch.object(cli, "_service", return_value=service):
            with self.assertRaisesRegex(PermissionError, "dedizierten SC123-Pfad"):
                cli._run(args)
        self.assertEqual(service.auth_levels, [])
        self.assertEqual(service.write_calls, 0)
        self.assertEqual(service.session_entries, 1)

    def test_tui_rejects_protected_sc123_byte_before_session_or_auth(self):
        service = ProtectedWriteService()
        payload = bytearray(70)
        payload[40] = 83
        state = {
            "block": 60,
            "baseline": bytes(70),
            "payload": payload,
            "changed": {"Waermef_Ew.Reserve[0]"},
            "write_enabled": True,
            "auth_level": 3,
            "pass4": "4567",
            "allowlist": WriteAllowlist(),
            "auth": None,
            "message": "",
        }
        with self.assertRaisesRegex(PermissionError, "dedizierten SC123-Pfad"):
            tui._save(service, state)
        self.assertEqual(service.auth_levels, [])
        self.assertEqual(service.write_calls, 0)
        self.assertEqual(service.session_entries, 0)


if __name__ == "__main__":
    unittest.main()
