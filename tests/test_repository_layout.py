import os
from pathlib import Path
import subprocess
import tomllib
import unittest

from open_dachs_manager.serial_worker import DEFAULT_SERIAL_WORKER_SOCKET


ROOT = Path(__file__).resolve().parents[1]


class RepositoryLayoutTests(unittest.TestCase):
    def test_distribution_and_commands_use_open_dachs_name(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["name"], "open-dachs-manager")
        scripts = project["project"]["scripts"]
        self.assertEqual(scripts["open-dachs"], "open_dachs_manager.cli:main")
        self.assertEqual(
            scripts["open-dachs-serial-worker"],
            "open_dachs_manager.serial_worker:main",
        )
        self.assertEqual(DEFAULT_SERIAL_WORKER_SOCKET, "/run/open-dachs-manager/serial.sock")

    def test_installation_assets_are_present_and_executable(self):
        for relative in ("README.md", "LICENSE", "DEPENDENCIES.md", "docs/PROTOKOLL.md"):
            self.assertTrue((ROOT / relative).is_file(), relative)
        for relative in ("install.sh", "uninstall.sh"):
            path = ROOT / relative
            self.assertTrue(path.is_file(), relative)
            self.assertTrue(os.access(path, os.X_OK), relative)
            subprocess.run(["bash", "-n", str(path)], check=True)
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        for package in ("python3", "python3-venv", "python3-pip", "git", "ca-certificates"):
            self.assertIn(package, installer)
        self.assertIn("apt-get install", installer)
        self.assertIn("open-dachs-manager-install", installer)
        self.assertIn("--exclude='build'", installer)
        for service in (
            "systemd/open-dachs-manager-serial.service",
            "systemd/open-dachs-manager-web.service",
        ):
            text = (ROOT / service).read_text(encoding="utf-8")
            self.assertNotIn("/root/senertec", text)
            self.assertNotIn("dachs-v3", text)
            self.assertIn("User=open-dachs", text)

    def test_foreign_logo_and_runtime_secrets_are_not_bundled(self):
        self.assertFalse((ROOT / "src/open_dachs_manager/web/logo_small.png").exists())
        forbidden_names = {"users.json", ".env"}
        self.assertFalse(any(path.name in forbidden_names for path in ROOT.rglob("*")))
        forbidden_suffixes = {".class", ".jar", ".pdf", ".xml"}
        source_root = ROOT / "src"
        self.assertFalse(any(path.suffix.lower() in forbidden_suffixes for path in source_root.rglob("*")))

    def test_repository_contains_only_addressable_runtime_pack_and_no_service_text_catalogue(self):
        import json

        data_root = ROOT / "src/open_dachs_manager/data"
        self.assertEqual(
            {path.name for path in data_root.iterdir() if path.is_file()},
            {
                "formats.json",
                "labels_master.properties",
                "meldehist_types_de.properties",
                "msr2_pack_master_version.json",
                "physical_offsets.json",
                "ui_metadata.json",
                "write_allowlist.json",
            },
        )
        pack = json.loads((data_root / "msr2_pack_master_version.json").read_text(encoding="utf-8"))
        self.assertTrue(pack["blocks"])
        self.assertTrue(all(0 <= int(block) <= 255 for block in pack["blocks"]))
        self.assertFalse((data_root / "servicecodes_de.properties").exists())

    def test_web_uses_project_brand_and_new_cookie(self):
        index = (ROOT / "src/open_dachs_manager/web/index.html").read_text(encoding="utf-8")
        web = (ROOT / "src/open_dachs_manager/web.py").read_text(encoding="utf-8")
        self.assertIn("Open Dachs Manager", index)
        self.assertNotIn("Dachs V3 Control", index)
        self.assertIn("open_dachs_session", web)
        self.assertNotIn('"dachs_session"', web)


if __name__ == "__main__":
    unittest.main()
