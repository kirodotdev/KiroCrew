/**
 * Capture Settings → Agent Harness at desktop and phone widths, in English and
 * Simplified Chinese, for the layered-layout change.
 *
 * Usage: node scripts/capture-agent-harness-levels.mjs <payloads.json> [base-url] [out-dir] [prefix]
 * Needs a Vite dev server serving website/ (e.g. `npx vite --host 127.0.0.1 --port 5199`).
 *
 * `payloads.json` maps a policy id to the server's probe row for that harness,
 * built from `backend_cards.card_payload()` plus `host_auth.sign_in_remedy()`:
 *
 *   PYTHONPATH=src python3 -c "import json; from kiro_crew.agent_sdk import backend_cards as b; \
 *     from kiro_crew.agent_sdk.host_auth import sign_in_remedy as r, signs_in_separately as s; \
 *     print(json.dumps({p: {**b.card_payload(i), 'id': i, 'policy_id': p, \
 *     'auth': {'sign_in_remedy': r(i), 'signs_in_separately': s(i)}} \
 *     for i, p in [('', 'kiro'), ('kas', 'kas'), ('claude', 'claude'), ('codex', 'codex')]}))" > payloads.json
 *
 * Every /api/ call is answered from fixtures; no gateway is involved.
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { KIROCREW_CONFIG_FIXTURE } from './lib/stub-dashboard-api.mjs'

const PAYLOADS = JSON.parse(readFileSync(process.argv[2], 'utf-8'))
const base = process.argv[3] || 'http://127.0.0.1:5199'
const out = process.argv[4] || '../.github/screenshots/agent-harness-levels'
const prefix = process.argv[5] || 'shot'
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
const backends = ['kas', 'kiro', 'claude', 'codex'].filter(n => PAYLOADS[n]).map(row)

const json = (route, body, code = 200) => route.fulfill({
  status: code, contentType: 'application/json', body: JSON.stringify(body),
})

const signedIn = {
  authenticated: true, provider: 'Google', identity: 'social', transport: 'device',
  expires_at: '2099-01-01T00:00:00Z', expired: false, has_refresh_token: true, refresh_rejected: false, usable: true,
}

const signedOut = {
  authenticated: false, provider: '', identity: '', transport: 'device',
  expires_at: null, expired: false, has_refresh_token: false, refresh_rejected: false, usable: false,
}

// `pick` is the row checked before the frame; null keeps the page as it opens.
const scenes = [
  { name: 'default', pick: null },
  { name: 'claude', pick: 'Claude Code' },
  // The level-two fold opened; skipped on a build without it.
  { name: 'claude-about-open', pick: 'Claude Code', open: true },
  // A stored Kiro sign-in, so its section folds to one line.
  { name: 'default-signed-in', pick: null, signedIn: true },
]
const widths = [1280, 375]
const langs = ['en', 'zh-CN']

const browser = await chromium.launch()
try {
  for (const lang of langs) {
    for (const width of widths) {
      for (const s of scenes) {
        const page = await browser.newPage({ viewport: { width, height: 900 }, deviceScaleFactor: 1 })
        const errors = []
        page.on('pageerror', error => errors.push(String(error)))
        await page.route(url => new URL(url).pathname.startsWith('/api/'), route => {
          const p = new URL(route.request().url()).pathname
          if (p === '/api/config/kirocrew') {
            return json(route, { ...KIROCREW_CONFIG_FIXTURE, agent: { ...KIROCREW_CONFIG_FIXTURE.agent, acp_backend: 'kas' } })
          }
          if (p === '/api/config/schema') {
            return json(route, { entries: [{ path: 'agent.acp_backend', type: 'enum', enumValues: backends.map(b => b.id) }] })
          }
          if (p === '/api/acp-backends') return json(route, { backends })
          if (p === '/api/kas-login') return json(route, s.signedIn ? signedIn : signedOut)
          return json(route, {})
        })
        await page.goto(`${base}/capture/settings-agent-harness.html?theme=light&lang=${lang}&path=%2Fsettings%2Fagent`)
        const detail = page.getByTestId('agent-harness-detail')
        await detail.waitFor({ timeout: 20000 })
        if (s.pick) {
          await page.getByRole('radio', { name: s.pick }).check()
          await detail.waitFor()
        }
        if (s.open) {
          const about = page.getByTestId('agent-harness-about-summary')
          if (!(await about.count())) {
            await page.close()
            continue
          }
          await about.click()
        }
        await page.waitForTimeout(400)
        // The settings shell scrolls inside a fixed-height root; grow the
        // viewport to the tab's full height so one frame holds the whole page.
        const h = await page.evaluate(() => {
          let max = document.body.scrollHeight
          for (const el of document.querySelectorAll('*')) {
            if (el.scrollHeight > el.clientHeight + 4) max = Math.max(max, el.scrollHeight + 80)
          }
          return max
        })
        await page.setViewportSize({ width, height: Math.min(Math.max(h, 900), 6000) })
        await page.waitForTimeout(200)
        const file = `${out}/${prefix}-${s.name}-${width}-${lang}.png`
        await page.screenshot({ path: file, fullPage: true })
        if (errors.length) throw new Error(`${file}: ${errors.join('; ')}`)
        console.log(file)
        await page.close()
      }
    }
  }
} finally {
  await browser.close()
}
