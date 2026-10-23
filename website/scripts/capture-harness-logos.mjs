/**
 * Capture Settings → Agent Harness's harness list, to show each harness's own
 * brand mark (`components/HarnessLogo.tsx`) in the light and dark themes.
 *
 * Usage: node scripts/capture-harness-logos.mjs <payloads.json> [base-url] [out-dir]
 * Needs a Vite dev server serving website/ (e.g. `npx vite --host 127.0.0.1 --port 5199`).
 *
 * `payloads.json` maps a policy id to `backend_cards.card_payload()` for that
 * harness, written from the projection itself, e.g.:
 *
 *   PYTHONPATH=src python3 -c "import json; from kiro_crew.agent_sdk import backend_cards as b; \
 *     print(json.dumps({p: {**b.card_payload(i), 'id': i, 'policy_id': p} for i, p in [('', 'kiro'), \
 *     ('kas', 'kas'), ('claude', 'claude'), ('codex', 'codex'), ('deepseek', 'deepseek'), \
 *     ('goose', 'goose'), ('opencode', 'opencode'), ('pi', 'pi')]}))" > payloads.json
 *
 * A ninth row, `zephyr`, is a harness this frontend has no mark for: the frame
 * shows it keeps the generic glyph. Every other /api/ call is answered from the
 * fixtures below; no gateway is involved.
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { KIROCREW_CONFIG_FIXTURE } from './lib/stub-dashboard-api.mjs'

const PAYLOADS = JSON.parse(readFileSync(process.argv[2], 'utf-8'))
const base = process.argv[3] || 'http://127.0.0.1:5199'
const out = process.argv[4] || '../.github/screenshots/harness-logos'
mkdirSync(out, { recursive: true })

const IDS = ['kiro', 'kas', 'claude', 'codex', 'deepseek', 'goose', 'opencode', 'pi']
const row = payload => ({
  selectable: true,
  independent_setup: payload.policy_id !== 'kiro' && payload.policy_id !== 'kas',
  installed: 'installed',
  missing_components: [],
  install_command: '',
  restart_required: false,
  ...payload,
})
const backends = [
  // The server spells Kiro CLI `kiro` on these rows (ACP_BACKEND_KIRO).
  ...IDS.map(id => row({ ...PAYLOADS[id], id })),
  row({ ...PAYLOADS.codex, id: 'zephyr', policy_id: 'zephyr' }),
]
const enumValues = ['kiro', 'kas', 'claude', 'codex', 'deepseek', 'goose', 'opencode', 'pi', 'zephyr']

const json = (route, body, code = 200) => route.fulfill({
  status: code, contentType: 'application/json', body: JSON.stringify(body),
})

const signedOut = {
  authenticated: false, provider: '', identity: '', transport: 'device',
  expires_at: null, expired: false, has_refresh_token: false, refresh_rejected: false, usable: false,
}

// Load the list in one theme at one size, and check every row paints the mark it
// should before anything is captured (a missing asset leaves an empty 14px span,
// which a screenshot can hide).
async function openList(browser, theme, viewport, deviceScaleFactor) {
  const page = await browser.newPage({ viewport, deviceScaleFactor })
  const errors = []
  page.on('pageerror', error => errors.push(String(error)))
  await page.route(url => new URL(url).pathname.startsWith('/api/'), route => {
    const p = new URL(route.request().url()).pathname
    if (p === '/api/config/kirocrew') {
      return json(route, { ...KIROCREW_CONFIG_FIXTURE, agent: { ...KIROCREW_CONFIG_FIXTURE.agent, acp_backend: 'kas' } })
    }
    if (p === '/api/config/schema') return json(route, { entries: [{ path: 'agent.acp_backend', type: 'enum', enumValues }] })
    if (p === '/api/acp-backends') return json(route, { backends })
    if (p === '/api/kas-login') return json(route, signedOut)
    return json(route, {})
  })
  await page.goto(`${base}/capture/settings-agent-harness.html?theme=${theme}&path=%2Fsettings%2Fagent`)
  await page.getByRole('radio', { name: 'zephyr' }).waitFor({ timeout: 20000 })
  const count = await page.getByRole('radio').count()
  if (count !== IDS.length + 1) throw new Error(`expected ${IDS.length + 1} rows, saw ${count}`)
  for (const id of ['claude', 'codex', 'deepseek', 'goose', 'opencode', 'pi']) {
    await page.getByTestId(`harness-logo-${id}`).waitFor()
  }
  const ghosts = await page.getByTestId('kiro-ghost-mark').count()
  if (ghosts !== 2) throw new Error(`expected the Kiro ghost on Kiro CLI and Kiro Agent, saw it ${ghosts} times`)
  if (await page.getByTestId('harness-logo-fallback').count() !== 1) throw new Error('expected one generic mark (zephyr)')
  await page.waitForTimeout(800)
  return { page, errors }
}

const browser = await chromium.launch()
try {
  for (const theme of ['light', 'dark']) {
    // The whole list as it renders, with the in-use row's detail open.
    const full = await openList(browser, theme, { width: 1100, height: 1100 }, 1.4)
    const list = full.page.locator('fieldset').filter({ has: full.page.getByRole('radio', { name: 'zephyr' }) })
    await list.screenshot({ path: `${out}/harness-logos-${theme}.png`, animations: 'disabled' })
    await full.page.close()
    // A 3x close-up of the collapsed rows, since each mark is 14px in the full frame.
    const zoom = await openList(browser, theme, { width: 500, height: 1600 }, 3)
    const first = await zoom.page.getByRole('radio', { name: 'Kiro CLI' }).locator('xpath=ancestor::label[1]').boundingBox()
    const last = await zoom.page.getByRole('radio', { name: 'zephyr' }).locator('xpath=ancestor::label[1]').boundingBox()
    await zoom.page.screenshot({
      path: `${out}/harness-logos-${theme}-closeup.png`,
      animations: 'disabled',
      clip: { x: first.x, y: first.y, width: Math.min(first.width, 300), height: last.y + last.height - first.y },
    })
    await zoom.page.close()
    const errors = [...full.errors, ...zoom.errors]
    console.log(`${theme}: captured${errors.length ? ` (page errors: ${errors.join(' | ')})` : ''}`)
  }
} finally {
  await browser.close()
}
