/**
 * Render the setup gate's sign-in step on a desktop install whose Kiro CLI is the
 * app's bundled copy, on macOS and on Windows.
 *
 * Usage: node scripts/capture-bundled-sign-in-hint.mjs [base-url] [out-dir] [prefix]
 * Needs a Vite dev server serving website/ (e.g. `npx vite --host 127.0.0.1 --port 5199`).
 * Every /api/ call is answered from the fixtures below; no gateway is involved.
 * The commands are the strings the gateway serves for a bundled copy
 * (`kiro_cli_bundle.login_commands`).
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const out = process.argv[3] || './evidence'
const prefix = process.argv[4] || 'after'
mkdirSync(out, { recursive: true })

const status = {
  platform: 'macOS', installed: true, authenticated: false, ready: false,
  initial_setup_complete: false, repair_required: false, bundled_cli: true,
  docs_url: 'https://kiro.dev/cli/', login_command: 'kiro-cli login',
  sso_login_command: 'kiro-cli login --use-device-flow --license pro',
  setup_allowed: true, sandbox_unavailable: false, sandbox_backend_available: true,
  sandbox_failure_kind: '', sandbox_detail: '', sandbox_remedy: '',
  missing_agent_specs: [], agent_spec_repair_error: '',
}

const posixBin = "'/Applications/Kiro Crew.app/Contents/Resources/backend-dist/kiro-cli/kiro-cli-chat'"
const windowsRun = 'powershell.exe -NoProfile -Command "Set-Item Env:KIRO_NO_AUTO_UPDATE 1; '
  + "& 'C:\\Users\\me\\AppData\\Local\\Programs\\Kiro Crew\\resources\\backend-dist\\kiro-cli\\kiro-cli.exe'"

const scenarios = {
  'macos-bundled': {
    platform: 'macOS',
    login_command: `${posixBin} login`,
    sso_login_command: `${posixBin} login --use-device-flow --license pro`,
  },
  'windows-bundled': {
    platform: 'Windows',
    login_command: `${windowsRun} login"`,
    sso_login_command: `${windowsRun} login --use-device-flow --license pro"`,
  },
}

const json = (route, body, code = 200) => route.fulfill({
  status: code, contentType: 'application/json', body: JSON.stringify(body),
})

const browser = await chromium.launch()
try {
  for (const [name, overrides] of Object.entries(scenarios)) {
    const page = await browser.newPage({ viewport: { width: 1200, height: 900 }, deviceScaleFactor: 1.5 })
    page.on('pageerror', error => { throw error })
    await page.route(url => new URL(url).pathname.startsWith('/api/'), route => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/kiro-prerequisite') return json(route, { ...status, ...overrides })
      if (path === '/api/config/kirocrew') return json(route, { agent: {} })
      if (path === '/api/acp-backends') return json(route, { backends: [] })
      return json(route, {})
    })
    await page.goto(`${base}/capture/setup-check-probe-error.html?theme=dark`)
    await page.getByText(/Organization SSO/).first().waitFor()
    await page.getByText(/Organization SSO/).first().scrollIntoViewIfNeeded()
    await page.screenshot({ path: `${out}/${prefix}-${name}.png`, animations: 'disabled' })
    console.log(`${prefix}-${name}: captured`)
    await page.close()
  }
} finally {
  await browser.close()
}
