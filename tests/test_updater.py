import base64
import csv
import datetime as dt
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import tempfile
import unittest
import zipfile
import threading

from open_dachs_manager import updater
from open_dachs_manager.web import DachsHTTPServer, DachsWebApp, init_users


def make_wheel(directory: Path, version="1.8.18"):
    name=f"open_dachs_manager-{version}-py3-none-any.whl"; path=directory/name
    dist=f"open_dachs_manager-{version}.dist-info"
    members={"open_dachs_manager/__init__.py":f'__version__ = "{version}"\n'.encode(), f"{dist}/METADATA":f"Metadata-Version: 2.1\nName: open-dachs-manager\nVersion: {version}\n".encode(), f"{dist}/WHEEL":b"Wheel-Version: 1.0\nTag: py3-none-any\n"}
    rows=[]
    for item,raw in members.items():
        digest=base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode(); rows.append([item,"sha256="+digest,str(len(raw))])
    rows.append([f"{dist}/RECORD","",""])
    out=io.StringIO(); csv.writer(out,lineterminator="\n").writerows(rows); members[f"{dist}/RECORD"]=out.getvalue().encode()
    with zipfile.ZipFile(path,"w",zipfile.ZIP_STORED) as archive:
        for item,raw in members.items(): archive.writestr(item,raw)
    return path


def make_manifest(wheel: Path, version="1.8.18", release_id=42):
    return {"schema":updater.MANIFEST_SCHEMA,"repo":updater.REPOSITORY,"version":version,"tag":"v"+version,"release_id":release_id,"commit":"a"*40,"wheel":wheel.name,"wheel_size":wheel.stat().st_size,"wheel_sha256":hashlib.sha256(wheel.read_bytes()).hexdigest(),"dependency_fingerprint":updater.DEPENDENCY_FINGERPRINT,"db_schema_from":1,"db_schema_to":1,"rollback_compatible":True,"min_helper_version":"1.0.0","auto_eligible":True,"published_at":"2026-11-20T00:00:00Z"}


class UpdaterPureTests(unittest.TestCase):
    def test_strict_versions(self):
        self.assertEqual(updater.parse_version("v1.8.17"),(1,8,17))
        for bad in ("1.8.17","v01.8.17","v1.8.17-rc1","v1.8","v1.8.17/evil"):
            with self.assertRaises(updater.UpdateError): updater.parse_version(bad)

    def test_url_allowlist(self):
        updater._safe_url(updater.GITHUB_API)
        updater._safe_url("https://api.github.com/repos/x3muha/open-dachs-manager/releases/assets/9",asset_id=9)
        for bad in ("http://github.com/x","https://evil.example/x","https://github.com@evil.example.test/x","https://github.com:444/x"):
            with self.assertRaises(updater.UpdateError): updater._safe_url(bad)

    def test_manifest_is_exact_canonical_schema(self):
        with tempfile.TemporaryDirectory() as td:
            wheel=make_wheel(Path(td)); value=make_manifest(wheel); raw=updater.canonical_json(value)
            self.assertEqual(updater.validate_manifest(raw),value)
            with self.assertRaises(updater.UpdateError): updater.validate_manifest(raw+b"\n")
            value["extra"]=1
            with self.assertRaises(updater.UpdateError): updater.validate_manifest(updater.canonical_json(value))

    def test_wheel_record_hash_and_version(self):
        with tempfile.TemporaryDirectory() as td:
            wheel=make_wheel(Path(td)); manifest=make_manifest(wheel); updater.verify_wheel(wheel,manifest)
            manifest["wheel_sha256"]="0"*64
            with self.assertRaises(updater.UpdateError): updater.verify_wheel(wheel,manifest)

    def test_corrupt_settings_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"update-settings.json").write_text('{"automatic":true')
            paths=updater.UpdatePaths(root,root/"stage",root/"request",root/"key")
            manager=updater.UpdateManager(root,"1.8.17",paths=paths)
            self.assertFalse(manager.settings["automatic"]); self.assertTrue(manager.settings_corrupt)
            with self.assertRaises(updater.UpdateError): manager.save_settings({"automatic":True,"timezone":"Europe/Berlin","window_start":"02:00","window_end":"04:00"},local_access=False,password_ok=True)

    def test_local_override_cannot_enable_or_apply(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); manager=updater.UpdateManager(root,"1.8.17",paths=updater.UpdatePaths(root,root/"s",root/"r",root/"k"))
            with self.assertRaises(PermissionError): manager.save_settings({"automatic":True,"timezone":"Europe/Berlin","window_start":"02:00","window_end":"04:00"},local_access=True)
            manager.save_settings({"automatic":False,"timezone":"Europe/Berlin","window_start":"02:00","window_end":"04:00"},local_access=True)
            with self.assertRaises(PermissionError): manager.apply("admin",{},local_access=True,session_nonce="n",auth_epoch=1)

    def test_preview_is_one_time_user_and_hash_bound(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); manager=updater.UpdateManager(root,"1.8.17",paths=updater.UpdatePaths(root,root/"s",root/"r",root/"k"))
            manager.state["available"]={"version":"1.8.18","tag":"v1.8.18","published_at":"2026-11-20T00:00:00Z","release_notes":"x","compatible":True,"manifest_sha256":"1"*64,"wheel_sha256":"2"*64}
            preview=manager.preview_update("admin",True,local_access=False,session_nonce="session-a",auth_epoch=7)
            manager.state["available"]["wheel_sha256"]="3"*64
            with self.assertRaises(updater.UpdateError): manager.apply("admin",{"preview_token":preview["preview_token"],"confirmation":preview["confirmation"],"request_id":"a"*32},local_access=False,session_nonce="session-a",auth_epoch=7)
            self.assertIsNone(manager.preview)

    def test_etag_backoff_and_no_serial_import(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            def failing(*args,**kwargs): raise updater.UpdateError("offline")
            manager=updater.UpdateManager(root,"1.8.17",paths=updater.UpdatePaths(root,root/"s",root/"r",root/"k"),fetcher=failing)
            first=manager.check(); self.assertEqual(manager.state["failures"],1); self.assertIn("offline",first["error"])
            source=Path(updater.__file__).read_text(); self.assertNotIn("serial_worker",source); self.assertNotIn("DachsService",source)

    def test_preview_rejects_other_session_or_auth_epoch(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); manager=updater.UpdateManager(root,"1.8.17",paths=updater.UpdatePaths(root,root/"s",root/"r",root/"k"))
            manager.state["available"]={"version":"1.8.18","tag":"v1.8.18","published_at":"2026-11-20T00:00:00+00:00","release_notes":"x","compatible":True,"manifest_sha256":"1"*64,"wheel_sha256":"2"*64}
            preview=manager.preview_update("admin",True,local_access=False,session_nonce="one",auth_epoch=9)
            with self.assertRaisesRegex(updater.UpdateError,"session binding"):
                manager.apply("admin",{"preview_token":preview["preview_token"],"confirmation":preview["confirmation"],"request_id":"a"*32},local_access=False,session_nonce="two",auth_epoch=9)

    def test_window_is_strict_and_scheduler_ticks_each_minute(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); manager=updater.UpdateManager(root,"1.8.17",paths=updater.UpdatePaths(root,root/"s",root/"r",root/"k"))
            for start,end in (("02:00","02:00"),("04:00","02:00")):
                with self.assertRaises(updater.UpdateError):
                    manager.save_settings({"automatic":False,"timezone":"Europe/Berlin","window_start":start,"window_end":end},local_access=False)
        self.assertLessEqual(updater.SCHEDULER_INTERVAL_SECONDS,60)
        self.assertEqual(updater.CHECK_INTERVAL_SECONDS,6*3600)

    def test_terminal_auto_failure_reconciles_without_status_get(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); paths=updater.UpdatePaths(root,root/"s",root/"r",root/"k")
            manager=updater.UpdateManager(root,"1.8.17",paths=paths)
            manager.settings={**manager.settings,"automatic":True}; manager.state["operation"]={"request_id":"a"*32}
            paths.job_status.write_text(json.dumps({"request_id":"a"*32,"automatic":True,"status":"recovery-required"}))
            manager._reconcile_job_status()
            self.assertFalse(manager.settings["automatic"])
            self.assertFalse(json.loads(paths.settings.read_text())["automatic"])


class UpdateContractTests(unittest.TestCase):
    def test_installer_and_systemd_fixed_layout(self):
        install=Path("install.sh").read_text(); web=Path("systemd/open-dachs-manager-web.service").read_text(); serial=Path("systemd/open-dachs-manager-serial.service").read_text()
        self.assertIn('ODM_RELEASES="$ODM_INSTALL_ROOT/releases"',install); self.assertIn('ODM_CURRENT="$ODM_INSTALL_ROOT/current"',install)
        self.assertIn("current/venv",web); self.assertIn("current/venv",serial)
        self.assertIn("NoNewPrivileges=true",web)
        path=Path("systemd/open-dachs-manager-update.path").read_text(); self.assertIn("PathExists=/var/lib/open-dachs-manager/update-request.json",path)
        update=Path("systemd/open-dachs-manager-update.service").read_text()
        self.assertNotIn("CapabilityBoundingSet="+chr(10),update)
        self.assertIn("RuntimeDirectoryMode=0700",update); self.assertIn("TimeoutStartSec=30min",update)
        self.assertIn("open-dachs-manager-updater",install)

    def test_release_workflow_contract(self):
        text=Path(".github/workflows/release.yml").read_text()
        for required in ("tags: [\"v*\"]","ODM_RELEASE_SIGNING_KEY","openssl pkeyutl -sign","open_dachs_update_manifest.json","open_dachs_update_manifest.sig","contents: write","python -m pytest","RECORD","draft=false","prerelease=false"):
            self.assertIn(required,text)
        self.assertNotRegex(text,r"uses: actions/[^@]+@v\d")
        self.assertIn("cmp /tmp/dist-a/*.whl /tmp/dist-b/*.whl",text)
        self.assertIn("len(assets)==3",text); self.assertIn("target_commitish",text)
        self.assertIn("release-metadata.json",text); self.assertIn("environment: release-approval",text)

    def test_ui_has_explicit_preview_and_no_get_apply(self):
        html=Path("src/open_dachs_manager/web/index.html").read_text(); js=Path("src/open_dachs_manager/web/app.js").read_text()
        self.assertIn("updatePreviewForm",html); self.assertIn("updateConfirmation",html)
        self.assertIn('api("/api/update/apply", {method:"POST"',js)
        self.assertNotIn('api("/api/update/apply")',js)
        self.assertIn("state.system.updatePreview = null",js)
        self.assertIn("error instanceof TypeError",js)
        self.assertIn("updateAutomaticAck",html); self.assertIn("updateAutomaticPassword",html)

    def test_helper_has_fixed_paths_and_no_arguments(self):
        text=Path("scripts/open-dachs-update-helper.py").read_text()
        self.assertRegex(text,r'if len\(sys\.argv\)\s*!=\s*1'); self.assertIn("os.O_NOFOLLOW",text)
        self.assertIn('SERVICES = ("open-dachs-manager-serial.service", "open-dachs-manager-web.service")',text)
        self.assertNotIn("from open_dachs_manager",text)
        self.assertFalse(any(line.startswith("import open_dachs_manager") for line in text.splitlines()))
        for marker in ("/usr/bin/systemctl","--max-redirs", "PRAGMA quick_check", "recovery-required", "original_error_class", "rollback_error_class"):
            self.assertIn(marker,text)

    def test_update_audit_is_separate_from_controller_write_audit(self):
        source=Path("src/open_dachs_manager/web.py").read_text()
        self.assertIn("CREATE TABLE IF NOT EXISTS update_audit",source)
        route=source[source.index('if path == "/api/update/apply"'):source.index('if path == "/api/notifications/test"')]
        self.assertIn("update_audit",route); self.assertNotIn("store.audit(",route)


class UpdateHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        init_users(self.temp.name,admin_password="admin-password-123",guest_password="guest-password-123")
        self.app=DachsWebApp(data_dir=self.temp.name,interval=60)
        self.admin=self.app.login("admin","admin-password-123")[0]
        self.guest=self.app.login("gast","guest-password-123")[0]
        self.server=DachsHTTPServer(("127.0.0.1",0),self.app,base_path="/dachs")
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2); self.temp.cleanup()

    def request(self,method,path,token=None,payload=None,guard=True):
        connection=http.client.HTTPConnection(*self.server.server_address,timeout=3); headers={}; body=None
        if token: headers["Cookie"]="open_dachs_session="+token
        if payload is not None:
            headers["Content-Type"]="application/json"; body=json.dumps(payload)
            if guard: headers["X-Open-Dachs-Request"]="1"
        connection.request(method,"/dachs"+path,body=body,headers=headers); response=connection.getresponse(); raw=response.read(); connection.close()
        return response.status,json.loads(raw or b"{}")

    def test_status_is_base_path_safe_and_guest_is_redacted(self):
        self.app.update_manager.state["available"]={"version":"1.8.18","release_notes":"secret details"}
        self.assertEqual(self.request("GET","/api/update/status")[0],401)
        status,guest=self.request("GET","/api/update/status",self.guest); self.assertEqual(status,200); self.assertTrue(guest["update_available"]); self.assertNotIn("available",guest)
        status,admin=self.request("GET","/api/update/status",self.admin); self.assertEqual(status,200); self.assertEqual(admin["available"]["version"],"1.8.18")

    def test_mutations_require_admin_guard_and_strict_payload(self):
        payload={"automatic":False,"timezone":"Europe/Berlin","window_start":"02:00","window_end":"04:00","password":"","acknowledge":False}
        self.assertEqual(self.request("POST","/api/update/settings",self.guest,payload)[0],403)
        self.assertEqual(self.request("POST","/api/update/settings",self.admin,payload,guard=False)[0],403)
        self.assertEqual(self.request("POST","/api/update/settings",self.admin,{**payload,"extra":1})[0],400)
        self.assertEqual(self.request("POST","/api/update/settings",self.admin,payload)[0],200)

    def test_auto_enable_requires_password_and_acknowledgement(self):
        base={"automatic":True,"timezone":"Europe/Berlin","window_start":"02:00","window_end":"04:00"}
        self.assertEqual(self.request("POST","/api/update/settings",self.admin,{**base,"password":"admin-password-123","acknowledge":False})[0],403)
        self.assertEqual(self.request("POST","/api/update/settings",self.admin,{**base,"password":"wrong","acknowledge":True})[0],403)
        self.assertEqual(self.request("POST","/api/update/settings",self.admin,{**base,"password":"admin-password-123","acknowledge":True})[0],200)
        db=sqlite3.connect(Path(self.temp.name)/"dachs-web.db")
        self.assertEqual(db.execute("select count(*) from update_audit").fetchone()[0],1)
        self.assertEqual(db.execute("select count(*) from write_audit").fetchone()[0],0); db.close()


if __name__ == "__main__": unittest.main()
