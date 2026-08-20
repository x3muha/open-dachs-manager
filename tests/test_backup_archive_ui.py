from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "src" / "open_dachs_manager" / "web"


class BackupArchiveUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        cls.app = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
        cls.style = (WEB_ROOT / "style.css").read_text(encoding="utf-8")
        cls.installer = (ROOT / "install.sh").read_text(encoding="utf-8")

    def test_admin_archive_is_wired_without_delete_or_manual_archive_action(self):
        self.assertIn(
            'id="backupArchivePanel" class="panel backup-archive-panel backup-admin" hidden',
            self.index,
        )
        self.assertIn('id="backupArchiveRefresh"', self.index)
        self.assertIn('id="backupArchiveList"', self.index)
        archive_html = self.index[
            self.index.index('id="backupArchivePanel"'):
            self.index.index('<section class="panel backup-panel">')
        ]
        self.assertNotIn("Löschen", archive_html)
        self.assertNotIn("Backup erstellen", archive_html)
        self.assertIn('api("/api/backup/archive")', self.app)
        self.assertIn('$("backupArchiveRefresh").addEventListener', self.app)
        self.assertIn('data-load-backup-archive', self.app)
        self.assertIn('data-open-maintenance-report', self.app)

    def test_archive_download_is_base_path_safe_and_reinspected_fail_closed(self):
        self.assertIn(
            'appUrl(`/api/backup/archive/${encodeURIComponent(id)}/download`)',
            self.app,
        )
        self.assertIn(
            'api(`/api/backup/archive/${encodeURIComponent(archiveId)}/download`)',
            self.app,
        )
        self.assertIn('api("/api/backup/inspect"', self.app)
        self.assertIn('inspection.digest_present !== true', self.app)
        self.assertIn('inspection.digest_verified !== true', self.app)
        self.assertIn('inspection.live_restore_compatible !== true', self.app)
        self.assertIn('![38, 42].includes(requested)', self.app)
        self.assertIn('successful !== requested', self.app)
        self.assertIn('restorable !== 38', self.app)
        self.assertIn('failed !== 0', self.app)
        self.assertIn('archiveState === "ready"', self.app)
        self.assertIn('item?.pack_compatible === true', self.app)
        self.assertIn(
            'download="${escapeHtml(filename)}">JSON herunterladen</a><button type="button" '
            'data-load-backup-archive="${escapeHtml(id)}" ${ready ? "" : "disabled"}',
            self.app,
        )

    def test_archive_restore_starts_empty_and_in_dry_run(self):
        load_start = self.app.index("async function loadBackupArchiveForRestore")
        load_end = self.app.index("\nfunction backupImageText", load_start)
        load = self.app[load_start:load_end]
        self.assertIn('$("restoreWriteEnabled").checked = false', load)
        self.assertIn('$("restoreConfirmation").value = ""', load)
        self.assertIn('$("restorePass4").value = ""', load)
        self.assertIn("renderRestoreBlockList();", load)
        self.assertIn("Kein Ziel ist ausgewählt; Dry-Run bleibt", load)
        self.assertIn('blockChoiceMarkup(item, "restore", false', self.app)

    def test_archive_refresh_cannot_repopulate_admin_state_after_logout(self):
        refresh_start = self.app.index("async function refreshBackupArchive")
        refresh_end = self.app.index(
            "\nasync function showBackupArchiveEntry", refresh_start
        )
        refresh = self.app[refresh_start:refresh_end]
        self.assertIn(
            "const requestGeneration = state.backup.importGeneration;", refresh
        )
        self.assertGreaterEqual(
            refresh.count(
                'requestGeneration !== state.backup.importGeneration || state.user?.role !== "admin"'
            ),
            2,
        )

    def test_archive_markup_escapes_server_fields_and_has_mobile_layout(self):
        render_start = self.app.index("function renderBackupArchive()")
        render_end = self.app.index("\nasync function refreshBackupArchive", render_start)
        render = self.app[render_start:render_end]
        for fragment in (
            "escapeHtml(id)",
            "escapeHtml(stateText)",
            'escapeHtml(item?.created_by || "unbekannt")',
            'escapeHtml(item?.pack_revision || "—")',
            "escapeHtml(filename)",
            "escapeHtml(downloadUrl)",
        ):
            self.assertIn(fragment, render)
        self.assertIn("@media (max-width:480px)", self.style)
        self.assertIn(".backup-archive-metrics,.backup-archive-hashes { grid-template-columns:1fr; }", self.style)

    def test_maintenance_links_backup_and_blocks_ambiguous_completion_actions(self):
        self.assertIn('id="maintenanceBackupSummary"', self.index)
        self.assertIn("Backup #${escapeHtml(backup.id)}", self.app)
        self.assertIn('item.status === "completing"', self.app)
        self.assertIn('item.status === "uncertain"', self.app)
        self.assertIn("Abschluss läuft", self.app)
        self.assertIn("Zielzustand unklar – prüfen", self.app)
        self.assertIn('const deletable = ["draft", "completed"].includes(item.status);', self.app)
        self.assertIn('item.status === "completing"', self.app)
        self.assertIn("Nach Abschluss verfügbar", self.app)
        self.assertIn('$("maintenanceCompletion").hidden = !editable;', self.app)
        self.assertIn("Das Backup-Archiv blieb erhalten", self.app)

    def test_completing_report_detail_exports_are_inert_and_restore_later(self):
        editor_start = self.app.index("function renderMaintenanceEditor()")
        editor_end = self.app.index("\nfunction maintenanceProtocolFromForm", editor_start)
        editor = self.app[editor_start:editor_end]
        self.assertIn('if (item.status === "completing")', editor)
        self.assertIn('link.removeAttribute("href")', editor)
        self.assertIn('link.setAttribute("aria-disabled", "true")', editor)
        self.assertIn('link.setAttribute("tabindex", "-1")', editor)
        self.assertIn('link.classList.add("disabled")', editor)
        self.assertIn("link.href = appUrl(", editor)
        self.assertIn('link.removeAttribute("aria-disabled")', editor)
        self.assertIn('link.removeAttribute("tabindex")', editor)
        self.assertIn('link.classList.remove("disabled")', editor)

    def test_installer_creates_archive_with_nofollow_descriptor_operations(self):
        self.assertIn('os.open(data_dir, directory_flags)', self.installer)
        self.assertIn('os.O_DIRECTORY | os.O_NOFOLLOW', self.installer)
        self.assertIn('os.mkdir("backup-archive", mode=0o700, dir_fd=parent_fd)', self.installer)
        self.assertIn('os.open("backup-archive", directory_flags, dir_fd=parent_fd)', self.installer)
        self.assertIn("os.fchown(archive_fd", self.installer)
        self.assertIn("os.fchmod(archive_fd, 0o700)", self.installer)
        self.assertNotIn('install -d -m 0700', self.installer)
        self.assertIn("! -name 'backup-archive' -print -quit", self.installer)


if __name__ == "__main__":
    unittest.main()
