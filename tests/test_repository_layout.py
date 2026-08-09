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
        motor_asset = ROOT / "src/open_dachs_manager/web/motor-horizontal.png"
        self.assertTrue(motor_asset.is_file())
        self.assertEqual(motor_asset.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        forbidden_names = {"users.json", ".env"}
        self.assertFalse(any(path.name in forbidden_names for path in ROOT.rglob("*")))
        forbidden_suffixes = {".class", ".jar", ".pdf", ".xml"}
        source_root = ROOT / "src"
        self.assertFalse(any(path.suffix.lower() in forbidden_suffixes for path in source_root.rglob("*")))

    def test_repository_contains_addressable_pack_and_compact_fault_catalogue(self):
        import json

        data_root = ROOT / "src/open_dachs_manager/data"
        self.assertEqual(
            {path.name for path in data_root.iterdir() if path.is_file()},
            {
                "formats.json",
                "fault_catalog_de.json",
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
        fault_catalog = json.loads((data_root / "fault_catalog_de.json").read_text(encoding="utf-8"))
        self.assertEqual(fault_catalog["schema"], "open-dachs-manager/fault-catalog/v1")
        self.assertEqual(len(fault_catalog["codes"]), 222)
        self.assertEqual(fault_catalog["codes"]["163"], "Leistung zu klein")

    def test_web_uses_project_brand_and_new_cookie(self):
        index = (ROOT / "src/open_dachs_manager/web/index.html").read_text(encoding="utf-8")
        web = (ROOT / "src/open_dachs_manager/web.py").read_text(encoding="utf-8")
        self.assertIn("Open Dachs Manager", index)
        self.assertNotIn("Dachs V3 Control", index)
        self.assertIn("open_dachs_session", web)
        self.assertNotIn('"dachs_session"', web)

    def test_web_has_overview_and_technical_hmi_views(self):
        index = (ROOT / "src/open_dachs_manager/web/index.html").read_text(encoding="utf-8")
        self.assertIn('id="schematicStage" class="hmi-schematic-stage"', index)
        self.assertIn('data-schematic-mode="overview"', index)
        self.assertIn('data-schematic-mode="technical"', index)
        self.assertIn('data-schematic-view="overview"', index)
        self.assertIn('data-schematic-view="technical"', index)
        self.assertIn('id="hmiServiceText"', index)
        self.assertIn('id="hmiWarningText"', index)
        self.assertIn('id="v95-overview-dachs-austritt"', index)
        self.assertIn('id="v95-tech-dachs-austritt"', index)
        self.assertIn('d="M440 235 H350" marker-end="url(#hmiArrowHot)"', index)
        self.assertIn('d="M112 525 H440" marker-end="url(#hmiArrowReturn)"', index)
        self.assertIn('d="M980 235 H1288" marker-end="url(#hmiArrowGas)"', index)
        self.assertIn('id="legacySchematicStage" class="legacy-schematic" hidden', index)

    def test_fault_catalog_is_its_own_main_tab(self):
        index = (ROOT / "src/open_dachs_manager/web/index.html").read_text(encoding="utf-8")
        app = (ROOT / "src/open_dachs_manager/web/app.js").read_text(encoding="utf-8")
        self.assertIn('data-view="faultCatalogView">Fehlerkatalog</button>', index)
        self.assertIn('<section id="faultCatalogView" class="app-view" hidden>', index)
        self.assertLess(index.index('id="faultCatalogView"'), index.index('id="settingsView"'))
        self.assertIn('showView("faultCatalogView")', app)
        self.assertIn('viewId === "faultCatalogView"', app)

    def test_technical_hmi_rows_and_changelog_popup_are_wired(self):
        index = (ROOT / "src/open_dachs_manager/web/index.html").read_text(encoding="utf-8")
        app = (ROOT / "src/open_dachs_manager/web/app.js").read_text(encoding="utf-8")
        self.assertIn('data-open-changelog aria-haspopup="dialog">V3 1.0</button>', index)
        self.assertIn('id="changelogModal" class="modal-backdrop" hidden', index)
        self.assertIn('aria-labelledby="changelogTitle"', index)
        self.assertIn('function openChangelog(event)', app)
        self.assertIn('function closeChangelog()', app)
        self.assertIn('class="tech95-data-cell" x="734" y="246" width="160" height="62"', index)
        self.assertIn('class="tech95-data-cell" x="1096" y="488" width="308" height="52"', index)
        self.assertIn('x="1108" y="505">Spannung L1 / L2 / L3</text><text id="v95-tech-voltage" class="tech95-value" x="1390" y="530"', index)
        self.assertIn('x="634" y="574">Kapseltemperatur</text><text id="v95-tech-kapsel" class="tech95-value" x="884" y="574"', index)
        self.assertIn('id="techHoneycomb95"', index)
        self.assertIn('id="v95-tech-abgas-motor" class="tech95-reading-value"', index)
        self.assertIn('id="v95-tech-abgas-hka" class="tech95-reading-value"', index)
        self.assertIn('id="v98-tech-soot-fill"', index)
        self.assertIn('id="sootFilterSettingsForm"', index)
        self.assertIn('/api/settings/soot-filter', app)
        self.assertEqual(index.count('href="static/motor-horizontal.png?v=2"'), 2)


if __name__ == "__main__":
    unittest.main()
