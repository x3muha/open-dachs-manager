import os
from pathlib import Path
import hashlib
import subprocess
import tempfile
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
        self.assertEqual(__version__, "1.8.17")
        self.assertEqual(DachsRequestHandler.server_version, f"OpenDachsManager/{__version__}")
        scripts = project["project"]["scripts"]
        self.assertEqual(scripts["open-dachs"], "open_dachs_manager.cli:main")
        self.assertEqual(
            scripts["open-dachs-serial-worker"],
            "open_dachs_manager.serial_worker:main",
        )
        self.assertEqual(DEFAULT_SERIAL_WORKER_SOCKET, "/run/open-dachs-manager/serial.sock")

    def test_public_release_documents_match_runtime_version(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        manual = (ROOT / "docs/BEDIENUNGSANLEITUNG.md").read_text(encoding="utf-8")
        safety = (ROOT / "docs/SAFETY.md").read_text(encoding="utf-8")

        self.assertTrue(
            readme.startswith(f"# Open Dachs Manager · V3 {__version__}\n")
        )
        self.assertIn(f"**Projektstatus: Version {__version__}.**", readme)
        self.assertIn(f"Version V3 {__version__}", manual)
        self.assertIn(f"V3 {__version__} wird standardmäßig", manual)
        self.assertIn(f"Version {__version__} basiert", safety)

    def test_touch_operator_view_reuses_process_image_and_has_direct_route(self):
        html = (ROOT / "src/open_dachs_manager/web/index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "src/open_dachs_manager/web/app.js").read_text(encoding="utf-8")
        stylesheet = (ROOT / "src/open_dachs_manager/web/style.css").read_text(encoding="utf-8")

        self.assertEqual(html.count('id="plantBoardPanel"'), 1)
        self.assertIn('data-view="operatorView">Bedienplatz</button>', html)
        self.assertIn('id="operatorPlantMount"', html)
        self.assertIn('id="operatorChart"', html)
        self.assertIn('["drehzahl", "wirkleistung", "abgas_motor"]', javascript)
        self.assertIn('window.location.hash === "#bedienplatz"', javascript)
        self.assertIn('target.append(panel)', javascript)
        self.assertIn('.operator-view .hmi-diagram', stylesheet)

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
        self.assertIn("ODM_SERVICE_RESET_TEMPERATURE_POLICY_APPROVED=0", installer)
        self.assertIn("OPEN_DACHS_SERVICE_RESET_TEMPERATURE_POLICY_APPROVED=%s", installer)
        self.assertIn("ODM_EXISTING_SERVICE_RESET_POLICY", installer)
        self.assertIn("ODM_SMTP_PRIVATE_ALLOWLIST=\"\"", installer)
        self.assertIn("OPEN_DACHS_SMTP_PRIVATE_ALLOWLIST=%s", installer)
        self.assertIn("ODM_EXISTING_SMTP_ALLOWLIST", installer)
        self.assertIn("ODM_SMTP_PLAIN_ALLOWLIST=\"\"", installer)
        self.assertIn("OPEN_DACHS_SMTP_PLAIN_ALLOWLIST=%s", installer)
        self.assertIn("ODM_EXISTING_SMTP_PLAIN_ALLOWLIST", installer)
        for service in (
            "systemd/open-dachs-manager-serial.service",
            "systemd/open-dachs-manager-web.service",
        ):
            text = (ROOT / service).read_text(encoding="utf-8")
            self.assertNotIn("/root/senertec", text)
            self.assertNotIn("dachs-v3", text)
            self.assertIn("User=open-dachs", text)
        web_service = (ROOT / "systemd/open-dachs-manager-web.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("CapabilityBoundingSet=CAP_NET_BIND_SERVICE", web_service)
        self.assertIn("AmbientCapabilities=CAP_NET_BIND_SERVICE", web_service)

    def test_installer_service_reset_policy_is_fail_closed_and_preserved(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        marker = "# Manual service-code reset remains fail-closed"
        start = installer.index(marker)
        end = installer.index("\n{\n", start)
        policy_block = installer[start:end]

        cases = (
            (None, "0"),
            ("OPEN_DACHS_SERVICE_RESET_TEMPERATURE_POLICY_APPROVED=1\n", "1"),
            ("OPEN_DACHS_SERVICE_RESET_TEMPERATURE_POLICY_APPROVED=true\n", "1"),
            ("OPEN_DACHS_SERVICE_RESET_TEMPERATURE_POLICY_APPROVED=0\n", "0"),
            ("OPEN_DACHS_SERVICE_RESET_TEMPERATURE_POLICY_APPROVED=maybe\n", "0"),
            (
                "OPEN_DACHS_SERVICE_RESET_TEMPERATURE_POLICY_APPROVED=1\n"
                "OPEN_DACHS_SERVICE_RESET_TEMPERATURE_POLICY_APPROVED=0\n",
                "0",
            ),
            (
                "OPEN_DACHS_SERVICE_RESET_TEMPERATURE_POLICY_APPROVED=0\n"
                "OPEN_DACHS_SERVICE_RESET_TEMPERATURE_POLICY_APPROVED=on\n",
                "1",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "open-dachs-manager.env"
            for content, expected in cases:
                with self.subTest(content=content, expected=expected):
                    if content is None:
                        config.unlink(missing_ok=True)
                    else:
                        config.write_text(content, encoding="utf-8")
                    completed = subprocess.run(
                        [
                            "bash",
                            "-c",
                            policy_block
                            + "\nprintf '%s' \"$ODM_SERVICE_RESET_TEMPERATURE_POLICY_APPROVED\"",
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                        env={**os.environ, "ODM_CONFIG_FILE": str(config)},
                    )
                    self.assertEqual(completed.stdout, expected)

    def test_installer_smtp_private_allowlist_is_root_managed_and_preserved(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        marker = "# Private SMTP targets are blocked"
        start = installer.index(marker)
        end = installer.index("\n{\n", start)
        policy_block = installer[start:end]
        cases = (
            (None, ""),
            ("OPEN_DACHS_SMTP_PRIVATE_ALLOWLIST=relay.intern:587\n", "relay.intern:587"),
            (
                "OPEN_DACHS_SMTP_PRIVATE_ALLOWLIST=old.intern:587\n"
                "OPEN_DACHS_SMTP_PRIVATE_ALLOWLIST=10.0.0.2:587,new.intern:587\n",
                "10.0.0.2:587,new.intern:587",
            ),
            ("OPEN_DACHS_SMTP_PRIVATE_ALLOWLIST=relay;touch /tmp/nope\n", ""),
        )
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "open-dachs-manager.env"
            for content, expected in cases:
                with self.subTest(content=content, expected=expected):
                    if content is None:
                        config.unlink(missing_ok=True)
                    else:
                        config.write_text(content, encoding="utf-8")
                    completed = subprocess.run(
                        [
                            "bash", "-c", policy_block
                            + "\nprintf '%s' \"$ODM_SMTP_PRIVATE_ALLOWLIST\"",
                        ],
                        check=True, capture_output=True, text=True,
                        env={**os.environ, "ODM_CONFIG_FILE": str(config)},
                    )
                    self.assertEqual(completed.stdout, expected)
                    self.assertFalse(Path("/tmp/nope").exists())

    def test_installer_smtp_plain_allowlist_is_separate_and_preserved(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        start = installer.index("# Plain SMTP is a separate")
        end = installer.index("\n{\n", start)
        policy_block = installer[start:end]
        cases = (
            (None, ""),
            ("OPEN_DACHS_SMTP_PLAIN_ALLOWLIST=relay.intern:25\n", "relay.intern:25"),
            (
                "OPEN_DACHS_SMTP_PLAIN_ALLOWLIST=old.intern:25\n"
                "OPEN_DACHS_SMTP_PLAIN_ALLOWLIST=10.0.0.2:25,new.intern:25\n",
                "10.0.0.2:25,new.intern:25",
            ),
            ("OPEN_DACHS_SMTP_PLAIN_ALLOWLIST=relay;touch /tmp/nope\n", ""),
        )
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "open-dachs-manager.env"
            for content, expected in cases:
                with self.subTest(content=content, expected=expected):
                    if content is None:
                        config.unlink(missing_ok=True)
                    else:
                        config.write_text(content, encoding="utf-8")
                    completed = subprocess.run(
                        [
                            "bash", "-c", policy_block
                            + "\nprintf '%s' \"$ODM_SMTP_PLAIN_ALLOWLIST\"",
                        ],
                        check=True, capture_output=True, text=True,
                        env={**os.environ, "ODM_CONFIG_FILE": str(config)},
                    )
                    self.assertEqual(completed.stdout, expected)
                    self.assertFalse(Path("/tmp/nope").exists())

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
                "display_status_texts_de.json",
                "update_release_public_key.pem",
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

    def test_overview_reads_native_hours_per_start_and_keeps_automatic_pw4(self):
        app = (ROOT / "src/open_dachs_manager/web/app.js").read_text(encoding="utf-8")
        ratio_start = app.index("function operatingHoursPerStartDisplay()")
        ratio_end = app.index("\nfunction renderOverview()", ratio_start)
        ratio = app[ratio_start:ratio_end]
        self.assertIn(
            "dashboardRow(OPERATING_HOURS_PER_START_BLOCK, OPERATING_HOURS_PER_START_KEY)",
            ratio,
        )
        self.assertNotIn("state.live?.operating_hours_per_start", ratio)
        self.assertIn('minimumFractionDigits: 1', ratio)
        self.assertIn('maximumFractionDigits: 1', ratio)
        self.assertIn("Bh/Start", ratio)
        self.assertIn("Block 38 · direkt aus dem Regler", ratio)
        self.assertNotIn("Block 22", ratio)
        self.assertIn("Betriebsstunden je Start", ratio)
        self.assertIn("function operatingHoursPerStartCard()", ratio)
        self.assertIn("function operatingHoursPerStartDetail()", ratio)
        self.assertIn("function runtime24hCard()", ratio)
        self.assertIn("state.live?.runtime_24h", ratio)
        self.assertIn('<div class="metric-label">Laufzeit seit Start</div>', ratio)
        self.assertNotIn("Laufzeit seit Start (24-h-Zähler)", ratio)
        self.assertIn("realRuntimeDisplay()", ratio)
        self.assertIn('title="Reglerzähler (0x0f0e) setzen"', ratio)
        self.assertIn("6 h (Nachtabschaltung) in ", ratio)
        self.assertIn('"24 h in "', ratio)
        self.assertIn("nur aktive Laufzeit, Reset bei jedem Start", ratio)
        # The removed runtime_24h_auto module stays gone; the new, separate
        # rule is read from state.live.runtime_auto_reset only.
        self.assertNotIn("metric?.auto_reset", ratio)
        self.assertNotIn("runtime_24h?.auto_reset", ratio)
        self.assertIn("state.live?.runtime_auto_reset", ratio)
        self.assertNotIn("Autoreset", app)
        self.assertNotIn("runtime-24h-auto", app)
        self.assertIn('return "—"', ratio)

        card_start = app.index("function dashboardCardHtml(card)")
        card_end = app.index("\nfunction renderOverview()", card_start)
        card = app[card_start:card_end]
        self.assertIn("Number(card.block) === OPERATING_HOURS_PER_START_BLOCK", card)
        self.assertIn("card.key === OPERATING_HOURS_PER_START_KEY", card)
        self.assertNotIn('field?.source === "operating_hours_per_start"', card)
        self.assertIn("return operatingHoursPerStartCard();", card)
        self.assertIn('field?.source === "runtime_24h_shutdown"', card)
        self.assertIn("return runtime24hCard();", card)
        self.assertIn('field?.source === "soot_filter_regeneration"', card)
        self.assertIn("return sootRegenerationCard();", card)
        self.assertIn('field?.source === "ram_telemetry"', card)
        self.assertIn("return ramTelemetryCard();", card)

        telemetry_start = app.index("function ramTelemetryValue(item)")
        telemetry_end = app.index("\nfunction dashboardCardHtml(card)", telemetry_start)
        telemetry = app[telemetry_start:telemetry_end]
        self.assertIn("state.live?.ram_telemetry", telemetry)
        self.assertIn('"nicht verfügbar"', telemetry)
        self.assertNotIn("Deutung mittel", telemetry)
        self.assertNotIn("Typzuordnung", telemetry)
        self.assertNotIn("Zustandsdeutung", telemetry)
        render = telemetry[telemetry.index("function renderSootRegeneration()"):]
        self.assertNotIn("RAM 0x0d", render)
        self.assertNotIn("recorded_at", render)
        self.assertNotIn("detail-extra", telemetry[:telemetry.index("function ramTelemetryCard()")])
        self.assertIn('$("ramTelemetryCards")', telemetry)
        self.assertIn("function ramTelemetryCard()", telemetry)
        self.assertIn("function sootRegenerationCard()", telemetry)
        self.assertIn("function renderSootRegeneration()", telemetry)
        self.assertIn("state.live?.soot_filter_regeneration", telemetry)
        self.assertIn('"nicht unterstützt"', telemetry)
        self.assertIn("pausiert bei Stillstand", telemetry)
        self.assertNotIn("Füllstand", telemetry.replace("kein Füllstand", ""))
        self.assertIn('$("sootRegenerationCards")', telemetry)
        html = (ROOT / "src/open_dachs_manager/web/index.html").read_text(encoding="utf-8")
        # Both sub-sections live inside the System panel "Status und Meldungen"
        # of the overview; the former separate overview panel is gone.
        system_start = html.index("<h3>Status und Meldungen</h3>")
        system_end = html.index("</section>", system_start)
        system_panel = html[system_start:system_end]
        self.assertIn('id="systemStateCards"', system_panel)
        self.assertIn("Rußfilterregeneration", system_panel)
        self.assertIn('id="sootRegenerationCards"', system_panel)
        self.assertIn("Interne Reglerwerte", system_panel)
        self.assertIn('id="ramTelemetryCards"', system_panel)
        self.assertLess(system_panel.index('id="sootRegenerationCards"'), system_panel.index('id="ramTelemetryCards"'))
        self.assertNotIn('class="panel ram-telemetry"', html)
        self.assertNotIn("Nur lesend aus CPU-0-RAM", html)
        self.assertNotIn("Nur lesend über Dienst 0x02", html)
        self.assertNotIn("Die Auswahl wird lokal auf dem Pi gespeichert. Zusätzliche Blöcke", html)
        self.assertNotIn("Ruhige HMI-Darstellung", html)
        self.assertEqual(html.count('id="ramTelemetryCards"'), 1)

        overview_start = app.index("function renderOverview()")
        overview_end = app.index("\nfunction allDashboardFields()", overview_start)
        overview = app[overview_start:overview_end]
        self.assertIn('cards.map(dashboardCardHtml).join("")', overview)
        self.assertIn("motorCards.push(operatingHoursPerStartDetail());", overview)
        self.assertNotIn("operatingHoursPerStartCard() + configuredCards", overview)
        self.assertIn("for (const field of dashboardDerivedFields()) add(field);", app)

        write_start = app.index("async function applyOverviewPowerTarget(event)")
        write_end = app.index("\nfunction updateConnection()", write_start)
        write = app[write_start:write_end]
        self.assertIn('/api/overview/power-target', write)
        self.assertNotIn('authLevel', write)
        self.assertNotIn('pass4', write)
        self.assertIn('PW4 automatisch', app)

    def test_display_status_lines_lead_system_status_panel(self):
        app = (ROOT / "src/open_dachs_manager/web/app.js").read_text(encoding="utf-8")
        html = (ROOT / "src/open_dachs_manager/web/index.html").read_text(encoding="utf-8")
        system_start = html.index("<h3>Status und Meldungen</h3>")
        system_end = html.index("</section>", system_start)
        system_panel = html[system_start:system_end]
        # The display list sits directly below the status entries, before the
        # soot-regeneration and RAM sub-sections.
        self.assertIn('id="displayStatusHistory"', system_panel)
        self.assertLess(system_panel.index('id="systemStateCards"'), system_panel.index('id="displayStatusHistory"'))
        self.assertLess(system_panel.index('id="displayStatusHistory"'), system_panel.index('id="sootRegenerationCards"'))
        self.assertIn('static/app.js?v=71', html)
        self.assertIn('static/style.css?v=50', html)

        overview_start = app.index("function renderOverview()")
        overview_end = app.index("\nfunction allDashboardFields()", overview_start)
        overview = app[overview_start:overview_end]
        self.assertIn('$("systemStateCards").innerHTML = displayStatusDetails() + controllerClockDetail()', overview)
        clock_start = app.index("function controllerClockDetail()")
        clock = app[clock_start:app.index("\nfunction displayStatusCard()", clock_start)]
        self.assertIn('systemDetailItem("Regleruhr", value, available)', clock)
        self.assertIn("clock?.available === true", clock)
        self.assertLess(overview.index("displayStatusDetails()"), overview.index("system.map("))

        details_start = app.index("function displayStatusLine(number)")
        details_end = app.index("\nfunction displayStatusCard()", details_start)
        details = app[details_start:details_end]
        self.assertIn('systemDetailItem("Display Zeile 1"', details)
        self.assertIn('systemDetailItem("Display Zeile 2"', details)
        self.assertIn("data-display-status-history", details)
        self.assertIn('"nicht verfügbar"', details)
        # No addresses, confidences or explanatory text in the system view.
        for forbidden in ("0x", "Konfidenz", "berechnet", "recorded_at", "Firmware"):
            self.assertNotIn(forbidden, details)

        card_start = app.index("function dashboardCardHtml(card)")
        card_end = app.index("\nfunction renderOverview()", card_start)
        self.assertIn('field?.source === "display_status"', app[card_start:card_end])
        self.assertIn("return displayStatusCard();", app[card_start:card_end])
        self.assertIn('api("/api/display-status/events?limit=50")', app)
        self.assertIn('$("systemStateCards").addEventListener("click", toggleDisplayStatusHistory);', app)
        self.assertNotIn('fetch("/api/display-status', app)


if __name__ == "__main__":
    unittest.main()
