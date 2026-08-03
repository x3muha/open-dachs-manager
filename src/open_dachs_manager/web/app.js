"use strict";

const state = {
  user: null,
  schema: null,
  live: null,
  selectedView: "overviewView",
  selectedBlock: 50,
  selectedCpu: 0,
  block: null,
  showReserved: false,
  histories: new Map(),
  chartSeries: { temperature: [], motor: [], exhaust: [] },
  chartHidden: { temperature: new Set(), motor: new Set(), exhaust: new Set() },
  chartZoomRanges: new Map(),
  chartPointers: new WeakMap(),
  chartGeometries: new WeakMap(),
  historyWindow: null,
  chartRefresh: { inFlight: false, pending: false },
  authPreview: null,
  maintenance: { reports: [], current: null, autosaveTimer: null },
  refreshTimer: null,
  chartTimer: null,
};

const $ = (id) => document.getElementById(id);
const loginView = $("loginView");
const appView = $("appView");

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[char]));
}

function formatValue(value, unit = "") {
  if (value === null || value === undefined || value === "") return "—";
  return `${escapeHtml(value)}${unit ? ` ${escapeHtml(unit)}` : ""}`;
}

function numeric(value) {
  if (typeof value === "number") return value;
  const parsed = Number(String(value ?? "").replace(",", ".").replace(/[^0-9+\-.eE]/g, ""));
  return Number.isFinite(parsed) ? parsed : null;
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  const text = await response.text();
  let payload = {};
  try { payload = text ? JSON.parse(text) : {}; } catch (_) { payload = { error: text }; }
  if (response.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("Anmeldung erforderlich");
  }
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function showLogin(message = "") {
  state.user = null;
  if (state.refreshTimer) clearInterval(state.refreshTimer);
  if (state.chartTimer) clearInterval(state.chartTimer);
  loginView.hidden = false;
  appView.hidden = true;
  $("loginError").textContent = message;
}

function showApp(user) {
  state.user = user;
  loginView.hidden = true;
  appView.hidden = false;
  $("userBadge").textContent = `${user.username} · ${user.role === "admin" ? "Admin" : "Gast"}`;
  const isAdmin = user.role === "admin";
  $("writeControls").hidden = !isAdmin;
  $("saveBlockButton").hidden = !isAdmin;
  $("monitorToggle").hidden = !isAdmin;
  $("serialToggle").hidden = !isAdmin;
  $("overviewPowerTargetForm").hidden = !isAdmin;
  $("accountControls").hidden = !isAdmin;
  $("guestAccountControls").hidden = !isAdmin;
  document.querySelectorAll(".maintenance-admin").forEach((element) => { element.hidden = !isAdmin; });
  $("settingsRoleHint").textContent = isAdmin ? "Admin: lesen und schreiben" : "Gast: nur lesen";
  $("settingsRoleHint").className = `status-pill ${isAdmin ? "ok" : "neutral"}`;
  if (isAdmin) updateWriteGuard(); else renderOverviewPowerWriteMode();
}

async function login(event) {
  event.preventDefault();
  $("loginError").textContent = "";
  try {
    await api("/api/login", { method: "POST", body: JSON.stringify({ username: $("loginUsername").value, password: $("loginPassword").value }) });
    await boot();
  } catch (error) {
    $("loginError").textContent = error.message;
  }
}

async function boot() {
  try {
    const session = await api("/api/session");
    if (!session.authenticated) return showLogin();
    showApp(session.user);
    state.schema = await api("/api/schema");
    renderRegisterTabs();
    await refreshLive();
    await refreshMaintenance(false);
    await loadBlock(state.selectedBlock);
    refreshMonitorStatus();
    refreshAudit();
    state.refreshTimer = setInterval(refreshLive, 1100);
    state.chartTimer = setInterval(() => {
      if (state.selectedView === "monitorView") refreshCharts();
    }, 6500);
  } catch (error) {
    if (error.message !== "Anmeldung erforderlich") showLogin(error.message);
  }
}

function valueIndex() {
  const map = new Map();
  for (const row of (state.live?.values || [])) map.set(`${row.block}:${row.key}`, row);
  return map;
}

function seriesValue(id) {
  const series = (state.schema?.series || []).find((item) => item.id === id);
  if (!series) return null;
  return valueIndex().get(`${series.block}:${series.key}`) || null;
}

const SENSOR_SERIES = new Set(["dachs_austritt", "dachs_eintritt", "vorlauf", "ruecklauf", "kuehlwasser", "regler", "abgas_motor", "kapsel", "abgas_hka"]);
const INVALID_SENSOR_VALUES = new Set([-1, 0, 90, 127, 255]);

function isInvalidSensor(seriesId, row) {
  if (!row) return false;
  const value = numeric(row.value);
  if (value === null) return true;
  if (SENSOR_SERIES.has(seriesId) && INVALID_SENSOR_VALUES.has(value)) return true;
  if (seriesId === "drehzahl" && (value < 0 || value > 3000)) return true;
  if (seriesId === "wirkleistung" && (value < -6 || value > 6)) return true;
  if (seriesId === "abgas_motor" && (value < 0 || value > 600)) return true;
  if (seriesId === "abgas_hka" && (value < 0 || value > 200)) return true;
  return false;
}

function rowText(seriesId) {
  const series = (state.schema?.series || []).find((item) => item.id === seriesId);
  const row = seriesValue(seriesId);
  if (!row || isInvalidSensor(seriesId, row)) return "—";
  return `${row.value ?? "—"}${row.unit || series?.unit ? ` ${row.unit || series.unit}` : ""}`;
}

function setText(elementId, seriesId) {
  const element = $(elementId);
  if (element) element.textContent = rowText(seriesId);
}

function phaseText(prefix, unit, digits = null) {
  const ids = [`${prefix}_l1`, `${prefix}_l2`, `${prefix}_l3`];
  const values = ids.map((id) => seriesValue(id)).map((row) => {
    if (!row) return "—";
    const value = numeric(row.value);
    return value === null ? String(row.value ?? "—") : (digits === null ? String(row.value) : value.toFixed(digits));
  });
  return `${values.join(" / ")} ${unit}`;
}

function setElectricalText(prefix, unit, elementIds, digits = null) {
  const value = phaseText(prefix, unit, digits);
  elementIds.forEach((id) => { if ($(id)) $(id).textContent = value; });
}

function renderOverview() {
  const overviewIds = ["kuehlwasser", "dachs_eintritt", "abgas_motor", "abgas_hka", "kapsel", "regler", "betriebsstunden_gesamt", "betriebsstunden", "starts", "arbeit_elektr", "arbeit_therm_hka", "arbeit_therm_kon", "servicecode"];
  const cards = (state.schema?.series || []).filter((item) => overviewIds.includes(item.id)).filter((item) => !isInvalidSensor(item.id, seriesValue(item.id)));
  $("overviewCards").innerHTML = cards.map((series) => {
    const row = seriesValue(series.id);
    return `<article class="metric-card"><div class="metric-label">${escapeHtml(series.title)}</div><div class="metric-value">${formatValue(row?.value, row?.unit || series.unit)}</div><div class="metric-extra">Block ${series.block} · ${row ? escapeHtml(row.recorded_at) : "wartet auf Messung"}</div></article>`;
  }).join("");
  const motor = ["motorstatus", "drehzahl", "wirkleistung", "betriebsstunden", "kuehlwasser", "regler"].map((id) => seriesValue(id)).filter((row, index) => row && !isInvalidSensor(["motorstatus", "drehzahl", "wirkleistung", "betriebsstunden", "kuehlwasser", "regler"][index], row));
  $("motorStateCards").innerHTML = motor.map((row) => `<div class="detail-item"><div class="detail-label">${escapeHtml(row.label)}</div><div class="detail-value">${formatValue(row.value, row.unit)}</div></div>`).join("") || `<p class="muted">Noch keine Motordaten.</p>`;
  const system = ["servicecode", "warncode", "anzahl_warnungen", "anzahl_stoerungen"].map((id) => seriesValue(id)).filter(Boolean);
  $("systemStateCards").innerHTML = system.map((row) => `<div class="detail-item"><div class="detail-label">${escapeHtml(row.label)}</div><div class="detail-value">${formatValue(row.value, row.unit)}</div></div>`).join("") || `<p class="muted">Noch keine Statusdaten.</p>`;
  const ids = { "value-vorlauf":"kuehlwasser", "value-ruecklauf":"dachs_eintritt", "value-kuehlwasser":"kuehlwasser", "value-tech-kuehlwasser":"kuehlwasser", "value-abgas-motor":"abgas_motor", "value-tech-abgas-motor":"abgas_motor", "value-abgas-hka":"abgas_hka", "value-kapsel":"kapsel", "value-tech-kapsel":"kapsel", "value-drehzahl":"drehzahl", "value-wirkleistung":"wirkleistung", "value-betriebsstunden":"betriebsstunden", "value-motorstatus":"motorstatus" };
  Object.entries(ids).forEach(([elementId, seriesId]) => setText(elementId, seriesId));
  ["compact-value-kuehlwasser", "compact-value-kuehlwasser-box"].forEach((id) => setText(id, "kuehlwasser"));
  ["compact-value-dachs-eintritt", "tech-value-dachs-eintritt"].forEach((id) => setText(id, "dachs_eintritt"));
  ["compact-value-abgas-motor", "compact-value-abgas-hka", "tech-value-abgas-hka"].forEach((id, index) => setText(id, index === 0 ? "abgas_motor" : "abgas_hka"));
  ["compact-value-kapsel"].forEach((id) => setText(id, "kapsel"));
  ["compact-value-regler", "compact-value-regler-side", "tech-value-regler"].forEach((id) => setText(id, "regler"));
  ["compact-value-drehzahl"].forEach((id) => setText(id, "drehzahl"));
  ["compact-value-wirkleistung"].forEach((id) => setText(id, "wirkleistung"));
  ["compact-value-betriebsstunden"].forEach((id) => setText(id, "betriebsstunden"));
  ["compact-value-betriebsstunden-gesamt", "tech-value-betriebsstunden-gesamt"].forEach((id) => setText(id, "betriebsstunden_gesamt"));
  ["compact-value-starts", "tech-value-starts"].forEach((id) => setText(id, "starts"));
  ["compact-value-servicecode", "tech-value-servicecode"].forEach((id) => setText(id, "servicecode"));
  ["tech-value-voltage"].forEach((id) => { if ($(id)) $(id).textContent = phaseText("spannung", "V", 1); });
  ["tech-value-current"].forEach((id) => { if ($(id)) $(id).textContent = phaseText("strom", "A", 1); });
  ["tech-value-impedance"].forEach((id) => { if ($(id)) $(id).textContent = phaseText("impedanz", "Ohm", 2); });
  ["tech-value-frequency"].forEach((id) => { if ($(id)) $(id).textContent = rowText("frequenz"); });
  setElectricalText("spannung", "V", ["compact-value-voltage"], 1);
  setElectricalText("strom", "A", ["compact-value-current"], 1);
  setElectricalText("impedanz", "Ohm", ["compact-value-impedance"], 2);
  setText("compact-value-frequency", "frequenz");
  renderOverviewPower();
  renderMaintenanceStatus(state.live?.maintenance || {});
}

function renderOverviewPower() {
  const actual = seriesValue("wirkleistung");
  const target = seriesValue("wirkleistung_soll");
  const admin = state.user?.role === "admin";
  $("overviewPowerActual").textContent = actual ? `${actual.value ?? "—"} ${actual.unit || "kW"}` : "—";
  $("overviewPowerTargetRead").textContent = target ? `${target.value ?? "—"} ${target.unit || "kW"}` : "—";
  $("overviewPowerTargetForm").hidden = !admin;

  const input = $("overviewPowerTargetInput");
  const liveValue = target?.value;
  if (admin && liveValue !== null && liveValue !== undefined && liveValue !== "") {
    input.dataset.current = String(liveValue);
    if (document.activeElement !== input && input.dataset.userEdited !== "1") input.value = String(liveValue);
  }
  renderOverviewPowerWriteMode();
}

function renderOverviewPowerWriteMode() {
  const mode = $("overviewPowerMode");
  const note = $("overviewPowerTargetNote");
  const button = $("overviewPowerTargetApply");
  if (!mode || !note || !button) return;
  if (state.user?.role !== "admin") {
    mode.textContent = "Gast · nur Anzeige";
    mode.className = "status-pill neutral";
    note.textContent = "Gastzugang: Sollwert ist nicht editierbar.";
    return;
  }
  const authLevel = Number($("authLevel")?.value ?? -1);
  mode.textContent = `Admin · LIVE · Auth ${authLevel}`;
  mode.className = "status-pill warn";
  button.textContent = "Sollwert schreiben";
  note.textContent = "Immer schreibbereit: Read → Validate → Auth/PW4 → Write → Readback.";
}

function updateOverviewPowerTarget(field) {
  if (!field) return;
  state.live ||= { monitor: {}, values: [] };
  state.live.values = (state.live.values || []).filter((row) => !(row.block === 50 && row.key === "Hka_Ew.usSollGenerator"));
  state.live.values.push({
    block: 50,
    key: field.key,
    label: field.label,
    raw: field.raw,
    value: field.value,
    unit: field.unit || "kW",
    recorded_at: new Date().toISOString(),
  });
}

async function applyOverviewPowerTarget(event) {
  event.preventDefault();
  if (state.user?.role !== "admin") return;
  const input = $("overviewPowerTargetInput");
  const value = input.value.trim().replace(",", ".");
  if (!value || !Number.isFinite(Number(value))) return toast("Bitte einen gültigen Sollwert in kW eingeben.");
  const button = $("overviewPowerTargetApply");
  button.disabled = true;
  try {
    const result = await api("/api/overview/power-target", { method: "POST", body: JSON.stringify({
      value,
      auth_level: Number($("authLevel").value || -1),
      pass4: $("pass4").value,
    }) });
    if (result.written) {
      const block = await api("/api/block/50");
      updateOverviewPowerTarget((block.fields || []).find((field) => field.key === "Hka_Ew.usSollGenerator"));
      input.dataset.userEdited = "";
      renderOverviewPower();
      toast("Wirkleistung Soll geschrieben und per Readback bestätigt.");
    } else toast("Sollwert wurde nicht geschrieben.");
    await refreshAudit();
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

function updateConnection() {
  const monitor = state.live?.monitor || {};
  const pill = $("connectionSummary");
  if (monitor.serial_enabled === false) { pill.textContent = "Web-Polling pausiert"; pill.className = "status-pill neutral"; return; }
  if (!monitor.last_cycle) { pill.textContent = "Warte auf serielle Messung"; pill.className = "status-pill neutral"; return; }
  if (monitor.last_error && !monitor.ok_blocks) { pill.textContent = "Seriell gestört"; pill.className = "status-pill error"; return; }
  pill.textContent = `Seriell OK · ${monitor.ok_blocks}/${monitor.polled_blocks || (state.schema?.fast_monitor_blocks || []).length} Blöcke`;
  pill.className = `status-pill ${monitor.last_error ? "warn" : "ok"}`;
  $("lastUpdate").textContent = `letzter Zyklus ${new Date(monitor.last_cycle).toLocaleTimeString("de-DE")}`;
}

async function refreshLive() {
  try {
    state.live = await api("/api/live");
    renderOverview();
    updateConnection();
    refreshMonitorStatus();
  } catch (_) { /* api handles session loss */ }
}

function refreshMonitorStatus() {
  const monitor = state.live?.monitor || {};
  const status = $("monitorStatus");
  const serialEnabled = monitor.serial_enabled !== false;
  status.textContent = !serialEnabled ? "Web-Serialzugriff pausiert" : (monitor.enabled ? (monitor.last_error ? `Aktiv · ${monitor.last_error}` : "Aktiv") : "Pausiert");
  status.className = `status-pill ${!serialEnabled ? "neutral" : (monitor.enabled ? (monitor.last_error ? "warn" : "ok") : "neutral")}`;
  $("monitorToggle").textContent = monitor.enabled ? "Überwachung stoppen" : "Überwachung starten";
  $("monitorToggle").disabled = !serialEnabled;
  $("serialToggle").textContent = serialEnabled ? "Web-Serialzugriff pausieren" : "Web-Serialzugriff aktivieren";
  $("serialToggle").className = serialEnabled ? "danger" : "primary";
  $("monitorStats").innerHTML = [
    ["Web-Serialzugriff", serialEnabled ? "Aktiv" : "Pausiert"],
    ["Poll-Intervall", `${monitor.interval_seconds ?? "—"} s`],
    ["Block 26", `alle ${monitor.slow_interval_seconds ?? 10} s`],
    ["Zyklen", monitor.cycles ?? 0],
    ["Gute Blöcke", monitor.ok_blocks ?? 0],
    ["Fehlerblöcke", monitor.failed_blocks ?? 0],
  ].map(([label, value]) => `<article class="metric-card"><div class="metric-label">${label}</div><div class="metric-value">${escapeHtml(value)}</div></article>`).join("");
}

function showView(viewId) {
  state.selectedView = viewId;
  document.querySelectorAll(".app-view").forEach((view) => { view.hidden = view.id !== viewId; });
  document.querySelectorAll(".tab-button").forEach((button) => button.classList.toggle("active", button.dataset.view === viewId));
  if (viewId === "monitorView") refreshCharts();
  if (viewId === "auditView") refreshAudit();
  if (viewId === "maintenanceView") refreshMaintenance(true);
}

function maintenanceNumber(value, suffix = "") {
  const number = numeric(value);
  if (number === null) return "—";
  return `${new Intl.NumberFormat("de-DE", { maximumFractionDigits: 1 }).format(number)}${suffix}`;
}

function renderMaintenanceStatus(status = {}) {
  const level = ["green", "yellow", "red"].includes(status.level) ? status.level : "unknown";
  [$("maintenanceDashboard"), $("maintenanceStatusPanel")].forEach((element) => {
    if (element) element.className = element.className.replace(/maintenance-(green|yellow|red|unknown)/g, "").trim() + ` maintenance-${level}`;
  });
  $("maintenanceDashboardTitle").textContent = status.title || "Wartungsstatus noch nicht gelesen";
  $("maintenanceStatusTitle").textContent = status.title || "Wartungsstatus noch nicht gelesen";
  const details = `Noch ${maintenanceNumber(status.remaining_hours, " Bh")} · ${maintenanceNumber(status.remaining_days, " Tage")} · Intervall ${maintenanceNumber(status.interval_hours, " Bh")}`;
  $("maintenanceDashboardDetails").textContent = details;
  $("maintenanceStatusMetrics").innerHTML = [
    ["Betriebsstunden bis Wartung", maintenanceNumber(status.remaining_hours, " Bh")],
    ["Tage bis Wartung", maintenanceNumber(status.remaining_days, " Tage")],
    ["Wartungsintervall", maintenanceNumber(status.interval_hours, " Bh")],
    ["Ausgeführte Wartungen", maintenanceNumber(status.maintenance_count)],
    ["Letzte Wartung", status.last_maintenance || "—"],
    ["Reglerflag", status.due ? "Wartung steht an" : (status.confirmed ? "Bestätigung gesetzt" : "Keine Anforderung")],
  ].map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
}

function renderMaintenanceReports() {
  const reports = state.maintenance.reports || [];
  $("maintenanceReportRows").innerHTML = reports.map((item) => {
    const summary = item.summary || {};
    const snapshot = item.snapshot || {};
    const captured = (snapshot.captured_blocks || []).length;
    const attempted = (snapshot.attempted_blocks || []).length;
    const snapshotText = attempted ? `<small>Snapshot ${captured}/${attempted} Blöcke</small>` : "";
    const status = item.status !== "completed" ? "Entwurf" : item.completion_mode === "demo" ? "Demo abgeschlossen" : "Abgeschlossen";
    return `<tr><td><button class="history-ring" type="button" data-maintenance-report="${item.id}">#${item.id}</button></td><td>${escapeHtml(new Date(item.created_at).toLocaleString("de-DE"))}${snapshotText}</td><td><span class="status-pill ${item.status === "completed" ? "ok" : "warn"}">${status}</span></td><td>${escapeHtml(item.technician || "—")}</td><td>${formatValue(summary.operating_hours, "Bh")}</td><td class="report-links"><a href="/api/maintenance/reports/${item.id}/export/html">HTML</a><a href="/api/maintenance/reports/${item.id}/export/pdf">PDF</a><a href="/api/maintenance/reports/${item.id}/export/json">JSON</a></td></tr>`;
  }).join("") || `<tr><td colspan="6" class="muted">Noch keine Berichte.</td></tr>`;
}

async function refreshMaintenance(reloadCurrent = false) {
  try {
    const data = await api("/api/maintenance/reports");
    state.maintenance.reports = data.items || [];
    renderMaintenanceReports();
    renderMaintenanceStatus(data.status || state.live?.maintenance || {});
    if (reloadCurrent) {
      const reportId = state.maintenance.current?.id || state.maintenance.reports.find((item) => item.status === "draft")?.id;
      if (reportId) await loadMaintenanceReport(reportId);
    }
  } catch (error) { toast(error.message); }
}

async function createMaintenanceReport() {
  const existingDraft = (state.maintenance.reports || []).find((item) => item.status === "draft");
  if (existingDraft && !window.confirm(`Es gibt bereits den offenen Wartungsentwurf #${existingDraft.id}. Trotzdem einen neuen Anlagen-Snapshot beginnen?`)) return;
  clearTimeout(state.maintenance.autosaveTimer);
  state.maintenance.autosaveTimer = null;
  const button = $("maintenanceCreate");
  button.disabled = true;
  button.textContent = "Lese gesamten Anlagenzustand …";
  try {
    const item = await api("/api/maintenance/reports", { method: "POST", body: "{}" });
    state.maintenance.current = item;
    renderMaintenanceEditor();
    await refreshMaintenance(false);
    const snapshot = item.snapshot || {};
    toast(`Anlagenzustand schreibfrei gelesen: ${(snapshot.captured_blocks || []).length}/${(snapshot.attempted_blocks || []).length} Blöcke lokal archiviert.`);
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = "Wartung starten & Anlage einlesen"; }
}

async function loadMaintenanceReport(reportId) {
  try {
    clearTimeout(state.maintenance.autosaveTimer);
    state.maintenance.autosaveTimer = null;
    state.maintenance.current = await api(`/api/maintenance/reports/${reportId}`);
    renderMaintenanceEditor();
  } catch (error) { toast(error.message); }
}

function renderMaintenanceEditor() {
  const item = state.maintenance.current;
  if (!item) { $("maintenanceEditor").hidden = true; return; }
  $("maintenanceEditor").hidden = false;
  $("maintenanceReportNumber").textContent = `#${item.id}`;
  $("maintenanceReportTitle").textContent = item.status !== "completed" ? "Wartungsentwurf bearbeiten" : item.completion_mode === "demo" ? "Abgeschlossener Demo-Wartungsbericht" : "Abgeschlossener Wartungsbericht";
  const snapshot = item.snapshot || {};
  const snapshotText = (snapshot.attempted_blocks || []).length ? ` · Snapshot ${(snapshot.captured_blocks || []).length}/${snapshot.attempted_blocks.length} Blöcke` : "";
  $("maintenanceReportMeta").textContent = `Anlagenstand ${new Date(item.created_at).toLocaleString("de-DE")} · Seriennummer ${item.summary?.serial_number || "—"} · ${item.summary?.operating_hours || "—"} Bh${snapshotText}`;
  const comparison = item.comparison;
  $("maintenanceComparison").innerHTML = comparison ? `<div class="section-head"><div><p class="eyebrow">VERGLEICH MIT BERICHT #${comparison.report_id}</p><h4>Zählerentwicklung seit ${escapeHtml(new Date(comparison.created_at).toLocaleString("de-DE"))}</h4></div></div><div class="maintenance-comparison-grid">${(comparison.rows || []).map((row) => `<div><span>${escapeHtml(row.label)}</span><strong>${row.delta === null || row.delta === undefined ? "—" : `${numeric(row.delta) >= 0 ? "+" : ""}${escapeHtml(row.delta)}`}</strong><small>${escapeHtml(row.previous ?? "—")} → ${escapeHtml(row.current ?? "—")}</small></div>`).join("")}</div>` : `<p class="source-note">Dies ist der erste archivierte Bericht; ein Zählervergleich erscheint ab dem nächsten Bericht.</p>`;
  ["Html", "Pdf", "Json"].forEach((name) => { $(`maintenanceExport${name}`).href = `/api/maintenance/reports/${item.id}/export/${name.toLowerCase()}`; });
  const protocol = item.protocol || {};
  $("maintenanceFuelType").value = protocol.fuel_type || "gas";
  $("maintenanceTechnician").value = protocol.technician || "";
  $("maintenanceNotes").value = protocol.notes || "";
  const statuses = item.checklist_status || [];
  $("maintenanceChecklist").innerHTML = (item.checklist_definition || []).map((definition, index) => {
    const allowed = new Set(definition.allowed_status || statuses.map((status) => status.value));
    const options = statuses.filter((status) => allowed.has(status.value));
    return `<label><span><b>${String(index + 1).padStart(2, "0")}</b>${escapeHtml(definition.label)}</span><select data-checklist-id="${escapeHtml(definition.id)}"><option value="">Bitte bewerten …</option>${options.map((status) => `<option value="${escapeHtml(status.value)}" ${protocol.checklist?.[definition.id] === status.value ? "selected" : ""}>${escapeHtml(status.label)}</option>`).join("")}</select></label>`;
  }).join("");
  $("maintenanceMeasurements").innerHTML = (item.measurement_definition || []).map((definition) => `<label><span>${escapeHtml(definition.label)}</span><span class="maintenance-measurement-input"><input inputmode="decimal" data-measurement-key="${escapeHtml(definition.key)}" value="${escapeHtml(protocol.measurements?.[definition.key] ?? "")}"><em>${escapeHtml(definition.unit)}</em></span></label>`).join("");
  const supplementalStatuses = item.supplemental_status || [];
  $("maintenanceSupplemental").innerHTML = (item.supplemental_definition || []).map((definition, index) => `<label><span><b>Z${index + 1}</b>${escapeHtml(definition.label)}</span><select data-supplemental-id="${escapeHtml(definition.id)}"><option value="">Noch offen …</option>${supplementalStatuses.map((status) => `<option value="${escapeHtml(status.value)}" ${protocol.supplemental?.[definition.id] === status.value ? "selected" : ""}>${escapeHtml(status.label)}</option>`).join("")}</select></label>`).join("");
  const editable = state.user?.role === "admin" && item.status === "draft";
  $("maintenanceForm").querySelectorAll("input,select,textarea,button").forEach((element) => { element.disabled = !editable; });
  $("maintenanceCompletion").hidden = !editable;
  const liveCompletion = Boolean(item.maintenance_live_writes_enabled);
  $("maintenanceCompletion").classList.toggle("live", liveCompletion);
  $("maintenanceCompletionEyebrow").textContent = liveCompletion ? "REGLERABSCHLUSS" : "DEMOMODUS · HARDWARE GESPERRT";
  $("maintenanceCompletionTitle").textContent = liveCompletion ? "Wartung am MSR2 bestätigen" : "Testlauf lokal abschließen";
  $("maintenanceCompletionDescription").textContent = liveCompletion ? "Schreibt Block 100 und setzt anschließend das Bestätigungsbit in Block 104. Beide Schritte benötigen ACK und Readback." : "Validiert und archiviert das Protokoll ausschließlich auf dem Pi. Block 100, Block 104 und das Bestätigungsbit bleiben unverändert.";
  $("maintenanceAuthLevelField").hidden = !liveCompletion;
  $("maintenancePass4Field").hidden = !liveCompletion;
  $("maintenanceConfirmation").placeholder = item.confirmation_text || (liveCompletion ? "WARTUNG ABSCHLIESSEN" : "DEMO ABSCHLIESSEN");
  $("maintenanceComplete").textContent = liveCompletion ? "Wartung endgültig abschließen" : "Demolauf abschließen";
  $("maintenanceComplete").classList.toggle("danger", liveCompletion);
  $("maintenanceComplete").classList.toggle("primary", !liveCompletion);
  $("maintenanceSaveHint").textContent = item.status !== "completed" ? "Änderungen werden lokal auf dem Pi gespeichert." : item.completion_mode === "demo" ? `Demolauf lokal abgeschlossen am ${new Date(item.completed_at).toLocaleString("de-DE")} · MSR2 unverändert` : `Abgeschlossen am ${new Date(item.completed_at).toLocaleString("de-DE")}`;
}

function maintenanceProtocolFromForm() {
  const checklist = {};
  $("maintenanceChecklist").querySelectorAll("select[data-checklist-id]").forEach((select) => { if (select.value) checklist[select.dataset.checklistId] = select.value; });
  const measurements = {};
  $("maintenanceMeasurements").querySelectorAll("input[data-measurement-key]").forEach((input) => { measurements[input.dataset.measurementKey] = input.value.trim(); });
  const supplemental = {};
  $("maintenanceSupplemental").querySelectorAll("select[data-supplemental-id]").forEach((select) => { if (select.value) supplemental[select.dataset.supplementalId] = select.value; });
  return {
    fuel_type: $("maintenanceFuelType").value,
    technician: $("maintenanceTechnician").value.trim(),
    notes: $("maintenanceNotes").value.trim(),
    checklist,
    supplemental,
    measurements,
  };
}

async function saveMaintenanceDraft(event = null, silent = false, rerender = true) {
  event?.preventDefault();
  clearTimeout(state.maintenance.autosaveTimer);
  state.maintenance.autosaveTimer = null;
  const item = state.maintenance.current;
  if (!item || item.status !== "draft" || state.user?.role !== "admin") return;
  try {
    state.maintenance.current = await api(`/api/maintenance/reports/${item.id}`, { method: "POST", body: JSON.stringify({ protocol: maintenanceProtocolFromForm() }) });
    if (rerender) renderMaintenanceEditor();
    await refreshMaintenance(false);
    if (silent && !rerender) $("maintenanceSaveHint").textContent = `Lokal gespeichert · ${new Date().toLocaleTimeString("de-DE")}`;
    if (!silent) toast("Wartungsentwurf lokal gespeichert.");
  } catch (error) { toast(error.message); }
}

function scheduleMaintenanceAutosave() {
  const item = state.maintenance.current;
  if (!item || item.status !== "draft" || state.user?.role !== "admin") return;
  clearTimeout(state.maintenance.autosaveTimer);
  $("maintenanceSaveHint").textContent = "Speichere lokal …";
  state.maintenance.autosaveTimer = setTimeout(() => {
    state.maintenance.autosaveTimer = null;
    saveMaintenanceDraft(null, true, false);
  }, 500);
}

async function completeMaintenance() {
  const item = state.maintenance.current;
  if (!item || item.status !== "draft" || state.user?.role !== "admin") return;
  const liveCompletion = Boolean(item.maintenance_live_writes_enabled);
  const question = liveCompletion ? "Wartung jetzt wirklich am MSR2 abschließen? Block 100 und das Bestätigungsbit in Block 104 werden geschrieben." : "Demolauf jetzt lokal abschließen? Der MSR2-Regler bleibt vollständig unverändert.";
  if (!window.confirm(question)) return;
  clearTimeout(state.maintenance.autosaveTimer);
  state.maintenance.autosaveTimer = null;
  const button = $("maintenanceComplete");
  button.disabled = true;
  try {
    state.maintenance.current = await api(`/api/maintenance/reports/${item.id}/complete`, { method: "POST", body: JSON.stringify({
      protocol: maintenanceProtocolFromForm(),
      auth_level: Number($("maintenanceAuthLevel").value || -1),
      pass4: $("maintenancePass4").value,
      confirmation: $("maintenanceConfirmation").value,
    }) });
    renderMaintenanceEditor();
    await refreshMaintenance(false);
    await refreshAudit();
    toast(liveCompletion ? "Wartung geschrieben, bestätigt und per Readback geprüft." : "Demolauf lokal abgeschlossen. Es wurden keine Reglerdaten geschrieben.");
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
}

function setSchematicMode(mode) {
  const stage = $("schematicStage");
  if (!stage) return;
  stage.classList.toggle("detail-mode", mode === "detail");
  document.querySelectorAll("[data-schematic-mode]").forEach((button) => button.classList.toggle("active", button.dataset.schematicMode === mode));
}

function renderRegisterTabs() {
  const regulatorTabs = (state.schema?.blocks || []).map((block) => {
    const visibleFields = (block.fields || []).filter((field) => state.showReserved || !field.reserved).length;
    const count = block.special === "message-history" ? "10 Meldungen"
      : block.special === "oil-refill-history" ? "3 Einträge"
      : block.special === "run-history" ? "gemeinsames Diagramm"
      : block.special === "service-history" ? "13 Servicecodes + 5 Warnungen"
      : block.special === "motor-snapshot" ? "Motor-Messwertspeicher"
      : block.special === "mc-status" ? "MC1/MC2-Diagnose"
      : `${visibleFields} Felder`;
    return `<button role="tab" data-cpu="0" data-block="${block.block}" class="${state.selectedCpu === 0 && block.block === state.selectedBlock ? "active" : ""}">${block.block} · ${escapeHtml(block.name)} <small>(${count})</small></button>`;
  }).join("");
  const networkTabs = (state.schema?.network_protection || []).map((target) => (
    `<button role="tab" data-cpu="${target.cpu}" data-block="${target.block}" class="critical-tab ${state.selectedCpu === target.cpu && state.selectedBlock === target.block ? "active" : ""}">CPU ${target.cpu} · Netzschutz <small>(${(target.fields || []).length} Felder)</small></button>`
  )).join("");
  $("settingsTabs").innerHTML = regulatorTabs + networkTabs;
}

async function loadBlock(block, cpu = 0) {
  state.selectedBlock = block;
  state.selectedCpu = Number(cpu);
  const targetText = state.selectedCpu ? `CPU ${state.selectedCpu}, Block ${block}` : `Block ${block}`;
  $("blockReadStatus").textContent = `Lese ${targetText} …`;
  $("settingsTabs").querySelectorAll("button").forEach((button) => button.classList.toggle(
    "active",
    Number(button.dataset.cpu || 0) === state.selectedCpu && Number(button.dataset.block) === block,
  ));
  try {
    state.block = await api(state.selectedCpu ? `/api/network-protection/${state.selectedCpu}` : `/api/block/${block}`);
    $("selectedBlockEyebrow").textContent = state.selectedCpu ? `CPU ${state.selectedCpu} · BLOCK ${block} · NETZSCHUTZ` : `BLOCK ${block}`;
    $("selectedBlockTitle").textContent = state.block.name;
    $("blockReadStatus").textContent = state.block.ok ? `OK · ${state.block.rtt_ms} ms` : `Fehler: ${state.block.status}`;
    $("saveBlockButton").hidden = state.user?.role !== "admin";
    document.querySelector(".settings-panel")?.classList.toggle("critical-settings", Boolean(state.block.critical));
    renderFields();
  } catch (error) { $("blockReadStatus").textContent = error.message; }
}

function renderMessageHistory() {
  const history = state.block?.history;
  const entries = history?.entries || [];
  const current = history?.current_ring;
  const currentText = current === null || current === undefined ? "unbekannt" : `Ring ${current}`;
  const rows = entries.map((entry) => {
    const rawText = entry.has_event
      ? `Wert ${entry.raw_value}${entry.raw_value_label ? ` · ${entry.raw_value_label}` : ""}`
      : "leer";
    const typeText = `${entry.type} · ${entry.type_label}`;
    const moduleText = `${entry.module} · ${entry.module_label}`;
    return `<tr class="${entry.active ? "message-history-active" : ""}">
      <td class="history-ring">${entry.active ? "● " : ""}${String(entry.index).padStart(2, "0")}</td>
      <td>${escapeHtml(entry.timestamp_text || "—")}</td>
      <td><strong>${escapeHtml(entry.message)}</strong><small>${escapeHtml(rawText)}</small></td>
      <td>${entry.message_id === null || entry.message_id === undefined ? "—" : escapeHtml(entry.message_id)}</td>
      <td>${escapeHtml(typeText)}</td>
      <td>${escapeHtml(moduleText)}</td>
    </tr>`;
  }).join("");
  const admin = state.user?.role === "admin";
  const rawFields = (state.block?.fields || []).filter((field) => state.showReserved || !field.reserved);
  const rawEditor = rawFields.length ? `<details class="message-history-raw"><summary>Rohfelder bearbeiten${admin ? " (Admin)" : " (nur lesen)"}</summary><p class="muted">Die Tabelle oben bleibt verständlich. Hier können die physikalischen Werte für Meldungswert, gepackten Typ/Modul und Zeitstempel wie bei den übrigen Feldern vorbereitet werden.</p><div class="register-grid">${rawFields.map((field) => `<div class="register-field ${admin ? "" : "readonly"}"><div class="register-field-head"><label for="field-${encodeURIComponent(field.key)}">${escapeHtml(field.label || field.key)}</label><small>${escapeHtml(field.type || "")} · ${field.size ?? "?"} B</small></div><input id="field-${encodeURIComponent(field.key)}" data-key="${escapeHtml(field.key)}" data-baseline="${escapeHtml(field.raw ?? "")}" value="${escapeHtml(field.raw ?? "")}" ${admin ? "" : "disabled"}><div class="field-meta">${escapeHtml(field.key)} · Offset ${field.offset ?? "?"}</div></div>`).join("")}</div></details>` : "";
  $("settingsFields").innerHTML = `<div class="message-history-card">
    <div class="message-history-summary"><span class="status-pill ok">Aktueller Ring: ${escapeHtml(currentText)}</span><span class="muted">10 Einträge · Anzeige und Rohwerte</span></div>
    <div class="table-wrap"><table class="data-table message-history-table"><thead><tr><th>Ring</th><th>Zeitstempel</th><th>Meldung</th><th>Meldungs-ID</th><th>Typ</th><th>Modul</th></tr></thead><tbody>${rows || `<tr><td colspan="6" class="muted">Keine Meldungseinträge vorhanden.</td></tr>`}</tbody></table></div>
    ${rawEditor}
  </div>`;
}

function renderRawFieldsDetails(note="Die verständliche Auswertung bleibt oben; die physikalischen Felder stehen hier für Diagnose und kontrollierte Änderungen bereit.") {
  const admin = state.user?.role === "admin";
  const fields = (state.block?.fields || []).filter((field) => state.showReserved || !field.reserved);
  if (!fields.length) return "";
  return `<details class="message-history-raw"><summary>Rohfelder bearbeiten${admin ? " (Admin)" : " (nur lesen)"}</summary><p class="muted">${escapeHtml(note)}</p><div class="register-grid">${fields.map((field) => {
    const editValue = field.edit_value ?? field.value ?? field.raw ?? "";
    return `<div class="register-field ${admin ? "" : "readonly"}"><div class="register-field-head"><label for="field-${encodeURIComponent(field.key)}">${escapeHtml(field.label || field.key)}</label><small>${escapeHtml(field.type || "")} · ${field.size ?? "?"} B</small></div><input id="field-${encodeURIComponent(field.key)}" data-key="${escapeHtml(field.key)}" data-baseline="${escapeHtml(editValue)}" value="${escapeHtml(editValue)}" ${admin ? "" : "disabled"}><div class="field-meta">${escapeHtml(field.key)} · Offset ${field.offset ?? "?"} · ${escapeHtml(field.unit || "")}</div></div>`;
  }).join("")}</div></details>`;
}

function renderOilRefillHistory() {
  const history = state.block?.oil_refill_history;
  const entries = history?.entries || [];
  const rows = entries.map((entry) => `<tr class="${entry.has_event ? "" : "history-empty"}">
    <td>${entry.index}</td>
    <td>${escapeHtml(entry.timestamp_text || "—")}</td>
    <td>${entry.has_event ? `${escapeHtml(entry.operating_hours)} h` : "—"}</td>
    <td>${entry.has_event ? escapeHtml(entry.amount) : "—"}</td>
  </tr>`).join("");
  $("settingsFields").innerHTML = `<div class="message-history-card">
    <div class="message-history-summary"><span class="status-pill ok">Nachfüllzähler: ${escapeHtml(history?.counter ?? "—")}</span><span class="muted">3 Einträge · je Zeitstempel, Betriebsstunden und Menge</span></div>
    <div class="table-wrap"><table class="data-table"><thead><tr><th>Eintrag</th><th>Zeitstempel</th><th>Betriebsstunden beim Nachfüllen</th><th>Nachgefüllte Menge (Rohwert)</th></tr></thead><tbody>${rows || `<tr><td colspan="4" class="muted">Keine Einträge vorhanden.</td></tr>`}</tbody></table></div>
    <p class="source-note">Für die Menge ist noch keine belastbare Einheit oder Skalierung bestätigt. Deshalb wird sie bewusst als Rohwert angezeigt.</p>
    ${renderRawFieldsDetails("Block 102 besteht aus drei ineinander verschachtelten 10-Byte-Datensätzen. Änderungen werden wie alle Felder erst im Dry-Run vorbereitet und nur bei aktivierter Schreibfreigabe übertragen.")}
  </div>`;
}

function renderMcStatus() {
  const status = state.block?.mc_status;
  const controllers = status?.controllers || [];
  const summaries = controllers.map((controller) => `<article class="mc-controller-card"><h4>${escapeHtml(controller.name)}</h4><dl><div><dt>Fehlergrund</dt><dd>${controller.error_reason}</dd></div><div><dt>Fehlercode</dt><dd>${controller.error_code}</dd></div><div><dt>Schutzart</dt><dd>${controller.protection_type}</dd></div></dl><small>Flags: ${escapeHtml(controller.flags_hex)}</small></article>`).join("");
  const flags = (status?.flags || []).filter((entry) => state.showReserved || !entry.reserved);
  const flagRows = flags.map((entry) => `<tr><td>${entry.bit}${entry.width > 1 ? `–${entry.bit + entry.width - 1}` : ""}</td><td>${escapeHtml(entry.label)}</td>${[entry.mc1, entry.mc2].map((value) => `<td><span class="mc-state ${escapeHtml(value.state)}">${escapeHtml(value.text)}</span><small>${value.value}</small></td>`).join("")}</tr>`).join("");
  const actors = (controller) => (controller?.actors || []).map((actor) => `<span class="actor-chip ${actor.active ? "active" : ""}">${escapeHtml(actor.label)}: ${actor.active ? "EIN" : "AUS"}</span>`).join("");
  $("settingsFields").innerHTML = `<div class="message-history-card mc-status-card">
    <div class="mc-summary-grid">${summaries}</div>
    <p class="source-note">Die 64 Statusbits werden einzeln dargestellt. Für die separaten Rohbytes „Fehlergrund“ und „Fehlercode“ ist noch keine belastbare Code-Tabelle dokumentiert; unbekannte Bedeutungen werden nicht geraten.</p>
    <div class="mc-io-grid"><section><h4>MC1-Ausgänge</h4><div class="actor-list">${actors(controllers[0])}</div></section><section><h4>MC2-Ausgänge</h4><div class="actor-list">${actors(controllers[1])}</div></section><section><h4>Dachs-Zustand</h4><div class="actor-list"><span class="actor-chip ${status?.state?.oil_pressure ? "active" : ""}">Öldruck: ${status?.state?.oil_pressure ? "EIN" : "AUS"}</span><span class="actor-chip ${status?.state?.liquid_switch ? "active" : ""}">Flüssigkeitsschalter: ${status?.state?.liquid_switch ? "EIN" : "AUS"}</span></div></section></div>
    <details class="mc-flags" open><summary>Aufschlüsselung aller MC1-/MC2-Statusfelder</summary><div class="table-wrap"><table class="data-table mc-table"><thead><tr><th>Bit</th><th>Prüfung / Rückmeldung</th><th>MC1</th><th>MC2</th></tr></thead><tbody>${flagRows}</tbody></table></div></details>
    ${renderRawFieldsDetails("Die MC-Map enthält die beiden 8-Byte-Statusfelder, Zustand und Aktorausgänge. Fehlergrund, Fehlercode und Schutzart liegen an den Offsets 46–51.")}
  </div>`;
}

function renderDiagnosticCodeList(title, items) {
  if (!Array.isArray(items) || !items.length) return "";
  return `<section><h5>${escapeHtml(title)}</h5><ul>${items.map((entry) => `<li><strong>${escapeHtml(entry.code)}</strong> · ${escapeHtml(entry.text)}</li>`).join("")}</ul></section>`;
}

function renderServiceHistory() {
  const history = state.block?.service_history;
  const serviceRows = (history?.services || []).map((entry) => {
    const diagnostics = renderDiagnosticCodeList("Mögliche Ursachen", entry.causes) + renderDiagnosticCodeList("Mögliche Maßnahmen", entry.measures);
    const detail = diagnostics ? `<details class="service-diagnostics"><summary>Ursachen / Maßnahmen</summary>${diagnostics}</details>` : "—";
    const status = entry.has_event
      ? `Entstörart ${entry.disturbance_reset ? "ja" : "nein"} · Auto-Rücksetzen ${entry.auto_reset ? "ja" : "nein"}`
      : "—";
    return `<tr class="${entry.has_event ? "" : "history-empty"} ${entry.active ? "message-history-active" : ""}">
      <td class="history-ring">${entry.active ? "● " : ""}${entry.slot}${entry.recency ? `<small>vor ${entry.recency}</small>` : ""}</td>
      <td>${escapeHtml(entry.timestamp_text || "—")}</td>
      <td>${entry.code === null || entry.code === undefined ? "—" : `<strong>SC ${escapeHtml(entry.code)}</strong><small>Rohwert ${escapeHtml(entry.raw_code)}</small>`}</td>
      <td><strong>${escapeHtml(entry.text)}</strong></td>
      <td>${escapeHtml(status)}${entry.has_event ? `<small>Δ Motorlaufzeit: ${escapeHtml(entry.delta_motor_runtime)} · Flags: ${escapeHtml(entry.status_flags)}</small>` : ""}</td>
      <td>${detail}</td>
    </tr>`;
  }).join("");
  const warningRows = (history?.warnings || []).map((entry) => `<tr class="${entry.has_event ? "" : "history-empty"} ${entry.active ? "message-history-active" : ""}">
    <td class="history-ring">${entry.active ? "● " : ""}${entry.slot}${entry.recency ? `<small>vor ${entry.recency}</small>` : ""}</td>
    <td>${escapeHtml(entry.timestamp_text || "—")}</td>
    <td>${entry.code === null || entry.code === undefined ? "—" : `<strong>W ${escapeHtml(entry.code)}</strong><small>Rohwert ${escapeHtml(entry.raw_code)}</small>`}</td>
    <td><strong>${escapeHtml(entry.text)}</strong></td>
    <td>${entry.has_event ? `${escapeHtml(entry.type)} · ${escapeHtml(entry.type_label)}` : "—"}</td>
    <td>${entry.has_event ? `${escapeHtml(entry.module)} · ${escapeHtml(entry.module_label)}` : "—"}</td>
  </tr>`).join("");
  $("settingsFields").innerHTML = `<div class="message-history-card service-history-card">
    <div class="message-history-summary service-ring-summary"><span class="status-pill ok">Service-Ring ${escapeHtml(history?.service_ring ?? "—")}</span><span class="status-pill">Messwert-Ring ${escapeHtml(history?.snapshot_ring ?? "—")}</span><span class="status-pill">Warn-Ring ${escapeHtml(history?.warning_ring ?? "—")}</span></div>
    <h4>Servicecode-Historie</h4>
    <div class="table-wrap"><table class="data-table message-history-table service-history-table"><thead><tr><th>Ring</th><th>Zeitstempel</th><th>Code</th><th>Bedeutung</th><th>Status</th><th>Diagnosehinweise</th></tr></thead><tbody>${serviceRows || `<tr><td colspan="6" class="muted">Keine Serviceeinträge vorhanden.</td></tr>`}</tbody></table></div>
    <h4>Warnhistorie</h4>
    <div class="table-wrap"><table class="data-table message-history-table"><thead><tr><th>Ring</th><th>Zeitstempel</th><th>Warncode</th><th>Bedeutung</th><th>Typ</th><th>Modul</th></tr></thead><tbody>${warningRows || `<tr><td colspan="6" class="muted">Keine Warneinträge vorhanden.</td></tr>`}</tbody></table></div>
    <p class="source-note">Code, Zeitstempel, Status und Ringposition stammen direkt aus den gelesenen Blöcken. Nicht sicher dokumentierte Bedeutungen bleiben als Code sichtbar.</p>
    ${renderRawFieldsDetails(`Rohfelder des aktuell gewählten Teilblocks ${state.selectedBlock}; die Übersicht liest Block 80 und 82 immer gemeinsam.`)}
  </div>`;
}

function renderMotorSnapshot() {
  const snapshot = state.block?.motor_snapshot;
  const context = snapshot?.service_context;
  const contextText = context?.code
    ? `<strong>SC ${escapeHtml(context.code)} · ${escapeHtml(context.text)}</strong><span>${escapeHtml(context.timestamp_text || "Zeit unbekannt")} · Service-Ringplatz ${escapeHtml(context.slot)}</span>`
    : `<strong>Kein Servicecode eindeutig zugeordnet</strong><span>Messwert-Ring und Service-Ring enthalten dafür aktuell keinen belegten gemeinsamen Eintrag.</span>`;
  const sections = (snapshot?.sections || []).map((section) => `<section class="snapshot-section"><h4>${escapeHtml(section.title)}</h4><div class="snapshot-values">${(section.items || []).map((item) => `<div class="snapshot-value ${item.derived ? "derived" : ""}"><span>${escapeHtml(item.label)}</span><strong>${escapeHtml(item.value ?? "—")} ${escapeHtml(item.unit || "")}</strong><small>${item.derived ? "berechnet" : `Offset ${item.offset ?? "?"}`}</small></div>`).join("")}</div></section>`).join("");
  $("settingsFields").innerHTML = `<div class="message-history-card motor-snapshot-card">
    <div class="snapshot-head"><div><span class="status-pill ok">Messwertspeicher ${escapeHtml(snapshot?.slot ?? "—")}</span><span class="status-pill">Netz/MC: Block ${escapeHtml(snapshot?.paired_mc_block ?? "—")}</span></div><div class="snapshot-service-context">${contextText}</div></div>
    <div class="snapshot-grid">${sections}</div>
    <p class="source-note">Die Werte sind nach Betrieb, internen Temperaturen, Heizkreis/Speicher, Regelwerten sowie Aktoren/Hardware gruppiert. Der zugehörige Netz- und MC-Messwertspeicher liegt im angegebenen Folgeblock.</p>
    ${renderRawFieldsDetails(`Alle physikalischen Felder von Block ${state.selectedBlock} bleiben hier vollständig sichtbar und für Admins editierbar.`)}
  </div>`;
}

function renderRunHistory() {
  const history = state.block?.run_history;
  const summary = history?.summary || {};
  const metrics = [
    ["Betriebsstunden", summary.operating_hours, "h"], ["Starts gesamt", summary.starts, ""],
    ["Elektrische Arbeit", summary.electric_work_kwh, "kWh"], ["Thermische Arbeit Dachs", summary.thermal_work_hka_kwh, "kWh"],
    ["Thermische Energie Kondenser", summary.thermal_work_condenser_kwh, "kWh"], ["Warmwassermenge", summary.hot_water_m3, "m³"],
  ];
  const metricCards = metrics.map(([label,value,unit]) => `<article class="run-metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value ?? "—")} ${escapeHtml(unit)}</strong></article>`).join("");
  const dayRows = (history?.days || []).map((day) => `<div class="run-day-row"><div class="run-day-label"><strong>${escapeHtml(day.day_label)}</strong><span>${escapeHtml(day.date_text)} · Ring ${day.ring_slot}</span></div><div class="run-raster" title="${day.runtime_hours} Betriebsstunden">${(day.quarters || []).map((active,index) => `<i class="${active ? "active" : ""}" title="${String(Math.floor(index / 4)).padStart(2,"0")}:${String((index % 4) * 15).padStart(2,"0")}"></i>`).join("")}</div><div class="run-day-stats"><strong>${day.runtime_hours} h</strong><span>${day.starts} Starts</span></div></div>`).join("");
  const shutdownRows = (history?.shutdowns || []).map((entry) => `<tr class="${entry.timestamp_plausible ? "" : "history-empty"}"><td>${entry.index}</td><td>${escapeHtml(entry.timestamp_text || "—")}</td><td>${escapeHtml(entry.code)}</td></tr>`).join("");
  $("settingsFields").innerHTML = `<div class="message-history-card run-history-card">
    <div class="run-summary-grid">${metricCards}</div>
    <div class="run-axis"><span>00:00</span><span>04:00</span><span>08:00</span><span>12:00</span><span>16:00</span><span>20:00</span><span>24:00</span></div>
    <div class="run-days">${dayRows}</div>
    <section class="run-shutdowns"><h4>Letzte Abschaltgründe</h4><div class="table-wrap"><table class="data-table"><thead><tr><th>Eintrag</th><th>Zeitstempel</th><th>Abschaltcode</th></tr></thead><tbody>${shutdownRows}</tbody></table></div></section>
    <p class="source-note">Gemeinsame Auswertung der Blöcke 28 (Ring/Starts/Tage 1–5), 30 (Tage 6–7/Abschaltungen), 31 (aktueller Tag) und 32 (Summenwerte).</p>
    ${renderRawFieldsDetails(`Rohfelder des aktuell gewählten Teilblocks ${state.selectedBlock}; das Diagramm liest immer alle vier zusammengehörigen Blöcke.`)}
  </div>`;
}

function renderFieldEditor(field, admin) {
  const value = String(field.edit_value ?? field.value ?? "");
  const choices = Array.isArray(field.choices) ? field.choices : [];
  const editorId = `field-${encodeURIComponent(field.key)}`;
  if (!choices.length) {
    return `<input id="${editorId}" data-key="${escapeHtml(field.key)}" data-baseline="${escapeHtml(value)}" value="${escapeHtml(value)}" inputmode="decimal" ${admin ? "" : "disabled"}>`;
  }
  const knownValue = choices.some((choice) => String(choice.value) === value);
  const options = choices.map((choice) => {
    const raw = String(choice.value);
    const label = choice.label || raw;
    return `<option value="${escapeHtml(raw)}" ${knownValue && raw === value ? "selected" : ""}>${escapeHtml(label)} · Rohwert ${escapeHtml(raw)}</option>`;
  }).join("");
  return `<div class="field-choice-editor">
    <select id="${editorId}" data-key="${escapeHtml(field.key)}" data-editor="choice" data-baseline="${escapeHtml(value)}" ${admin ? "" : "disabled"}>
      ${options}
      <option value="__raw__" ${knownValue ? "" : "selected"}>Rohwert manuell eingeben …</option>
    </select>
    <input class="choice-raw-input" data-choice-raw value="${escapeHtml(value)}" inputmode="numeric" ${knownValue ? "hidden" : ""} ${admin ? "" : "disabled"} aria-label="Rohwert für ${escapeHtml(field.label || field.key)}">
  </div>`;
}

function renderFieldHelp(field) {
  const notes = [];
  if (field.help) notes.push(field.help);
  if (Array.isArray(field.choices) && field.choices.length) notes.push("Bekannte Auswahlwerte; die manuelle Rohwert-Eingabe bleibt verfügbar.");
  if (!field.help && (field.min !== null && field.min !== undefined || field.max !== null && field.max !== undefined)) {
    const lower = field.min !== null && field.min !== undefined ? field.min : "offen";
    const upper = field.max !== null && field.max !== undefined ? field.max : "offen";
    notes.push(`Dokumentierter Bereich: ${lower} bis ${upper}${field.step !== null && field.step !== undefined ? `, Schritt ${field.step}` : ""}.`);
  }
  return notes.length ? `<div class="field-help">${escapeHtml(notes.join(" "))}</div>` : "";
}

function bindChoiceEditors(container) {
  container.querySelectorAll('select[data-editor="choice"]').forEach((select) => {
    select.addEventListener("change", () => {
      const rawInput = select.closest(".field-choice-editor")?.querySelector("[data-choice-raw]");
      if (!rawInput) return;
      rawInput.hidden = select.value !== "__raw__";
      if (!rawInput.hidden && !rawInput.disabled) rawInput.focus();
    });
  });
}

function renderFields() {
  const admin = state.user?.role === "admin";
  if (state.selectedBlock === 18 || state.block?.history) {
    renderMessageHistory();
    return;
  }
  if (state.block?.oil_refill_history) {
    renderOilRefillHistory();
    return;
  }
  if (state.block?.service_history && !state.block?.motor_snapshot) {
    renderServiceHistory();
    return;
  }
  if (state.block?.motor_snapshot) {
    renderMotorSnapshot();
    return;
  }
  if (state.block?.mc_status) {
    renderMcStatus();
    return;
  }
  if (state.block?.run_history) {
    renderRunHistory();
    return;
  }
  const fields = (state.block?.fields || []).filter((field) => state.showReserved || !field.reserved);
  const critical = Boolean(state.block?.critical);
  const warning = critical ? `<div class="network-protection-warning"><strong>NETZSCHUTZ · CPU ${state.selectedCpu} · BLOCK 16</strong><span>Besonders sicherheitsrelevante Einstellungen. Die rote Markierung schützt vor Verwechslung mit normalen Reglerfeldern; Schreiben erfolgt wie bei allen Registern nur mit Admin-Haken, Auth, ACK und Readback.</span></div>` : "";
  $("settingsFields").innerHTML = warning + fields.map((field) => `<div class="register-field ${admin ? "" : "readonly"} ${critical || field.critical ? "critical-field" : ""}">
    <div class="register-field-head"><label for="field-${encodeURIComponent(field.key)}">${escapeHtml(field.label || field.key)}</label><small>${escapeHtml(field.type || "")} · ${field.size} B</small></div>
    ${renderFieldEditor(field, admin)}
    ${renderFieldHelp(field)}
    <div class="field-meta">${escapeHtml(field.key)} · Offset ${field.offset ?? "?"} · ${escapeHtml(field.unit || "")}</div>
  </div>`).join("") || `<p class="muted">Keine dekodierten Felder für diesen Block.</p>`;
  bindChoiceEditors($("settingsFields"));
}

async function saveBlock() {
  if (state.user?.role !== "admin" || !state.block) return;
  const changes = [];
  $("settingsFields").querySelectorAll("[data-key]").forEach((editor) => {
    let value = editor.value;
    if (editor.dataset.editor === "choice" && value === "__raw__") {
      value = editor.closest(".field-choice-editor")?.querySelector("[data-choice-raw]")?.value ?? "";
    }
    if (String(value) !== String(editor.dataset.baseline)) changes.push({ key: editor.dataset.key, value });
  });
  if (!changes.length) return toast("Keine Änderungen vorbereitet.");
  try {
    const endpoint = state.selectedCpu ? `/api/network-protection/${state.selectedCpu}` : `/api/block/${state.selectedBlock}`;
    const result = await api(endpoint, { method:"POST", body:JSON.stringify({
      changes,
      auth_level: Number($("authLevel").value || -1),
      pass4: $("pass4").value,
      write_enabled: $("writeEnabled").checked,
    }) });
    const target = state.selectedCpu ? `Netzschutz CPU ${state.selectedCpu}` : `Block ${state.selectedBlock}`;
    toast(result.written ? `${target} geschrieben und Readback bestätigt.` : `Dry-Run für ${target} gespeichert – Hardware wurde nicht geändert.`);
    await loadBlock(state.selectedBlock, state.selectedCpu);
    await refreshAudit();
  } catch (error) { toast(error.message); }
}

function updateWriteGuard() {
  const status = $("writeGuardStatus");
  const enabled = Boolean($("writeEnabled")?.checked);
  if (!status) return;
  status.textContent = enabled
    ? "LIVE-SCHREIBEN aktiv: Änderungen werden nach PW4/Auth und Readback an die Anlage übertragen."
    : "DRY-RUN: Änderungen können vorbereitet werden, die Hardware bleibt unverändert.";
  status.className = `write-guard-status ${enabled ? "warn" : "neutral"}`;
  renderOverviewPowerWriteMode();
}

function renderAuthPreview(data) {
  state.authPreview = data || null;
  $("authPreviewSerial").textContent = data?.serial_number || "—";
  $("authPreviewHours").textContent = data?.operating_hours === undefined ? "—" : `${data.operating_hours} h`;
  $("authPreviewPw4").textContent = data?.pw4 || "—";
  const status = $("authPreviewStatus");
  status.textContent = data?.ok ? "PW4 gültig" : (data?.error || "PW4 konnte nicht geprüft werden");
  status.className = data?.ok ? "" : "muted";
  $("authPreviewFormula").textContent = data?.formula ? `Formel: ${data.formula}` : "Formel: letzte 4 Stellen des MSR2-Tagescodes";
  $("authPreviewApply").disabled = !(data?.ok && data?.pw4);
}

async function refreshAuthPreview() {
  if (state.user?.role !== "admin") return;
  const status = $("authPreviewStatus");
  status.textContent = "Lese Block 20 und 22 …";
  status.className = "muted";
  try {
    renderAuthPreview(await api("/api/auth-preview"));
  } catch (error) {
    renderAuthPreview({ ok: false, error: error.message });
  }
}

function applyAuthPreview() {
  const pw4 = state.authPreview?.pw4;
  if (!state.authPreview?.ok || !pw4) return;
  $("pass4").value = pw4;
  toast("Berechnete PW4 ins Eingabefeld übernommen.");
}

async function toggleMonitor() {
  if (state.user?.role !== "admin") return;
  try { state.live = { ...(state.live || {}), monitor: await api("/api/monitor", { method:"POST", body:JSON.stringify({ enabled: !(state.live?.monitor?.enabled) }) }) }; refreshMonitorStatus(); }
  catch (error) { toast(error.message); }
}

async function toggleSerial() {
  if (state.user?.role !== "admin") return;
  const enabled = state.live?.monitor?.serial_enabled === false;
  try {
    state.live = { ...(state.live || {}), monitor: await api("/api/serial", { method:"POST", body:JSON.stringify({ enabled }) }) };
    updateConnection();
    refreshMonitorStatus();
  } catch (error) { toast(error.message); }
}

async function changePassword(event) {
  event.preventDefault();
  try {
    await api("/api/password", { method:"POST", body:JSON.stringify({
      current_password: $("currentWebPassword").value,
      new_password: $("newWebPassword").value,
    }) });
    showLogin("Passwort geändert. Bitte neu anmelden.");
  } catch (error) { toast(error.message); }
}

async function changeGuestPassword(event) {
  event.preventDefault();
  try {
    await api("/api/users/gast/password", { method:"POST", body:JSON.stringify({
      current_password: $("guestPasswordAdminCurrent").value,
      new_password: $("newGuestPassword").value,
    }) });
    $("guestPasswordForm").reset();
    toast("Gastpasswort geändert. Offene Gastsitzungen wurden beendet.");
  } catch (error) { toast(error.message); }
}

function chartColors() { return { grid:getComputedStyle(document.documentElement).getPropertyValue("--chart-grid").trim() || "#dbe3e5", ink:getComputedStyle(document.documentElement).getPropertyValue("--muted").trim() || "#69767b", background:getComputedStyle(document.documentElement).getPropertyValue("--surface-alt").trim() || "#f7f9f9" }; }

function chartPoints(series) {
  return series.flatMap((item) => (item.points || []).map((point) => ({ ...point, item, number:numeric(point.value), time:new Date(point.recorded_at).getTime() })))
    .filter((point) => Number.isFinite(point.time) && point.number !== null && !isInvalidSensor(point.item.id, point));
}

function chartAxisConfig(group) {
  if (group === "motor") return {
    left: { ids: ["drehzahl"], label: "Drehzahl (1/min)", mobileLabel: "Drehzahl", preferredMax: 3000 },
    right: { ids: ["wirkleistung"], label: "Wirkleistung (kW)", mobileLabel: "Leistung", preferredMax: 6 },
  };
  if (group === "exhaust") return {
    left: { ids: ["abgas_motor"], label: "Motorabgas (°C)", mobileLabel: "Motorabgas", preferredMax: 600 },
    right: { ids: ["abgas_hka"], label: "Dachsabgas (°C)", mobileLabel: "Dachsabgas", preferredMax: 200 },
  };
  return null;
}

function chartSeriesHidden(group, item) {
  return Boolean(state.chartHidden[group]?.has(item.id));
}

function niceStep(value) {
  const safe = Math.max(Math.abs(value) || 1, 1e-6);
  const exponent = 10 ** Math.floor(Math.log10(safe));
  const normalized = safe / exponent;
  const factor = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 2.5 ? 2.5 : normalized <= 5 ? 5 : 10;
  return factor * exponent;
}

function niceCeiling(value) {
  const safe = Math.max(Math.abs(value) || 1, 1e-6);
  const exponent = 10 ** Math.floor(Math.log10(safe));
  const normalized = safe / exponent;
  const factor = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 2.5 ? 2.5 : normalized <= 5 ? 5 : 10;
  return factor * exponent;
}

function dynamicScale(values) {
  if (!values.length) return { min: 0, max: 1, step: .25 };
  let min = Math.min(...values), max = Math.max(...values);
  if (min === max) {
    const delta = Math.max(1, Math.abs(max) * .05);
    min -= delta;
    max += delta;
  }
  const step = niceStep((max - min) / 4);
  return { min: Math.floor(min / step) * step, max: Math.ceil(max / step) * step, step };
}

function lowerBoundScale(values, preferredMax) {
  const actual = values.length ? Math.max(...values, 0) : 0;
  let max = actual > 0 ? niceCeiling(actual * 1.08) : preferredMax;
  if (preferredMax && actual <= preferredMax) max = Math.min(max, preferredMax);
  if (!Number.isFinite(max) || max <= 0) max = preferredMax || 1;
  return { min: 0, max, step: niceStep(max / 4) };
}

function formatAxisTick(value, step) {
  const digits = step < 1 ? 2 : Number.isInteger(step) ? 0 : 1;
  return Number(value).toFixed(digits).replace(/\.0+$/, "");
}

function nearestChartPoint(data, time) {
  if (!data.length) return null;
  let best = data[0];
  let distance = Math.abs(best.time - time);
  for (let index = 1; index < data.length; index += 1) {
    const candidate = data[index];
    const candidateDistance = Math.abs(candidate.time - time);
    if (candidateDistance < distance) {
      best = candidate;
      distance = candidateDistance;
    }
  }
  return best;
}

function chartValueText(value, unit) {
  if (!Number.isFinite(value)) return "—";
  const digits = unit === "kW" || unit === "φ" ? 2 : Number.isInteger(value) ? 0 : 1;
  const text = Number(value).toFixed(digits).replace(/\.0+$/, "");
  return `${text}${unit ? ` ${unit}` : ""}`;
}

function drawChartHover(ctx, pointer, visibleSeries, dataById, start, end, left, top, plotW, plotH, width, height, group, axisConfig, leftScale, rightScale) {
  if (!pointer || !Number.isFinite(pointer.hoverX) || pointer.hoverX < left || pointer.hoverX > left + plotW) return;
  const hoverTime = start + ((pointer.hoverX - left) / Math.max(1, plotW)) * (end - start);
  const samples = [];
  for (const item of visibleSeries) {
    const point = nearestChartPoint(dataById.get(item.id) || [], hoverTime);
    if (!point) continue;
    const axis = axisConfig && axisConfig.right.ids.includes(item.id) ? rightScale : leftScale;
    const y = top + ((axis.max - point.number) / Math.max(1, axis.max - axis.min)) * plotH;
    samples.push({ item, point, y });
  }
  if (!samples.length) return;

  ctx.save();
  ctx.strokeStyle = "rgba(40, 99, 167, .65)";
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(pointer.hoverX, top);
  ctx.lineTo(pointer.hoverX, top + plotH);
  ctx.stroke();
  ctx.setLineDash([]);
  for (const sample of samples) {
    ctx.fillStyle = sample.item.color;
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(pointer.hoverX, sample.y, 4.5, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
  }

  const title = new Date(hoverTime).toLocaleString("de-DE", {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
  const lines = samples.map(({ item, point }) => `${item.title}: ${chartValueText(point.number, item.unit)}`);
  ctx.font = "11px system-ui";
  const boxWidth = Math.min(width - 16, Math.max(190, ...[title, ...lines].map((line) => ctx.measureText(line).width + 32)));
  const boxHeight = 28 + lines.length * 17;
  let boxX = pointer.hoverX + 12;
  if (boxX + boxWidth > width - 8) boxX = pointer.hoverX - boxWidth - 12;
  let boxY = Number.isFinite(pointer.hoverY) ? pointer.hoverY + 12 : top + 8;
  boxY = Math.max(top + 8, Math.min(top + plotH - boxHeight - 8, boxY));
  ctx.fillStyle = "rgba(22, 34, 39, .94)";
  ctx.strokeStyle = "rgba(255, 255, 255, .24)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.roundRect(boxX, boxY, boxWidth, boxHeight, 5);
  ctx.fill();
  ctx.stroke();
  ctx.fillStyle = "#ffffff";
  ctx.font = "600 11px system-ui";
  ctx.textAlign = "left";
  ctx.fillText(title, boxX + 10, boxY + 16);
  ctx.font = "11px system-ui";
  lines.forEach((line, index) => {
    const sample = samples[index];
    ctx.fillStyle = sample.item.color;
    ctx.fillRect(boxX + 10, boxY + 23 + index * 17, 7, 7);
    ctx.fillStyle = "#ffffff";
    ctx.fillText(line, boxX + 22, boxY + 30 + index * 17);
  });
  ctx.restore();
}

function drawChart(canvas, series, rangeHours, group, requestedWindow = null) {
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect(); const dpr = window.devicePixelRatio || 1; const width = Math.max(280, Math.floor(rect.width)); const baseHeight = Number(canvas.dataset.chartHeight || canvas.height) || 300; const compact = width < 560 || window.matchMedia?.("(max-width: 780px)").matches; const height = compact ? Math.min(baseHeight, canvas.id === "temperatureChart" ? 250 : 230) : baseHeight;
  canvas.width = width * dpr; canvas.height = height * dpr; const ctx = canvas.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const colors = chartColors(); ctx.fillStyle = colors.background; ctx.fillRect(0, 0, width, height);
  const axisConfig = chartAxisConfig(group); const dualAxis = Boolean(axisConfig);
  const left = dualAxis ? (compact ? 52 : 78) : (compact ? 46 : 58), right = dualAxis ? (compact ? 52 : 78) : (compact ? 12 : 18), top = compact ? 22 : 20, bottom = compact ? 29 : 32, plotW = Math.max(80, width - left - right), plotH = Math.max(80, height - top - bottom);
  const visibleSeries = series.filter((item) => !chartSeriesHidden(group, item));
  const allPoints = chartPoints(series); const points = chartPoints(visibleSeries);
  const customWindow = requestedWindow && Number.isFinite(requestedWindow.start) && Number.isFinite(requestedWindow.end) && requestedWindow.end > requestedWindow.start;
  const geometry = { left, top, width:plotW, height:plotH, group, start:customWindow ? requestedWindow.start : 0, end:customWindow ? requestedWindow.end : 0 };
  if (!allPoints.length) { ctx.fillStyle=colors.ink; ctx.font="13px system-ui"; ctx.fillText("Noch keine gespeicherten Messwerte", 20, height / 2); state.chartGeometries.set(canvas, geometry); return; }
  const times = allPoints.map((point) => point.time); const fullStart = customWindow ? requestedWindow.start : Math.min(...times); const fullEnd = customWindow ? requestedWindow.end : Math.max(...times, fullStart + rangeHours * 3600000);
  const zoom = state.chartZoomRanges.get(group); const start = zoom ? zoom.start : fullStart; const end = zoom ? zoom.end : fullEnd;
  geometry.start = start; geometry.end = end; state.chartGeometries.set(canvas, geometry);
  const visible = points.filter((point) => point.time >= start && point.time <= end); const numbers = visible.map((point) => point.number);
  const dataById = new Map();
  for (const item of visibleSeries) {
    const data = (item.points || []).map((point) => ({
      time: new Date(point.recorded_at).getTime(),
      number: numeric(point.value),
      // Keep the received value for isInvalidSensor(). Without this field
      // numeric(undefined) becomes 0 and every temperature/exhaust point
      // was incorrectly filtered as an invalid sensor value.
      value: point.value,
    }))
      .filter((point) => Number.isFinite(point.time) && point.number !== null && !isInvalidSensor(item.id, point) && point.time >= start && point.time <= end)
      .sort((a,b)=>a.time-b.time);
    dataById.set(item.id, data);
  }
  let leftScale, rightScale;
  if (dualAxis) {
    const valuesFor = (axis) => visibleSeries.flatMap((item) => axis.ids.includes(item.id) ? (dataById.get(item.id) || []).map((point) => point.number) : []);
    leftScale = lowerBoundScale(valuesFor(axisConfig.left), axisConfig.left.preferredMax);
    rightScale = lowerBoundScale(valuesFor(axisConfig.right), axisConfig.right.preferredMax);
  } else {
    leftScale = dynamicScale(numbers);
  }
  ctx.strokeStyle=colors.grid; ctx.lineWidth=1; ctx.fillStyle=colors.ink; ctx.font=compact ? "10px system-ui" : "11px system-ui";
  const leftTickStep = (leftScale.max - leftScale.min) / 4;
  const rightTickStep = dualAxis ? (rightScale.max - rightScale.min) / 4 : null;
  for (let i=0;i<=4;i++) {
    const y=top+plotH*i/4;
    ctx.beginPath(); ctx.moveTo(left,y); ctx.lineTo(left+plotW,y); ctx.stroke();
    const leftValue=leftScale.max-(leftScale.max-leftScale.min)*i/4;
    ctx.textAlign="right"; ctx.fillText(formatAxisTick(leftValue,leftTickStep),left-8,y+4);
    if (dualAxis) {
      const rightValue=rightScale.max-(rightScale.max-rightScale.min)*i/4;
      ctx.textAlign="left"; ctx.fillText(formatAxisTick(rightValue,rightTickStep),left+plotW+8,y+4);
    }
  }
  if (dualAxis) {
    ctx.font=compact ? "9px system-ui" : "10px system-ui"; ctx.textAlign="left"; ctx.fillText(compact ? (axisConfig.left.mobileLabel || axisConfig.left.label) : axisConfig.left.label,left,13); ctx.textAlign="right"; ctx.fillText(compact ? (axisConfig.right.mobileLabel || axisConfig.right.label) : axisConfig.right.label,left+plotW,13); ctx.textAlign="left";
  }
  for (const item of visibleSeries) {
    const data = dataById.get(item.id) || [];
    if (!data.length) continue;
    const axis = dualAxis && axisConfig.right.ids.includes(item.id) ? rightScale : leftScale;
    ctx.strokeStyle=item.color; ctx.lineWidth=2.2; ctx.beginPath();
    data.forEach((point,index)=>{const x=left+((point.time-start)/Math.max(1,end-start))*plotW;const y=top+((axis.max-point.number)/Math.max(1,axis.max-axis.min))*plotH;if(index)ctx.lineTo(x,y);else ctx.moveTo(x,y);}); ctx.stroke();
  }
  if (!visibleSeries.length || !visible.length) { ctx.fillStyle=colors.ink; ctx.font="13px system-ui"; ctx.textAlign="center"; ctx.fillText("Alle Werte ausgeblendet", left+plotW/2, top+plotH/2); ctx.textAlign="left"; }
  ctx.fillStyle=colors.ink; ctx.textAlign="left"; ctx.fillText(new Date(start).toLocaleTimeString("de-DE",{hour:"2-digit",minute:"2-digit"}),left,height-8); ctx.textAlign="right"; ctx.fillText(new Date(end).toLocaleTimeString("de-DE",{hour:"2-digit",minute:"2-digit"}),left+plotW,height-8); ctx.textAlign="left";
  const pointer = state.chartPointers.get(canvas);
  drawChartHover(ctx, pointer, visibleSeries, dataById, start, end, left, top, plotW, plotH, width, height, group, axisConfig, leftScale, rightScale);
  if (pointer?.dragging && Number.isFinite(pointer.currentX)) { const x1=Math.max(left,Math.min(left+plotW,pointer.startX)); const x2=Math.max(left,Math.min(left+plotW,pointer.currentX)); ctx.fillStyle="rgba(40,99,167,.16)"; ctx.fillRect(Math.min(x1,x2),top,Math.abs(x2-x1),plotH); ctx.strokeStyle="#2863a7"; ctx.setLineDash([5,4]); ctx.strokeRect(Math.min(x1,x2),top,Math.abs(x2-x1),plotH); ctx.setLineDash([]); }
}

function redrawChartGroup(group) {
  const canvas = group === "temperature" ? $("temperatureChart") : group === "motor" ? $("motorChart") : $("exhaustChart");
  const range = Number($("temperatureRange")?.value || 24); drawChart(canvas, state.chartSeries[group] || [], range, group, state.historyWindow); updateZoomControl(group);
}

function updateZoomControl(group) {
  document.querySelectorAll(`[data-action="reset-chart-zoom"][data-chart-group="${group}"]`).forEach((button) => { button.hidden = !state.chartZoomRanges.has(group); });
}

function resetChartZoom(group) { state.chartZoomRanges.delete(group); state.chartPointers.delete($(group === "temperature" ? "temperatureChart" : group === "motor" ? "motorChart" : "exhaustChart")); redrawChartGroup(group); }

function bindChartZoom(canvas, group) {
  if (!canvas || canvas.dataset.zoomBound) return;
  canvas.dataset.zoomBound = "1";
  const position = (event) => { const rect=canvas.getBoundingClientRect(); return { x:event.clientX-rect.left, y:event.clientY-rect.top }; };
  canvas.addEventListener("pointerdown", (event) => { if (event.pointerType === "mouse" && event.button !== 0) return; const point=position(event); const geometry=state.chartGeometries.get(canvas); if (!geometry || point.x < geometry.left || point.x > geometry.left+geometry.width || point.y < geometry.top || point.y > geometry.top+geometry.height) return; state.chartPointers.set(canvas,{dragging:true,startX:point.x,currentX:point.x,hoverX:point.x,hoverY:point.y,pointerId:event.pointerId}); canvas.setPointerCapture?.(event.pointerId); event.preventDefault(); redrawChartGroup(group); });
  canvas.addEventListener("pointermove", (event) => { const point=position(event); const pointer=state.chartPointers.get(canvas); if (pointer?.dragging) state.chartPointers.set(canvas,{...pointer,currentX:point.x,hoverX:point.x,hoverY:point.y}); else state.chartPointers.set(canvas,{hoverX:point.x,hoverY:point.y,dragging:false}); redrawChartGroup(group); });
  canvas.addEventListener("pointerup", (event) => { const pointer=state.chartPointers.get(canvas); if (!pointer?.dragging || pointer.pointerId !== event.pointerId) return; const geometry=state.chartGeometries.get(canvas); const point=position(event); const endX=point.x; if (geometry && Math.abs(endX-pointer.startX)>=8) { const x1=Math.max(geometry.left,Math.min(geometry.left+geometry.width,pointer.startX)); const x2=Math.max(geometry.left,Math.min(geometry.left+geometry.width,endX)); state.chartZoomRanges.set(group,{start:geometry.start+(Math.min(x1,x2)-geometry.left)/geometry.width*(geometry.end-geometry.start),end:geometry.start+(Math.max(x1,x2)-geometry.left)/geometry.width*(geometry.end-geometry.start)}); } state.chartPointers.set(canvas,{dragging:false,hoverX:endX,hoverY:point.y}); canvas.releasePointerCapture?.(event.pointerId); redrawChartGroup(group); event.preventDefault(); });
  canvas.addEventListener("pointerleave", () => { const pointer=state.chartPointers.get(canvas); if (pointer?.dragging) return; state.chartPointers.delete(canvas); redrawChartGroup(group); });
  canvas.addEventListener("dblclick", (event) => { resetChartZoom(group); event.preventDefault(); });
}

function renderChartLegend(group, elementId) {
  const element = $(elementId);
  if (!element) return;
  const hidden = state.chartHidden[group] || new Set();
  element.innerHTML = (state.chartSeries[group] || []).map((item) => {
    const isHidden = hidden.has(item.id);
    return `<button type="button" class="legend-item${isHidden ? " is-hidden" : ""}" data-chart-group="${group}" data-chart-series="${escapeHtml(item.id)}" style="--legend-color:${escapeHtml(item.color)}" aria-pressed="${!isHidden}" title="Kurve ein-/ausblenden">${escapeHtml(item.title)}</button>`;
  }).join("");
  element.querySelectorAll("[data-chart-series]").forEach((button) => button.addEventListener("click", () => {
    const seriesId = button.dataset.chartSeries;
    if (hidden.has(seriesId)) hidden.delete(seriesId); else hidden.add(seriesId);
    renderChartLegend(group, elementId);
    redrawChartGroup(group);
  }));
}

async function refreshCharts() {
  if (!state.schema) return;
  if (state.chartRefresh.inFlight) {
    state.chartRefresh.pending = true;
    return;
  }
  state.chartRefresh.inFlight = true;
  state.chartRefresh.pending = false;
  try {
    const selection = historySelection();
    state.historyWindow = selection.window;
    $("historyRangeStatus").textContent = `Lade Diagramme · ${selection.label}`;
    const groups = {
      temperature: ["kuehlwasser", "dachs_eintritt", "kapsel", "regler"],
      motor: ["wirkleistung", "drehzahl"],
      exhaust: ["abgas_motor", "abgas_hka"],
    };
    const ids = Object.values(groups).flat();
    const chartItems = (state.schema.series || []).filter((item) => ids.includes(item.id));
    const params = new URLSearchParams(selection.query);
    params.set("limit", "2000");
    params.set("series", JSON.stringify(chartItems.map(({ id, block, key }) => ({ id, block, key }))));
    const batch = await api(`/api/history-batch?${params.toString()}`);
    const historyFor = (groupIds) => groupIds.map((id) => {
      const item = chartItems.find((candidate) => candidate.id === id);
      return item ? { ...item, points: batch.series?.[id] || [] } : null;
    }).filter(Boolean);
    state.chartSeries.temperature = historyFor(groups.temperature);
    state.chartSeries.motor = historyFor(groups.motor);
    state.chartSeries.exhaust = historyFor(groups.exhaust);
    $("historyRangeStatus").textContent = `Aktiv: ${selection.label} · ${chartItems.length} Kurven`;
    renderChartLegend("temperature", "temperatureLegend");
    renderChartLegend("motor", "motorLegend");
    renderChartLegend("exhaust", "exhaustLegend");
    bindChartZoom($("temperatureChart"), "temperature"); bindChartZoom($("motorChart"), "motor"); bindChartZoom($("exhaustChart"), "exhaust");
    redrawChartGroup("temperature"); redrawChartGroup("motor"); redrawChartGroup("exhaust");
  } catch (error) {
    $("historyRangeStatus").textContent = `Fehler: ${error.message}`;
  } finally {
    state.chartRefresh.inFlight = false;
    if (state.chartRefresh.pending && state.selectedView === "monitorView") {
      state.chartRefresh.pending = false;
      setTimeout(refreshCharts, 0);
    }
  }
}

function historySelection() {
  const quickHours = Number($("temperatureRange")?.value || 24);
  const startText = $("historyStart")?.value || "";
  const endText = $("historyEnd")?.value || "";
  if (!startText && !endText) {
    return { query: `hours=${quickHours}`, window: null, label: `letzte ${quickHours === 24 ? "24 Stunden" : `${quickHours} Stunden`}` };
  }
  let start = startText ? new Date(startText).getTime() : null;
  let end = endText ? new Date(endText).getTime() : null;
  if (start !== null && !Number.isFinite(start)) throw new Error("ungültiges Startdatum");
  if (end !== null && !Number.isFinite(end)) throw new Error("ungültiges Enddatum");
  if (start === null) start = end - 24 * 3600000;
  if (end === null) end = start + 24 * 3600000;
  if (end <= start) throw new Error("Ende muss nach dem Start liegen");
  if (end - start > 30 * 24 * 3600000) throw new Error("Der Zeitraum darf höchstens 30 Tage umfassen");
  const query = new URLSearchParams({ from: new Date(start).toISOString(), to: new Date(end).toISOString() }).toString();
  return { query, window: { start, end }, label: `${new Date(start).toLocaleString("de-DE")} bis ${new Date(end).toLocaleString("de-DE")}` };
}

async function refreshAudit() {
  if (state.user?.role !== "admin") { $("auditRows").innerHTML = `<tr><td colspan="5">Nur für Admin sichtbar.</td></tr>`; return; }
  try {
    const data = await api("/api/audit");
    $("auditRows").innerHTML = (data.items || []).map((item) => { const audit=item.audit||{}; const result=audit.written ? "GESCHRIEBEN + READBACK" : (audit.dry_run ? "DRY-RUN" : (audit.error || "Fehler")); const target=Number(audit.cpu||0) ? `CPU ${audit.cpu} · ${item.block}` : item.block; return `<tr class="${audit.critical ? "critical-audit" : ""}"><td>${escapeHtml(new Date(item.recorded_at).toLocaleString("de-DE"))}</td><td>${escapeHtml(item.username)}</td><td>${escapeHtml(target)}</td><td>${escapeHtml(result)}</td><td>${escapeHtml((audit.changed_keys || []).join(", "))}</td></tr>`; }).join("") || `<tr><td colspan="5">Noch keine Schreibversuche.</td></tr>`;
  } catch (error) { toast(error.message); }
}

function toast(message) { const element=$("toast"); element.textContent=message; element.classList.add("visible"); clearTimeout(toast.timer); toast.timer=setTimeout(()=>element.classList.remove("visible"),4200); }

document.addEventListener("DOMContentLoaded", () => {
  $("loginForm").addEventListener("submit", login);
  $("logoutButton").addEventListener("click", async () => { await api("/api/logout", { method:"POST", body:"{}" }); showLogin(); });
  document.querySelectorAll(".tab-button").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
  document.querySelectorAll("[data-view-target]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.viewTarget)));
  $("maintenanceRefresh").addEventListener("click", () => refreshMaintenance(true));
  $("maintenanceCreate").addEventListener("click", createMaintenanceReport);
  $("maintenanceReportRows").addEventListener("click", (event) => {
    const button = event.target.closest("[data-maintenance-report]");
    if (button) loadMaintenanceReport(Number(button.dataset.maintenanceReport));
  });
  $("maintenanceForm").addEventListener("submit", saveMaintenanceDraft);
  $("maintenanceFuelType").addEventListener("change", () => saveMaintenanceDraft(null, true));
  $("maintenanceForm").addEventListener("change", (event) => {
    if (event.target.id !== "maintenanceFuelType") scheduleMaintenanceAutosave();
  });
  $("maintenanceComplete").addEventListener("click", completeMaintenance);
  $("settingsTabs").addEventListener("click", (event) => { const button=event.target.closest("button[data-block]"); if (button) loadBlock(Number(button.dataset.block), Number(button.dataset.cpu || 0)); });
  $("showReserved").addEventListener("change", () => {
    state.showReserved = $("showReserved").checked;
    renderRegisterTabs();
    renderFields();
  });
  $("overviewPowerTargetInput").addEventListener("input", () => { $("overviewPowerTargetInput").dataset.userEdited = "1"; });
  $("overviewPowerTargetForm").addEventListener("submit", applyOverviewPowerTarget);
  $("saveBlockButton").addEventListener("click", saveBlock);
  $("monitorToggle").addEventListener("click", toggleMonitor);
  $("serialToggle").addEventListener("click", toggleSerial);
  $("writeEnabled").addEventListener("change", async () => {
    updateWriteGuard();
    if ($("writeEnabled").checked && !$("pass4").value) await refreshAuthPreview();
  });
  $("authLevel").addEventListener("change", renderOverviewPowerWriteMode);
  $("authPreviewButton").addEventListener("click", refreshAuthPreview);
  $("authPreviewApply").addEventListener("click", applyAuthPreview);
  document.querySelectorAll("[data-schematic-mode]").forEach((button) => button.addEventListener("click", () => setSchematicMode(button.dataset.schematicMode)));
  $("passwordForm").addEventListener("submit", changePassword);
  $("guestPasswordForm").addEventListener("submit", changeGuestPassword);
  $("temperatureRange").addEventListener("change", () => { $("historyStart").value = ""; $("historyEnd").value = ""; refreshCharts(); });
  $("applyHistoryRange").addEventListener("click", refreshCharts);
  $("resetHistoryRange").addEventListener("click", () => { $("historyStart").value = ""; $("historyEnd").value = ""; $("temperatureRange").value = "24"; refreshCharts(); });
  document.querySelectorAll('[data-action="reset-chart-zoom"]').forEach((button) => button.addEventListener("click", () => resetChartZoom(button.dataset.chartGroup)));
  document.querySelectorAll("[data-action=refresh-live]").forEach((button)=>button.addEventListener("click", refreshLive));
  document.querySelectorAll("[data-action=reload-block]").forEach((button)=>button.addEventListener("click",()=>loadBlock(state.selectedBlock, state.selectedCpu)));
  document.querySelectorAll("[data-action=refresh-audit]").forEach((button)=>button.addEventListener("click",refreshAudit));
  window.addEventListener("resize", () => {
    if (state.selectedView !== "monitorView") return;
    ["temperature", "motor", "exhaust"].forEach(redrawChartGroup);
  });
  boot();
});
