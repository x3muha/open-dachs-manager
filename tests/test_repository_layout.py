import os
from pathlib import Path
import hashlib
import subprocess
import tomllib
import unittest

from open_dachs_manager import __version__
from open_dachs_manager.serial_worker import DEFAULT_SERIAL_WORKER_SOCKET
from open_dachs_manager.web import DachsRequestHandler


ROOT = Path(__file__).resolve().parents[1]


class RepositoryLayoutTests(unittest.TestCase):
    def test_distribution_and_commands_use_open_dachs_name(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["name"], "open-dachs-manager")
        self.assertEqual(project["project"]["dynamic"], ["version"])
        self.assertNotIn("version", project["project"])
        self.assertEqual(
            project["tool"]["setuptools"]["dynamic"]["version"],
            {"attr": "open_dachs_manager.__version__"},
        )
        self.assertEqual(__version__, "1.5.0")
        self.assertEqual(DachsRequestHandler.server_version, f"OpenDachsManager/{__version__}")
        scripts = project["project"]["scripts"]
        self.assertEqual(scripts["open-dachs"], "open_dachs_manager.cli:main")
        self.assertEqual(
            scripts["open-dachs-serial-worker"],
            "open_dachs_manager.serial_worker:main",
        )
        self.assertEqual(DEFAULT_SERIAL_WORKER_SOCKET, "/run/open-dachs-manager/serial.sock")

    def test_installation_assets_are_present_and_executable(self):
        for relative in (
            "README.md",
            "LICENSE",
            "DEPENDENCIES.md",
            "docs/ASSETS.md",
            "docs/PROTOKOLL.md",
            "src/open_dachs_manager/web/ASSET-NOTICE.txt",
        ):
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
        self.assertIn("ODM_MAINTENANCE_LIVE_WRITES=0", installer)
        self.assertIn('! -f "$ODM_DATA_DIR/maintenance_settings.json"', installer)
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
        assembly = ROOT / "src/open_dachs_manager/web/dachs-generator-motor.png"
        self.assertTrue(assembly.is_file(), assembly.name)
        assembly_bytes = assembly.read_bytes()
        self.assertEqual(assembly_bytes[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(
            hashlib.sha256(assembly_bytes).hexdigest(),
            "5c0c130dfa3bfd8140b135ba7dbce5f56ca7a7fac624e02204075bdd257cde03",
        )
        asset_notice = (ROOT / "src/open_dachs_manager/web/ASSET-NOTICE.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("dachs-generator-motor.png", asset_notice)
        self.assertIn("not licensed under", asset_notice)
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

    def test_restore_response_cannot_repopulate_state_after_logout_or_file_change(self):
        app = (ROOT / "src/open_dachs_manager/web/app.js").read_text(encoding="utf-8")
        restore_start = app.index("async function restoreBackupImage(event)")
        restore_end = app.index("\nasync function refreshAudit()", restore_start)
        restore = app[restore_start:restore_end]
        self.assertIn("const restoreGeneration = state.backup.importGeneration;", restore)
        self.assertIn("const restoreImage = state.backup.image;", restore)
        self.assertIn("const restoreRequestIsStale = () =>", restore)
        self.assertIn("if (restoreRequestIsStale()) return;", restore)
        self.assertIn("if (!restoreRequestIsStale()) setRestoreBusy(false);", restore)
        self.assertIn("image: restoreImage", restore)
        self.assertNotIn("image: state.backup.image", restore)

    def test_system_management_is_separate_from_dachs_settings(self):
        index = (ROOT / "src/open_dachs_manager/web/index.html").read_text(encoding="utf-8")
        app = (ROOT / "src/open_dachs_manager/web/app.js").read_text(encoding="utf-8")
        settings_start = index.index('id="settingsView"')
        system_start = index.index('id="systemView"')
        self.assertIn('data-view="systemView" hidden>System</button>', index)
        self.assertLess(settings_start, system_start)
        self.assertIn('data-system-tab="users">Benutzer &amp; Berechtigungen</button>', index)
        self.assertIn('data-system-tab="tokens">API &amp; Tokens</button>', index)
        self.assertIn('data-system-tab="maintenance">Wartungsabschluss</button>', index)
        maintenance_start = index.index('id="maintenanceModeControls"')
        self.assertGreater(maintenance_start, system_start)
        self.assertNotIn('id="maintenanceModeControls"', index[settings_start:system_start])
        self.assertNotIn("Diese lokale Anzeige schreibt niemals in den Regler.", index)
        self.assertGreater(index.index('id="passwordForm"'), system_start)
        self.assertGreater(index.index('id="tokenCreateForm"'), system_start)
        self.assertIn('/api/v1/actions/set-value', index)
        self.assertIn('function refreshSystemUsers()', app)
        self.assertIn('function refreshSystemTokens()', app)
        self.assertIn('else if (selected === "maintenance") refreshMaintenanceMode();', app)

    def test_technical_hmi_rows_and_changelog_popup_are_wired(self):
        index = (ROOT / "src/open_dachs_manager/web/index.html").read_text(encoding="utf-8")
        app = (ROOT / "src/open_dachs_manager/web/app.js").read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertTrue(changelog.startswith("# Änderungsverlauf\n"))
        for english_term in (
            "Patch-Release", "Healthcheck", "Readback", "No-op", "Livewert",
            "Snapshot", "Dashboard", "Popup", "Audit", "Write", "Mapping",
            "Space-Padding", "Update", "Beta",
        ):
            self.assertNotIn(english_term, changelog)
            self.assertNotIn(english_term, index[index.index('id="changelogModal"'):])
        self.assertEqual(index.count("V3 __OPEN_DACHS_VERSION__"), 3)
        self.assertIn('id="changelogModal" class="modal-backdrop" hidden', index)
        self.assertIn('aria-labelledby="changelogTitle"', index)
        self.assertIn('function openChangelog(event)', app)
        self.assertIn('function closeChangelog()', app)
        self.assertIn('class="tech95-data-cell" x="370" y="500" width="170" height="48"', index)
        self.assertIn('class="tech95-data-cell" x="1096" y="488" width="308" height="52"', index)
        self.assertIn('x="1108" y="505">Spannung L1 / L2 / L3</text><text id="v95-tech-voltage" class="tech95-value" x="1390" y="530"', index)
        self.assertIn('x="634" y="637">Kapseltemperatur</text><text id="v95-tech-kapsel" class="tech95-value" x="884" y="637"', index)
        self.assertIn('id="techHoneycomb95"', index)
        self.assertIn('id="v95-tech-abgas-motor" class="tech95-reading-value"', index)
        self.assertIn('id="v95-tech-abgas-hka" class="tech95-reading-value"', index)
        self.assertIn('id="v98-tech-soot-fill"', index)
        self.assertIn('id="sootFilterSettingsForm"', index)
        self.assertIn('/api/settings/soot-filter', app)
        self.assertEqual(index.count('href="static/dachs-generator-motor.png?v=1"'), 2)
        self.assertNotIn('href="static/motor-horizontal.png', index)
        self.assertNotIn('href="static/generator-horizontal.png', index)
        self.assertLess(index.index('x="563" y="183" text-anchor="middle">GENERATOR'), index.index('x="812" y="183" text-anchor="middle">MOTOR'))
        self.assertLess(index.index('x="469" y="171" text-anchor="middle">GENERATOR'), index.index('x="758" y="171" text-anchor="middle">MOTOR'))
        self.assertNotIn('class="hmi-generator"', index)
        self.assertNotIn('class="tech95-generator-symbol"', index)


if __name__ == "__main__":
    unittest.main()
