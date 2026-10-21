/**
 * Capture Settings → Agent Harness with the curated core card and its folded
 * detail lines.
 *
 * Usage: node scripts/capture-agent-backend-core-card.mjs <payloads.json> [base-url] [out-dir]
 * Needs a Vite dev server serving website/ (e.g. `npx vite --host 127.0.0.1 --port 5199`).
 *
 * `payloads.json` maps a policy id to `backend_cards.card_payload()` for that
 * harness, written from the projection itself, e.g.:
 *
 *   PYTHONPATH=src python3 -c "import json; from kiro_crew.agent_sdk import backend_cards as b; \
 *     print(json.dumps({p: {**b.card_payload(i), 'id': i, 'policy_id': p} \
 *     for i, p in [('', 'kiro'), ('kas', 'kas'), ('codex', 'codex')]}))" > payloads.json
 *
 * so every frame documents the answer the server gives. Every other /api/ call is
 * answered from the fixtures below; no gateway is involved.
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { KIROCREW_CONFIG_FIXTURE } from './lib/stub-dashboard-api.mjs'

const PAYLOADS = JSON.parse(readFileSync(process.argv[2], 'utf-8'))
const base = process.argv[3] || 'http://127.0.0.1:5199'
const out = process.argv[4] || '../.github/screenshots/harness-core-card'
mkdirSync(out, { recursive: true })

const row = name => ({
  selectable: true,
  independent_setup: name !== 'kiro' && name !== 'kas',
  installed: 'installed',
  missing_components: [],
  install_command: '',
  restart_required: false,
  ...PAYLOADS[name],
})
const backends = [row('kiro'), row('kas'), row('codex')]

const json = (route, body, code = 200) => route.fulfill({
  status: code, contentType: 'application/json', body: JSON.stringify(body),
})

const signedOut = {
  authenticated: false, provider: '', identity: '', transport: 'device',
  expires_at: null, expired: false, has_refresh_token: false, refresh_rejected: false, usable: false,
}

// Each scene checks one picker row and waits for the string the frame is evidence for.
const shots = [
  { name: 'kas-core-card', theme: 'light', pick: 'Kiro Agent', ready: 'supports 7 of 7 features' },
  { name: 'kas-details-open', theme: 'light', pick: 'Kiro Agent', ready: 'supports 7 of 7 features', open: true },
  { name: 'kiro-cli-details-open', theme: 'dark', pick: 'Kiro CLI', ready: 'supports 6 of 7 features', open: true },
  { name: 'codex-core-card', theme: 'light', pick: 'Codex', ready: 'supports 6 of 7 features' },
]

const browser = await chromium.launch()
try {
  for (const s of shots) {
    const page = await browser.newPage({ viewport: { width: 1360, height: 1100 }, deviceScaleFactor: 1.4 })
    const errors = []
    page.on('pageerror', error => errors.push(String(error)))
    await page.route(url => new URL(url).pathname.startsWith('/api/'), route => {
      const p = new URL(route.request().url()).pathname
      if (p === '/api/config/kirocrew') {
        return json(route, { ...KIROCREW_CONFIG_FIXTURE, agent: { ...KIROCREW_CONFIG_FIXTURE.agent, acp_backend: '' } })
      }
      if (p === '/api/config/schema') return json(route, { entries: [{ path: 'agent.acp_backend', type: 'enum', enumValues: ['', 'kas', 'codex'] }] })
      if (p === '/api/acp-backends') return json(route, { backends })
      if (p === '/api/kas-login') return json(route, signedOut)
      return json(route, {})
    })
    await page.goto(`${base}/capture/settings-agent-harness.html?theme=${s.theme}&path=%2Fsettings%2Fagent`)
    await page.getByRole('radio', { name: s.pick }).waitFor({ timeout: 20000 })
    await page.getByRole('radio', { name: s.pick }).check()
    const detail = page.getByTestId('agent-harness-detail')
    await detail.waitFor()
    await page.getByText(s.ready, { exact: false }).waitFor({ timeout: 20000 })
    if (s.open) {
      await detail.getByText('Other differences (not counted)', { exact: true }).click()
      await detail.locator('details[open]').waitFor()
    }
    await page.waitForTimeout(800)
    await detail.screenshot({ path: `${out}/${s.name}.png`, animations: 'disabled' })
    console.log(`${s.name}: captured${errors.length ? ` (page errors: ${errors.join(' | ')})` : ''}`)
    await page.close()
  }
} finally {
  await browser.close()
}
