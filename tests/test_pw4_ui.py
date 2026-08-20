import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "src" / "open_dachs_manager" / "web"


class Pw4UITests(unittest.TestCase):
    def test_every_auth_dropdown_contains_only_supported_levels(self):
        index = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        for element_id in (
            "authLevel",
            "restoreAuthLevel",
            "maintenanceAuthLevel",
            "apiAuthLevel",
        ):
            with self.subTest(element_id=element_id):
                match = re.search(
                    rf'<select id="{element_id}"[^>]*>(.*?)</select>',
                    index,
                    re.DOTALL,
                )
                self.assertIsNotNone(match)
                self.assertEqual(
                    re.findall(r'<option value="([^"]+)"', match.group(1)),
                    ["1", "2", "3", "4", "5"],
                )
                self.assertIn("5 · Legacy/V2", match.group(1))

    @unittest.skipUnless(shutil.which("node"), "Node.js ist für den JS-Regressionscheck nötig")
    def test_settings_pw4_discards_late_logout_response_navigation_and_timer(self):
        script = r"""
const vm = require('vm');
const code = require('fs').readFileSync(process.argv[1], 'utf8');
const elements = new Map();
const timers = [];
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    id, hidden:false, disabled:false, checked:false, value:'', textContent:'',
    innerHTML:'', className:'', dataset:{}, style:{},
    classList:{toggle:()=>{}, add:()=>{}, remove:()=>{}},
    addEventListener:()=>{}, querySelector:()=>null, querySelectorAll:()=>[],
    closest:()=>null, focus:()=>{}, reset:()=>{},
  });
  return elements.get(id);
}
const sandbox = {
  console, URLSearchParams, Intl, Date, Number, Math, JSON, Set, Map, WeakMap,
  Promise,
  setTimeout: (callback, delay) => {
    const item = {callback, delay, active:true};
    timers.push(item);
    return timers.length;
  },
  clearTimeout: (handle) => {
    if (timers[handle - 1]) timers[handle - 1].active = false;
  },
  setInterval: () => 0,
  clearInterval: () => {},
  document: {
    documentElement: {},
    getElementById: element,
    querySelector: () => ({content: ''}),
    querySelectorAll: () => [],
    addEventListener: () => {},
  },
  window: {
    devicePixelRatio: 1,
    matchMedia: () => ({matches: false}),
    addEventListener: () => {},
  },
  getComputedStyle: () => ({getPropertyValue: () => ''}),
};
vm.createContext(sandbox);
vm.runInContext(code, sandbox);

(async () => {
  let resolveRequest;
  sandbox.pendingApi = new Promise((resolve) => { resolveRequest = resolve; });
  vm.runInContext(`
    state.user = {role:'admin', username:'admin'};
    state.selectedView = 'settingsView';
    api = () => pendingApi;
  `, sandbox);
  const pending = vm.runInContext('refreshAuthPreview()', sandbox);
  vm.runInContext('showLogin()', sandbox);
  resolveRequest({ok:true, pw4:'1234', serial_number:'TEST', operating_hours:1});
  await pending;
  const late = vm.runInContext(`({
    preview: state.authPreview,
    shown: document.getElementById('authPreviewPw4').textContent,
    pass4: document.getElementById('pass4').value
  })`, sandbox);

  vm.runInContext(`
    state.user = {role:'admin', username:'admin'};
    state.selectedView = 'settingsView';
    api = async () => ({ok:true, pw4:'5678', serial_number:'TEST', operating_hours:1});
  `, sandbox);
  await vm.runInContext('refreshAuthPreview()', sandbox);
  vm.runInContext('applyAuthPreview()', sandbox);
  const beforeNavigation = vm.runInContext(`({
    shown: document.getElementById('authPreviewPw4').textContent,
    pass4: document.getElementById('pass4').value
  })`, sandbox);
  vm.runInContext("showView('overviewView')", sandbox);
  const afterNavigation = vm.runInContext(`({
    preview: state.authPreview,
    shown: document.getElementById('authPreviewPw4').textContent,
    pass4: document.getElementById('pass4').value
  })`, sandbox);

  vm.runInContext(`
    state.user = {role:'admin', username:'admin'};
    state.selectedView = 'settingsView';
  `, sandbox);
  await vm.runInContext('refreshAuthPreview()', sandbox);
  vm.runInContext('applyAuthPreview()', sandbox);
  const timer = timers.find((item) => item.active && item.delay === 60000);
  if (!timer) throw new Error('missing PW4 expiry timer');
  timer.callback();
  const afterTimer = vm.runInContext(`({
    preview: state.authPreview,
    shown: document.getElementById('authPreviewPw4').textContent,
    pass4: document.getElementById('pass4').value
  })`, sandbox);

  process.stdout.write(JSON.stringify({late, beforeNavigation, afterNavigation, afterTimer}));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        completed = subprocess.run(
            ["node", "-e", script, str(WEB_ROOT / "app.js")],
            check=True,
            text=True,
            capture_output=True,
        )
        result = json.loads(completed.stdout)
        self.assertIsNone(result["late"]["preview"])
        self.assertEqual(result["late"]["shown"], "—")
        self.assertEqual(result["late"]["pass4"], "")
        self.assertEqual(result["beforeNavigation"], {"shown": "5678", "pass4": "5678"})
        self.assertIsNone(result["afterNavigation"]["preview"])
        self.assertEqual(result["afterNavigation"]["shown"], "—")
        self.assertEqual(result["afterNavigation"]["pass4"], "")
        self.assertIsNone(result["afterTimer"]["preview"])
        self.assertEqual(result["afterTimer"]["shown"], "—")
        self.assertEqual(result["afterTimer"]["pass4"], "")


if __name__ == "__main__":
    unittest.main()
