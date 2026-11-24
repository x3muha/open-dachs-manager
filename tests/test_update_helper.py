import importlib.util
import datetime as dt
import hashlib
import json
import os
import shutil
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("odm_update_helper",ROOT/"scripts/open-dachs-update-helper.py")
helper=importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)

class HelperFilesystemTests(unittest.TestCase):
    @staticmethod
    def manifest_request(version="1.8.18", automatic=False):
        manifest={"schema":helper.MANIFEST_SCHEMA,"repo":helper.REPO,"version":version,"tag":"v"+version,
            "release_id":42,"commit":"a"*40,"wheel":f"open_dachs_manager-{version}-py3-none-any.whl",
            "wheel_size":123,"wheel_sha256":"b"*64,"dependency_fingerprint":helper.DEPENDENCIES,
            "db_schema_from":1,"db_schema_to":1,"rollback_compatible":True,"min_helper_version":"1.0.0",
            "auto_eligible":True,"published_at":"2026-11-20T00:00:00Z"}
        raw=helper.canonical(manifest)
        request={"schema":helper.REQUEST_SCHEMA,"request_id":"c"*32,"requested_at":"2026-11-24T00:00:00Z",
            "requested_by":"admin","automatic":automatic,"version":version,"release_id":42,"commit":"a"*40,
            "published_at":"2026-11-20T00:00:00Z","manifest_sha256":hashlib.sha256(raw).hexdigest(),"wheel_sha256":"b"*64}
        return manifest,request,raw

    def test_safe_regular_rejects_symlink_and_writable_file(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); target=root/"target"; target.write_text("x"); target.chmod(0o600)
            link=root/"link"; link.symlink_to(target)
            with self.assertRaises(OSError): helper.safe_regular(link)
            target.chmod(0o622)
            with self.assertRaises(helper.Failure): helper.safe_regular(target)

    def test_current_release_rejects_outside_target(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); releases=root/"releases"; releases.mkdir(); outside=root/"outside"; outside.mkdir(); current=root/"current"; current.symlink_to(outside)
            old_releases,old_current=helper.RELEASES,helper.CURRENT
            helper.RELEASES,helper.CURRENT=releases,current
            try:
                with self.assertRaises(helper.Failure): helper.current_release()
            finally: helper.RELEASES,helper.CURRENT=old_releases,old_current

    def test_sqlite_online_backup_and_quick_check(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as td:
            root=Path(td); database=root/"live.db"; backups=root/"backups"; backups.mkdir(mode=0o700)
            db=sqlite3.connect(database); db.execute("create table t(value text)"); db.execute("insert into t values ('ok')"); db.commit(); db.close()
            old_db,old_backups=helper.DATABASE,helper.BACKUPS; helper.DATABASE,helper.BACKUPS=database,backups
            try:
                with mock.patch.object(helper,"ensure_root_dir",return_value=backups), \
                     mock.patch.object(helper.os,"chown"):
                    result=helper.sqlite_backup("a"*32)
                check=sqlite3.connect(result); self.assertEqual(check.execute("pragma quick_check").fetchone()[0],"ok"); self.assertEqual(check.execute("select value from t").fetchone()[0],"ok"); check.close()
            finally: helper.DATABASE,helper.BACKUPS=old_db,old_backups

    def test_backup_destination_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as td:
            root=Path(td); database=root/"live.db"; backups=root/"backups"; backups.mkdir(mode=0o700)
            sqlite3.connect(database).close(); outside=root/"outside"; outside.write_text("x")
            (backups/("d"*32+".db")).symlink_to(outside)
            old_db,old_backups=helper.DATABASE,helper.BACKUPS; helper.DATABASE,helper.BACKUPS=database,backups
            try:
                with mock.patch.object(helper,"ensure_root_dir",return_value=backups), self.assertRaises(OSError):
                    helper.sqlite_backup("d"*32)
            finally: helper.DATABASE,helper.BACKUPS=old_db,old_backups

    def test_atomic_switch_keeps_direct_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); target=root/"release"; target.mkdir(); current=root/"current"
            old=helper.CURRENT; helper.CURRENT=current
            try:
                helper.switch_link(target); self.assertTrue(current.is_symlink()); self.assertEqual(Path(os.readlink(current)),target)
            finally: helper.CURRENT=old

    def test_manifest_full_validation_and_downgrade_replay(self):
        manifest,request,raw=self.manifest_request()
        self.assertEqual(helper.validate_manifest(raw,request,"1.8.17"),manifest)
        mutations={"release_id":True,"commit":"A"*40,"published_at":"nope","min_helper_version":"01.0.0",
                   "auto_eligible":1,"rollback_compatible":1,"wheel_sha256":"z"*64,
                   "dependency_fingerprint":"z"*64,"wheel_size":True}
        for field,bad in mutations.items():
            changed=dict(manifest); changed[field]=bad; encoded=helper.canonical(changed)
            bound=dict(request); bound["manifest_sha256"]=hashlib.sha256(encoded).hexdigest()
            if field in bound: bound[field]=bad
            if field=="wheel_sha256": bound["wheel_sha256"]=bad
            with self.subTest(field=field), self.assertRaises(helper.Failure):
                helper.validate_manifest(encoded,bound,"1.8.17")
        with self.assertRaisesRegex(helper.Failure,"candidate-not-newer"):
            helper.validate_manifest(raw,request,"1.8.18")
        with self.assertRaisesRegex(helper.Failure,"candidate-not-newer"):
            helper.validate_manifest(raw,request,"1.9.0")

    def test_github_auto_age_and_actual_timestamp_contract(self):
        manifest,request,_=self.manifest_request(automatic=True)
        release={"id":42,"draft":False,"prerelease":False,"tag_name":"v1.8.18","target_commitish":"a"*40,
                 "published_at":"2026-11-20T00:00:00Z"}
        helper.validate_github_release(release,manifest,request,now=dt.datetime(2026,11,22,0,0,1,tzinfo=dt.timezone.utc))
        with self.assertRaisesRegex(helper.Failure,"too-young"):
            helper.validate_github_release(release,manifest,request,now=dt.datetime(2026,11,21,23,59,tzinfo=dt.timezone.utc))
        release["published_at"]="2026-11-19T00:00:00Z"
        with self.assertRaisesRegex(helper.Failure,"published-at-binding"):
            helper.validate_github_release(release,manifest,request,now=dt.datetime(2026,11,24,tzinfo=dt.timezone.utc))

    def test_parent_symlink_and_recursive_release_permissions_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as td:
            root=Path(td); real=root/"real"; real.mkdir(); (root/"link").symlink_to(real)
            real_lstat=helper.os.lstat
            def root_owned_lstat(path,*args,**kwargs):
                info=real_lstat(path,*args,**kwargs)
                if Path(path)==root/"link": return info
                return mock.Mock(st_mode=info.st_mode,st_uid=0)
            with mock.patch.object(helper.os,"lstat",side_effect=root_owned_lstat), self.assertRaisesRegex(helper.Failure,"symlink-parent"):
                helper.verify_parent_chain(root/"link")
            releases=root/"releases"; releases.mkdir(); candidate=releases/"1.8.18-x"; candidate.mkdir()
            child=candidate/"bad"; child.write_text("x"); child.chmod(0o666)
            old=helper.RELEASES; helper.RELEASES=releases
            try:
                release_lstat=helper.os.lstat
                def root_owned_release_lstat(path,*args,**kwargs):
                    info=release_lstat(path,*args,**kwargs)
                    return mock.Mock(st_mode=info.st_mode,st_uid=0)
                with mock.patch.object(helper.os,"lstat",side_effect=root_owned_release_lstat), self.assertRaisesRegex(helper.Failure,"writable-child"):
                    helper.validate_release_tree(candidate)
                child.chmod(0o600); outside=root/"outside"; outside.write_text("x"); (candidate/"escape").symlink_to(outside)
                with mock.patch.object(helper.os,"lstat",side_effect=root_owned_release_lstat), self.assertRaisesRegex(helper.Failure,"symlink-escape"):
                    helper.validate_release_tree(candidate)
            finally: helper.RELEASES=old

    def test_disk_gates_check_release_and_backup_filesystems_separately(self):
        manifest,_,_=self.manifest_request(); previous=Path(__file__).parent
        calls=[]
        def usage(path):
            calls.append(Path(path)); return shutil._ntuple_diskusage(10**12,0,10**12)
        old_releases,old_backups,old_db=helper.RELEASES,helper.BACKUPS,helper.DATABASE
        with tempfile.TemporaryDirectory(dir=ROOT) as td:
            root=Path(td); helper.RELEASES=root/"releases"; helper.RELEASES.mkdir(); helper.BACKUPS=root/"backups"; helper.BACKUPS.mkdir()
            helper.DATABASE=root/"db"; helper.DATABASE.write_bytes(b"x")
            try:
                with mock.patch.object(helper.shutil,"disk_usage",side_effect=usage): helper.disk_gates(manifest,previous)
                self.assertEqual(calls,[helper.RELEASES,helper.BACKUPS])
            finally: helper.RELEASES,helper.BACKUPS,helper.DATABASE=old_releases,old_backups,old_db

    def test_crash_recovery_covers_every_persisted_phase(self):
        phases=("verify","verified","backup","backup-complete","candidate-ready","stopping","stopped","switching","switched","starting","verifying")
        previous=Path("/releases/1.8.17-x"); candidate=Path("/releases/1.8.18-y")
        base={"request_id":"a"*32,"version":"1.8.18","automatic":False,"manifest_sha256":"b"*64,"wheel_sha256":"c"*64,
              "previous":previous.name,"candidate":candidate.name,"status":"running"}
        manifest={"version":"1.8.18"}
        for phase in phases:
            prior={**base,"phase":phase}; evidence=dict(base); transitions=[]
            current=candidate if phase in {"switched","starting","verifying"} else previous
            with self.subTest(phase=phase), \
                 mock.patch.object(helper,"current_release",return_value=current), \
                 mock.patch.object(helper,"start_services"), \
                 mock.patch.object(helper,"health"), \
                 mock.patch.object(helper,"installed_version",return_value="1.8.17"), \
                 mock.patch.object(helper,"transition",side_effect=lambda e,p,ph,**kw:(e.update(kw),e.update(phase=ph),transitions.append(ph))):
                done=helper.recover_if_needed(prior,evidence,Path("/evidence"),previous,candidate,manifest)
            if current==candidate: self.assertTrue(done); self.assertIn("complete",transitions)
            elif phase in {"stopping","stopped","switching"}: self.assertFalse(done)
            else: self.assertFalse(done)

    def test_post_switch_recovery_precedes_remote_revalidation(self):
        source=(ROOT/"scripts/open-dachs-update-helper.py").read_text()
        recovery=source.index("if recover_if_needed(prior, evidence")
        verified=source.index('transition(evidence, evidence_path, "verified")',recovery)
        remote=source.index("validate_github_release(fetch_github_release",verified)
        self.assertLess(recovery,verified)
        self.assertLess(verified,remote)
        self.assertIn("if not preserve_request:",source)

    def test_partial_mutable_snapshot_is_rebuilt_not_reused(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as td:
            base=Path(td); product=base/"product"; backups=base/"backups"
            product.mkdir(); backups.mkdir(mode=0o700)
            (product/"users.json").write_text("fresh")
            request_id="d"*32; partial=backups/f"{request_id}-files"
            partial.mkdir(mode=0o700); (partial/"users.json").write_text("partial")
            old=(helper.PRODUCT_DATA,helper.BACKUPS,helper.MUTABLE_FILES,helper.MUTABLE_DIRS)
            helper.PRODUCT_DATA,helper.BACKUPS=product,backups
            helper.MUTABLE_FILES=("users.json",); helper.MUTABLE_DIRS=()
            try:
                with mock.patch.object(helper,"validate_existing_snapshot_tree"), \
                     mock.patch.object(helper,"ensure_root_dir",return_value=partial), \
                     mock.patch.object(helper.os,"chown"), mock.patch.object(helper.os,"fchown"), \
                     mock.patch.object(helper.pwd,"getpwnam",return_value=mock.Mock(pw_uid=os.getuid())):
                    result=helper.snapshot_mutable(request_id)
                self.assertEqual((result/"users.json").read_text(),"fresh")
                manifest=json.loads((result/"SNAPSHOT.json").read_text())
                self.assertEqual(manifest["request_id"],request_id)
            finally:
                helper.PRODUCT_DATA,helper.BACKUPS,helper.MUTABLE_FILES,helper.MUTABLE_DIRS=old

    def test_serial_config_missing_defaults_and_malformed_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as td:
            config=Path(td)/"env"; old=helper.CONFIG; helper.CONFIG=config
            try:
                self.assertEqual(helper.configured_runtime(),("8084","","/dev/ttyUSB0"))
                for bad in ("relative", "/dev/../tmp/owned", "/tmp/ttyUSB3", "/dev/serial/by-id/a/b"):
                    config.write_text(f"OPEN_DACHS_SERIAL_PORT={bad}\n")
                    with self.subTest(bad=bad), self.assertRaisesRegex(helper.Failure,"serial-config-invalid"):
                        helper.configured_runtime()
                config.write_text("OPEN_DACHS_SERIAL_PORT=/dev/serial/by-id/reader\nOPEN_DACHS_WEB_PORT=8085\nOPEN_DACHS_BASE_PATH=/dachs\n")
                self.assertEqual(helper.configured_runtime(),("8085","/dachs","/dev/serial/by-id/reader"))
            finally:
                helper.CONFIG=old

    def test_serial_owner_must_be_exact_worker_main_pid(self):
        good=[mock.Mock(returncode=0,stdout="123\n"),mock.Mock(returncode=0,stdout=" 123\n")]
        device=mock.Mock(st_mode=helper.stat.S_IFCHR | 0o660)
        with mock.patch.object(helper.os.path,"exists",return_value=True), \
             mock.patch.object(helper.os.path,"realpath",return_value="/dev/ttyUSB3"), \
             mock.patch.object(helper.os,"stat",return_value=device), \
             mock.patch.object(helper.subprocess,"run",side_effect=good) as run:
            helper.verify_serial_owner("/dev/serial/by-id/reader")
            self.assertEqual(run.call_args_list[1].args[0],["/usr/bin/fuser","/dev/ttyUSB3"])
        bad=[mock.Mock(returncode=0,stdout="123\n"),mock.Mock(returncode=0,stdout="123 456\n")]
        with mock.patch.object(helper.os.path,"exists",return_value=True), \
             mock.patch.object(helper.os.path,"realpath",return_value="/dev/ttyUSB3"), \
             mock.patch.object(helper.os,"stat",return_value=device), \
             mock.patch.object(helper.subprocess,"run",side_effect=bad), \
             self.assertRaisesRegex(helper.Failure,"serial-owner-mismatch"):
            helper.verify_serial_owner("/dev/serial/by-id/reader")

    def test_rollback_failure_never_claims_rolled_back(self):
        evidence={}; transitions=[]; original=helper.Failure("original")
        with mock.patch.object(helper,"switch_link",side_effect=helper.Failure("rollback-switch")), \
             mock.patch.object(helper,"transition",side_effect=lambda e,p,ph,**kw:(e.update(kw),e.update(phase=ph),transitions.append(dict(e)))):
            self.assertFalse(helper.rollback(evidence,Path("/e"),Path("/previous"),original))
        self.assertEqual(evidence["status"],"recovery-required")
        self.assertEqual(evidence["original_error_class"],"original")
        self.assertEqual(evidence["rollback_error_class"],"rollback-switch")

if __name__=="__main__": unittest.main()
