import json
from pathlib import Path
import shutil
import subprocess
import unittest
from urllib.parse import parse_qs

from open_dachs_manager.web import _history_bounds


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "src" / "open_dachs_manager" / "web"


class HistoryUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        cls.app = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
        cls.style = (WEB_ROOT / "style.css").read_text(encoding="utf-8")

    def test_requested_presets_free_hours_dates_and_enter_forms_are_present(self):
        select_start = self.index.index('id="temperatureRange"')
        select_end = self.index.index("</select>", select_start)
        select = self.index[select_start:select_end]
        values = [1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 24, 48]
        self.assertEqual(
            [value for value in values if f'<option value="{value}"' in select],
            values,
        )
        self.assertNotIn('value="168"', select)
        self.assertNotIn('value="720"', select)
        self.assertIn('id="historyHoursForm"', self.index)
        self.assertIn('id="historyHours" type="number" min="1" max="720" step="1"', self.index)
        self.assertIn('id="historyDateForm"', self.index)
        self.assertIn('id="historyStart" type="datetime-local"', self.index)
        self.assertIn('id="historyEnd" type="datetime-local"', self.index)
        self.assertIn('$("historyHoursForm").addEventListener("submit", applyHistoryHours)', self.app)
        self.assertIn('$("historyDateForm").addEventListener("submit", applyHistoryDates)', self.app)

    def test_all_charts_share_response_window_zoom_and_running_background(self):
        self.assertIn("chartZoomRange: null", self.app)
        self.assertNotIn("chartZoomRanges", self.app)
        self.assertIn("state.chartZoomRange={start:", self.app)
        self.assertIn("redrawAllCharts();", self.app)
        self.assertIn("state.historyWindow = Number.isFinite(responseStart)", self.app)
        self.assertIn("buildChartRunBands(batch.series?.drehzahl || [], state.historyWindow)", self.app)
        self.assertIn("drawChartRunBands(ctx, colors, start, end", self.app)
        self.assertIn("historyAutoRefreshDue()", self.app)
        self.assertIn('if (request.mode !== "hours") return null;', self.app)
        self.assertIn("return 5 * 60_000;", self.app)
        self.assertIn("--chart-running-bg", self.style)
        self.assertIn("--chart-stopped-bg", self.style)

    def test_motor_status_timeline_is_event_preserving_and_shares_zoom(self):
        motor_chart = self.index.index('id="motorChart"')
        status_chart = self.index.index('id="motorStatusChart"')
        exhaust_chart = self.index.index('id="exhaustChart"')
        self.assertLess(motor_chart, status_chart)
        self.assertLess(status_chart, exhaust_chart)
        self.assertIn('id="motorStatusLegend"', self.index)
        self.assertIn('id="motorStatusHoverText"', self.index)
        self.assertIn('aria-label="Motorstatus aus Block 24 als Zeitband;', self.index)
        self.assertIn('id="motorStatusChart" class="chart motor-status-chart" height="112" tabindex="0"', self.index)
        self.assertIn('api(`/api/motor-status-history?${selection.query}`)', self.app)
        self.assertIn('["temperature", "motor", "motorStatus", "exhaust"]', self.app)
        self.assertIn('bindChartZoom($("motorStatusChart"), "motorStatus")', self.app)
        self.assertIn('["temperatureChart", "motorChart", "motorStatusChart", "exhaustChart"]', self.app)
        self.assertNotIn("drawChartRunBands(ctx, colors, start, end, left, top, plotW, plotH);\n  const segments", self.app)
        for variable in (
            "--motor-status-off",
            "--motor-status-preparation",
            "--motor-status-start",
            "--motor-status-running",
            "--motor-status-shutdown",
            "--motor-status-fault",
            "--motor-status-ok",
            "--motor-status-unknown",
        ):
            self.assertIn(variable, self.style)
        self.assertIn("#monitorView #motorStatusChart { height:96px; }", self.style)
        self.assertIn(".motor-status-chart { height:112px; touch-action:pan-y; }", self.style)
        self.assertIn('if (group === "motorStatus") canvas.addEventListener("keydown"', self.app)

    @unittest.skipUnless(shutil.which("node"), "Node.js ist für den JS-Regressionscheck nötig")
    def test_motor_status_tones_labels_and_gaps_are_deterministic(self):
        script = """
const vm = require('vm');
const code = require('fs').readFileSync(process.argv[1], 'utf8');
const dummy = () => ({});
const sandbox = {
  console, URLSearchParams, Intl, Date, Number, Math, JSON, Set, Map, WeakMap,
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
  document: {
    documentElement: {},
    getElementById: dummy,
    querySelector: () => ({content: ''}),
    querySelectorAll: () => [],
    addEventListener: () => {},
  },
  window: {devicePixelRatio: 1, matchMedia: () => ({matches: false}), addEventListener: () => {}},
  getComputedStyle: () => ({getPropertyValue: () => ''}),
};
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
const result = vm.runInContext(`(() => {
  state.schema = {blocks:[{block:24,fields:[{key:'Hka_Mw1.bMotorStatus',choices:[
    {value:0,label:'OK'},
    {value:11,label:'Abschaltroutine 1'},
    {value:16,label:'Dachs >4 Minuten AUS'},
    {value:20,label:'Startvorbereitung'},
    {value:21,label:'Starteinleitung'},
    {value:22,label:'Anlasser ein'},
    {value:35,label:'KEINE Stellmotorbewegung'}
  ]}]}]};
  state.motorStatusSegments = [
    {from:'2026-08-20T10:00:00Z',to:'2026-08-20T10:01:00Z',status:16},
    {from:'2026-08-20T10:05:00Z',to:'2026-08-20T10:06:00Z',status:35}
  ];
  const segments = normalizedMotorStatusSegments();
  const collision = layoutMotorStatusMarkers([
    {start:Date.parse('2026-08-20T10:00:00Z'),end:Date.parse('2026-08-20T10:00:01Z'),status:16},
    {start:Date.parse('2026-08-20T10:00:01Z'),end:Date.parse('2026-08-20T10:00:02Z'),status:20},
    {start:Date.parse('2026-08-20T10:00:02Z'),end:Date.parse('2026-08-20T10:00:03Z'),status:21},
    {start:Date.parse('2026-08-20T10:00:03Z'),end:Date.parse('2026-08-20T10:00:04Z'),status:22},
    {start:Date.parse('2026-08-20T10:00:04Z'),end:Date.parse('2026-08-20T10:01:00Z'),status:35}
  ],Date.parse('2026-08-20T00:00:00Z'),Date.parse('2026-08-21T00:00:00Z'),50,10,900,60);
  const narrowCanvas = {getBoundingClientRect:() => ({left:10,top:20,width:266,height:96})};
  state.chartGeometries.set(narrowCanvas,{canvasWidth:280,canvasHeight:96});
  const pointerScale = chartPointerPosition(narrowCanvas,{clientX:276,clientY:116});
  const heightCanvas = {dataset:{},height:320,getAttribute:() => '320'};
  const baseHeightFirst = chartBaseHeight(heightCanvas);
  heightCanvas.height = 640;
  const baseHeightSecond = chartBaseHeight(heightCanvas);
  return {
    tones:[0,16,20,21,22,35,11,10,99].map(motorStatusTone),
    label:motorStatusLabel(21),
    unknown:motorStatusLabel(99),
    first:motorStatusSegmentAt(segments,Date.parse('2026-08-20T10:00:30Z'))?.status,
    gap:motorStatusSegmentAt(segments,Date.parse('2026-08-20T10:03:00Z')),
    second:motorStatusSegmentAt(segments,Date.parse('2026-08-20T10:05:30Z'))?.status,
    collisionCount:collision.length,
    collisionStatuses:collision.map((hitbox) => hitbox.segment.status),
    collisionRects:new Set(collision.map((hitbox) => [hitbox.x,hitbox.y,hitbox.width,hitbox.height].join('/'))).size,
    pointerScale,
    baseHeights:[baseHeightFirst,baseHeightSecond],
  };
})()`, sandbox);
process.stdout.write(JSON.stringify(result));
"""
        completed = subprocess.run(
            ["node", "-e", script, str(WEB_ROOT / "app.js")],
            check=True,
            text=True,
            capture_output=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(
            result["tones"],
            ["ok", "off", "preparation", "start", "start", "running", "shutdown", "fault", "unknown"],
        )
        self.assertEqual(result["label"], "Starteinleitung")
        self.assertEqual(result["unknown"], "Unbekannter Status")
        self.assertEqual(result["first"], 16)
        self.assertIsNone(result["gap"])
        self.assertEqual(result["second"], 35)
        self.assertEqual(result["collisionCount"], 5)
        self.assertEqual(result["collisionStatuses"], [16, 20, 21, 22, 35])
        self.assertEqual(result["collisionRects"], 5)
        self.assertEqual(result["pointerScale"], {"x": 280, "y": 96})
        self.assertEqual(result["baseHeights"], [320, 320])

    @unittest.skipUnless(shutil.which("node"), "Node.js ist für den JS-Regressionscheck nötig")
    def test_running_band_builder_keeps_gaps_and_transitions_deterministic(self):
        script = """
const vm = require('vm');
const code = require('fs').readFileSync(process.argv[1], 'utf8');
const dummy = () => ({});
const sandbox = {
  console, URLSearchParams, Intl, Date, Number, Math, JSON, Set, Map, WeakMap,
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
  document: {
    documentElement: {},
    getElementById: dummy,
    querySelector: () => ({content: ''}),
    querySelectorAll: () => [],
    addEventListener: () => {},
  },
  window: {devicePixelRatio: 1, matchMedia: () => ({matches: false}), addEventListener: () => {}},
  getComputedStyle: () => ({getPropertyValue: () => ''}),
};
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
const transition = vm.runInContext(`buildChartRunBands([
  {recorded_at:'2026-08-19T10:00:00Z',value:0},
  {recorded_at:'2026-08-19T10:01:00Z',value:1500},
  {recorded_at:'2026-08-19T10:02:00Z',value:0}
], {start:Date.parse('2026-08-19T10:00:00Z'),end:Date.parse('2026-08-19T10:03:00Z')})`, sandbox);
const downsampled = vm.runInContext(`buildChartRunBands(Array.from(
  {length:Math.floor((30 * 24 * 60) / 22) + 1},
  (_, index) => ({recorded_at:new Date(Date.parse('2026-08-01T00:00:00Z') + index * 22 * 60000).toISOString(),value:0})
), {
  start:Date.parse('2026-08-01T00:00:00Z'),end:Date.parse('2026-08-31T00:00:00Z')
})`, sandbox);
const sparse = vm.runInContext(`buildChartRunBands([
  {recorded_at:'2026-08-19T00:00:00Z',value:0},
  {recorded_at:'2026-08-19T01:59:00Z',value:0}
], {
  start:Date.parse('2026-08-19T00:00:00Z'),end:Date.parse('2026-08-19T02:00:00Z')
})`, sandbox);
const real48HourGap = vm.runInContext(`(() => {
  const before=Date.parse('2026-08-17T17:12:23.401181Z');
  const after=Date.parse('2026-08-17T17:17:17.215677Z');
  const bucket=(48 * 3600000) / 2000;
  const point=(time) => ({recorded_at:new Date(time).toISOString(),value:0});
  return buildChartRunBands([
    point(before - 2 * bucket), point(before - bucket), point(before),
    point(after), point(after + bucket),
  ], {
    start:Date.parse('2026-08-17T00:00:00Z'),end:Date.parse('2026-08-19T00:00:00Z')
  });
})()`, sandbox);
const real30DayGap = vm.runInContext(`(() => {
  const before=Date.parse('2026-08-11T13:15:47.830751Z');
  const after=Date.parse('2026-08-11T14:13:11.505852Z');
  const bucket=(30 * 24 * 3600000) / 2000;
  const point=(time) => ({recorded_at:new Date(time).toISOString(),value:0});
  return buildChartRunBands([
    point(before - 2 * bucket), point(before - bucket), point(before),
    point(after), point(after + bucket),
  ], {
    start:Date.parse('2026-08-01T00:00:00Z'),end:Date.parse('2026-08-31T00:00:00Z')
  });
})()`, sandbox);
process.stdout.write(JSON.stringify({transition, downsampled, sparse, real48HourGap, real30DayGap}));
"""
        completed = subprocess.run(
            ["node", "-e", script, str(WEB_ROOT / "app.js")],
            check=True,
            text=True,
            capture_output=True,
        )
        result = json.loads(completed.stdout)
        bands = result["transition"]
        self.assertEqual([item["running"] for item in bands], [False, True, False])
        self.assertEqual(
            [(item["start"], item["end"]) for item in bands],
            [
                (1787133600000, 1787133660000),
                (1787133660000, 1787133720000),
                (1787133720000, 1787133780000),
            ],
        )
        self.assertEqual(
            result["downsampled"],
            [{"start": 1785542400000, "end": 1788134400000, "running": False}],
        )
        self.assertEqual(len(result["sparse"]), 2)
        self.assertLessEqual(result["sparse"][0]["end"] - result["sparse"][0]["start"], 60_000)
        self.assertGreater(
            result["sparse"][1]["start"] - result["sparse"][0]["end"],
            60 * 60_000,
        )
        # Genuine production gaps must remain neutral even when the RPM before
        # and after the outage is the same.  Normal neighbouring bucket points
        # around each gap still merge into one band on either side.
        self.assertEqual(len(result["real48HourGap"]), 2)
        self.assertTrue(all(not item["running"] for item in result["real48HourGap"]))
        self.assertGreater(
            result["real48HourGap"][1]["start"] - result["real48HourGap"][0]["end"],
            3 * 60_000,
        )
        self.assertEqual(len(result["real30DayGap"]), 2)
        self.assertTrue(all(not item["running"] for item in result["real30DayGap"]))
        self.assertGreater(
            result["real30DayGap"][1]["start"] - result["real30DayGap"][0]["end"],
            35 * 60_000,
        )

    def test_navigation_auth_roles_and_maintenance_pw4_match_the_ui_contract(self):
        nav = self.index[
            self.index.index('<nav class="main-tabs"'):
            self.index.index("</nav>", self.index.index('<nav class="main-tabs"'))
        ]
        labels = [
            "Übersicht", "Überwachung", "Einstellung", "Backup", "Audit",
            "System", "Wartung", "Fehlerkatalog",
        ]
        positions = [nav.index(f">{label}</button>") for label in labels]
        self.assertEqual(positions, sorted(positions))
        for element_id in (
            "authLevel", "restoreAuthLevel", "maintenanceAuthLevel", "apiAuthLevel",
        ):
            self.assertIn(f'<select id="{element_id}"', self.index)
        self.assertNotIn('max="255"', self.index)
        self.assertIn('id="maintenancePw4Read"', self.index)
        self.assertIn('api("/api/auth-preview")', self.app)
        self.assertIn('$("maintenancePw4Read").addEventListener("click", readMaintenancePw4)', self.app)
        self.assertIn("pw4Generation", self.app)
        self.assertIn("generation !== state.maintenance.pw4Generation", self.app)
        self.assertIn('state.maintenance.current?.status !== "draft"', self.app)
        self.assertIn('clearMaintenancePw4("Beim Verlassen der Wartung verworfen")', self.app)
        self.assertIn('clearMaintenancePw4("Nach Abschlussversuch verworfen")', self.app)
        self.assertIn("}, 60_000);", self.app)

    def test_server_rejects_free_hour_values_outside_the_ui_contract(self):
        for hours in (1, 720):
            _start, _end, duration = _history_bounds(parse_qs(f"hours={hours}"))
            self.assertEqual(duration, hours * 3600)
        for hours in ("0", "721", "-1", "1.5", "text"):
            with self.subTest(hours=hours), self.assertRaises(ValueError):
                _history_bounds(parse_qs(f"hours={hours}"))


if __name__ == "__main__":
    unittest.main()
