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
  chartZoomRange: null,
  chartRunBands: [],
  motorStatusSegments: [],
  motorStatusBackfillComplete: true,
  motorStatusHitboxes: new WeakMap(),
  chartPointers: new WeakMap(),
  chartGeometries: new WeakMap(),
  historyRequest: { mode: "hours", hours: 24 },
  historyWindow: null,
  chartRefresh: { inFlight: false, pending: false, lastCompletedAt: 0 },
  authPreview: null,
  authPreviewGeneration: 0,
  authPreviewClearTimer: null,
  maintenanceSettings: null,
  sootFilterSettings: null,
  maintenance: { reports: [], current: null, autosaveTimer: null, pw4Generation: 0, pw4ClearTimer: null },
  dashboard: { settings: null, editCards: [], draggedIndex: null },
  system: { selectedTab: "users", users: [], tokens: [], apiSettings: null },
  backup: { image: null, inspection: null, busy: false, importGeneration: 0, archive: [], archiveLoaded: false },
  serviceCatalogTimer: null,
  serviceCatalogLoaded: false,
  changelogTrigger: null,
  refreshTimer: null,
  chartTimer: null,
};

const $ = (id) => document.getElementById(id);
const loginView = $("loginView");
const appView = $("appView");
const BASE_PATH = document.querySelector('meta[name="open-dachs-base-path"]')?.content || "";
const BACKUP_MAX_FILE_BYTES = 1024 * 1024;
const RESTORE_CONFIRMATION = "SICHERUNG WIEDERHERSTELLEN";

function appUrl(path) {
  const suffix = String(path || "").startsWith("/") ? String(path || "") : `/${path || ""}`;
  return `${BASE_PATH}${suffix}` || "/";
}

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
  const response = await fetch(appUrl(path), { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  const text = await response.text();
  let payload = {};
  try { payload = text ? JSON.parse(text) : {}; } catch (_) { payload = { error: text }; }
  if (response.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("Anmeldung erforderlich");
  }
  if (!response.ok) {
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.payload = payload;
    error.status = response.status;
    throw error;
  }
  return payload;
}

function showLogin(message = "") {
  state.user = null;
  state.backup.image = null;
  state.backup.inspection = null;
  state.backup.busy = false;
  state.backup.importGeneration += 1;
  state.backup.archive = [];
  state.backup.archiveLoaded = false;
  if (state.refreshTimer) clearInterval(state.refreshTimer);
  if (state.chartTimer) clearInterval(state.chartTimer);
  if ($("writeEnabled")) $("writeEnabled").checked = false;
  clearAuthPreview();
  clearMaintenancePw4();
  if ($("writeGuardStatus")) updateWriteGuard();
  if ($("restoreFile")) $("restoreFile").value = "";
  if ($("restorePass4")) $("restorePass4").value = "";
  if ($("restoreConfirmation")) $("restoreConfirmation").value = "";
  if ($("restoreWriteEnabled")) $("restoreWriteEnabled").checked = false;
  if ($("restoreBlockList")) $("restoreBlockList").innerHTML = `<p class="muted">Noch kein geprüftes Backup-Image geladen.</p>`;
  if ($("restoreResults")) $("restoreResults").innerHTML = "";
  if ($("restoreSelectAll")) $("restoreSelectAll").disabled = true;
  if ($("restoreSelectNone")) $("restoreSelectNone").disabled = true;
  if ($("restoreSubmit")) $("restoreSubmit").disabled = true;
  if ($("restoreImageStatus")) setBackupStatus("restoreImageStatus", "Noch kein Backup-Image geprüft.", "neutral");
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
  document.querySelectorAll(".system-admin").forEach((element) => { element.hidden = !isAdmin; });
  $("maintenanceModeControls").hidden = !isAdmin;
  $("sootFilterSave").hidden = !isAdmin;
  $("sootFilterZeroTemperature").disabled = !isAdmin;
  $("sootFilterFullTemperature").disabled = !isAdmin;
  $("dashboardEdit").hidden = !isAdmin;
  document.querySelectorAll(".maintenance-admin").forEach((element) => { element.hidden = !isAdmin; });
  document.querySelectorAll(".backup-admin").forEach((element) => { element.hidden = !isAdmin; });
  $("settingsRoleHint").textContent = isAdmin ? "Admin: lesen und schreiben" : "Gast: nur lesen";
  $("settingsRoleHint").className = `status-pill ${isAdmin ? "ok" : "neutral"}`;
  if (!isAdmin && state.selectedView === "systemView") showView("overviewView");
  renderSootFilterSettings();
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
    state.dashboard.settings = await api("/api/settings/dashboard");
    state.sootFilterSettings = await api("/api/settings/soot-filter");
    renderSootFilterSettings();
    renderRegisterTabs();
    renderBackupBlockList();
    updateRestoreMode();
    if (session.user.role === "admin") await refreshMaintenanceMode();
    await refreshLive();
    await refreshMaintenance(false);
    await loadBlock(state.selectedBlock);
    refreshMonitorStatus();
    refreshAudit();
    state.refreshTimer = setInterval(refreshLive, 1100);
    state.chartTimer = setInterval(() => {
      if (state.selectedView === "monitorView" && historyAutoRefreshDue()) refreshCharts();
    }, 6500);
  } catch (error) {
    if (error.message !== "Anmeldung erforderlich") showLogin(error.message);
  }
}

function renderMaintenanceMode() {
  const settings = state.maintenanceSettings;
  if (!settings || state.user?.role !== "admin") return;
  const testMode = Boolean(settings.test_mode);
  $("maintenanceTestMode").checked = testMode;
  $("maintenanceModeControls").classList.toggle("live-mode", !testMode);
  $("maintenanceModeStatus").textContent = testMode
    ? "Testmodus aktiv: Wartungsberichte werden ausschließlich lokal abgeschlossen. Der Regler bleibt unverändert."
    : "Echtbetrieb aktiv: Ein Wartungsabschluss kann Block 100 und danach das Bestätigungsbit in Block 104 schreiben.";
  $("maintenanceModeStatus").className = `write-guard-status ${testMode ? "ok" : "error"}`;
}

async function refreshMaintenanceMode() {
  state.maintenanceSettings = await api("/api/settings/maintenance");
  renderMaintenanceMode();
}

async function changeMaintenanceMode() {
  if (state.user?.role !== "admin") return;
  const control = $("maintenanceTestMode");
  const testMode = control.checked;
  control.disabled = true;
  try {
    state.maintenanceSettings = await api("/api/settings/maintenance", {
      method: "POST",
      body: JSON.stringify({ test_mode: testMode }),
    });
    renderMaintenanceMode();
    if (state.maintenance.current?.id) {
      await loadMaintenanceReport(Number(state.maintenance.current.id));
    }
    toast(testMode
      ? "Testmodus gespeichert. Wartungsabschlüsse schreiben nicht in den Regler."
      : "Echtbetrieb gespeichert. Ein Abschluss benötigt weiterhin PW4, Bestätigung, ACK und Readback.");
  } catch (error) {
    renderMaintenanceMode();
    toast(error.message);
  } finally {
    control.disabled = false;
  }
}

function renderSootFilterSettings() {
  const settings = state.sootFilterSettings;
  if (!settings) return;
  $("sootFilterZeroTemperature").value = settings.zero_temperature_c;
  $("sootFilterFullTemperature").value = settings.full_temperature_c;
  const roleText = state.user?.role === "admin"
    ? "Als Admin kannst du die Kennlinie ändern."
    : "Gastzugang: Kennlinie nur lesbar.";
  $("sootFilterSettingsStatus").textContent = `${settings.zero_temperature_c} °C = 0 % · ${settings.full_temperature_c} °C = 100 % · Grün < 60 % · Orange 60–89 % · Rot ab 90 %. ${roleText}`;
}

async function saveSootFilterSettings(event) {
  event.preventDefault();
  if (state.user?.role !== "admin") return;
  const zero = numeric($("sootFilterZeroTemperature").value);
  const full = numeric($("sootFilterFullTemperature").value);
  const button = $("sootFilterSave");
  button.disabled = true;
  try {
    state.sootFilterSettings = await api("/api/settings/soot-filter", {
      method: "POST",
      body: JSON.stringify({ zero_temperature_c: zero, full_temperature_c: full }),
    });
    renderSootFilterSettings();
    await refreshLive();
    toast("Rußfilter-Kennlinie lokal gespeichert.");
  } catch (error) {
    renderSootFilterSettings();
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

function renderSootFilterEstimate() {
  const gauge = $("v98-tech-soot-gauge");
  const bar = $("v98-tech-soot-bar");
  const value = $("v98-tech-soot-fill");
  const source = $("v98-tech-soot-source");
  const estimate = state.live?.soot_filter;
  gauge.classList.remove("green", "orange", "red", "unknown");
  if (!estimate?.available || estimate.percent === null) {
    gauge.classList.add("unknown");
    bar.setAttribute("width", "0");
    value.textContent = "—";
    source.textContent = "Motorabgastemperatur nicht verfügbar";
    return;
  }
  const percent = Math.max(0, Math.min(100, Number(estimate.percent)));
  const level = ["green", "orange", "red"].includes(estimate.level) ? estimate.level : "unknown";
  gauge.classList.add(level);
  bar.setAttribute("width", String(Math.round(136 * percent / 100)));
  value.textContent = `${Math.round(percent)} %`;
  source.textContent = `aus ${estimate.source_temperature_c} °C Motorabgas · ${estimate.zero_temperature_c}–${estimate.full_temperature_c} °C`;
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

function activeMessageCode(seriesId, offset) {
  const raw = numeric(seriesValue(seriesId)?.raw);
  return raw !== null && raw > 0 ? Math.round(raw) + offset : 0;
}

function renderHmiMessages() {
  const service = seriesValue("servicecode");
  const warning = seriesValue("warncode");
  const serviceCode = activeMessageCode("servicecode", 100);
  const warningCode = activeMessageCode("warncode", 600);
  const serviceActive = serviceCode > 0;
  const warningActive = warningCode > 0;
  const serviceText = serviceActive
    ? String(service?.value || `SC ${serviceCode} · Unbekannter Servicecode`)
    : "Kein aktiver Servicecode";
  const warningText = warningActive
    ? String(warning?.value || `WARN ${warningCode} · Unbekannter Warncode`)
    : "Keine aktive Warnung";

  $("hmiServiceMessage").className = `hmi-message-card ${serviceActive ? "alarm" : "normal"}`;
  $("hmiWarningMessage").className = `hmi-message-card ${warningActive ? "warning" : "normal"}`;
  $("hmiServiceSymbol").textContent = serviceActive ? "!" : "✓";
  $("hmiWarningSymbol").textContent = warningActive ? "▲" : "✓";
  $("hmiServiceText").textContent = serviceText;
  $("hmiWarningText").textContent = warningText;
  $("hmiServiceCode").textContent = serviceActive ? `SC ${serviceCode}` : "SC —";
  $("hmiWarningCode").textContent = warningActive ? `WARN ${warningCode}` : "WARN —";
  $("hmiFaultCount").textContent = rowText("anzahl_stoerungen");
  $("hmiWarningCount").textContent = rowText("anzahl_warnungen");

  const level = serviceActive ? "alarm" : warningActive ? "warning" : "normal";
  const stateCode = serviceActive ? `SC ${serviceCode}` : warningActive ? `W ${warningCode}` : "OK";
  ["hmiOverviewAlarmIndicator", "hmiTechnicalAlarmIndicator"].forEach((id) => {
    if ($(id)) $(id).setAttribute("class", `hmi-svg-state ${level}`);
  });
  ["v95-overview-state-code", "v95-tech-state-code"].forEach((id) => {
    if ($(id)) $(id).textContent = stateCode;
  });
}

function setSchematicMode(mode) {
  const selected = mode === "technical" ? "technical" : "overview";
  document.querySelectorAll("[data-schematic-view]").forEach((view) => {
    view.hidden = view.dataset.schematicView !== selected;
  });
  document.querySelectorAll("[data-schematic-mode]").forEach((button) => {
    button.classList.toggle("active", button.dataset.schematicMode === selected);
  });
  try { localStorage.setItem("open-dachs-schematic-mode", selected); } catch (_) { /* optional */ }
}

function openChangelog(event) {
  state.changelogTrigger = event?.currentTarget || document.activeElement;
  $("changelogModal").hidden = false;
  document.body.classList.add("modal-open");
  $("changelogClose").focus();
}

function closeChangelog() {
  if ($("changelogModal").hidden) return;
  $("changelogModal").hidden = true;
  document.body.classList.remove("modal-open");
  if (state.changelogTrigger instanceof HTMLElement) state.changelogTrigger.focus();
  state.changelogTrigger = null;
}

async function openCurrentFaultCatalog() {
  const code = activeMessageCode("servicecode", 100) || activeMessageCode("warncode", 600);
  showView("faultCatalogView");
  $("serviceCatalogSearch").value = code ? String(code) : "";
  await refreshServiceCatalog();
  $("serviceCatalogResults").scrollIntoView({ behavior: "smooth", block: "start" });
}

function dashboardField(block, key) {
  const derived = dashboardDerivedFields().find((item) => Number(item.block) === Number(block) && item.key === key);
  if (derived) return { ...derived, label: derived.label || derived.key, reserved: false, derived: true };
  const series = (state.schema?.series || []).find((item) => Number(item.block) === Number(block) && item.key === key);
  if (series) return { ...series, label: series.title, reserved: false };
  const blockSchema = (state.schema?.blocks || []).find((item) => Number(item.block) === Number(block));
  const field = (blockSchema?.fields || []).find((item) => item.key === key);
  return field ? { ...field, block: Number(block), label: field.label || field.key, unit: "" } : null;
}

function dashboardRow(block, key) {
  return valueIndex().get(`${Number(block)}:${key}`) || null;
}

function dashboardCards() {
  return state.dashboard.settings?.cards || state.dashboard.settings?.default_cards || [];
}

function dashboardDerivedFields() {
  return state.dashboard.settings?.derived_fields || state.schema?.dashboard?.derived_fields || [];
}

function operatingHoursPerStartDisplay() {
  const metric = state.live?.operating_hours_per_start;
  const ratio = Number(metric?.value);
  const available = metric?.available === true && Number.isFinite(ratio);
  const value = available
    ? `${ratio.toLocaleString("de-DE", { maximumFractionDigits: 1 })} Bh/Start`
    : "—";
  const extra = available
    ? `Block 22 · ${Number(metric.starts).toLocaleString("de-DE")} Starts`
    : "Block 22 · wartet auf gültige Betriebssekunden und Starts";
  return { value, extra };
}

function operatingHoursPerStartCard() {
  const { value, extra } = operatingHoursPerStartDisplay();
  return `<article class="metric-card metric-card-derived"><div class="metric-label">Betriebsstunden je Start</div><div class="metric-value">${escapeHtml(value)}</div><div class="metric-extra">${escapeHtml(extra)}</div></article>`;
}

function operatingHoursPerStartDetail() {
  const { value } = operatingHoursPerStartDisplay();
  return `<div class="detail-item detail-item-derived"><div class="detail-label">Betriebsstunden je Start</div><div class="detail-value">${escapeHtml(value)}</div></div>`;
}

function renderOverview() {
  const cards = dashboardCards();
  const configuredCards = cards.map((card) => {
    const field = dashboardField(card.block, card.key);
    if (field?.derived === true && field?.source === "operating_hours_per_start") {
      return operatingHoursPerStartCard();
    }
    const row = dashboardRow(card.block, card.key);
    const knownSeries = (state.schema?.series || []).find((item) => Number(item.block) === Number(card.block) && item.key === card.key);
    const invalid = knownSeries && isInvalidSensor(knownSeries.id, row);
    const label = field?.label || card.key;
    const value = row && !invalid ? formatValue(row.value, row.unit || field?.unit || "") : "—";
    return `<article class="metric-card"><div class="metric-label">${escapeHtml(label)}</div><div class="metric-value">${value}</div><div class="metric-extra">Block ${escapeHtml(card.block)} · ${row && !invalid ? escapeHtml(row.recorded_at) : "wartet auf Messung"}</div></article>`;
  }).join("");
  $("overviewCards").innerHTML = configuredCards || `<article class="metric-card metric-card-empty"><div class="metric-label">Keine Kacheln ausgewählt</div><div class="metric-extra">Als Admin über „Bearbeiten“ Werte hinzufügen.</div></article>`;
  const motor = ["motorstatus", "drehzahl", "wirkleistung", "betriebsstunden", "kuehlwasser", "regler"].map((id) => seriesValue(id)).filter((row, index) => row && !isInvalidSensor(["motorstatus", "drehzahl", "wirkleistung", "betriebsstunden", "kuehlwasser", "regler"][index], row));
  const motorCards = motor.map((row) => `<div class="detail-item"><div class="detail-label">${escapeHtml(row.label)}</div><div class="detail-value">${formatValue(row.value, row.unit)}</div></div>`);
  motorCards.push(operatingHoursPerStartDetail());
  $("motorStateCards").innerHTML = motorCards.join("");
  const system = ["servicecode", "warncode", "anzahl_warnungen", "anzahl_stoerungen"].map((id) => seriesValue(id)).filter(Boolean);
  $("systemStateCards").innerHTML = system.map((row) => `<div class="detail-item"><div class="detail-label">${escapeHtml(row.label)}</div><div class="detail-value">${formatValue(row.value, row.unit)}</div></div>`).join("") || `<p class="muted">Noch keine Statusdaten.</p>`;
  const ids = {
    "board-value-dachs-austritt":"dachs_austritt",
    "board-value-dachs-eintritt":"dachs_eintritt",
    "board-value-vorlauf":"vorlauf",
    "board-value-ruecklauf":"ruecklauf",
    "board-value-kuehlwasser":"kuehlwasser",
    "board-value-abgas-motor":"abgas_motor",
    "board-value-abgas-hka":"abgas_hka",
    "board-value-kapsel":"kapsel",
    "board-value-regler":"regler",
    "board-value-drehzahl":"drehzahl",
    "board-value-wirkleistung":"wirkleistung",
    "board-value-wirkleistung-soll":"wirkleistung_soll",
    "board-value-betriebsstunden":"betriebsstunden",
    "board-value-motorstatus":"motorstatus",
    "board-value-betriebsstunden-gesamt":"betriebsstunden_gesamt",
    "board-value-starts":"starts",
    "board-value-servicecode":"servicecode",
    "v95-overview-dachs-austritt":"dachs_austritt",
    "v95-overview-dachs-eintritt":"dachs_eintritt",
    "v95-overview-vorlauf":"vorlauf",
    "v95-overview-ruecklauf":"ruecklauf",
    "v95-overview-kuehlwasser":"kuehlwasser",
    "v95-overview-abgas-motor":"abgas_motor",
    "v95-overview-abgas-hka":"abgas_hka",
    "v95-overview-kapsel":"kapsel",
    "v95-overview-regler":"regler",
    "v95-overview-drehzahl":"drehzahl",
    "v95-overview-wirkleistung":"wirkleistung",
    "v95-overview-wirkleistung-soll":"wirkleistung_soll",
    "v95-overview-betriebsstunden":"betriebsstunden",
    "v95-overview-motorstatus":"motorstatus",
    "v95-overview-betriebsstunden-gesamt":"betriebsstunden_gesamt",
    "v95-overview-starts":"starts",
    "v95-tech-dachs-austritt":"dachs_austritt",
    "v95-tech-dachs-eintritt":"dachs_eintritt",
    "v95-tech-eintritt-inside":"dachs_eintritt",
    "v95-tech-kuehlwasser":"kuehlwasser",
    "v95-tech-abgas-motor":"abgas_motor",
    "v95-tech-abgas-hka":"abgas_hka",
    "v95-tech-kapsel":"kapsel",
    "v95-tech-regler":"regler",
    "v95-tech-drehzahl":"drehzahl",
    "v95-tech-wirkleistung":"wirkleistung",
    "v95-tech-wirkleistung-soll":"wirkleistung_soll",
    "v95-tech-betriebsstunden":"betriebsstunden",
    "v95-tech-motorstatus":"motorstatus",
    "v95-tech-betriebsstunden-gesamt":"betriebsstunden_gesamt",
  };
  Object.entries(ids).forEach(([elementId, seriesId]) => setText(elementId, seriesId));
  setElectricalText("spannung", "V", ["board-value-voltage", "v95-overview-voltage", "v95-tech-voltage"], 1);
  setElectricalText("strom", "A", ["board-value-current", "v95-overview-current", "v95-tech-current"], 1);
  setElectricalText("impedanz", "Ohm", ["board-value-impedance", "v95-tech-impedance"], 2);
  ["board-value-frequency", "v95-overview-frequency", "v95-tech-frequency"].forEach((id) => setText(id, "frequenz"));
  renderSootFilterEstimate();
  renderHmiMessages();
  renderOverviewPower();
  renderMaintenanceStatus(state.live?.maintenance || {});
}

function allDashboardFields() {
  const fields = [];
  const seen = new Set();
  const add = (field) => {
    const identity = `${Number(field.block)}:${field.key}`;
    if (!field.key || seen.has(identity)) return;
    seen.add(identity);
    fields.push({
      block: Number(field.block),
      key: field.key,
      label: field.label || field.title || field.key,
      reserved: Boolean(field.reserved),
      derived: Boolean(field.derived),
      source: field.source || "",
    });
  };
  for (const field of dashboardDerivedFields()) add(field);
  for (const series of (state.schema?.series || [])) add({ ...series, label: series.title });
  for (const block of (state.schema?.blocks || [])) {
    for (const field of (block.fields || [])) add({ ...field, block: block.block });
  }
  return fields.sort((left, right) => left.block - right.block || left.label.localeCompare(right.label, "de"));
}

function renderDashboardEditor() {
  const cards = state.dashboard.editCards;
  const maximum = Number(state.dashboard.settings?.max_cards || 24);
  $("dashboardCardCount").textContent = `${cards.length}/${maximum}`;
  $("dashboardCardList").innerHTML = cards.map((card, index) => {
    const field = dashboardField(card.block, card.key);
    const source = field?.derived ? `Berechnet aus Block ${escapeHtml(card.block)}` : `Block ${escapeHtml(card.block)} · ${escapeHtml(card.key)}`;
    return `<article class="dashboard-edit-card" draggable="true" data-dashboard-index="${index}"><span class="drag-handle" aria-hidden="true">⠿</span><div><strong>${escapeHtml(field?.label || card.key)}</strong><small>${source}</small></div><div class="dashboard-card-actions"><button type="button" data-dashboard-move="up" data-dashboard-index="${index}" aria-label="Nach oben">↑</button><button type="button" data-dashboard-move="down" data-dashboard-index="${index}" aria-label="Nach unten">↓</button><button class="danger" type="button" data-dashboard-remove="${index}" aria-label="Entfernen">×</button></div></article>`;
  }).join("") || `<p class="muted">Noch keine Kachel gewählt.</p>`;

  const selected = new Set(cards.map((card) => `${Number(card.block)}:${card.key}`));
  const query = $("dashboardFieldSearch").value.trim().toLocaleLowerCase("de");
  const available = allDashboardFields().filter((field) => {
    if (selected.has(`${field.block}:${field.key}`)) return false;
    const haystack = `${field.block} ${field.label} ${field.key}`.toLocaleLowerCase("de");
    return !query || haystack.includes(query);
  });
  $("dashboardFieldList").innerHTML = available.slice(0, 160).map((field, index) => `<button type="button" data-dashboard-add="${index}"><span><strong>${escapeHtml(field.label)}</strong><small>${field.derived ? `Berechnet aus Block ${field.block}` : `Block ${field.block} · ${escapeHtml(field.key)}${field.reserved ? " · Reserve" : ""}`}</small></span><b>+</b></button>`).join("") || `<p class="muted">Kein weiterer passender Wert.</p>`;
  $("dashboardFieldList").dataset.fields = JSON.stringify(available.slice(0, 160).map((field) => ({ block: field.block, key: field.key })));
  $("dashboardSave").disabled = cards.length > maximum;

  $("dashboardCardList").querySelectorAll("[draggable=true]").forEach((element) => {
    element.addEventListener("dragstart", () => { state.dashboard.draggedIndex = Number(element.dataset.dashboardIndex); });
    element.addEventListener("dragover", (event) => event.preventDefault());
    element.addEventListener("drop", (event) => {
      event.preventDefault();
      const from = state.dashboard.draggedIndex;
      const to = Number(element.dataset.dashboardIndex);
      if (!Number.isInteger(from) || from === to) return;
      const [card] = state.dashboard.editCards.splice(from, 1);
      state.dashboard.editCards.splice(to, 0, card);
      state.dashboard.draggedIndex = null;
      renderDashboardEditor();
    });
  });
}

function openDashboardEditor() {
  if (state.user?.role !== "admin") return;
  state.dashboard.editCards = dashboardCards().map((card) => ({ block: Number(card.block), key: card.key }));
  $("dashboardFieldSearch").value = "";
  $("dashboardEditor").hidden = false;
  renderDashboardEditor();
}

function closeDashboardEditor() {
  $("dashboardEditor").hidden = true;
  state.dashboard.draggedIndex = null;
}

function moveDashboardCard(index, delta) {
  const target = index + delta;
  if (index < 0 || target < 0 || target >= state.dashboard.editCards.length) return;
  const [card] = state.dashboard.editCards.splice(index, 1);
  state.dashboard.editCards.splice(target, 0, card);
  renderDashboardEditor();
}

async function saveDashboardEditor() {
  const button = $("dashboardSave");
  button.disabled = true;
  try {
    state.dashboard.settings = await api("/api/settings/dashboard", {
      method: "POST",
      body: JSON.stringify({ cards: state.dashboard.editCards }),
    });
    closeDashboardEditor();
    renderOverview();
    toast("Dashboard-Kacheln gespeichert. Neue Blöcke erscheinen nach dem nächsten langsamen Lesezyklus.");
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

function renderServiceCatalog(data) {
  const status = $("serviceCatalogStatus");
  if (!data?.available) {
    status.textContent = "Der integrierte Open-Dachs-Klartextkatalog konnte nicht geladen werden.";
    $("serviceCatalogResults").innerHTML = `<p class="muted">Bitte Installation und Paketdaten prüfen.</p>`;
    return;
  }
  const detailStatus = data.details_available
    ? " · lokale Ursachen und Maßnahmen verfügbar"
    : " · Klartexte integriert; Ursachen und Maßnahmen optional";
  status.textContent = `${data.count} Open-Dachs-Klartexte verfügbar${detailStatus}${data.query ? ` · Treffer für „${data.query}“` : ""}.`;
  $("serviceCatalogResults").innerHTML = (data.items || []).map((entry) => {
    const causes = renderDiagnosticCodeList("Mögliche Ursachen", entry.causes);
    const measures = renderDiagnosticCodeList("Mögliche Maßnahmen", entry.measures);
    const detail = causes || measures
      ? `${causes}${measures}`
      : `<p class="muted">Klartext ist integriert. Für diesen Code sind keine zusätzlichen Ursachen oder Maßnahmen hinterlegt.</p>`;
    return `<details class="service-catalog-entry" ${String(entry.code) === "163" && data.query ? "open" : ""}><summary><strong>SC ${escapeHtml(entry.code)}</strong><span>${escapeHtml(entry.text)}</span><small>${(entry.causes || []).length} Ursachen · ${(entry.measures || []).length} Maßnahmen</small></summary><div class="service-catalog-detail">${detail}</div></details>`;
  }).join("") || `<p class="muted">Keine passenden Servicecodes gefunden.</p>`;
}

async function refreshServiceCatalog() {
  const query = $("serviceCatalogSearch")?.value.trim() || "";
  try {
    const params = new URLSearchParams({ q: query, limit: "250" });
    renderServiceCatalog(await api(`/api/service-codes?${params.toString()}`));
    state.serviceCatalogLoaded = true;
  } catch (error) {
    $("serviceCatalogStatus").textContent = error.message;
  }
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
  mode.textContent = "Admin · LIVE · PW4 automatisch";
  mode.className = "status-pill warn";
  button.textContent = "Sollwert schreiben";
  note.textContent = "PW4 wird beim Schreiben frisch aus der Anlage berechnet; Prüfung und Rücklesekontrolle bleiben aktiv.";
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
  if (viewId === "systemView" && state.user?.role !== "admin") return;
  if (state.selectedView === "settingsView" && viewId !== "settingsView") {
    clearAuthPreview("Beim Verlassen der Einstellung verworfen");
  }
  if (state.selectedView === "maintenanceView" && viewId !== "maintenanceView") {
    clearMaintenancePw4("Beim Verlassen der Wartung verworfen");
  }
  state.selectedView = viewId;
  document.querySelectorAll(".app-view").forEach((view) => { view.hidden = view.id !== viewId; });
  document.querySelectorAll(".tab-button").forEach((button) => button.classList.toggle("active", button.dataset.view === viewId));
  if (viewId === "monitorView") refreshCharts();
  if (viewId === "auditView") refreshAudit();
  if (viewId === "maintenanceView") refreshMaintenance(true);
  if (viewId === "faultCatalogView" && !state.serviceCatalogLoaded) refreshServiceCatalog();
  if (viewId === "systemView") refreshSystemView();
  if (viewId === "backupView") {
    renderBackupBlockList();
    updateRestoreMode();
    if (state.user?.role === "admin") refreshBackupArchive();
  }
}

function maintenanceNumber(value, suffix = "") {
  const number = numeric(value);
  if (number === null) return "—";
  return `${new Intl.NumberFormat("de-DE", { maximumFractionDigits: 1 }).format(number)}${suffix}`;
}

function renderMaintenanceStatus(status = {}) {
  const level = ["green", "yellow", "red"].includes(status.level) ? status.level : "unknown";
  [$("maintenanceHeader"), $("maintenanceStatusPanel")].forEach((element) => {
    if (element) element.className = element.className.replace(/maintenance-(green|yellow|red|unknown)/g, "").trim() + ` maintenance-${level}`;
  });
  $("maintenanceHeaderValue").textContent = `${maintenanceNumber(status.remaining_hours, " Bh")} | ${maintenanceNumber(status.remaining_days, " Tage")}`;
  $("maintenanceHeader").title = status.title || "Wartungsstatus noch nicht gelesen";
  $("maintenanceStatusTitle").textContent = status.title || "Wartungsstatus noch nicht gelesen";
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
  const isAdmin = state.user?.role === "admin";
  $("maintenanceReportRows").innerHTML = reports.map((item) => {
    const summary = item.summary || {};
    const snapshot = item.snapshot || {};
    const captured = (snapshot.captured_blocks || []).length;
    const attempted = (snapshot.attempted_blocks || []).length;
    const snapshotText = attempted ? `<small>Snapshot ${captured}/${attempted} Blöcke</small>` : "";
    const presentation = maintenanceStatusPresentation(item);
    const backup = maintenanceBackupMetadata(item);
    const backupCell = backup ? maintenanceBackupTableMarkup(backup) : `<span class="status-pill neutral">Altbericht ohne Backup</span>`;
    const deletable = ["draft", "completed"].includes(item.status);
    const deleteAction = isAdmin && deletable ? `<button class="danger report-delete" type="button" data-maintenance-delete="${escapeHtml(item.id)}">Löschen</button>` : "—";
    const exportLinks = item.status === "completing"
      ? `<span class="muted">Nach Abschluss verfügbar</span>`
      : `<a href="${escapeHtml(appUrl(`/api/maintenance/reports/${encodeURIComponent(item.id)}/export/html`))}">HTML</a><a href="${escapeHtml(appUrl(`/api/maintenance/reports/${encodeURIComponent(item.id)}/export/pdf`))}">PDF</a><a href="${escapeHtml(appUrl(`/api/maintenance/reports/${encodeURIComponent(item.id)}/export/json`))}">JSON</a>`;
    return `<tr class="maintenance-report-${escapeHtml(item.status || "unknown")}"><td><button class="history-ring" type="button" data-maintenance-report="${escapeHtml(item.id)}">#${escapeHtml(item.id)}</button></td><td>${escapeHtml(formatArchiveDate(item.created_at))}${snapshotText}</td><td><span class="status-pill ${presentation.tone}">${escapeHtml(presentation.label)}</span></td><td>${escapeHtml(item.technician || "—")}</td><td>${formatValue(summary.operating_hours, "Bh")}</td><td>${backupCell}</td><td class="report-links">${exportLinks}</td><td>${deleteAction}</td></tr>`;
  }).join("") || `<tr><td colspan="8" class="muted">Noch keine Berichte.</td></tr>`;
}

function maintenanceStatusPresentation(item = {}) {
  if (item.status === "completed") return { label: item.completion_mode === "demo" ? "Demo abgeschlossen" : "Abgeschlossen", tone: "ok" };
  if (item.status === "completing") return { label: "Abschluss läuft", tone: "warn" };
  if (item.status === "uncertain") return { label: "Zielzustand unklar – prüfen", tone: "error" };
  if (item.status === "draft") return { label: "Entwurf", tone: "warn" };
  return { label: item.status || "Unbekannt", tone: "error" };
}

function maintenanceBackupMetadata(item = {}) {
  const primary = item.backup_archive;
  const fallback = item.maintenance_backup ?? item.backup;
  const candidate = primary && typeof primary === "object" ? primary
    : fallback && typeof fallback === "object" ? fallback : {};
  const id = candidate.id ?? (typeof primary !== "object" ? primary : null) ?? item.backup_archive_id ?? item.backup_id;
  if (id === null || id === undefined || id === "") return null;
  return {
    ...candidate,
    id,
    created_at: candidate.created_at ?? item.backup_created_at,
    requested_targets: candidate.requested_targets ?? item.backup_requested_targets,
    successful_targets: candidate.successful_targets ?? item.backup_successful_targets,
    failed_targets: candidate.failed_targets ?? item.backup_failed_targets,
    image_sha256: candidate.image_sha256 ?? item.backup_image_sha256,
    file_sha256: candidate.file_sha256 ?? item.backup_file_sha256,
  };
}

function maintenanceBackupTableMarkup(backup) {
  const requested = archiveTargetCount(backup, "requested");
  const successful = archiveTargetCount(backup, "successful");
  const failed = archiveTargetCount(backup, "failed");
  const complete = archiveEntryReady(backup);
  const identity = state.user?.role === "admin"
    ? `<button type="button" class="button-link" data-open-backup="${escapeHtml(backup.id)}">Backup #${escapeHtml(backup.id)}</button>`
    : `<strong>Backup #${escapeHtml(backup.id)}</strong>`;
  return `<div class="maintenance-backup-cell">
    ${identity}
    <small class="${complete ? "backup-integrity-ok" : "backup-integrity-error"}">${successful}/${requested || "?"}${failed ? ` · ${failed} Fehler` : ""}</small>
  </div>`;
}

function renderMaintenanceBackupSummary(item) {
  const container = $("maintenanceBackupSummary");
  if (!container) return;
  const backup = maintenanceBackupMetadata(item);
  if (!backup) {
    container.innerHTML = `<div class="maintenance-backup-card legacy"><div><p class="eyebrow">SICHERHEITSBACKUP</p><strong>Altbericht ohne verknüpftes Pflichtbackup</strong><small>Berichte aus früheren Versionen bleiben weiterhin lesbar.</small></div></div>`;
    return;
  }
  const requested = archiveTargetCount(backup, "requested");
  const successful = archiveTargetCount(backup, "successful");
  const failed = archiveTargetCount(backup, "failed");
  const ready = archiveEntryReady(backup);
  const digest = String(backup.image_sha256 || "");
  const archiveAction = state.user?.role === "admin" ? `<button type="button" data-open-backup="${escapeHtml(backup.id)}">Im Backup-Archiv anzeigen</button>` : "";
  container.innerHTML = `<div class="maintenance-backup-card ${ready ? "verified" : "invalid"}">
    <div><p class="eyebrow">SICHERHEITSBACKUP #${escapeHtml(backup.id)}</p><strong>${ready ? "Vollständig und geprüft" : "Backupstatus prüfen"}</strong><small>${escapeHtml(formatArchiveDate(backup.created_at))} · ${successful}/${requested || "?"} Ziele${failed ? ` · ${failed} fehlgeschlagen` : ""}</small></div>
    <code title="${escapeHtml(digest)}">SHA-256 ${escapeHtml(digest || "nicht gemeldet")}</code>
    ${archiveAction}
  </div>`;
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
    clearMaintenancePw4();
    state.maintenance.current = item;
    renderMaintenanceEditor();
    await refreshMaintenance(false);
    if (state.user?.role === "admin") await refreshBackupArchive(true);
    const snapshot = item.snapshot || {};
    const backup = maintenanceBackupMetadata(item);
    const backupText = backup ? ` Pflichtbackup #${backup.id}: ${archiveTargetCount(backup, "successful")}/${archiveTargetCount(backup, "requested") || "?"} Ziele geprüft.` : "";
    toast(`Anlagenzustand schreibfrei gelesen: ${(snapshot.captured_blocks || []).length}/${(snapshot.attempted_blocks || []).length} Blöcke lokal archiviert.${backupText}`);
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = "Wartung starten & Pflichtbackup erstellen"; }
}

async function loadMaintenanceReport(reportId) {
  try {
    clearTimeout(state.maintenance.autosaveTimer);
    state.maintenance.autosaveTimer = null;
    if (Number(state.maintenance.current?.id) !== Number(reportId)) clearMaintenancePw4();
    state.maintenance.current = await api(`/api/maintenance/reports/${reportId}`);
    renderMaintenanceEditor();
  } catch (error) { toast(error.message); }
}

async function deleteMaintenanceReport(reportId) {
  if (state.user?.role !== "admin") return;
  const item = (state.maintenance.reports || []).find((candidate) => Number(candidate.id) === Number(reportId));
  if (item && !["draft", "completed"].includes(item.status)) {
    toast(item.status === "uncertain" ? "Dieser Bericht hat einen unklaren Zielzustand und darf nicht gelöscht oder erneut abgeschlossen werden." : "Ein laufender Wartungsabschluss darf nicht gelöscht oder erneut gestartet werden.", "error");
    return;
  }
  const status = item?.status === "completed" ? "abgeschlossen" : "offen";
  const backup = maintenanceBackupMetadata(item || {});
  const backupNotice = backup
    ? ` Das verknüpfte Backup #${backup.id} bleibt unverändert im geschützten Backup-Archiv erhalten.`
    : " Bereits vorhandene Sicherheitsbackups bleiben unverändert im geschützten Backup-Archiv erhalten.";
  const question = `Wartung #${reportId} (${status}) wirklich dauerhaft löschen? Snapshot, Protokoll und Exporte werden aus dem lokalen Pi-Archiv entfernt. Ein bereits erfolgter MSR2-Abschluss wird dadurch nicht rückgängig gemacht.${backupNotice}`;
  if (!window.confirm(question)) return;
  clearTimeout(state.maintenance.autosaveTimer);
  state.maintenance.autosaveTimer = null;
  const button = document.querySelector(`[data-maintenance-delete="${reportId}"]`);
  if (button) button.disabled = true;
  try {
    await api(`/api/maintenance/reports/${reportId}`, { method: "DELETE" });
    if (Number(state.maintenance.current?.id) === Number(reportId)) {
      state.maintenance.current = null;
      renderMaintenanceEditor();
    }
    await refreshMaintenance(false);
    toast(`Wartung #${reportId} wurde gelöscht. Das Backup-Archiv blieb erhalten.`);
  } catch (error) {
    toast(error.message);
    if (button) button.disabled = false;
  }
}

function renderMaintenanceEditor() {
  const item = state.maintenance.current;
  if (!item) { clearMaintenancePw4(); $("maintenanceEditor").hidden = true; $("maintenanceBackupSummary").innerHTML = ""; return; }
  $("maintenanceEditor").hidden = false;
  $("maintenanceReportNumber").textContent = `#${item.id}`;
  $("maintenanceReportTitle").textContent = item.status === "draft" ? "Wartungsentwurf bearbeiten"
    : item.status === "completing" ? "Wartungsabschluss läuft"
    : item.status === "uncertain" ? "Zielzustand unklar – fachlich prüfen"
    : item.completion_mode === "demo" ? "Abgeschlossener Demo-Wartungsbericht" : "Abgeschlossener Wartungsbericht";
  const snapshot = item.snapshot || {};
  const snapshotText = (snapshot.attempted_blocks || []).length ? ` · Snapshot ${(snapshot.captured_blocks || []).length}/${snapshot.attempted_blocks.length} Blöcke` : "";
  $("maintenanceReportMeta").textContent = `Anlagenstand ${new Date(item.created_at).toLocaleString("de-DE")} · Seriennummer ${item.summary?.serial_number || "—"} · ${item.summary?.operating_hours || "—"} Bh${snapshotText}`;
  renderMaintenanceBackupSummary(item);
  const comparison = item.comparison;
  $("maintenanceComparison").innerHTML = comparison ? `<div class="section-head"><div><p class="eyebrow">VERGLEICH MIT BERICHT #${comparison.report_id}</p><h4>Zählerentwicklung seit ${escapeHtml(new Date(comparison.created_at).toLocaleString("de-DE"))}</h4></div></div><div class="maintenance-comparison-grid">${(comparison.rows || []).map((row) => `<div><span>${escapeHtml(row.label)}</span><strong>${row.delta === null || row.delta === undefined ? "—" : `${numeric(row.delta) >= 0 ? "+" : ""}${escapeHtml(row.delta)}`}</strong><small>${escapeHtml(row.previous ?? "—")} → ${escapeHtml(row.current ?? "—")}</small></div>`).join("")}</div>` : `<p class="source-note">Dies ist der erste archivierte Bericht; ein Zählervergleich erscheint ab dem nächsten Bericht.</p>`;
  ["Html", "Pdf", "Json"].forEach((name) => {
    const link = $(`maintenanceExport${name}`);
    if (item.status === "completing") {
      link.removeAttribute("href");
      link.setAttribute("aria-disabled", "true");
      link.setAttribute("tabindex", "-1");
      link.classList.add("disabled");
      return;
    }
    link.href = appUrl(`/api/maintenance/reports/${encodeURIComponent(item.id)}/export/${name.toLowerCase()}`);
    link.removeAttribute("aria-disabled");
    link.removeAttribute("tabindex");
    link.classList.remove("disabled");
  });
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
  if (!liveCompletion) clearMaintenancePw4();
  $("maintenanceConfirmation").placeholder = item.confirmation_text || (liveCompletion ? "WARTUNG ABSCHLIESSEN" : "DEMO ABSCHLIESSEN");
  $("maintenanceComplete").textContent = liveCompletion ? "Wartung endgültig abschließen" : "Demolauf abschließen";
  $("maintenanceComplete").classList.toggle("danger", liveCompletion);
  $("maintenanceComplete").classList.toggle("primary", !liveCompletion);
  $("maintenanceSaveHint").textContent = item.status === "draft" ? "Änderungen werden lokal auf dem Pi gespeichert."
    : item.status === "completing" ? "Abschluss läuft. Nicht neu laden, löschen oder erneut ausführen."
    : item.status === "uncertain" ? "Zielzustand unklar – vor jedem weiteren Schritt Regler, ACK, Readback und Audit fachlich prüfen."
    : item.completion_mode === "demo" ? `Demolauf lokal abgeschlossen am ${new Date(item.completed_at).toLocaleString("de-DE")} · MSR2 unverändert` : `Abgeschlossen am ${new Date(item.completed_at).toLocaleString("de-DE")}`;
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
  const requestPayload = {
    protocol: maintenanceProtocolFromForm(),
    auth_level: Number($("maintenanceAuthLevel").value || -1),
    pass4: $("maintenancePass4").value,
    confirmation: $("maintenanceConfirmation").value,
  };
  clearMaintenancePw4("Nach Abschlussversuch verworfen");
  try {
    state.maintenance.current = await api(`/api/maintenance/reports/${item.id}/complete`, { method: "POST", body: JSON.stringify(requestPayload) });
    renderMaintenanceEditor();
    await refreshMaintenance(false);
    await refreshAudit();
    toast(liveCompletion ? "Wartung geschrieben, bestätigt und per Readback geprüft." : "Demolauf lokal abgeschlossen. Es wurden keine Reglerdaten geschrieben.");
  } catch (error) {
    try {
      state.maintenance.current = await api(`/api/maintenance/reports/${item.id}`);
      renderMaintenanceEditor();
      await refreshMaintenance(false);
    } catch (_) { /* Der ursprüngliche Fehler bleibt maßgeblich. */ }
    toast(error.message, "error");
  }
  finally { button.disabled = state.maintenance.current?.status !== "draft"; }
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
  const networkTabs = (state.schema?.network_protection || []).map((target) => {
    const readOnly = target.writable === false ? " · nur lesen" : "";
    const itemLabel = target.live_values ? "Werte" : "Felder";
    return `<button role="tab" data-cpu="${target.cpu}" data-block="${target.block}" class="critical-tab ${state.selectedCpu === target.cpu && state.selectedBlock === target.block ? "active" : ""}">CPU ${target.cpu} · B${target.block} · ${escapeHtml(target.tab_label || "Netzschutz")} <small>(${(target.fields || []).length} ${itemLabel}${readOnly})</small></button>`;
  }).join("");
  $("settingsTabs").innerHTML = regulatorTabs + networkTabs;
}

async function loadBlock(block, cpu = 0) {
  const requestedBlock = Number(block);
  const requestedCpu = Number(cpu);
  state.selectedBlock = requestedBlock;
  state.selectedCpu = requestedCpu;
  const requestedTarget = requestedCpu
    ? (state.schema?.network_protection || []).find((item) => Number(item.cpu) === requestedCpu && Number(item.block) === requestedBlock)
    : null;
  $("saveBlockButton").hidden = state.user?.role !== "admin" || requestedTarget?.writable === false;
  const targetText = requestedCpu ? `CPU ${requestedCpu}, Block ${requestedBlock}` : `Block ${requestedBlock}`;
  $("blockReadStatus").textContent = `Lese ${targetText} …`;
  $("settingsTabs").querySelectorAll("button").forEach((button) => button.classList.toggle(
    "active",
    Number(button.dataset.cpu || 0) === requestedCpu && Number(button.dataset.block) === requestedBlock,
  ));
  try {
    const loaded = await api(requestedCpu ? `/api/network-protection/${requestedCpu}/${requestedBlock}` : `/api/block/${requestedBlock}`);
    if (state.selectedCpu !== requestedCpu || state.selectedBlock !== requestedBlock) return;
    state.block = loaded;
    $("selectedBlockEyebrow").textContent = requestedCpu ? `CPU ${requestedCpu} · BLOCK ${requestedBlock} · NETZSCHUTZ` : `BLOCK ${requestedBlock}`;
    $("selectedBlockTitle").textContent = state.block.name;
    $("blockReadStatus").textContent = state.block.ok ? `OK · ${state.block.rtt_ms} ms` : `Fehler: ${state.block.status}`;
    $("saveBlockButton").hidden = state.user?.role !== "admin" || state.block.writable === false;
    document.querySelector(".settings-panel")?.classList.toggle("critical-settings", Boolean(state.block.critical));
    renderFields();
  } catch (error) {
    if (state.selectedCpu === requestedCpu && state.selectedBlock === requestedBlock) {
      $("blockReadStatus").textContent = error.message;
    }
  }
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
  const shutdownRows = (history?.shutdowns || []).map((entry) => `<tr class="${entry.timestamp_plausible ? "" : "history-empty"}"><td>${entry.index}</td><td>${escapeHtml(entry.timestamp_text || "—")}</td><td>${entry.has_event ? `${escapeHtml(entry.code)} (${escapeHtml(entry.code_hex)})` : "—"}</td><td>${escapeHtml(entry.reason || "—")}</td></tr>`).join("");
  $("settingsFields").innerHTML = `<div class="message-history-card run-history-card">
    <div class="run-summary-grid">${metricCards}</div>
    <div class="run-axis"><span>00:00</span><span>04:00</span><span>08:00</span><span>12:00</span><span>16:00</span><span>20:00</span><span>24:00</span></div>
    <div class="run-days">${dayRows}</div>
    <section class="run-shutdowns"><h4>Letzte Abschaltgründe</h4><div class="table-wrap"><table class="data-table"><thead><tr><th>Eintrag</th><th>Zeitstempel</th><th>Abschaltcode</th><th>Text des Abschaltcodes</th></tr></thead><tbody>${shutdownRows}</tbody></table></div></section>
    <p class="source-note">Gemeinsame Auswertung der Blöcke 28 (Ring/Starts/Tage 1–5), 30 (Tage 6–7/Abschaltungen), 31 (aktueller Tag) und 32 (Summenwerte).</p>
    ${renderRawFieldsDetails(`Rohfelder des aktuell gewählten Teilblocks ${state.selectedBlock}; das Diagramm liest immer alle vier zusammengehörigen Blöcke.`)}
  </div>`;
}

function renderFieldEditor(field, admin) {
  const value = String(field.edit_value ?? field.value ?? "");
  const rawValue = String(field.raw ?? "");
  const choices = Array.isArray(field.choices) ? field.choices : [];
  const editorId = `field-${encodeURIComponent(field.key)}`;
  if (!choices.length) {
    const rawModeAvailable = state.selectedCpu > 0 && state.selectedBlock === 20 && rawValue !== "";
    const editor = `<input id="${editorId}" data-key="${escapeHtml(field.key)}" data-baseline="${escapeHtml(value)}" data-raw-baseline="${escapeHtml(rawValue)}" data-raw-mode="false" value="${escapeHtml(value)}" inputmode="decimal" ${admin ? "" : "disabled"}>`;
    if (!rawModeAvailable) return editor;
    return `<div class="field-number-editor">
      ${editor}
      <label class="raw-mode-toggle"><input type="checkbox" data-raw-mode-toggle ${admin ? "" : "disabled"}> Rohwert bearbeiten (${escapeHtml(rawValue)})</label>
    </div>`;
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
  if (field.write !== false && Array.isArray(field.choices) && field.choices.length) notes.push("Bekannte Auswahlwerte; die manuelle Rohwert-Eingabe bleibt verfügbar.");
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

function bindRawEditors(container) {
  container.querySelectorAll("[data-raw-mode-toggle]").forEach((toggle) => {
    const wrapper = toggle.closest(".field-number-editor");
    const editor = wrapper?.querySelector("[data-key]");
    if (!editor) return;
    toggle.addEventListener("change", () => {
      if (toggle.checked) {
        editor.dataset.displayDraft = editor.value;
        editor.value = editor.dataset.rawDraft ?? editor.dataset.rawBaseline ?? "";
        editor.dataset.rawMode = "true";
        editor.inputMode = "text";
        if (!editor.disabled) editor.focus();
      } else {
        editor.dataset.rawDraft = editor.value;
        editor.value = editor.dataset.displayDraft ?? editor.dataset.baseline ?? "";
        editor.dataset.rawMode = "false";
        editor.inputMode = "decimal";
      }
    });
  });
}

function renderReadOnlyFieldValue(field) {
  const value = field.value === null || field.value === undefined ? "—" : field.value;
  const raw = field.raw === null || field.raw === undefined ? "—" : field.raw;
  return `<div class="network-readonly-value"><strong>${escapeHtml(value)}${field.unit ? ` ${escapeHtml(field.unit)}` : ""}</strong><span>Rohwert ${escapeHtml(raw)}</span></div>`;
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
  const readOnlyTarget = state.block?.writable === false;
  const warningText = readOnlyTarget
    ? `${state.block.read_only_reason || "Dieser Block ist nur lesbar."} Die Anzeige löst weder Authentifizierung noch ein Schreibtelegramm aus.`
    : state.selectedBlock === 20
      ? "Besonders sicherheitsrelevante Schutzkonfiguration. Der originale Vollblock-Schreibdienst ist belegt, am aktuellen Gerät aber noch nicht durch einen Live-Write erprobt. Schreiben erfolgt nur mit Admin-Haken, Auth, bytegenauem CAS, ACK und vollständigem Readback; für exakte Expertenwerte gibt es je Feld den Rohwert-Schalter."
      : "Besonders sicherheitsrelevante Einstellungen. Die rote Markierung schützt vor Verwechslung mit normalen Reglerfeldern; Schreiben erfolgt wie bei allen Registern nur mit Admin-Haken, Auth, ACK und Readback.";
  const warning = critical ? `<div class="network-protection-warning"><strong>NETZSCHUTZ · CPU ${state.selectedCpu} · BLOCK ${state.selectedBlock}${readOnlyTarget ? " · NUR LESEN" : ""}</strong><span>${escapeHtml(warningText)}</span></div>` : "";
  $("settingsFields").innerHTML = warning + fields.map((field) => `<div class="register-field ${admin && field.write !== false ? "" : "readonly"} ${critical || field.critical ? "critical-field" : ""}">
    <div class="register-field-head"><label for="field-${encodeURIComponent(field.key)}">${escapeHtml(field.label || field.key)}</label><small>${escapeHtml(field.type || "")} · ${field.size} B</small></div>
    ${field.write === false ? renderReadOnlyFieldValue(field) : renderFieldEditor(field, admin)}
    ${renderFieldHelp(field)}
    <div class="field-meta">${escapeHtml(field.key)} · Offset ${field.offset ?? "?"} · ${escapeHtml(field.unit || "")}${state.selectedCpu > 0 && field.raw !== null && field.raw !== undefined ? ` · Rohwert ${escapeHtml(field.raw)}` : ""}</div>
  </div>`).join("") || `<p class="muted">Keine dekodierten Felder für diesen Block.</p>`;
  bindChoiceEditors($("settingsFields"));
  bindRawEditors($("settingsFields"));
}

async function saveBlock() {
  if (state.user?.role !== "admin" || !state.block) return;
  if (state.block.writable === false) {
    return toast(`CPU ${state.selectedCpu}, Block ${state.selectedBlock} ist nur lesbar.`);
  }
  const changes = [];
  $("settingsFields").querySelectorAll("[data-key]").forEach((editor) => {
    let value = editor.value;
    let compareValue = value;
    if (editor.dataset.rawMode === "true") {
      compareValue = value;
      value = `raw:${compareValue}`;
      if (String(compareValue) !== String(editor.dataset.rawBaseline)) changes.push({ key: editor.dataset.key, value });
      return;
    }
    if (editor.dataset.editor === "choice" && value === "__raw__") {
      compareValue = editor.closest(".field-choice-editor")?.querySelector("[data-choice-raw]")?.value ?? "";
      value = `raw:${compareValue}`;
    }
    if (String(compareValue) !== String(editor.dataset.baseline)) changes.push({ key: editor.dataset.key, value });
  });
  if (!changes.length) return toast("Keine Änderungen vorbereitet.");
  try {
    const endpoint = state.selectedCpu ? `/api/network-protection/${state.selectedCpu}/${state.selectedBlock}` : `/api/block/${state.selectedBlock}`;
    const result = await api(endpoint, { method:"POST", body:JSON.stringify({
      changes,
      auth_level: Number($("authLevel").value || -1),
      pass4: $("pass4").value,
      write_enabled: $("writeEnabled").checked,
    }) });
    const target = state.selectedCpu ? `Netzschutz CPU ${state.selectedCpu}` : `Block ${state.selectedBlock}`;
    if (result.written && result.readback_ok) {
      toast(`${target} geschrieben und Readback bestätigt.`);
    } else if (result.dry_run) {
      toast(`Dry-Run für ${target} gespeichert – Hardware wurde nicht geändert.`);
    } else if (result.readback_ok && result.write_attempted === false) {
      toast(`${target} war bereits unverändert; kein Auth- oder Schreibtelegramm gesendet.`);
    } else if (result.write_attempted) {
      toast(`ACHTUNG: Schreibtelegramm für ${target} wurde gesendet, aber nicht sicher bestätigt. Zielzustand prüfen!`, "error");
    } else {
      toast(`Schreiben von ${target} fehlgeschlagen; es wurde kein bestätigter Zielzustand erreicht.`, "error");
    }
    await loadBlock(state.selectedBlock, state.selectedCpu);
    await refreshAudit();
  } catch (error) {
    const audit = error.payload?.audit || error.payload;
    if (audit?.write_attempted && !audit?.readback_ok) {
      toast(`ACHTUNG: Schreibtelegramm wurde gesendet, aber nicht sicher bestätigt. Zielzustand prüfen! ${error.message}`, "error");
    } else {
      toast(error.message, "error");
    }
  }
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

function clearAuthPreview(status = "Noch nicht gelesen") {
  state.authPreviewGeneration += 1;
  if (state.authPreviewClearTimer) clearTimeout(state.authPreviewClearTimer);
  state.authPreviewClearTimer = null;
  state.authPreview = null;
  if ($("pass4")) $("pass4").value = "";
  if ($("authPreviewSerial")) {
    renderAuthPreview(null);
    $("authPreviewStatus").textContent = status;
  }
}

async function refreshAuthPreview() {
  if (state.user?.role !== "admin" || state.selectedView !== "settingsView") return;
  const username = state.user.username;
  clearAuthPreview("Lese Block 20 und 22 …");
  const generation = state.authPreviewGeneration;
  const status = $("authPreviewStatus");
  status.className = "muted";
  try {
    const preview = await api("/api/auth-preview");
    if (generation !== state.authPreviewGeneration
      || state.user?.role !== "admin"
      || state.user?.username !== username
      || state.selectedView !== "settingsView") return;
    if (!preview?.ok || !/^\d{4}$/.test(String(preview.pw4 || ""))) {
      throw new Error("PW4 konnte nicht sicher berechnet werden");
    }
    renderAuthPreview(preview);
    state.authPreviewClearTimer = setTimeout(() => {
      if (generation === state.authPreviewGeneration) {
        clearAuthPreview("PW4 nach 60 Sekunden verworfen");
      }
    }, 60_000);
  } catch (error) {
    if (generation === state.authPreviewGeneration
      && state.user?.role === "admin"
      && state.user?.username === username
      && state.selectedView === "settingsView") {
      renderAuthPreview({ ok: false, error: error.message });
    }
  }
}

function applyAuthPreview() {
  const pw4 = state.authPreview?.pw4;
  if (state.user?.role !== "admin"
    || state.selectedView !== "settingsView"
    || !state.authPreview?.ok
    || !/^\d{4}$/.test(String(pw4 || ""))) return;
  $("pass4").value = pw4;
  toast("Berechnete PW4 ins Eingabefeld übernommen.");
}

function clearMaintenancePw4(status = "Noch nicht gelesen") {
  state.maintenance.pw4Generation += 1;
  if (state.maintenance.pw4ClearTimer) clearTimeout(state.maintenance.pw4ClearTimer);
  state.maintenance.pw4ClearTimer = null;
  if ($("maintenancePass4")) $("maintenancePass4").value = "";
  if ($("maintenancePw4Status")) $("maintenancePw4Status").textContent = status;
}

async function readMaintenancePw4() {
  if (state.user?.role !== "admin"
    || state.maintenance.current?.status !== "draft"
    || !state.maintenance.current?.maintenance_live_writes_enabled) return;
  const button = $("maintenancePw4Read");
  const reportId = Number(state.maintenance.current.id);
  const generation = ++state.maintenance.pw4Generation;
  if (state.maintenance.pw4ClearTimer) clearTimeout(state.maintenance.pw4ClearTimer);
  state.maintenance.pw4ClearTimer = null;
  $("maintenancePass4").value = "";
  button.disabled = true;
  $("maintenancePw4Status").textContent = "Lese Block 20 und 22 …";
  try {
    const preview = await api("/api/auth-preview");
    if (generation !== state.maintenance.pw4Generation
      || state.user?.role !== "admin"
      || Number(state.maintenance.current?.id) !== reportId
      || state.maintenance.current?.status !== "draft"
      || !state.maintenance.current?.maintenance_live_writes_enabled) return;
    if (!preview?.ok || !/^\d{4}$/.test(String(preview.pw4 || ""))) throw new Error("PW4 konnte nicht sicher berechnet werden");
    $("maintenancePass4").value = preview.pw4;
    $("maintenancePw4Status").textContent = `PW4 ${preview.pw4} gelesen und für diese Wartung übernommen`;
    state.maintenance.pw4ClearTimer = setTimeout(() => {
      if (generation === state.maintenance.pw4Generation) {
        clearMaintenancePw4("PW4 nach 60 Sekunden verworfen");
      }
    }, 60_000);
    toast("PW4 schreibfrei aus der Anlage berechnet und übernommen.");
  } catch (error) {
    if (generation === state.maintenance.pw4Generation) {
      clearMaintenancePw4(`Fehler: ${error.message}`);
      toast(error.message, "error");
    }
  } finally {
    button.disabled = false;
  }
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

function selectSystemTab(tab) {
  const selected = ["users", "tokens", "maintenance"].includes(tab) ? tab : "users";
  state.system.selectedTab = selected;
  document.querySelectorAll("[data-system-tab]").forEach((button) => {
    button.classList.toggle("active", button.dataset.systemTab === selected);
  });
  document.querySelectorAll("[data-system-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.systemPanel !== selected;
  });
  if (selected === "tokens") refreshSystemTokens();
  else if (selected === "maintenance") refreshMaintenanceMode();
  else refreshSystemUsers();
}

async function refreshSystemView() {
  if (state.user?.role !== "admin") return;
  selectSystemTab(state.system.selectedTab);
}

function renderSystemUsers() {
  const items = state.system.users || [];
  $("userRows").innerHTML = items.map((user) => {
    const self = user.username === state.user?.username;
    return `<tr data-user-row="${escapeHtml(user.username)}">
      <td><strong>${escapeHtml(user.username)}</strong>${self ? `<small>Aktuelle Sitzung</small>` : ""}</td>
      <td><select data-user-role ${self ? "disabled" : ""}><option value="guest" ${user.role === "guest" ? "selected" : ""}>Gast · lesen</option><option value="admin" ${user.role === "admin" ? "selected" : ""}>Admin · schreiben</option></select></td>
      <td><label class="check-label"><input data-user-enabled type="checkbox" ${user.enabled ? "checked" : ""} ${self ? "disabled" : ""}> ${user.enabled ? "Aktiv" : "Deaktiviert"}</label></td>
      <td><input data-user-password type="password" minlength="12" autocomplete="new-password" placeholder="unverändert"></td>
      <td class="table-actions"><button type="button" data-user-save="${escapeHtml(user.username)}">Speichern</button><button type="button" class="danger" data-user-delete="${escapeHtml(user.username)}" ${self ? "disabled" : ""}>Löschen</button></td>
    </tr>`;
  }).join("") || `<tr><td colspan="5" class="muted">Keine Benutzer vorhanden.</td></tr>`;
}

async function refreshSystemUsers() {
  if (state.user?.role !== "admin") return;
  try {
    const data = await api("/api/users");
    state.system.users = data.items || [];
    renderSystemUsers();
  } catch (error) { toast(error.message); }
}

async function createSystemUser(event) {
  event.preventDefault();
  try {
    await api("/api/users", { method: "POST", body: JSON.stringify({
      username: $("userCreateName").value,
      role: $("userCreateRole").value,
      password: $("userCreatePassword").value,
    }) });
    $("userCreateForm").reset();
    await refreshSystemUsers();
    toast("Benutzer angelegt.");
  } catch (error) { toast(error.message); }
}

async function saveSystemUser(username, row) {
  try {
    await api(`/api/users/${encodeURIComponent(username)}`, { method: "POST", body: JSON.stringify({
      role: row.querySelector("[data-user-role]").value,
      enabled: row.querySelector("[data-user-enabled]").checked,
      password: row.querySelector("[data-user-password]").value,
    }) });
    await refreshSystemUsers();
    toast(`Benutzer ${username} gespeichert. Offene Sitzungen wurden beendet.`);
  } catch (error) { toast(error.message); }
}

async function deleteSystemUser(username) {
  if (!window.confirm(`Benutzer ${username} wirklich löschen?`)) return;
  try {
    await api(`/api/users/${encodeURIComponent(username)}`, { method: "DELETE" });
    await refreshSystemUsers();
    toast(`Benutzer ${username} gelöscht.`);
  } catch (error) { toast(error.message); }
}

function renderApiSettings() {
  const settings = state.system.apiSettings;
  if (!settings) return;
  $("apiWriteEnabled").checked = Boolean(settings.write_enabled);
  $("apiAuthLevel").value = settings.auth_level ?? 4;
  $("apiWriteStatus").textContent = settings.write_enabled
    ? "API-LIVE-SCHREIBEN aktiv: Tokens mit Schreibrecht können serverseitige Aktionen auslösen."
    : "API-Schreiben ist deaktiviert. Lese- und Historienzugriffe bleiben verfügbar.";
  $("apiWriteStatus").className = `write-guard-status ${settings.write_enabled ? "warn" : "ok"}`;
}

function tokenScopesFrom(container) {
  return Array.from(container.querySelectorAll('input[type="checkbox"][value]:checked')).map((item) => item.value);
}

function renderSystemTokens() {
  const items = state.system.tokens || [];
  $("tokenRows").innerHTML = items.map((token) => `<tr data-token-row="${token.id}">
    <td><strong>${escapeHtml(token.name)}</strong><small>von ${escapeHtml(token.owner_username)}</small></td>
    <td><code>${escapeHtml(token.token_prefix)}…</code></td>
    <td><div class="scope-list">${["read", "history", "write"].map((scope) => `<label class="check-label"><input type="checkbox" value="${scope}" ${token.scopes.includes(scope) ? "checked" : ""}> ${scope}</label>`).join("")}</div></td>
    <td><label class="check-label"><input data-token-enabled type="checkbox" ${token.enabled ? "checked" : ""}> ${token.enabled ? "Aktiv" : "Deaktiviert"}</label></td>
    <td>${token.last_used_at ? escapeHtml(new Date(token.last_used_at).toLocaleString("de-DE")) : "Noch nie"}</td>
    <td class="table-actions"><button type="button" data-token-save="${token.id}">Speichern</button><button type="button" class="danger" data-token-delete="${token.id}">Löschen</button></td>
  </tr>`).join("") || `<tr><td colspan="6" class="muted">Noch keine API-Tokens.</td></tr>`;
}

async function refreshSystemTokens() {
  if (state.user?.role !== "admin") return;
  try {
    const [tokens, settings, history] = await Promise.all([
      api("/api/tokens"),
      api("/api/settings/api"),
      api("/api/history/adaptive/status"),
    ]);
    state.system.tokens = tokens.items || [];
    state.system.apiSettings = settings;
    renderSystemTokens();
    renderApiSettings();
    const raw = history.raw || {};
    const rollups = history.rollups || {};
    $("adaptiveHistoryStatus").textContent = `Rohzone: ${raw.count || 0} Snapshots, davon ${raw.preserved || 0} im Motorfenster · Verdichtung: ${rollups.count || 0} Zeitfenster · Rohhaltung ${history.raw_retention_hours} h · Vor-/Nachlauf je ${Math.round((history.event_margin_seconds || 0) / 3600)} h.`;
  } catch (error) { toast(error.message); }
}

async function saveApiSettings(event) {
  event.preventDefault();
  try {
    state.system.apiSettings = await api("/api/settings/api", { method: "POST", body: JSON.stringify({
      write_enabled: $("apiWriteEnabled").checked,
      auth_level: Number($("apiAuthLevel").value),
    }) });
    renderApiSettings();
    toast("API-Einstellungen gespeichert. Es wurde kein Reglerwert geschrieben.");
  } catch (error) { toast(error.message); }
}

async function createApiToken(event) {
  event.preventDefault();
  try {
    const created = await api("/api/tokens", { method: "POST", body: JSON.stringify({
      name: $("tokenCreateName").value,
      scopes: tokenScopesFrom($("tokenCreateForm")),
    }) });
    $("tokenCreateForm").reset();
    $("tokenSecret").textContent = created.token;
    $("tokenSecretPanel").hidden = false;
    await refreshSystemTokens();
    toast("API-Token erzeugt. Es wird nur jetzt vollständig angezeigt.");
  } catch (error) { toast(error.message); }
}

async function saveApiToken(tokenId, row) {
  try {
    await api(`/api/tokens/${tokenId}`, { method: "POST", body: JSON.stringify({
      scopes: tokenScopesFrom(row),
      enabled: row.querySelector("[data-token-enabled]").checked,
    }) });
    await refreshSystemTokens();
    toast("API-Token gespeichert.");
  } catch (error) { toast(error.message); }
}

async function deleteApiToken(tokenId) {
  if (!window.confirm("API-Token unwiderruflich löschen?")) return;
  try {
    await api(`/api/tokens/${tokenId}`, { method: "DELETE" });
    await refreshSystemTokens();
    toast("API-Token gelöscht.");
  } catch (error) { toast(error.message); }
}

function chartColors() {
  const style = getComputedStyle(document.documentElement);
  return {
    grid: style.getPropertyValue("--chart-grid").trim() || "#dbe3e5",
    ink: style.getPropertyValue("--muted").trim() || "#69767b",
    background: style.getPropertyValue("--surface-alt").trim() || "#f7f9f9",
    running: style.getPropertyValue("--chart-running-bg").trim() || "rgba(22,163,74,.11)",
    stopped: style.getPropertyValue("--chart-stopped-bg").trim() || "rgba(220,38,38,.09)",
  };
}

function motorStatusColors() {
  const style = getComputedStyle(document.documentElement);
  const color = (name, fallback) => style.getPropertyValue(name).trim() || fallback;
  return {
    background: color("--surface-alt", "#f7f9f9"),
    ink: color("--muted", "#69767b"),
    grid: color("--chart-grid", "#dbe3e5"),
    gap: color("--motor-status-gap-line", "rgba(105,118,123,.2)"),
    off: color("--motor-status-off", "#ffffff"),
    offBorder: color("--motor-status-off-border", "#94a3b8"),
    preparation: color("--motor-status-preparation", "#f59e0b"),
    start: color("--motor-status-start", "#dc2626"),
    running: color("--motor-status-running", "#16a34a"),
    shutdown: color("--motor-status-shutdown", "#7c3aed"),
    fault: color("--motor-status-fault", "#7f1d1d"),
    ok: color("--motor-status-ok", "#64748b"),
    unknown: color("--motor-status-unknown", "#94a3b8"),
  };
}

function motorStatusTone(code) {
  if (code === 0) return "ok";
  if (code === 15 || code === 16) return "off";
  if (code === 20) return "preparation";
  if (code >= 21 && code <= 24) return "start";
  if (code >= 30 && code <= 35) return "running";
  if (code >= 11 && code <= 13) return "shutdown";
  if (code === 10 || code === 14) return "fault";
  return "unknown";
}

function motorStatusCatalog() {
  const block = (state.schema?.blocks || []).find((item) => Number(item.block) === 24);
  const field = (block?.fields || []).find((item) => item.key === "Hka_Mw1.bMotorStatus");
  return new Map((field?.choices || []).map((choice) => [Number(choice.value), String(choice.label)]));
}

function motorStatusLabel(code) {
  return motorStatusCatalog().get(Number(code)) || "Unbekannter Status";
}

function median(values) {
  if (!values.length) return null;
  const ordered = [...values].sort((a, b) => a - b);
  const middle = Math.floor(ordered.length / 2);
  return ordered.length % 2 ? ordered[middle] : (ordered[middle - 1] + ordered[middle]) / 2;
}

function buildChartRunBands(points, windowRange) {
  if (!windowRange || !Number.isFinite(windowRange.start) || !Number.isFinite(windowRange.end)) return [];
  const samples = (points || []).map((point) => ({
    time: new Date(point.recorded_at).getTime(),
    rpm: numeric(point.value),
  })).filter((point) => Number.isFinite(point.time) && point.rpm !== null && point.rpm >= 0 && point.rpm <= 3000)
    .sort((a, b) => a.time - b.time);
  if (!samples.length) return [];
  const diffs = samples.slice(1).map((point, index) => point.time - samples[index].time).filter((value) => value > 0);
  const duration = windowRange.end - windowRange.start;
  const bucketGap = Math.max(1000, duration / 2000);
  const observedGap = median(diffs);
  // A regular observed interval is trustworthy only while it is plausible
  // for the server's 2,000-point reduction of the selected window.  Two lone
  // points hours apart must not redefine that outage as the normal cadence.
  const observedTrusted = Number.isFinite(observedGap)
    && observedGap <= Math.max(60_000, bucketGap * 2);
  const expectedGap = observedTrusted ? observedGap : bucketGap;
  // Consecutive non-empty server buckets can place their first samples almost
  // two bucket widths apart.  Keep a small margin above that, but do not carry
  // a known RPM state across several empty buckets: that would paint a real
  // history outage red or green instead of leaving it neutral.
  const maximumGap = Math.max(
    60_000,
    Math.min(6 * 3600_000, Math.max(bucketGap * 2.5, observedTrusted ? observedGap * 2.5 : 0)),
  );
  const bands = [];
  const append = (from, to, running) => {
    const start = Math.max(windowRange.start, from);
    const end = Math.min(windowRange.end, to);
    if (!(end > start)) return;
    const previous = bands[bands.length - 1];
    if (previous && previous.running === running && start <= previous.end + 1) previous.end = end;
    else bands.push({ start, end, running });
  };
  samples.forEach((point, index) => {
    const next = samples[index + 1];
    let start = point.time;
    if (index === 0 && point.time - windowRange.start <= maximumGap) start = windowRange.start;
    let end;
    if (next && next.time - point.time <= maximumGap) end = next.time;
    else if (next) end = point.time + Math.min(expectedGap, maximumGap);
    else if (windowRange.end - point.time <= maximumGap) end = windowRange.end;
    else end = point.time + Math.min(expectedGap, maximumGap);
    append(start, end, point.rpm > 0);
  });
  return bands;
}

function drawChartRunBands(ctx, colors, start, end, left, top, plotW, plotH) {
  for (const band of state.chartRunBands) {
    const bandStart = Math.max(start, band.start);
    const bandEnd = Math.min(end, band.end);
    if (!(bandEnd > bandStart)) continue;
    const x = left + ((bandStart - start) / Math.max(1, end - start)) * plotW;
    const width = ((bandEnd - bandStart) / Math.max(1, end - start)) * plotW;
    ctx.fillStyle = band.running ? colors.running : colors.stopped;
    ctx.fillRect(x, top, width, plotH);
  }
}

function chartTimeLabel(timestamp, duration) {
  const date = new Date(timestamp);
  if (duration >= 12 * 3600000) {
    return date.toLocaleString("de-DE", { day:"2-digit", month:"2-digit", hour:"2-digit", minute:"2-digit" });
  }
  return date.toLocaleTimeString("de-DE", { hour:"2-digit", minute:"2-digit" });
}

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

function chartBaseHeight(canvas) {
  if (!canvas.dataset.chartHeight) {
    canvas.dataset.chartHeight = canvas.getAttribute("height") || "300";
  }
  return Number(canvas.dataset.chartHeight) || 300;
}

function drawChart(canvas, series, rangeHours, group, requestedWindow = null) {
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect(); const dpr = window.devicePixelRatio || 1; const width = Math.max(280, Math.floor(rect.width)); const baseHeight = chartBaseHeight(canvas); const compact = width < 560 || window.matchMedia?.("(max-width: 780px)").matches; const height = compact ? Math.min(baseHeight, canvas.id === "temperatureChart" ? 250 : 230) : baseHeight;
  canvas.width = width * dpr; canvas.height = height * dpr; const ctx = canvas.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const colors = chartColors(); ctx.fillStyle = colors.background; ctx.fillRect(0, 0, width, height);
  const axisConfig = chartAxisConfig(group); const dualAxis = Boolean(axisConfig);
  // Uniform margins align one timestamp vertically across all four charts.
  const left = compact ? 52 : 78, right = compact ? 52 : 78, top = compact ? 22 : 20, bottom = compact ? 29 : 32, plotW = Math.max(80, width - left - right), plotH = Math.max(80, height - top - bottom);
  const visibleSeries = series.filter((item) => !chartSeriesHidden(group, item));
  const allPoints = chartPoints(series); const points = chartPoints(visibleSeries);
  const customWindow = requestedWindow && Number.isFinite(requestedWindow.start) && Number.isFinite(requestedWindow.end) && requestedWindow.end > requestedWindow.start;
  const times = allPoints.map((point) => point.time);
  const fallbackEnd = Date.now();
  const fullStart = customWindow ? requestedWindow.start : (times.length ? Math.min(...times) : fallbackEnd - rangeHours * 3600000);
  const fullEnd = customWindow ? requestedWindow.end : (times.length ? Math.max(...times, fullStart + rangeHours * 3600000) : fallbackEnd);
  const zoom = state.chartZoomRange; const start = zoom ? zoom.start : fullStart; const end = zoom ? zoom.end : fullEnd;
  const geometry = { left, top, width:plotW, height:plotH, canvasWidth:width, canvasHeight:height, group, start, end };
  geometry.start = start; geometry.end = end; state.chartGeometries.set(canvas, geometry);
  drawChartRunBands(ctx, colors, start, end, left, top, plotW, plotH);
  const noStoredPoints = !allPoints.length;
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
  if (noStoredPoints || !visibleSeries.length || !visible.length) { ctx.fillStyle=colors.ink; ctx.font="13px system-ui"; ctx.textAlign="center"; ctx.fillText(noStoredPoints ? "Noch keine gespeicherten Messwerte" : "Alle Werte ausgeblendet", left+plotW/2, top+plotH/2); ctx.textAlign="left"; }
  ctx.fillStyle=colors.ink; ctx.textAlign="left"; ctx.fillText(chartTimeLabel(start, end - start),left,height-8); ctx.textAlign="right"; ctx.fillText(chartTimeLabel(end, end - start),left+plotW,height-8); ctx.textAlign="left";
  const pointer = state.chartPointers.get(canvas);
  drawChartHover(ctx, pointer, visibleSeries, dataById, start, end, left, top, plotW, plotH, width, height, group, axisConfig, leftScale, rightScale);
  if (pointer?.dragging && Number.isFinite(pointer.currentX)) { const x1=Math.max(left,Math.min(left+plotW,pointer.startX)); const x2=Math.max(left,Math.min(left+plotW,pointer.currentX)); ctx.fillStyle="rgba(40,99,167,.16)"; ctx.fillRect(Math.min(x1,x2),top,Math.abs(x2-x1),plotH); ctx.strokeStyle="#2863a7"; ctx.setLineDash([5,4]); ctx.strokeRect(Math.min(x1,x2),top,Math.abs(x2-x1),plotH); ctx.setLineDash([]); }
}

function normalizedMotorStatusSegments() {
  return (state.motorStatusSegments || []).map((segment) => ({
    start: new Date(segment.from).getTime(),
    end: new Date(segment.to).getTime(),
    status: Number(segment.status),
  })).filter((segment) => Number.isFinite(segment.start) && Number.isFinite(segment.end)
    && segment.end > segment.start && Number.isInteger(segment.status))
    .sort((a, b) => a.start - b.start);
}

function motorStatusSegmentAt(segments, time) {
  return segments.find((segment) => time >= segment.start && time < segment.end) || null;
}

function layoutMotorStatusMarkers(segments, start, end, left, top, plotW, plotH) {
  const events = segments.filter((segment) => segment.start >= start && segment.start <= end)
    .map((segment) => ({
      segment,
      x: left + ((segment.start - start) / Math.max(1, end - start)) * plotW,
    }));
  const groups = [];
  for (const event of events) {
    const group = groups[groups.length - 1];
    if (group && event.x - group.anchor <= 4) group.events.push(event);
    else groups.push({ anchor: event.x, events: [event] });
  }
  const hitboxes = [];
  const tileWidth = 4;
  const rowsPerColumn = Math.max(1, Math.floor(plotH / 4));
  for (const group of groups) {
    const columns = Math.ceil(group.events.length / rowsPerColumn);
    const groupWidth = columns * tileWidth;
    const center = group.events.reduce((sum, event) => sum + event.x, 0) / group.events.length;
    const baseX = Math.max(left, Math.min(left + plotW - groupWidth, center - groupWidth / 2));
    for (let column = 0; column < columns; column += 1) {
      const columnEvents = group.events.slice(column * rowsPerColumn, (column + 1) * rowsPerColumn);
      const tileHeight = plotH / columnEvents.length;
      columnEvents.forEach((event, row) => hitboxes.push({
        x: baseX + column * tileWidth,
        y: top + row * tileHeight,
        width: tileWidth,
        height: tileHeight,
        segment: event.segment,
      }));
    }
  }
  return hitboxes;
}

function drawMotorStatusHover(ctx, pointer, segments, hitboxes, start, end, left, top, plotW, plotH, width) {
  const output = $("motorStatusHoverText");
  if (!pointer || !Number.isFinite(pointer.hoverX) || pointer.hoverX < left || pointer.hoverX > left + plotW) {
    if (output) output.textContent = "";
    return;
  }
  const hoverTime = start + ((pointer.hoverX - left) / Math.max(1, plotW)) * (end - start);
  const marker = (hitboxes || []).find((hitbox) => pointer.hoverX >= hitbox.x
    && pointer.hoverX <= hitbox.x + hitbox.width
    && Number.isFinite(pointer.hoverY)
    && pointer.hoverY >= hitbox.y
    && pointer.hoverY <= hitbox.y + hitbox.height);
  const segment = marker?.segment || motorStatusSegmentAt(segments, hoverTime);
  const displayTime = segment ? segment.start : hoverTime;
  const colors = motorStatusColors();
  const title = new Date(displayTime).toLocaleString("de-DE", {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
  const line = segment
    ? `${segment.status} · ${motorStatusLabel(segment.status)}`
    : "Keine Messdaten";
  if (output) output.textContent = `${title}: ${line}`;

  ctx.save();
  ctx.strokeStyle = "rgba(40, 99, 167, .72)";
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(pointer.hoverX, top);
  ctx.lineTo(pointer.hoverX, top + plotH);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.font = "11px system-ui";
  const boxWidth = Math.min(width - 16, Math.max(210, title.length * 6.2 + 24, line.length * 6.2 + 38));
  const boxHeight = 50;
  let boxX = pointer.hoverX + 12;
  if (boxX + boxWidth > width - 8) boxX = pointer.hoverX - boxWidth - 12;
  boxX = Math.max(8, Math.min(width - boxWidth - 8, boxX));
  const boxY = Math.max(6, top + Math.floor((plotH - boxHeight) / 2));
  ctx.fillStyle = "rgba(22, 34, 39, .95)";
  ctx.strokeStyle = "rgba(255, 255, 255, .24)";
  ctx.beginPath();
  ctx.roundRect(boxX, boxY, boxWidth, boxHeight, 5);
  ctx.fill();
  ctx.stroke();
  ctx.fillStyle = "#ffffff";
  ctx.font = "600 11px system-ui";
  ctx.textAlign = "left";
  ctx.fillText(title, boxX + 10, boxY + 17);
  if (segment) {
    ctx.fillStyle = colors[motorStatusTone(segment.status)];
    ctx.fillRect(boxX + 10, boxY + 30, 9, 9);
  } else {
    ctx.fillStyle = colors.background;
    ctx.fillRect(boxX + 10, boxY + 30, 9, 9);
    ctx.strokeStyle = colors.gap;
    ctx.beginPath();
    ctx.moveTo(boxX + 10, boxY + 39);
    ctx.lineTo(boxX + 19, boxY + 30);
    ctx.moveTo(boxX + 14, boxY + 39);
    ctx.lineTo(boxX + 19, boxY + 34);
    ctx.stroke();
  }
  if (!segment || motorStatusTone(segment.status) === "off") {
    ctx.strokeStyle = colors.offBorder;
    ctx.strokeRect(boxX + 10, boxY + 30, 9, 9);
  }
  ctx.fillStyle = "#ffffff";
  ctx.font = "11px system-ui";
  ctx.fillText(line, boxX + 25, boxY + 39);
  ctx.restore();
}

function drawMotorStatusChart(canvas, rangeHours, requestedWindow = null) {
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(280, Math.floor(rect.width));
  const compact = width < 560 || window.matchMedia?.("(max-width: 780px)").matches;
  const height = compact ? 96 : 112;
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const colors = motorStatusColors();
  ctx.fillStyle = colors.background;
  ctx.fillRect(0, 0, width, height);
  const left = compact ? 52 : 78;
  const right = compact ? 52 : 78;
  const top = 15;
  const bottom = compact ? 25 : 27;
  const plotW = Math.max(80, width - left - right);
  const plotH = Math.max(34, height - top - bottom);
  const customWindow = requestedWindow && Number.isFinite(requestedWindow.start)
    && Number.isFinite(requestedWindow.end) && requestedWindow.end > requestedWindow.start;
  const fallbackEnd = Date.now();
  const fullStart = customWindow ? requestedWindow.start : fallbackEnd - rangeHours * 3600000;
  const fullEnd = customWindow ? requestedWindow.end : fallbackEnd;
  const zoom = state.chartZoomRange;
  const start = zoom ? zoom.start : fullStart;
  const end = zoom ? zoom.end : fullEnd;
  state.chartGeometries.set(canvas, {
    left, top, width: plotW, height: plotH,
    canvasWidth: width, canvasHeight: height,
    group: "motorStatus", start, end,
  });

  // The hatched base is deliberately distinct from white "Aus" intervals.
  ctx.save();
  ctx.beginPath();
  ctx.rect(left, top, plotW, plotH);
  ctx.clip();
  ctx.strokeStyle = colors.gap;
  ctx.lineWidth = 1;
  for (let x = left - plotH; x < left + plotW + plotH; x += 12) {
    ctx.beginPath();
    ctx.moveTo(x, top + plotH);
    ctx.lineTo(x + plotH, top);
    ctx.stroke();
  }
  ctx.restore();

  const segments = normalizedMotorStatusSegments().filter((segment) => segment.end > start && segment.start < end);
  for (const segment of segments) {
    const clippedStart = Math.max(start, segment.start);
    const clippedEnd = Math.min(end, segment.end);
    const x1 = left + ((clippedStart - start) / Math.max(1, end - start)) * plotW;
    const x2 = left + ((clippedEnd - start) / Math.max(1, end - start)) * plotW;
    const tone = motorStatusTone(segment.status);
    ctx.fillStyle = colors[tone];
    ctx.fillRect(x1, top, Math.max(1, x2 - x1), plotH);
    if (tone === "off") {
      ctx.strokeStyle = colors.offBorder;
      ctx.lineWidth = 1;
      ctx.strokeRect(x1 + .5, top + .5, Math.max(0, x2 - x1 - 1), plotH - 1);
    }
  }
  // Colliding sub-pixel transitions become separate colour tiles in a narrow
  // marker column. This keeps every event visible and individually hoverable
  // even when a 24-hour view compresses a complete start to one x-coordinate.
  const hitboxes = layoutMotorStatusMarkers(segments, start, end, left, top, plotW, plotH);
  for (const hitbox of hitboxes) {
    const tone = motorStatusTone(hitbox.segment.status);
    ctx.fillStyle = colors[tone];
    ctx.fillRect(hitbox.x, hitbox.y, hitbox.width, hitbox.height);
    ctx.strokeStyle = tone === "off" ? colors.offBorder : "rgba(255,255,255,.5)";
    ctx.lineWidth = 1;
    ctx.strokeRect(hitbox.x + .5, hitbox.y + .5, Math.max(0, hitbox.width - 1), Math.max(0, hitbox.height - 1));
  }
  state.motorStatusHitboxes.set(canvas, hitboxes);
  ctx.strokeStyle = colors.grid;
  ctx.lineWidth = 1;
  ctx.strokeRect(left + .5, top + .5, plotW - 1, plotH - 1);
  if (!segments.length) {
    ctx.fillStyle = colors.ink;
    ctx.font = "12px system-ui";
    ctx.textAlign = "center";
    ctx.fillText("Keine Motorstatusdaten im Zeitraum", left + plotW / 2, top + plotH / 2 + 4);
  }
  ctx.fillStyle = colors.ink;
  ctx.font = compact ? "10px system-ui" : "11px system-ui";
  ctx.textAlign = "left";
  ctx.fillText(chartTimeLabel(start, end - start), left, height - 7);
  ctx.textAlign = "right";
  ctx.fillText(chartTimeLabel(end, end - start), left + plotW, height - 7);
  ctx.textAlign = "left";
  const pointer = state.chartPointers.get(canvas);
  drawMotorStatusHover(ctx, pointer, segments, hitboxes, start, end, left, top, plotW, plotH, width);
  if (pointer?.dragging && Number.isFinite(pointer.currentX)) {
    const x1 = Math.max(left, Math.min(left + plotW, pointer.startX));
    const x2 = Math.max(left, Math.min(left + plotW, pointer.currentX));
    ctx.fillStyle = "rgba(40,99,167,.16)";
    ctx.fillRect(Math.min(x1, x2), top, Math.abs(x2 - x1), plotH);
    ctx.strokeStyle = "#2863a7";
    ctx.setLineDash([5, 4]);
    ctx.strokeRect(Math.min(x1, x2), top, Math.abs(x2 - x1), plotH);
    ctx.setLineDash([]);
  }
}

function redrawChartGroup(group) {
  const request = state.historyRequest || { mode: "hours", hours: 24 };
  const range = request.mode === "hours" ? Number(request.hours) : Math.max(1, (Number(request.end) - Number(request.start)) / 3600000);
  if (group === "motorStatus") {
    drawMotorStatusChart($("motorStatusChart"), range, state.historyWindow);
  } else {
    const canvas = group === "temperature" ? $("temperatureChart") : group === "motor" ? $("motorChart") : $("exhaustChart");
    drawChart(canvas, state.chartSeries[group] || [], range, group, state.historyWindow);
  }
  updateZoomControl();
}

function redrawAllCharts() {
  ["temperature", "motor", "motorStatus", "exhaust"].forEach(redrawChartGroup);
}

function updateZoomControl() {
  document.querySelectorAll('[data-action="reset-chart-zoom"]').forEach((button) => { button.hidden = !state.chartZoomRange; });
}

function chartPointerPosition(canvas, event) {
  const rect = canvas.getBoundingClientRect();
  const geometry = state.chartGeometries.get(canvas);
  const logicalWidth = Number(geometry?.canvasWidth) || rect.width;
  const logicalHeight = Number(geometry?.canvasHeight) || rect.height;
  return {
    x: (event.clientX - rect.left) * (rect.width > 0 ? logicalWidth / rect.width : 1),
    y: (event.clientY - rect.top) * (rect.height > 0 ? logicalHeight / rect.height : 1),
  };
}

function resetChartZoom() {
  state.chartZoomRange = null;
  ["temperatureChart", "motorChart", "motorStatusChart", "exhaustChart"].forEach((id) => state.chartPointers.delete($(id)));
  redrawAllCharts();
}

function bindChartZoom(canvas, group) {
  if (!canvas || canvas.dataset.zoomBound) return;
  canvas.dataset.zoomBound = "1";
  const position = (event) => chartPointerPosition(canvas, event);
  canvas.addEventListener("pointerdown", (event) => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    const point=position(event); const geometry=state.chartGeometries.get(canvas);
    if (!geometry || point.x < geometry.left || point.x > geometry.left+geometry.width || point.y < geometry.top || point.y > geometry.top+geometry.height) return;
    if (group === "motorStatus" && event.pointerType !== "mouse") {
      // A touch selects a status but keeps vertical page scrolling available.
      // Drag zoom remains available in each of the three larger charts.
      state.chartPointers.set(canvas,{dragging:false,hoverX:point.x,hoverY:point.y});
      redrawChartGroup(group);
      return;
    }
    state.chartPointers.set(canvas,{dragging:true,startX:point.x,currentX:point.x,hoverX:point.x,hoverY:point.y,pointerId:event.pointerId}); canvas.setPointerCapture?.(event.pointerId); event.preventDefault(); redrawChartGroup(group);
  });
  canvas.addEventListener("pointermove", (event) => { const point=position(event); const pointer=state.chartPointers.get(canvas); if (pointer?.dragging) state.chartPointers.set(canvas,{...pointer,currentX:point.x,hoverX:point.x,hoverY:point.y}); else state.chartPointers.set(canvas,{hoverX:point.x,hoverY:point.y,dragging:false}); redrawChartGroup(group); });
  canvas.addEventListener("pointerup", (event) => { const pointer=state.chartPointers.get(canvas); if (!pointer?.dragging || pointer.pointerId !== event.pointerId) return; const geometry=state.chartGeometries.get(canvas); const point=position(event); const endX=point.x; if (geometry && Math.abs(endX-pointer.startX)>=8) { const x1=Math.max(geometry.left,Math.min(geometry.left+geometry.width,pointer.startX)); const x2=Math.max(geometry.left,Math.min(geometry.left+geometry.width,endX)); state.chartZoomRange={start:geometry.start+(Math.min(x1,x2)-geometry.left)/geometry.width*(geometry.end-geometry.start),end:geometry.start+(Math.max(x1,x2)-geometry.left)/geometry.width*(geometry.end-geometry.start)}; } state.chartPointers.set(canvas,{dragging:false,hoverX:endX,hoverY:point.y}); canvas.releasePointerCapture?.(event.pointerId); redrawAllCharts(); event.preventDefault(); });
  canvas.addEventListener("pointerleave", () => { const pointer=state.chartPointers.get(canvas); if (pointer?.dragging) return; state.chartPointers.delete(canvas); redrawChartGroup(group); });
  if (group === "motorStatus") canvas.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End", "Escape"].includes(event.key)) return;
    if (event.key === "Escape") {
      state.chartPointers.delete(canvas);
      redrawChartGroup(group);
      event.preventDefault();
      return;
    }
    const hitboxes = [...(state.motorStatusHitboxes.get(canvas) || [])]
      .sort((first, second) => first.segment.start - second.segment.start);
    if (!hitboxes.length) return;
    const pointer = state.chartPointers.get(canvas);
    let index = hitboxes.findIndex((hitbox) => hitbox.segment.start === pointer?.eventStart);
    if (event.key === "Home") index = 0;
    else if (event.key === "End") index = hitboxes.length - 1;
    else if (event.key === "ArrowRight") index = Math.min(hitboxes.length - 1, index + 1);
    else index = index < 0 ? hitboxes.length - 1 : Math.max(0, index - 1);
    const hitbox = hitboxes[index];
    state.chartPointers.set(canvas,{
      dragging:false,
      hoverX:hitbox.x + hitbox.width / 2,
      hoverY:hitbox.y + hitbox.height / 2,
      eventStart:hitbox.segment.start,
    });
    redrawChartGroup(group);
    event.preventDefault();
  });
  canvas.addEventListener("dblclick", (event) => { resetChartZoom(); event.preventDefault(); });
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
    const [batch, motorStatus] = await Promise.all([
      api(`/api/history-batch?${params.toString()}`),
      api(`/api/motor-status-history?${selection.query}`),
    ]);
    const responseStart = new Date(batch.from).getTime();
    const responseEnd = new Date(batch.to).getTime();
    state.historyWindow = Number.isFinite(responseStart) && Number.isFinite(responseEnd) && responseEnd > responseStart
      ? { start: responseStart, end: responseEnd }
      : selection.window;
    const historyFor = (groupIds) => groupIds.map((id) => {
      const item = chartItems.find((candidate) => candidate.id === id);
      return item ? { ...item, points: batch.series?.[id] || [] } : null;
    }).filter(Boolean);
    state.chartSeries.temperature = historyFor(groups.temperature);
    state.chartSeries.motor = historyFor(groups.motor);
    state.chartSeries.exhaust = historyFor(groups.exhaust);
    state.chartRunBands = buildChartRunBands(batch.series?.drehzahl || [], state.historyWindow);
    state.motorStatusSegments = motorStatus.segments || [];
    state.motorStatusBackfillComplete = motorStatus.backfill_complete !== false;
    $("historyRangeStatus").textContent = `Aktiv: ${selection.label} · ${chartItems.length} Kurven · Motorstatus${state.motorStatusBackfillComplete ? "" : " · ältere Statusdaten werden nachgetragen"}`;
    renderChartLegend("temperature", "temperatureLegend");
    renderChartLegend("motor", "motorLegend");
    renderChartLegend("exhaust", "exhaustLegend");
    bindChartZoom($("temperatureChart"), "temperature"); bindChartZoom($("motorChart"), "motor"); bindChartZoom($("motorStatusChart"), "motorStatus"); bindChartZoom($("exhaustChart"), "exhaust");
    redrawAllCharts();
  } catch (error) {
    $("historyRangeStatus").textContent = `Fehler: ${error.message}`;
  } finally {
    state.chartRefresh.lastCompletedAt = Date.now();
    state.chartRefresh.inFlight = false;
    if (state.chartRefresh.pending && state.selectedView === "monitorView") {
      state.chartRefresh.pending = false;
      setTimeout(refreshCharts, 0);
    }
  }
}

function historySelection() {
  const request = state.historyRequest || { mode: "hours", hours: 24 };
  if (request.mode === "hours") {
    const hours = Number(request.hours);
    if (!Number.isInteger(hours) || hours < 1 || hours > 720) throw new Error("Stunden müssen eine ganze Zahl von 1 bis 720 sein");
    return { query: `hours=${hours}`, window: null, label: `letzte ${hours} ${hours === 1 ? "Stunde" : "Stunden"}` };
  }
  const start = Number(request.start);
  const end = Number(request.end);
  if (!Number.isFinite(start)) throw new Error("ungültiges Startdatum");
  if (!Number.isFinite(end)) throw new Error("ungültiges Enddatum");
  if (end <= start) throw new Error("Ende muss nach dem Start liegen");
  if (end - start > 30 * 24 * 3600000) throw new Error("Der Zeitraum darf höchstens 30 Tage umfassen");
  const query = new URLSearchParams({ from: new Date(start).toISOString(), to: new Date(end).toISOString() }).toString();
  return { query, window: { start, end }, label: `${new Date(start).toLocaleString("de-DE")} bis ${new Date(end).toLocaleString("de-DE")}` };
}

function historyAutoRefreshInterval() {
  const request = state.historyRequest || { mode: "hours", hours: 24 };
  if (request.mode !== "hours") return null;
  const hours = Number(request.hours);
  if (hours <= 6) return 6500;
  if (hours <= 24) return 30_000;
  if (hours <= 48) return 60_000;
  return 5 * 60_000;
}

function historyAutoRefreshDue() {
  const interval = historyAutoRefreshInterval();
  return interval !== null
    && Date.now() - Number(state.chartRefresh.lastCompletedAt || 0) >= interval;
}

function activateHistoryRequest(request) {
  state.historyRequest = request;
  state.chartZoomRange = null;
  state.chartRunBands = [];
  state.motorStatusSegments = [];
  ["temperatureChart", "motorChart", "motorStatusChart", "exhaustChart"].forEach((id) => state.chartPointers.delete($(id)));
  refreshCharts();
}

function applyHistoryPreset() {
  const hours = Number($("temperatureRange").value);
  $("historyHours").value = "";
  $("historyStart").value = "";
  $("historyEnd").value = "";
  activateHistoryRequest({ mode: "hours", hours });
}

function applyHistoryHours(event) {
  event.preventDefault();
  const raw = $("historyHours").value.trim();
  const hours = Number(raw);
  if (!raw || !Number.isInteger(hours) || hours < 1 || hours > 720) {
    $("historyRangeStatus").textContent = "Fehler: Freie Stunden müssen eine ganze Zahl von 1 bis 720 sein";
    return;
  }
  $("historyStart").value = "";
  $("historyEnd").value = "";
  activateHistoryRequest({ mode: "hours", hours });
}

function applyHistoryDates(event) {
  event.preventDefault();
  const startText = $("historyStart").value;
  const endText = $("historyEnd").value;
  let start = startText ? new Date(startText).getTime() : null;
  let end = endText ? new Date(endText).getTime() : null;
  if (start === null && end === null) {
    $("historyRangeStatus").textContent = "Fehler: Bitte mindestens Start oder Ende eingeben";
    return;
  }
  if (start !== null && !Number.isFinite(start)) return void ($("historyRangeStatus").textContent = "Fehler: ungültiges Startdatum");
  if (end !== null && !Number.isFinite(end)) return void ($("historyRangeStatus").textContent = "Fehler: ungültiges Enddatum");
  if (start === null) start = end - 24 * 3600000;
  if (end === null) end = start + 24 * 3600000;
  if (end <= start) return void ($("historyRangeStatus").textContent = "Fehler: Ende muss nach dem Start liegen");
  if (end - start > 30 * 24 * 3600000) return void ($("historyRangeStatus").textContent = "Fehler: Der Zeitraum darf höchstens 30 Tage umfassen");
  $("historyHours").value = "";
  activateHistoryRequest({ mode: "dates", start, end });
}

function resetHistoryRange() {
  $("historyHours").value = "";
  $("historyStart").value = "";
  $("historyEnd").value = "";
  $("temperatureRange").value = "24";
  activateHistoryRequest({ mode: "hours", hours: 24 });
}

function setBackupStatus(id, message, tone = "neutral") {
  const element = $(id);
  if (!element) return;
  element.textContent = message;
  element.classList.remove("backup-status-ok", "backup-status-warn", "backup-status-error", "backup-status-neutral");
  element.classList.add("backup-status", `backup-status-${tone}`);
}

function backupSchemaBlocks() {
  const seen = new Set();
  const regulator = (state.schema?.blocks || []).map((item) => ({ ...item, cpu: 0 }));
  const networkProtection = (state.schema?.network_protection || [])
    .filter((item) => item.backup_eligible !== false);
  return [...regulator, ...networkProtection].map((item) => {
    const cpu = Number(item.cpu ?? 0);
    const block = Number(item.block);
    return {
      ...item,
      cpu,
      block,
      target_key: item.target_key || `${cpu}:${block}`,
      name: item.name || (cpu ? `Netzschutz · Überwachungs-CPU ${cpu}` : `Block ${block}`),
    };
  }).filter((item) => {
    if (!Number.isInteger(item.cpu) || item.cpu < 0 || item.cpu > 2) return false;
    if (!Number.isInteger(item.block) || item.block < 0 || item.block > 255 || seen.has(item.target_key)) return false;
    seen.add(item.target_key);
    return true;
  }).sort((left, right) => left.cpu - right.cpu || left.block - right.block);
}

function blockChoiceMarkup(item, kind, checked = false, disabled = false) {
  const cpu = Number(item.cpu ?? 0);
  const targetKey = item.target_key || `${cpu}:${item.block}`;
  const label = item.name || item.block_name || `Block ${item.block}`;
  const problem = item.error ? `<small class="backup-block-error">${escapeHtml(item.error)}</small>` : "";
  return `<label class="backup-block-choice ${cpu ? "backup-network-choice" : ""} ${disabled ? "disabled" : ""}">
    <input type="checkbox" data-${kind}-block data-cpu="${cpu}" data-block="${item.block}" data-target-key="${escapeHtml(targetKey)}" value="${escapeHtml(targetKey)}" ${checked ? "checked" : ""} ${disabled ? "disabled" : ""}>
    <span><strong>CPU ${cpu} · Block ${item.block}</strong><small>${escapeHtml(label)}</small>${problem}</span>
  </label>`;
}

function selectedBlocks(kind) {
  const seen = new Set();
  return Array.from(document.querySelectorAll(`[data-${kind}-block]:checked:not(:disabled)`))
    .map((input) => ({ cpu: Number(input.dataset.cpu ?? 0), block: Number(input.dataset.block ?? input.value) }))
    .filter((target) => {
      const key = `${target.cpu}:${target.block}`;
      if (!Number.isInteger(target.cpu) || !Number.isInteger(target.block) || seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .sort((left, right) => left.cpu - right.cpu || left.block - right.block);
}

function setBlockSelection(kind, checked) {
  document.querySelectorAll(`[data-${kind}-block]:not(:disabled)`).forEach((input) => { input.checked = checked; });
  if (kind === "backup") updateBackupSelectionStatus();
  else updateRestoreSelectionStatus();
}

function renderBackupBlockList() {
  const container = $("backupBlockList");
  if (!container || !state.schema) return;
  const blocks = backupSchemaBlocks();
  const signature = blocks.map((item) => `${item.target_key}:${item.name}`).join("|");
  if (container.dataset.signature === signature) return;
  container.dataset.signature = signature;
  container.innerHTML = blocks.map((item) => blockChoiceMarkup(item, "backup", true)).join("")
    || `<p class="muted">Keine sicherbaren Blöcke im Mapping gefunden.</p>`;
  updateBackupSelectionStatus();
}

function updateBackupSelectionStatus() {
  const selected = selectedBlocks("backup").length;
  const available = document.querySelectorAll("[data-backup-block]:not(:disabled)").length;
  setBackupStatus("backupStatus", `${selected} von ${available} Blockzielen für die Sicherung ausgewählt.`, selected ? "neutral" : "warn");
  if ($("backupCreate")) $("backupCreate").disabled = selected === 0;
}

function countItems(value) {
  if (Array.isArray(value)) return value.length;
  const count = Number(value);
  return Number.isFinite(count) ? count : 0;
}

function archiveTargetCount(item, kind) {
  const aliases = {
    requested: ["requested_targets", "requested_blocks", "requested_count"],
    successful: ["successful_targets", "successful_blocks", "successful_count"],
    failed: ["failed_targets", "failed_blocks", "failed_count"],
  };
  for (const key of aliases[kind] || []) {
    if (item?.[key] !== undefined && item?.[key] !== null) return countItems(item[key]);
  }
  return countItems(item?.summary?.[kind]);
}

function archiveIntegrityOk(item) {
  const integrity = item?.integrity;
  if (typeof integrity === "boolean") return integrity;
  if (typeof integrity === "string") {
    const normalized = integrity.toLowerCase();
    if (["ok", "valid", "verified", "intact", "passed"].includes(normalized)) return true;
    if (["failed", "invalid", "mismatch", "corrupt", "unknown", "unchecked"].includes(normalized)) return false;
  }
  if (integrity && typeof integrity === "object") {
    if (typeof integrity.ok === "boolean") return integrity.ok;
    if (typeof integrity.verified === "boolean") return integrity.verified;
  }
  return false;
}

function archiveEntryReady(item) {
  const requested = archiveTargetCount(item, "requested");
  const successful = archiveTargetCount(item, "successful");
  const failed = archiveTargetCount(item, "failed");
  const archiveState = String(item?.state || "").toLowerCase();
  const stateReady = !archiveState || archiveState === "ready";
  const completeContract = requested === 38 || requested === 42;
  return archiveIntegrityOk(item)
    && item?.pack_compatible === true
    && stateReady
    && completeContract
    && successful === requested
    && failed === 0;
}

function formatArchiveDate(value) {
  const date = new Date(value || "");
  return Number.isFinite(date.getTime()) ? date.toLocaleString("de-DE") : "Zeitpunkt unbekannt";
}

function formatArchiveBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "Größe unbekannt";
  if (bytes < 1024) return `${bytes} B`;
  return `${new Intl.NumberFormat("de-DE", { maximumFractionDigits: 1 }).format(bytes / 1024)} KiB`;
}

function shortArchiveDigest(value) {
  const digest = String(value || "");
  return digest ? `${digest.slice(0, 12)}…` : "nicht gemeldet";
}

function archiveItems(response) {
  if (Array.isArray(response)) return response;
  if (Array.isArray(response?.items)) return response.items;
  if (Array.isArray(response?.backups)) return response.backups;
  return [];
}

function renderBackupArchive() {
  const container = $("backupArchiveList");
  if (!container) return;
  const items = [...(state.backup.archive || [])].sort((left, right) => {
    const rightTime = new Date(right?.created_at || 0).getTime() || 0;
    const leftTime = new Date(left?.created_at || 0).getTime() || 0;
    return rightTime - leftTime;
  });
  if (!items.length) {
    container.innerHTML = `<p class="muted backup-archive-empty">Noch kein Wartungsbackup im geschützten Archiv.</p>`;
    setBackupStatus("backupArchiveStatus", "Noch keine Wartungsbackups vorhanden. Beim nächsten Wartungsstart wird automatisch eines erstellt.", "neutral");
    return;
  }
  container.innerHTML = items.map((item) => {
    const id = item?.id ?? "";
    const requested = archiveTargetCount(item, "requested");
    const successful = archiveTargetCount(item, "successful");
    const failed = archiveTargetCount(item, "failed");
    const ready = archiveEntryReady(item);
    const reportId = item?.maintenance_report_id;
    const filename = String(item?.filename || `open-dachs-maintenance-backup-${id}.json`).split(/[\\/]/).pop() || "open-dachs-backup.json";
    const downloadUrl = appUrl(`/api/backup/archive/${encodeURIComponent(id)}/download`);
    const source = item?.source === "maintenance" ? "Wartungsstart" : (item?.source || "Archiv");
    const stateText = item?.state || (ready ? "vollständig" : "prüfen");
    const reportLink = reportId === null || reportId === undefined || reportId === ""
      ? `<span class="muted">Kein Bericht verknüpft</span>`
      : `<button type="button" class="button-link" data-open-maintenance-report="${escapeHtml(reportId)}">Wartungsbericht #${escapeHtml(reportId)}</button>`;
    return `<article class="backup-archive-card ${ready ? "verified" : "invalid"}" data-backup-archive-item="${escapeHtml(id)}">
      <header><div><p class="eyebrow">BACKUP #${escapeHtml(id)}</p><h4>${escapeHtml(formatArchiveDate(item?.created_at))}</h4></div><span class="status-pill ${ready ? "ok" : "warn"}">${ready ? "Integrität geprüft" : "Nicht vollständig geprüft"}</span></header>
      <div class="backup-archive-metrics">
        <div><span>Ziele</span><strong>${successful}/${requested || "?"}</strong><small>${failed ? `${failed} fehlgeschlagen` : `${requested} von ${requested} vollständig`}</small></div>
        <div><span>Status</span><strong>${escapeHtml(stateText)}</strong><small>${escapeHtml(source)} · ${escapeHtml(item?.created_by || "unbekannt")}</small></div>
        <div><span>Packrevision</span><strong>${escapeHtml(item?.pack_revision || "—")}</strong><small>${escapeHtml(formatArchiveBytes(item?.size_bytes))}</small></div>
      </div>
      <dl class="backup-archive-hashes"><div><dt>Abbild-SHA-256</dt><dd title="${escapeHtml(item?.image_sha256 || "")}">${escapeHtml(shortArchiveDigest(item?.image_sha256))}</dd></div><div><dt>Datei-SHA-256</dt><dd title="${escapeHtml(item?.file_sha256 || "")}">${escapeHtml(shortArchiveDigest(item?.file_sha256))}</dd></div></dl>
      <footer><div>${reportLink}<small>${escapeHtml(filename)}</small></div><div class="backup-archive-actions"><a class="button-link" href="${escapeHtml(downloadUrl)}" download="${escapeHtml(filename)}">JSON herunterladen</a><button type="button" data-load-backup-archive="${escapeHtml(id)}" ${ready ? "" : "disabled"}>Für Wiederherstellung laden</button></div></footer>
    </article>`;
  }).join("");
  const complete = items.filter(archiveEntryReady).length;
  setBackupStatus("backupArchiveStatus", `${items.length} Wartungsbackup${items.length === 1 ? "" : "s"} archiviert · ${complete} vollständig mit geprüftem 38er- oder 42er-Vertrag.`, complete === items.length ? "ok" : "warn");
}

async function refreshBackupArchive(silent = false) {
  if (state.user?.role !== "admin") return;
  const requestGeneration = state.backup.importGeneration;
  if (!silent) setBackupStatus("backupArchiveStatus", "Lade geschütztes Backup-Archiv …", "neutral");
  try {
    const response = await api("/api/backup/archive");
    if (requestGeneration !== state.backup.importGeneration || state.user?.role !== "admin") return;
    state.backup.archive = archiveItems(response);
    state.backup.archiveLoaded = true;
    renderBackupArchive();
  } catch (error) {
    if (requestGeneration !== state.backup.importGeneration || state.user?.role !== "admin") return;
    state.backup.archiveLoaded = false;
    setBackupStatus("backupArchiveStatus", `Backup-Archiv konnte nicht geladen werden: ${error.message}`, "error");
  }
}

async function showBackupArchiveEntry(archiveId) {
  if (state.user?.role !== "admin") return;
  showView("backupView");
  await refreshBackupArchive(true);
  const card = Array.from(document.querySelectorAll("[data-backup-archive-item]"))
    .find((element) => String(element.dataset.backupArchiveItem) === String(archiveId));
  if (!card) return toast(`Backup #${archiveId} wurde im Archiv nicht gefunden.`, "error");
  card.classList.add("highlight");
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  setTimeout(() => card.classList.remove("highlight"), 1800);
}

async function loadBackupArchiveForRestore(archiveId, button = null) {
  if (state.user?.role !== "admin" || archiveId === null || archiveId === undefined || archiveId === "") return;
  const archived = (state.backup.archive || []).find((item) => String(item?.id) === String(archiveId));
  if (!archived || !archiveEntryReady(archived)) {
    return setBackupStatus("backupArchiveStatus", `Backup #${archiveId} ist nicht als vollständig und geprüft freigegeben.`, "error");
  }
  if ($("restoreFile")) $("restoreFile").value = "";
  clearRestoreImage(`Lade Backup #${archiveId} aus dem geschützten Archiv …`);
  const importGeneration = state.backup.importGeneration;
  if (button) button.disabled = true;
  setRestoreBusy(true);
  try {
    const image = await api(`/api/backup/archive/${encodeURIComponent(archiveId)}/download`);
    if (importGeneration !== state.backup.importGeneration) return;
    if (!image || typeof image !== "object" || Array.isArray(image)) throw new Error("Das Archiv lieferte kein gültiges JSON-Backup-Image");
    setBackupStatus("restoreImageStatus", `Prüfe Backup #${archiveId} erneut über die Backup-Prüfung …`, "neutral");
    const response = await api("/api/backup/inspect", {
      method: "POST",
      body: JSON.stringify({ image }),
    });
    if (importGeneration !== state.backup.importGeneration) return;
    const inspection = response.inspection || response;
    const requested = Number(inspection.requested_blocks);
    const successful = Number(inspection.successful_blocks);
    const failed = Number(inspection.failed_blocks);
    const inspectedTargets = restoreInspectionBlocks(inspection);
    const restorable = inspectedTargets.filter((item) => item.restorable === true).length;
    const archivedRequested = archiveTargetCount(archived, "requested");
    if (!restoreIntegrityOk(inspection)
      || inspection.digest_present !== true
      || inspection.digest_verified !== true
      || inspection.live_restore_compatible !== true
      || ![38, 42].includes(requested)
      || requested !== archivedRequested
      || successful !== requested
      || failed !== 0
      || inspectedTargets.length !== requested
      || restorable !== 38) {
      throw new Error("Das Wartungsbackup erfüllt nach erneuter Prüfung nicht den vollständigen 38er-/42er-Integritätsvertrag mit exakt 38 Restore-Zielen");
    }
    state.backup.image = image;
    state.backup.inspection = inspection;
    if ($("restoreWriteEnabled")) $("restoreWriteEnabled").checked = false;
    if ($("restoreConfirmation")) $("restoreConfirmation").value = "";
    if ($("restorePass4")) $("restorePass4").value = "";
    if ($("restoreResults")) $("restoreResults").innerHTML = "";
    renderRestoreBlockList();
    setBackupStatus("restoreImageStatus", inspectionSummary(inspection, archived?.filename || `Backup #${archiveId}`), "ok");
    setBackupStatus("restoreStatus", "Archivbackup erneut geprüft. Kein Ziel ist ausgewählt; Dry-Run bleibt der sichere Standard.", "ok");
    updateRestoreMode();
    $("restoreImageStatus")?.scrollIntoView({ behavior: "smooth", block: "center" });
  } catch (error) {
    if (importGeneration !== state.backup.importGeneration) return;
    state.backup.image = null;
    state.backup.inspection = null;
    renderRestoreBlockList();
    setBackupStatus("restoreImageStatus", `Archivbackup abgelehnt: ${error.message}`, "error");
    updateRestoreMode();
  } finally {
    if (importGeneration === state.backup.importGeneration) setRestoreBusy(false);
    if (button && importGeneration === state.backup.importGeneration) button.disabled = false;
  }
}

function backupImageText(image) {
  return typeof image === "string" ? image : `${JSON.stringify(image, null, 2)}\n`;
}

function downloadBackupImage(image, filename) {
  const safeName = String(filename || `open-dachs-backup-${new Date().toISOString().replace(/[:.]/g, "-")}.json`)
    .split(/[\\/]/).pop() || "open-dachs-backup.json";
  const blob = new Blob([backupImageText(image)], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = safeName;
  link.hidden = true;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

async function createBackupImage(event) {
  event?.preventDefault();
  const blocks = selectedBlocks("backup");
  if (!blocks.length) return setBackupStatus("backupStatus", "Bitte mindestens ein Blockziel auswählen.", "warn");
  const button = $("backupCreate");
  button.disabled = true;
  setBackupStatus("backupStatus", `Lese ${blocks.length} Blockziele seriell und erstelle das Image …`, "neutral");
  try {
    const response = await api("/api/backup/create", {
      method: "POST",
      body: JSON.stringify({ blocks }),
    });
    if (!response.image) throw new Error("Der Server hat kein Backup-Image geliefert");
    downloadBackupImage(response.image, response.filename);
    const inspection = response.inspection || {};
    const summary = response.summary || {};
    const successful = countItems(inspection.successful_blocks ?? inspection.successful_count ?? summary.successful);
    const failed = countItems(inspection.failed_blocks ?? inspection.failed_count ?? summary.failed);
    setBackupStatus(
      "backupStatus",
      `Sicherung heruntergeladen: ${successful || blocks.length - failed} von ${blocks.length} Blockzielen erfolgreich${failed ? `, ${failed} fehlgeschlagen` : ""}.`,
      failed ? "warn" : "ok",
    );
  } catch (error) {
    setBackupStatus("backupStatus", `Sicherung fehlgeschlagen: ${error.message}`, "error");
  } finally {
    button.disabled = selectedBlocks("backup").length === 0;
  }
}

function restoreInspectionBlocks(inspection) {
  const items = inspection?.blocks || inspection?.records || [];
  const seen = new Set();
  return items.map((item) => {
    const cpu = Number(item.cpu ?? 0);
    const block = Number(item.block);
    return {
      ...item,
      cpu,
      block,
      target_key: item.target_key || `${cpu}:${block}`,
      name: item.name || item.block_name || (cpu ? `Netzschutz · Überwachungs-CPU ${cpu}` : `Block ${block}`),
    };
  }).filter((item) => {
    if (!Number.isInteger(item.cpu) || !Number.isInteger(item.block) || seen.has(item.target_key)) return false;
    seen.add(item.target_key);
    return true;
  }).sort((left, right) => left.cpu - right.cpu || left.block - right.block);
}

function renderRestoreBlockList() {
  const container = $("restoreBlockList");
  if (!container) return;
  const blocks = restoreInspectionBlocks(state.backup.inspection);
  const available = blocks.some((item) => item.restorable);
  container.innerHTML = blocks.map((item) => blockChoiceMarkup(item, "restore", false, !item.restorable)).join("")
    || `<p class="muted">Noch kein geprüftes Backup-Image geladen.</p>`;
  $("restoreSelectAll").disabled = !available;
  $("restoreSelectNone").disabled = !available;
  updateRestoreSelectionStatus();
}

function restoreIntegrityOk(inspection) {
  const integrity = inspection?.integrity;
  if (typeof integrity === "boolean") return integrity;
  if (typeof integrity === "string") return !["failed", "invalid", "mismatch"].includes(integrity.toLowerCase());
  if (integrity && typeof integrity === "object") {
    if (typeof integrity.ok === "boolean") return integrity.ok;
    if (typeof integrity.verified === "boolean") return integrity.verified;
  }
  if (inspection?.digest_present === true && inspection?.digest_verified === false) return false;
  return true;
}

function inspectionImageMetadata(inspection) {
  return inspection?.image && typeof inspection.image === "object" ? inspection.image : (inspection || {});
}

function inspectionSummary(inspection, filename) {
  const blocks = restoreInspectionBlocks(inspection);
  const restorable = blocks.filter((item) => item.restorable).length;
  const metadata = inspectionImageMetadata(inspection);
  const created = metadata.created_utc ? new Date(metadata.created_utc) : null;
  const createdText = created && Number.isFinite(created.getTime()) ? created.toLocaleString("de-DE") : "Zeitpunkt unbekannt";
  const digest = String(metadata.image_sha256 || "");
  const digestText = digest ? ` · SHA-256 ${digest.slice(0, 12)}…` : "";
  return `${filename || "Backup-Image"} · ${createdText} · ${restorable} von ${blocks.length} Blockzielen wiederherstellbar${digestText}`;
}

function clearRestoreImage(message = "Noch kein Backup-Image geprüft.") {
  state.backup.importGeneration += 1;
  state.backup.image = null;
  state.backup.inspection = null;
  if ($("restoreWriteEnabled")) $("restoreWriteEnabled").checked = false;
  if ($("restoreConfirmation")) $("restoreConfirmation").value = "";
  if ($("restorePass4")) $("restorePass4").value = "";
  if ($("restoreBlockList")) $("restoreBlockList").innerHTML = `<p class="muted">Noch kein geprüftes Backup-Image geladen.</p>`;
  if ($("restoreResults")) $("restoreResults").innerHTML = "";
  if ($("restoreSelectAll")) $("restoreSelectAll").disabled = true;
  if ($("restoreSelectNone")) $("restoreSelectNone").disabled = true;
  setBackupStatus("restoreImageStatus", message, "neutral");
  updateRestoreMode();
}

async function inspectRestoreFile(event) {
  const file = event.target.files?.[0];
  clearRestoreImage(file ? "Lese Backup-Image …" : "Noch kein Backup-Image geprüft.");
  const importGeneration = state.backup.importGeneration;
  if (!file) return;
  if (file.size > BACKUP_MAX_FILE_BYTES) {
    event.target.value = "";
    return setBackupStatus("restoreImageStatus", "Datei zu groß. Backup-Images dürfen höchstens 1 MB groß sein.", "error");
  }
  try {
    const text = await file.text();
    if (importGeneration !== state.backup.importGeneration) return;
    let image;
    try { image = JSON.parse(text); } catch (_) { throw new Error("Die Datei enthält kein gültiges JSON-Backup-Image"); }
    if (!image || typeof image !== "object" || Array.isArray(image)) throw new Error("Das Backup-Image muss ein JSON-Objekt sein");
    setBackupStatus("restoreImageStatus", "Prüfe Schema, Prüfsummen und enthaltene Blöcke …", "neutral");
    const response = await api("/api/backup/inspect", {
      method: "POST",
      body: JSON.stringify({ image }),
    });
    if (importGeneration !== state.backup.importGeneration) return;
    const inspection = response.inspection || response;
    const integrityOk = restoreIntegrityOk(inspection);
    if (!integrityOk) throw new Error("Die Integritäts- oder Prüfsummenprüfung ist fehlgeschlagen");
    state.backup.image = image;
    state.backup.inspection = inspection;
    renderRestoreBlockList();
    setBackupStatus("restoreImageStatus", inspectionSummary(inspection, file.name), "ok");
    setBackupStatus(
      "restoreStatus",
      "Image geprüft. Aus Sicherheitsgründen ist noch kein Blockziel ausgewählt; wähle die gewünschten CPU-/Blockziele für den Dry-Run.",
      "neutral",
    );
    updateRestoreMode();
  } catch (error) {
    if (importGeneration !== state.backup.importGeneration) return;
    state.backup.image = null;
    state.backup.inspection = null;
    renderRestoreBlockList();
    event.target.value = "";
    setBackupStatus("restoreImageStatus", `Image abgelehnt: ${error.message}`, "error");
    updateRestoreMode();
  }
}

function updateRestoreSelectionStatus() {
  const selected = selectedBlocks("restore").length;
  const available = document.querySelectorAll("[data-restore-block]:not(:disabled)").length;
  if (state.backup.image) {
    const live = Boolean($("restoreWriteEnabled")?.checked);
    const mode = live ? " LIVE-Modus ist aktiviert." : " Dry-Run: Es wird nichts geschrieben.";
    setBackupStatus("restoreStatus", `${selected} von ${available} wiederherstellbaren Blockzielen ausgewählt.${mode}`, live ? "error" : (selected ? "neutral" : "warn"));
  }
  updateRestoreActionAvailability();
}

function updateRestoreActionAvailability() {
  const button = $("restoreSubmit");
  if (!button) return;
  button.disabled = state.backup.busy || state.user?.role !== "admin" || !state.backup.image || selectedBlocks("restore").length === 0;
}

function setRestoreBusy(busy) {
  state.backup.busy = busy;
  ["restoreFile", "restoreAuthLevel", "restorePass4", "restoreConfirmation"].forEach((id) => {
    if ($(id)) $(id).disabled = busy;
  });
  $("restoreWriteEnabled").disabled = busy || (Boolean(state.backup.image) && state.backup.inspection?.live_restore_compatible === false);
  document.querySelectorAll("[data-restore-block]").forEach((input) => {
    input.disabled = busy || input.closest(".backup-block-choice")?.classList.contains("disabled");
  });
  const available = document.querySelectorAll("[data-restore-block]:not(:disabled)").length > 0;
  $("restoreSelectAll").disabled = busy || !available;
  $("restoreSelectNone").disabled = busy || !available;
  updateRestoreActionAvailability();
}

function updateRestoreMode() {
  const toggle = $("restoreWriteEnabled");
  const button = $("restoreSubmit");
  if (!toggle || !button) return;
  const liveCompatible = !state.backup.image || state.backup.inspection?.live_restore_compatible !== false;
  if (!liveCompatible) toggle.checked = false;
  toggle.disabled = state.backup.busy || !liveCompatible;
  const live = Boolean(toggle.checked);
  button.textContent = live ? "Auswahl jetzt wiederherstellen" : "Auswahl als Dry-Run prüfen";
  button.classList.toggle("danger", live);
  button.classList.toggle("primary", !live);
  if ($("restorePass4")) $("restorePass4").required = false;
  if ($("restoreConfirmation")) {
    $("restoreConfirmation").required = live;
    $("restoreConfirmation").placeholder = live ? RESTORE_CONFIRMATION : "im Dry-Run nicht erforderlich";
  }
  const panel = toggle.closest(".backup-restore-panel") || toggle.closest(".panel");
  panel?.classList.toggle("backup-live", live);
  if (state.backup.image && !state.backup.busy) {
    setBackupStatus(
      "restoreStatus",
      !liveCompatible
        ? "Nur Dry-Run möglich: Für eine Live-Wiederherstellung müssen Prüfsumme, Packstand und Reglerkennung zum aktuellen Gerät passen."
        : live
        ? `LIVE-Modus vorbereitet. Vor dem Schreiben ist die Bestätigung ${RESTORE_CONFIRMATION} und ein zusätzlicher Browser-Dialog erforderlich.`
        : "Dry-Run aktiv: Der Regler wird gelesen und verglichen, aber nicht verändert.",
      live ? "error" : (liveCompatible ? "ok" : "warn"),
    );
  }
  updateRestoreActionAvailability();
}

function normalizedRestoreResult(item, response) {
  const audit = item.audit || {};
  return {
    ...item,
    error: item.error || audit.error || "",
    changed: item.changed ?? ["planned", "written"].includes(item.action),
    dry_run: item.dry_run ?? audit.dry_run ?? response.dry_run ?? (response.mode === "dry-run"),
    written: item.written ?? audit.written ?? item.action === "written",
    write_attempted: item.write_attempted ?? audit.write_attempted ?? false,
    ack_positive: item.ack_positive ?? audit.ack_positive,
    readback_ok: item.readback_ok ?? audit.readback_ok,
    changed_bytes: item.changed_bytes ?? audit.changed_bytes,
  };
}

function restoreResultLabel(item) {
  if (item.status === "not-attempted") return { label: "Nicht ausgeführt", tone: "warn" };
  if (item.write_attempted && !(item.written && item.readback_ok)) return { label: "Zustand unklar", tone: "error" };
  if (item.status === "failed") return { label: "Fehler", tone: "error" };
  if (item.error) return { label: "Fehler", tone: "error" };
  if (item.action === "failed") return { label: "Fehler", tone: "error" };
  if (!item.changed) return { label: "Unverändert", tone: "ok" };
  if (item.dry_run) return { label: "Würde geschrieben", tone: "warn" };
  if (item.written && item.ack_positive && item.readback_ok) return { label: "Wiederhergestellt", tone: "ok" };
  if (item.written) return { label: "Prüfung fehlgeschlagen", tone: "error" };
  return { label: item.status || "Nicht geschrieben", tone: "error" };
}

function renderRestoreResults(response) {
  const container = $("restoreResults");
  if (!container) return;
  const results = response.results || [];
  if (!results.length) {
    container.innerHTML = `<p class="muted">Der Server hat keine Blockergebnisse geliefert.</p>`;
    return;
  }
  const rows = results.map((rawItem) => {
    const item = normalizedRestoreResult(rawItem, response);
    const outcome = restoreResultLabel(item);
    const bytes = Array.isArray(item.changed_bytes) ? item.changed_bytes.length : (item.changed_bytes ?? "—");
    const ack = item.ack_positive === true ? "positiv" : (item.ack_positive === false ? "negativ" : "—");
    const readback = item.readback_ok === true ? "OK" : (item.readback_ok === false ? "Fehler" : "—");
    const cpu = Number(item.cpu ?? 0);
    const target = Number.isInteger(cpu) ? `CPU ${cpu} · Block ${item.block}` : `Block ${item.block}`;
    return `<tr>
      <td><strong>${escapeHtml(target)}</strong></td>
      <td>${escapeHtml(item.name || item.block_name || `Block ${item.block}`)}</td>
      <td><span class="restore-result restore-result-${outcome.tone}">${escapeHtml(outcome.label)}</span></td>
      <td>${escapeHtml(bytes)}</td><td>${escapeHtml(ack)}</td><td>${escapeHtml(readback)}</td>
      <td>${escapeHtml(item.error || "—")}</td>
    </tr>`;
  }).join("");
  container.innerHTML = `<div class="table-wrap"><table class="data-table restore-result-table">
    <thead><tr><th>Ziel</th><th>Name</th><th>Ergebnis</th><th>Geänderte Bytes</th><th>ACK</th><th>Readback</th><th>Hinweis</th></tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

async function restoreBackupImage(event) {
  event?.preventDefault();
  if (state.user?.role !== "admin") return;
  const blocks = selectedBlocks("restore");
  const writeEnabled = Boolean($("restoreWriteEnabled").checked);
  const confirmation = $("restoreConfirmation").value;
  if (!state.backup.image) return setBackupStatus("restoreStatus", "Bitte zuerst ein Backup-Image einlesen und prüfen.", "warn");
  if (!blocks.length) return setBackupStatus("restoreStatus", "Bitte mindestens ein wiederherstellbares Blockziel auswählen.", "warn");
  if (writeEnabled && confirmation.trim() !== RESTORE_CONFIRMATION) {
    return setBackupStatus("restoreStatus", `Für den LIVE-Modus exakt ${RESTORE_CONFIRMATION} eingeben.`, "error");
  }
  if (writeEnabled && !window.confirm(`LIVE-Wiederherstellung starten? ${blocks.length} ausgewählte Blockziele können am MSR2 verändert werden. Jedes Ziel wird anschließend zurückgelesen und geprüft.`)) return;
  const restoreGeneration = state.backup.importGeneration;
  const restoreImage = state.backup.image;
  const restoreInspection = state.backup.inspection;
  const restoreRequestIsStale = () => (
    restoreGeneration !== state.backup.importGeneration
    || restoreImage !== state.backup.image
  );
  setRestoreBusy(true);
  setBackupStatus(
    "restoreStatus",
    writeEnabled ? `LIVE-Wiederherstellung für ${blocks.length} Blockziele läuft …` : `Dry-Run für ${blocks.length} Blockziele läuft; es wird nichts geschrieben …`,
    writeEnabled ? "error" : "neutral",
  );
  try {
    const response = await api("/api/backup/restore", {
      method: "POST",
      body: JSON.stringify({
        image: restoreImage,
        image_sha256: inspectionImageMetadata(restoreInspection).image_sha256 || null,
        blocks,
        auth_level: Number($("restoreAuthLevel").value),
        pass4: $("restorePass4").value,
        write_enabled: writeEnabled,
        confirmation,
      }),
    });
    if (restoreRequestIsStale()) return;
    renderRestoreResults(response);
    const summary = response.summary || {};
    const failed = countItems(response.failed_blocks ?? summary.failed);
    const changed = countItems(response.differing_blocks ?? summary.planned);
    const written = countItems(response.written_blocks ?? summary.written ?? summary.restored);
    const uncertain = countItems(response.uncertain_blocks ?? summary.uncertain);
    const unchanged = countItems(response.unchanged_blocks ?? summary.unchanged);
    const dryRun = response.dry_run ?? (response.mode === "dry-run");
    if (dryRun) {
      setBackupStatus("restoreStatus", `Dry-Run beendet: ${changed} abweichend, ${unchanged} unverändert${failed ? `, ${failed} fehlgeschlagen` : ""}. Der Regler wurde nicht verändert.`, failed ? "warn" : "ok");
    } else {
      setBackupStatus(
        "restoreStatus",
        `Wiederherstellung beendet: ${written} geschrieben und per Rückleseprüfung bestätigt, ${unchanged} unverändert${uncertain ? `, ${uncertain} mit unklarem Zielzustand` : ""}${failed ? `, ${failed} fehlgeschlagen` : ""}.`,
        failed || uncertain || response.ok === false ? "error" : "ok",
      );
      await refreshAudit();
    }
  } catch (error) {
    if (restoreRequestIsStale()) return;
    if (Array.isArray(error.payload?.results)) {
      renderRestoreResults(error.payload);
      if (writeEnabled) await refreshAudit();
    }
    setBackupStatus("restoreStatus", `${writeEnabled ? "Wiederherstellung" : "Dry-Run"} fehlgeschlagen: ${error.message}`, "error");
  } finally {
    if (!restoreRequestIsStale()) setRestoreBusy(false);
  }
}

async function refreshAudit() {
  if (state.user?.role !== "admin") { $("auditRows").innerHTML = `<tr><td colspan="5">Nur für Admin sichtbar.</td></tr>`; return; }
  try {
    const data = await api("/api/audit");
    $("auditRows").innerHTML = (data.items || []).map((item) => {
      const audit = item.audit || {};
      const scope = audit.readback_scope === "changed-fields" ? "FELD" : "BLOCK";
      const attempts = Number(audit.readback_attempts || 0);
      let result;
      if (audit.operation === "backup-restore" && !audit.changed && !audit.error) {
        result = "WIEDERHERSTELLUNG · UNVERÄNDERT";
      } else if (audit.operation === "backup-restore" && audit.write_attempted && !audit.written) {
        result = `WIEDERHERSTELLUNG · ZUSTAND UNKLAR${audit.error ? ` · ${audit.error}` : ""}`;
      } else if (audit.written) {
        result = `${audit.operation === "backup-restore" ? "WIEDERHERGESTELLT" : "GESCHRIEBEN"} · RÜCKLESEPRÜFUNG ${scope}${attempts ? ` · ${attempts}×` : ""}`;
      } else if (audit.dry_run) {
        result = audit.operation === "backup-restore" ? "WIEDERHERSTELLUNG · DRY-RUN" : "DRY-RUN";
      } else {
        result = audit.error || "Fehler";
      }
      const target = Number(audit.cpu || 0) ? `CPU ${audit.cpu} · ${item.block}` : item.block;
      return `<tr class="${audit.critical ? "critical-audit" : ""}"><td>${escapeHtml(new Date(item.recorded_at).toLocaleString("de-DE"))}</td><td>${escapeHtml(item.username)}</td><td>${escapeHtml(target)}</td><td>${escapeHtml(result)}</td><td>${escapeHtml((audit.changed_keys || []).join(", "))}</td></tr>`;
    }).join("") || `<tr><td colspan="5">Noch keine Schreibversuche.</td></tr>`;
  } catch (error) { toast(error.message); }
}

function toast(message, tone = "neutral") { const element=$("toast"); element.textContent=message; element.classList.toggle("error", tone === "error"); element.classList.add("visible"); clearTimeout(toast.timer); toast.timer=setTimeout(()=>element.classList.remove("visible"),4200); }

document.addEventListener("DOMContentLoaded", () => {
  $("loginForm").addEventListener("submit", login);
  $("logoutButton").addEventListener("click", async () => { await api("/api/logout", { method:"POST", body:"{}" }); showLogin(); });
  document.querySelectorAll(".tab-button").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
  document.querySelectorAll("[data-view-target]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.viewTarget)));
  $("backupSelectAll").addEventListener("click", () => setBlockSelection("backup", true));
  $("backupSelectNone").addEventListener("click", () => setBlockSelection("backup", false));
  $("backupBlockList").addEventListener("change", updateBackupSelectionStatus);
  $("backupCreate").addEventListener("click", createBackupImage);
  $("backupArchiveRefresh").addEventListener("click", () => refreshBackupArchive());
  $("backupArchiveList").addEventListener("click", (event) => {
    const loadButton = event.target.closest("[data-load-backup-archive]");
    if (loadButton) {
      loadBackupArchiveForRestore(loadButton.dataset.loadBackupArchive, loadButton);
      return;
    }
    const reportButton = event.target.closest("[data-open-maintenance-report]");
    if (reportButton) {
      showView("maintenanceView");
      loadMaintenanceReport(Number(reportButton.dataset.openMaintenanceReport));
    }
  });
  $("restoreFile").addEventListener("change", inspectRestoreFile);
  $("restoreSelectAll").addEventListener("click", () => setBlockSelection("restore", true));
  $("restoreSelectNone").addEventListener("click", () => setBlockSelection("restore", false));
  $("restoreBlockList").addEventListener("change", updateRestoreSelectionStatus);
  $("restoreWriteEnabled").checked = false;
  $("restoreWriteEnabled").addEventListener("change", updateRestoreMode);
  const restoreForm = $("restoreSubmit").closest("form");
  if (restoreForm) restoreForm.addEventListener("submit", restoreBackupImage);
  else $("restoreSubmit").addEventListener("click", restoreBackupImage);
  updateRestoreMode();
  $("dashboardEdit").addEventListener("click", openDashboardEditor);
  $("dashboardClose").addEventListener("click", closeDashboardEditor);
  $("dashboardCancel").addEventListener("click", closeDashboardEditor);
  $("dashboardSave").addEventListener("click", saveDashboardEditor);
  $("dashboardReset").addEventListener("click", () => {
    state.dashboard.editCards = (state.dashboard.settings?.default_cards || []).map((card) => ({ block: Number(card.block), key: card.key }));
    renderDashboardEditor();
  });
  $("dashboardFieldSearch").addEventListener("input", renderDashboardEditor);
  $("dashboardCardList").addEventListener("click", (event) => {
    const remove = event.target.closest("[data-dashboard-remove]");
    if (remove) {
      state.dashboard.editCards.splice(Number(remove.dataset.dashboardRemove), 1);
      return renderDashboardEditor();
    }
    const move = event.target.closest("[data-dashboard-move]");
    if (move) moveDashboardCard(Number(move.dataset.dashboardIndex), move.dataset.dashboardMove === "up" ? -1 : 1);
  });
  $("dashboardFieldList").addEventListener("click", (event) => {
    const button = event.target.closest("[data-dashboard-add]");
    if (!button) return;
    const fields = JSON.parse($("dashboardFieldList").dataset.fields || "[]");
    const field = fields[Number(button.dataset.dashboardAdd)];
    if (!field) return;
    const maximum = Number(state.dashboard.settings?.max_cards || 24);
    if (state.dashboard.editCards.length >= maximum) return toast(`Maximal ${maximum} Kacheln sind möglich.`);
    state.dashboard.editCards.push(field);
    renderDashboardEditor();
  });
  $("dashboardEditor").addEventListener("click", (event) => { if (event.target === $("dashboardEditor")) closeDashboardEditor(); });
  document.querySelectorAll("[data-open-changelog]").forEach((button) => button.addEventListener("click", openChangelog));
  $("changelogClose").addEventListener("click", closeChangelog);
  $("changelogDone").addEventListener("click", closeChangelog);
  $("changelogModal").addEventListener("click", (event) => { if (event.target === $("changelogModal")) closeChangelog(); });
  $("serviceCatalogSearch").addEventListener("input", () => {
    clearTimeout(state.serviceCatalogTimer);
    state.serviceCatalogTimer = setTimeout(refreshServiceCatalog, 250);
  });
  $("hmiOpenFaultCatalog").addEventListener("click", openCurrentFaultCatalog);
  document.querySelectorAll("[data-schematic-mode]").forEach((button) => button.addEventListener("click", () => setSchematicMode(button.dataset.schematicMode)));
  let initialSchematicMode = "overview";
  try { initialSchematicMode = localStorage.getItem("open-dachs-schematic-mode") || "overview"; } catch (_) { /* optional */ }
  setSchematicMode(initialSchematicMode);
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (!$("changelogModal").hidden) closeChangelog();
    else if (!$("dashboardEditor").hidden) closeDashboardEditor();
  });
  $("maintenanceRefresh").addEventListener("click", () => refreshMaintenance(true));
  $("maintenanceCreate").addEventListener("click", createMaintenanceReport);
  $("maintenanceReportRows").addEventListener("click", (event) => {
    const backupButton = event.target.closest("[data-open-backup]");
    if (backupButton) {
      showBackupArchiveEntry(backupButton.dataset.openBackup);
      return;
    }
    const deleteButton = event.target.closest("[data-maintenance-delete]");
    if (deleteButton) {
      deleteMaintenanceReport(Number(deleteButton.dataset.maintenanceDelete));
      return;
    }
    const button = event.target.closest("[data-maintenance-report]");
    if (button) loadMaintenanceReport(Number(button.dataset.maintenanceReport));
  });
  $("maintenanceBackupSummary").addEventListener("click", (event) => {
    const button = event.target.closest("[data-open-backup]");
    if (button) showBackupArchiveEntry(button.dataset.openBackup);
  });
  $("maintenanceForm").addEventListener("submit", saveMaintenanceDraft);
  $("maintenanceFuelType").addEventListener("change", () => saveMaintenanceDraft(null, true));
  $("maintenanceForm").addEventListener("change", (event) => {
    if (event.target.id !== "maintenanceFuelType") scheduleMaintenanceAutosave();
  });
  $("maintenancePw4Read").addEventListener("click", readMaintenancePw4);
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
  $("authPreviewButton").addEventListener("click", refreshAuthPreview);
  $("authPreviewApply").addEventListener("click", applyAuthPreview);
  $("passwordForm").addEventListener("submit", changePassword);
  $("systemTabs").addEventListener("click", (event) => {
    const button = event.target.closest("[data-system-tab]");
    if (button) selectSystemTab(button.dataset.systemTab);
  });
  $("userCreateForm").addEventListener("submit", createSystemUser);
  $("usersRefresh").addEventListener("click", refreshSystemUsers);
  $("userRows").addEventListener("click", (event) => {
    const save = event.target.closest("[data-user-save]");
    if (save) return saveSystemUser(save.dataset.userSave, save.closest("[data-user-row]"));
    const remove = event.target.closest("[data-user-delete]");
    if (remove) deleteSystemUser(remove.dataset.userDelete);
  });
  $("apiSettingsForm").addEventListener("submit", saveApiSettings);
  $("tokenCreateForm").addEventListener("submit", createApiToken);
  $("tokensRefresh").addEventListener("click", refreshSystemTokens);
  $("tokenRows").addEventListener("click", (event) => {
    const save = event.target.closest("[data-token-save]");
    if (save) return saveApiToken(Number(save.dataset.tokenSave), save.closest("[data-token-row]"));
    const remove = event.target.closest("[data-token-delete]");
    if (remove) deleteApiToken(Number(remove.dataset.tokenDelete));
  });
  $("tokenSecretCopy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("tokenSecret").textContent || "");
      toast("Token in die Zwischenablage kopiert.");
    } catch (_) { toast("Token konnte nicht automatisch kopiert werden."); }
  });
  $("maintenanceTestMode").addEventListener("change", changeMaintenanceMode);
  $("sootFilterSettingsForm").addEventListener("submit", saveSootFilterSettings);
  $("temperatureRange").addEventListener("change", applyHistoryPreset);
  $("historyHoursForm").addEventListener("submit", applyHistoryHours);
  $("historyDateForm").addEventListener("submit", applyHistoryDates);
  $("resetHistoryRange").addEventListener("click", resetHistoryRange);
  document.querySelectorAll('[data-action="reset-chart-zoom"]').forEach((button) => button.addEventListener("click", resetChartZoom));
  document.querySelectorAll("[data-action=refresh-live]").forEach((button)=>button.addEventListener("click", refreshLive));
  document.querySelectorAll("[data-action=reload-block]").forEach((button)=>button.addEventListener("click",()=>loadBlock(state.selectedBlock, state.selectedCpu)));
  document.querySelectorAll("[data-action=refresh-audit]").forEach((button)=>button.addEventListener("click",refreshAudit));
  window.addEventListener("resize", () => {
    if (state.selectedView !== "monitorView") return;
    redrawAllCharts();
  });
  boot();
});
