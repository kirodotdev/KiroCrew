/**
 * Capture Settings → Agent Harness when a stored Kiro CLI pick runs on KAS (#25288).
 *
 * Usage: node scripts/capture-25288-kiro-on-kas.mjs [base-url] [out-dir]
 * Needs a Vite dev server serving website/ (e.g. `npx vite --host 127.0.0.1 --port 5199`).
 *
 * `GET /api/acp-backends` answers `kiro_selection_runs_on_kas: true` and the config
 * reads `kas` (what the gate resolves a stored `"kiro"` to), so the panel shows the
 * one-click save. Every other /api/ call is answered from fixtures; no gateway runs.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { KIROCREW_CONFIG_FIXTURE } from './lib/stub-dashboard-api.mjs'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const out = process.argv[3] || '../temp-screenshots/25288'
mkdirSync(out, { recursive: true })

const row = (id, policy) => ({
  id,
  policy_id: policy,
  selectable: true,
  independent_setup: false,
  installed: 'installed',
  missing_components: [],
  install_command: '',
  restart_required: false,
  capabilities: [],
  security_notes: [],
  operator_notes: [],
  offered_by_build: true,
})
const backends = [row('kiro', 'kiro'), row('kas', 'kas')]

const json = (route, body, code = 200) => route.fulfill({
  status: code, contentType: 'application/json', body: JSON.stringify(body),
})

const signedOut = {
  authenticated: false, provider: '', identity: '', transport: 'device',
  expires_at: null, expired: false, has_refresh_token: false, refresh_rejected: false, usable: false,
}

const browser = await chromium.launch()
try {
  for (const theme of ['light', 'dark']) {
    let saved = false
    const page = await browser.newPage({ viewport: { width: 1360, height: 900 }, deviceScaleFactor: 1.4 })
    const errors = []
    page.on('pageerror', error => errors.push(String(error)))
    await page.route(url => new URL(url).pathname.startsWith('/api/'), route => {
      const p = new URL(route.request().url()).pathname
      if (p === '/api/config/kirocrew') {
        if (route.request().method() === 'PATCH') {
          saved = true
          return json(route, { ...KIROCREW_CONFIG_FIXTURE, agent: { ...KIROCREW_CONFIG_FIXTURE.agent, acp_backend: 'kas' } })
        }
        return json(route, { ...KIROCREW_CONFIG_FIXTURE, agent: { ...KIROCREW_CONFIG_FIXTURE.agent, acp_backend: 'kas' } })
      }
      if (p === '/api/config/schema') return json(route, { entries: [{ path: 'agent.acp_backend', type: 'enum', enumValues: ['kiro', 'kas'] }] })
      if (p === '/api/acp-backends') return json(route, { backends, kiro_selection_runs_on_kas: !saved })
      if (p === '/api/kas-login') return json(route, signedOut)
      return json(route, {})
    })
    await page.goto(`${base}/capture/settings-agent-harness.html?theme=${theme}&path=%2Fsettings%2Fagent`)
    const notice = page.getByTestId('kiro-runs-on-kas')
    await notice.waitFor({ timeout: 20000 })
    await page.waitForTimeout(600)
    await page.screenshot({ path: `${out}/notice-${theme}.png`, animations: 'disabled' })
    if (theme === 'light') {
      await page.getByRole('button', { name: 'Save Kiro Agent as my setting' }).click()
      await page.getByTestId('kiro-runs-on-kas-saved').waitFor({ timeout: 20000 })
      await page.waitForTimeout(600)
      await page.screenshot({ path: `${out}/after-save-${theme}.png`, animations: 'disabled' })
    }
    console.log(`${theme}: captured${errors.length ? ` (page errors: ${errors.join(' | ')})` : ''}`)
    await page.close()
  }
} finally {
  await browser.close()
}
