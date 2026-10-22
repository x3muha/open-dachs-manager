import http.client
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import unittest
from unittest import mock

from open_dachs_manager.web import (
    DachsHTTPServer,
    DachsWebApp,
    LocalAccessConflictError,
    LocalAccessManager,
    _local_access_origin,
    init_users,
)


LOCAL_REQUEST_HEADER = "X-Open-Dachs-Request"
LOCAL_REQUEST_HEADER_VALUE = "1"


class LocalAccessSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        init_users(
            self.temporary.name,
            admin_password="AdminPasswort123",
            guest_password="GastPasswort123",
        )
        self.app = DachsWebApp(data_dir=self.temporary.name, interval=60)

    def tearDown(self):
        self.temporary.cleanup()

    def test_defaults_are_disabled_on_loopback_admin_port(self):
        settings = self.app.local_access_settings()

        self.assertIs(settings["enabled"], False)
        self.assertEqual(settings["port"], 80)
        self.assertEqual(settings["role"], "admin")

    def test_settings_are_strictly_validated_and_invalid_updates_are_atomic(self):
        baseline = {"enabled": True, "port": 18085, "role": "guest"}
        self.app.set_local_access_settings(baseline)

        invalid_values = (
            ("enabled", 0),
            ("enabled", 1),
            ("enabled", "true"),
            ("enabled", None),
            ("port", True),
            ("port", "8085"),
            ("port", 8085.0),
            ("port", 0),
            ("port", 65536),
            ("role", "ADMIN"),
            ("role", " admin "),
            ("role", "operator"),
            ("role", True),
            ("role", None),
        )
        for field, value in invalid_values:
            with self.subTest(field=field, value=value):
                candidate = dict(baseline)
                candidate[field] = value
                with self.assertRaises(ValueError):
                    self.app.set_local_access_settings(candidate)
                current = self.app.local_access_settings()
                self.assertEqual(
                    {key: current[key] for key in baseline},
                    baseline,
                )

        for port in (1, 80, 65535):
            with self.subTest(valid_boundary_port=port):
                saved = self.app.set_local_access_settings(
                    {"enabled": False, "port": port, "role": "admin"}
                )
                self.assertIs(saved["enabled"], False)
                self.assertEqual(saved["port"], port)
                self.assertEqual(saved["role"], "admin")

    def test_settings_persist_as_private_json(self):
        expected = {"enabled": True, "port": 18086, "role": "guest"}
        saved = self.app.set_local_access_settings(expected)
        self.assertEqual({key: saved[key] for key in expected}, expected)

        settings_path = Path(self.temporary.name) / "local_access_settings.json"
        self.assertTrue(settings_path.is_file())
        self.assertEqual(stat.S_IMODE(settings_path.stat().st_mode), 0o600)

        stored = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["enabled"], expected["enabled"])
        self.assertEqual(stored["port"], expected["port"])
        self.assertEqual(stored["role"], expected["role"])

        reloaded = DachsWebApp(data_dir=self.temporary.name, interval=60)
        settings = reloaded.local_access_settings()
        self.assertEqual({key: settings[key] for key in expected}, expected)

    def test_standard_http_port_uses_canonical_origin_without_explicit_port(self):
        self.assertEqual(_local_access_origin(80), "http://127.0.0.1")
        self.assertEqual(_local_access_origin(8085), "http://127.0.0.1:8085")

    def test_synthetic_principal_name_is_reserved_for_password_users(self):
        for username in ("localhost-override", "LOCALHOST-OVERRIDE"):
            with self.subTest(username=username):
                with self.assertRaisesRegex(ValueError, "reserviert"):
                    self.app.create_user(username, "SicheresPasswort123", "admin")

        users = self.app._load_users()
        guest = next(user for user in users if user["username"] == "gast")
        guest["username"] = "Localhost-Override"
        self.app._save_users(users)
        self.assertIsNone(
            self.app.login("Localhost-Override", "GastPasswort123")
        )


class LocalAccessHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        init_users(
            self.temporary.name,
            admin_password="AdminPasswort123",
            guest_password="GastPasswort123",
        )
        self.app = DachsWebApp(data_dir=self.temporary.name, interval=60)
        self.admin_token = self.app.login("admin", "AdminPasswort123")[0]
        self.guest_token = self.app.login("gast", "GastPasswort123")[0]
        self.servers = []

    def tearDown(self):
        for server, thread in reversed(self.servers):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.temporary.cleanup()

    def start_server(self, *, local_access=False, local_role="admin"):
        server = DachsHTTPServer(
            ("127.0.0.1", 0),
            self.app,
            local_access=local_access,
            local_role=local_role,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return server

    @staticmethod
    def request(server, method, path, *, payload=None, headers=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=2)
        request_headers = dict(headers or {})
        body = None
        if payload is not None:
            request_headers.setdefault("Content-Type", "application/json")
            body = json.dumps(payload)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        result = (
            response.status,
            response.getheaders(),
            json.loads(raw) if raw else {},
        )
        connection.close()
        return result

    @staticmethod
    def raw_request(server, method, path, *, payload, header_items):
        connection = http.client.HTTPConnection(*server.server_address, timeout=2)
        body = json.dumps(payload).encode("utf-8")
        connection.putrequest(method, path)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(body)))
        for name, value in header_items:
            connection.putheader(name, value)
        connection.endheaders(body)
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        result = (
            response.status,
            response.getheaders(),
            json.loads(raw) if raw else {},
        )
        connection.close()
        return result

    def assert_local_request_rejected(self, result):
        status, _headers, payload = result
        self.assertIn(status, {400, 403, 421})
        self.assertFalse(payload.get("ok", False))

    def test_normal_listener_remains_cookie_protected_with_proxy_headers(self):
        server = self.start_server()
        status, _headers, payload = self.request(
            server,
            "GET",
            "/api/session",
            headers={
                "Forwarded": "for=127.0.0.1;host=127.0.0.1",
                "X-Forwarded-For": "127.0.0.1",
                "X-Forwarded-Host": f"127.0.0.1:{server.server_address[1]}",
                "X-Forwarded-Proto": "http",
                "X-Real-IP": "127.0.0.1",
            },
        )

        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "login required")

    def test_local_access_settings_are_admin_only_on_normal_listener(self):
        server = self.start_server()
        status, _headers, _payload = self.request(
            server,
            "GET",
            "/api/settings/local-access",
            headers={"Cookie": f"open_dachs_session={self.guest_token}"},
        )
        self.assertEqual(status, 403)

        status, _headers, settings = self.request(
            server,
            "GET",
            "/api/settings/local-access",
            headers={"Cookie": f"open_dachs_session={self.admin_token}"},
        )
        self.assertEqual(status, 200)
        self.assertFalse(settings["enabled"])
        self.assertEqual(settings["role"], "admin")

    def test_local_listener_supplies_synthetic_admin_without_cookie(self):
        server = self.start_server(local_access=True, local_role="admin")
        status, headers, payload = self.request(server, "GET", "/api/session")

        self.assertEqual(status, 200)
        self.assertTrue(payload["authenticated"])
        self.assertEqual(payload["user"]["username"], "localhost-override")
        self.assertEqual(payload["user"]["role"], "admin")
        self.assertFalse(
            any(name.lower() == "set-cookie" for name, _value in headers)
        )

    def test_local_listener_cannot_be_bound_to_a_non_loopback_address(self):
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            DachsHTTPServer(
                ("0.0.0.0", 0),
                self.app,
                local_access=True,
                local_role="admin",
            )

    def test_local_listener_does_not_bypass_bearer_api_authentication(self):
        server = self.start_server(local_access=True, local_role="admin")
        status, _headers, payload = self.request(server, "GET", "/api/v1/live")

        self.assertEqual(status, 401)
        self.assertIn("Bearer-Token", payload["error"])

        token = self.app.store.create_api_token(
            "admin", "Lokaler API-Test", ["read", "write"]
        )["token"]
        status, _headers, payload = self.request(
            server,
            "GET",
            "/api/v1/live",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["schema"], "open-dachs-api-live/v1")

        calls = []
        self.app.execute_api_set_value = (
            lambda principal, body: calls.append((principal, body)) or {"ok": True}
        )
        status, _headers, payload = self.request(
            server,
            "POST",
            "/api/v1/actions/set-value",
            payload={"request_id": "local-bearer-1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(calls[0][0]["name"], "Lokaler API-Test")

    def test_local_listener_requires_exact_host_and_rejects_proxy_headers(self):
        server = self.start_server(local_access=True, local_role="admin")
        port = server.server_address[1]

        for host in ("127.0.0.1", f"localhost:{port}", f"127.0.0.2:{port}"):
            with self.subTest(host=host):
                self.assert_local_request_rejected(
                    self.request(
                        server,
                        "GET",
                        "/api/session",
                        headers={"Host": host},
                    )
                )

        proxy_headers = (
            ("Forwarded", "for=127.0.0.1"),
            ("X-Forwarded-For", "127.0.0.1"),
            ("X-Forwarded-Host", f"127.0.0.1:{port}"),
            ("X-Forwarded-Proto", "http"),
            ("X-Forwarded-Surprise", "present"),
            ("X-Real-IP", "127.0.0.1"),
        )
        for name, value in proxy_headers:
            with self.subTest(proxy_header=name):
                self.assert_local_request_rejected(
                    self.request(
                        server,
                        "GET",
                        "/api/session",
                        headers={name: value},
                    )
                )

    def test_local_listener_allows_only_user_activated_foreign_navigation(self):
        server = self.start_server(local_access=True, local_role="admin")
        navigation = {
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-User": "?1",
        }

        status, _headers, _payload = self.request(
            server,
            "GET",
            "/api/session",
            headers=navigation,
        )
        self.assertEqual(status, 200)

        for missing in ("Sec-Fetch-Mode", "Sec-Fetch-Dest", "Sec-Fetch-User"):
            with self.subTest(missing=missing):
                headers = dict(navigation)
                headers.pop(missing)
                self.assert_local_request_rejected(
                    self.request(server, "GET", "/api/session", headers=headers)
                )

        scripted = dict(navigation)
        scripted["Sec-Fetch-Mode"] = "cors"
        self.assert_local_request_rejected(
            self.request(server, "GET", "/api/session", headers=scripted)
        )

    def test_local_mutations_require_exact_origin_and_request_marker(self):
        server = self.start_server(local_access=True, local_role="admin")
        port = server.server_address[1]
        origin = f"http://127.0.0.1:{port}"
        payload = {"enabled": False}

        rejected_headers = (
            {},
            {LOCAL_REQUEST_HEADER: LOCAL_REQUEST_HEADER_VALUE},
            {"Origin": origin},
            {"Origin": origin, LOCAL_REQUEST_HEADER: "wrong"},
            {
                "Origin": f"http://localhost:{port}",
                LOCAL_REQUEST_HEADER: LOCAL_REQUEST_HEADER_VALUE,
            },
            {
                "Origin": "null",
                LOCAL_REQUEST_HEADER: LOCAL_REQUEST_HEADER_VALUE,
            },
        )
        for headers in rejected_headers:
            with self.subTest(headers=headers):
                self.assert_local_request_rejected(
                    self.request(
                        server,
                        "POST",
                        "/api/monitor",
                        payload=payload,
                        headers=headers,
                    )
                )

        status, _headers, response = self.request(
            server,
            "POST",
            "/api/monitor",
            payload=payload,
            headers={
                "Origin": origin,
                LOCAL_REQUEST_HEADER: LOCAL_REQUEST_HEADER_VALUE,
            },
        )
        self.assertEqual(status, 200)
        self.assertIs(response["enabled"], False)

        for duplicate_headers in (
            (
                ("Origin", origin),
                ("Origin", origin),
                (LOCAL_REQUEST_HEADER, LOCAL_REQUEST_HEADER_VALUE),
            ),
            (
                ("Origin", origin),
                (LOCAL_REQUEST_HEADER, LOCAL_REQUEST_HEADER_VALUE),
                (LOCAL_REQUEST_HEADER, LOCAL_REQUEST_HEADER_VALUE),
            ),
        ):
            with self.subTest(duplicate_headers=duplicate_headers):
                self.assert_local_request_rejected(
                    self.raw_request(
                        server,
                        "POST",
                        "/api/monitor",
                        payload=payload,
                        header_items=duplicate_headers,
                    )
                )

    def test_local_guest_cannot_reach_admin_route_even_with_admin_cookie(self):
        server = self.start_server(local_access=True, local_role="guest")
        status, _headers, payload = self.request(
            server,
            "GET",
            "/api/users",
            headers={"Cookie": f"open_dachs_session={self.admin_token}"},
        )

        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "admin role required")

    def test_local_login_logout_and_audit_identity_never_create_local_cookie(self):
        normal = self.start_server()
        local = self.start_server(local_access=True, local_role="admin")
        port = int(local.server_address[1])
        guard = {
            "Origin": f"http://127.0.0.1:{port}",
            LOCAL_REQUEST_HEADER: LOCAL_REQUEST_HEADER_VALUE,
            "Cookie": f"open_dachs_session={self.admin_token}",
        }

        status, headers, _payload = self.request(
            local,
            "POST",
            "/api/login",
            payload={"username": "admin", "password": "AdminPasswort123"},
            headers=guard,
        )
        self.assertEqual(status, 403)
        self.assertFalse(any(name.lower() == "set-cookie" for name, _ in headers))

        status, headers, payload = self.request(
            local,
            "POST",
            "/api/logout",
            payload={},
            headers=guard,
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["local_access"])
        self.assertFalse(any(name.lower() == "set-cookie" for name, _ in headers))

        status, _headers, session = self.request(
            normal,
            "GET",
            "/api/session",
            headers={"Cookie": f"open_dachs_session={self.admin_token}"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(session["user"]["username"], "admin")

        calls = []
        self.app.write_block = lambda *args: calls.append(args) or {"written": False}
        status, _headers, _result = self.request(
            local,
            "POST",
            "/api/block/50",
            payload={
                "changes": [],
                "auth_level": 4,
                "pass4": "",
                "write_enabled": False,
            },
            headers=guard,
        )
        self.assertEqual(status, 200)
        self.assertEqual(calls[0][0], "localhost-override")


class LocalAccessManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        init_users(
            self.temporary.name,
            admin_password="AdminPasswort123",
            guest_password="GastPasswort123",
        )
        self.app = DachsWebApp(data_dir=self.temporary.name, interval=60)
        self.manager = LocalAccessManager(
            self.app,
            base_path="/dachs",
            primary_port=18084,
        )
        self.manager.start()

    def tearDown(self):
        self.manager.stop()
        self.temporary.cleanup()

    @staticmethod
    def free_port():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    @staticmethod
    def local_session(port):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request("GET", "/dachs/api/session")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def test_port_and_role_change_are_live_and_disable_closes_listener(self):
        first_port = self.free_port()
        first = self.app.set_local_access_settings({
            "enabled": True,
            "port": first_port,
            "role": "guest",
        })
        self.assertTrue(first["active"])
        self.assertEqual(first["url"], f"http://127.0.0.1:{first_port}/dachs/")
        self.assertEqual(self.local_session(first_port)[1]["user"]["role"], "guest")

        promoted = self.app.set_local_access_settings({
            "enabled": True,
            "port": first_port,
            "role": "admin",
        })
        self.assertTrue(promoted["active"])
        self.assertEqual(self.local_session(first_port)[1]["user"]["role"], "admin")

        second_port = self.free_port()
        moved = self.app.set_local_access_settings({
            "enabled": True,
            "port": second_port,
            "role": "admin",
        })
        self.assertTrue(moved["active"])
        self.assertEqual(self.local_session(second_port)[0], 200)
        with self.assertRaises(OSError):
            self.local_session(first_port)

        disabled = self.app.set_local_access_settings({
            "enabled": False,
            "port": second_port,
            "role": "admin",
        })
        self.assertFalse(disabled["active"])
        with self.assertRaises(OSError):
            self.local_session(second_port)

    def test_occupied_or_primary_port_preserves_current_listener_and_config(self):
        active_port = self.free_port()
        self.app.set_local_access_settings({
            "enabled": True,
            "port": active_port,
            "role": "admin",
        })
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            occupied_port = int(occupied.getsockname()[1])
            with self.assertRaises(LocalAccessConflictError):
                self.app.set_local_access_settings({
                    "enabled": True,
                    "port": occupied_port,
                    "role": "guest",
                })
        with self.assertRaises(LocalAccessConflictError):
            self.app.set_local_access_settings({
                "enabled": True,
                "port": 18084,
                "role": "guest",
            })
        with self.assertRaises(LocalAccessConflictError):
            self.app.set_local_access_settings({
                "enabled": False,
                "port": 18084,
                "role": "guest",
            })

        current = self.app.local_access_settings()
        self.assertEqual(current["port"], active_port)
        self.assertEqual(current["role"], "admin")
        self.assertTrue(current["active"])
        self.assertEqual(self.local_session(active_port)[0], 200)

    def test_directory_fsync_failure_after_replace_keeps_runtime_committed(self):
        port = self.free_port()
        real_fsync = os.fsync

        def fail_directory_fsync(file_descriptor):
            if stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
                raise OSError("simulierter Verzeichnis-fsync-Fehler")
            return real_fsync(file_descriptor)

        with mock.patch(
            "open_dachs_manager.web.os.fsync",
            side_effect=fail_directory_fsync,
        ):
            configured = self.app.set_local_access_settings({
                "enabled": True,
                "port": port,
                "role": "guest",
            })

        self.assertTrue(configured["active"])
        self.assertEqual(configured["port"], port)
        self.assertEqual(self.local_session(port)[1]["user"]["role"], "guest")
        persisted = json.loads(
            (Path(self.temporary.name) / "local_access_settings.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(persisted["port"], port)

    def test_failure_before_replace_preserves_old_file_and_listener(self):
        active_port = self.free_port()
        self.app.set_local_access_settings({
            "enabled": True,
            "port": active_port,
            "role": "admin",
        })
        candidate_port = self.free_port()

        with mock.patch(
            "open_dachs_manager.web.os.replace",
            side_effect=OSError("simulierter Fehler vor Commit"),
        ):
            with self.assertRaisesRegex(OSError, "vor Commit"):
                self.app.set_local_access_settings({
                    "enabled": True,
                    "port": candidate_port,
                    "role": "guest",
                })

        current = self.app.local_access_settings()
        self.assertEqual(current["port"], active_port)
        self.assertEqual(current["role"], "admin")
        self.assertTrue(current["active"])
        self.assertEqual(self.local_session(active_port)[0], 200)
        with self.assertRaises(OSError):
            self.local_session(candidate_port)
        persisted = json.loads(
            (Path(self.temporary.name) / "local_access_settings.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(persisted["port"], active_port)

    def test_persisted_enabled_listener_starts_successfully(self):
        with tempfile.TemporaryDirectory() as directory:
            init_users(
                directory,
                admin_password="AdminPasswort123",
                guest_password="GastPasswort123",
            )
            port = self.free_port()
            configured = DachsWebApp(data_dir=directory, interval=60)
            configured.set_local_access_settings({
                "enabled": True,
                "port": port,
                "role": "guest",
            })

            restarted = DachsWebApp(data_dir=directory, interval=60)
            manager = LocalAccessManager(
                restarted,
                base_path="/dachs",
                primary_port=18084,
            )
            try:
                status = manager.start()
                self.assertTrue(status["enabled"])
                self.assertTrue(status["active"])
                self.assertEqual(status["url"], f"http://127.0.0.1:{port}/dachs/")
                self.assertEqual(self.local_session(port)[1]["user"]["role"], "guest")
            finally:
                manager.stop()

    def test_occupied_persisted_port_fails_closed_without_startup_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            init_users(
                directory,
                admin_password="AdminPasswort123",
                guest_password="GastPasswort123",
            )
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
                occupied.bind(("127.0.0.1", 0))
                occupied.listen(1)
                occupied_port = int(occupied.getsockname()[1])
                configured = DachsWebApp(data_dir=directory, interval=60)
                configured.set_local_access_settings({
                    "enabled": True,
                    "port": occupied_port,
                    "role": "admin",
                })

                restarted = DachsWebApp(data_dir=directory, interval=60)
                manager = LocalAccessManager(
                    restarted,
                    base_path="",
                    primary_port=18084,
                )
                try:
                    status = manager.start()
                    self.assertTrue(status["enabled"])
                    self.assertFalse(status["active"])
                    self.assertIn(str(occupied_port), status["last_error"])
                finally:
                    manager.stop()


class LocalAccessUITests(unittest.TestCase):
    def test_local_user_panel_and_cookie_less_ui_contract_are_wired(self):
        root = Path(__file__).resolve().parents[1]
        index = (root / "src/open_dachs_manager/web/index.html").read_text(
            encoding="utf-8"
        )
        app = (root / "src/open_dachs_manager/web/app.js").read_text(
            encoding="utf-8"
        )

        for element_id in (
            "localAccessForm",
            "localAccessEnabled",
            "localAccessPort",
            "localAccessRole",
            "localAccessStatus",
            "localAccessUrl",
            "accountControls",
        ):
            self.assertIn(f'id="{element_id}"', index)
        self.assertIn('value="80"', index)
        self.assertLess(
            index.index('<option value="admin" selected>'),
            index.index('<option value="guest">Gast · nur lesen</option>'),
        )
        self.assertIn('api("/api/settings/local-access")', app)
        self.assertIn('api("/api/settings/local-access", {', app)
        self.assertIn('"X-Open-Dachs-Request": "1"', app)
        self.assertIn('user.auth_source === "localhost-override"', app)
        self.assertIn('$("logoutButton").hidden = isLocalAccess;', app)
        self.assertIn('$("accountControls").hidden = isLocalAccess;', app)
        self.assertNotIn('style="--legend-color:', app)
        self.assertIn('class="legend-item legend-tone-${tone}', app)


if __name__ == "__main__":
    unittest.main()
