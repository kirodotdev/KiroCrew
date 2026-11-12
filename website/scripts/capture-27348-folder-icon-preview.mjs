/**
 * Screenshot harness: Folder settings → Auto-generate shows the
 * icon right away instead of after Save.
 *
 * Runs the REAL built SPA behind the shared loopback static server, every
 * /api/** call answered from fixtures (gateway-free). The preview route is
 * held open until the "generating" frame is taken, so the spinner is real.
 *
 * MODE=before (a build without the preview) clicks Auto-generate and shows the
 * "picked after you save" hint; MODE=after (default) shows the spinner, the
 * previewed emoji, a second pick, and the failure line. THEME picks dark
 * (default) or light.
 *
 * Usage: DIST=<dist dir> MODE=after THEME=dark node scripts/capture-27348-folder-icon-preview.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/27348'
const MODE = process.env.MODE || 'after'
const THEME = process.env.THEME || 'dark'
mkdirSync(OUT, { recursive: true })

function assert(label, ok, detail = '') {
  console.log(`${label}: ${ok ? 'OK' : 'FAIL'}${detail ? ` — ${detail}` : ''}`)
  if (!ok) throw new Error(`${label} failed${detail ? `: ${detail}` : ''}`)
}

const folders = [
  { id: 'f1', name: 'Payments', icon: '📁', order: 0, collapsed: true },
  { id: 'f2', name: 'Travel plans', order: 1, collapsed: true },
]
const slots = [
  { key: 's1', title: 'Book the flights', messages: 2, running: false, agent: 'kirocrew', created: '2026-10-30T01:00:00Z', last_ts: '2026-10-30T02:00:00Z', folder_id: 'f2' },
]
const MODAL = '[role="dialog"]'

// The preview route: each answer waits on `gate` when one is set, so a frame
// can be taken while the request is in flight.
const picks = ['✈️', '☀️']
const previews = []
const patches = []
let gate = null
let failNext = false

const extra = async (path, route) => {
  const method = route.request().method()
  if (path === '/api/agents' || path === '/api/chat/agents') {
    json(route, { agents: [{ name: 'kirocrew', source: 'builtin' }], default_agent: 'kirocrew' })
    return true
  }
  if (path === '/api/chat/folders/f2/icon-preview' && method === 'POST') {
    previews.push(JSON.parse(route.request().postData() || '{}'))
    if (gate) await gate
    if (failNext) { failNext = false; json(route, { error: 'no icon could be generated', code: 'icon_generate_failed' }, 502); return true }
    json(route, { icon: picks[(previews.length - 1) % picks.length] })
    return true
  }
  if (path === '/api/chat/folders/f2' && method === 'PATCH') {
    patches.push(JSON.parse(route.request().postData() || '{}'))
    json(route, folders[1])
    return true
  }
  return false
}

async function main() {
  const { srv, base } = await serveDist(process.env.DIST || undefined)
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1400, height: 1000 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { folders, slots, theme: THEME, extra })
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  const shot = async name => {
    await page.waitForTimeout(400)
    await page.locator(MODAL).screenshot({ path: `${OUT}/${name}-${THEME}.png` })
    console.log('wrote', `${OUT}/${name}-${THEME}.png`)
  }
  const iconValue = () => page.locator('[data-testid="folder-config-icon"]').inputValue()

  await page.hover('[data-testid="folder-collapse-f2"]')
  await page.click('[data-testid="folder-menu-f2"]')
  await page.click('[data-testid="folder-settings-f2"]')
  await page.waitForSelector(MODAL, { timeout: 5000 })

  if (MODE === 'before') {
    await page.click('[data-testid="folder-config-icon-regenerate"]')
    assert('before: nothing to see until Save', (await iconValue()) === '')
    await shot('before-01-after-click')
    await browser.close(); srv.close(); return
  }

  // 01: the request is in flight — spinner on the button.
  let release
  gate = new Promise(r => { release = r })
  await page.click('[data-testid="folder-config-icon-regenerate"]')
  await page.waitForSelector('[data-testid="folder-config-icon-generating"]', { timeout: 5000 })
  await shot('after-01-generating')
  release(); gate = null

  // 02: the emoji is in the field and the preview, nothing saved yet.
  await page.waitForFunction(() => document.querySelector('[data-testid="folder-config-icon"]')?.value === '✈️', null, { timeout: 5000 })
  assert('after: no PATCH before Save', patches.length === 0)
  await shot('after-02-previewed')

  // 03: a second click asks for a different one.
  await page.click('[data-testid="folder-config-icon-regenerate"]')
  await page.waitForFunction(() => document.querySelector('[data-testid="folder-config-icon"]')?.value === '☀️', null, { timeout: 5000 })
  assert('after: second click sends the first pick as avoid', JSON.stringify(previews[1]?.avoid) === JSON.stringify(['✈️']), JSON.stringify(previews[1]))
  await shot('after-03-second-pick')

  // 04: a failure says so in the modal and keeps the field.
  failNext = true
  await page.click('[data-testid="folder-config-icon-regenerate"]')
  await page.waitForSelector('[data-testid="folder-config-icon-generate-error"]', { timeout: 5000 })
  assert('after: failed pick keeps the field', (await iconValue()) === '☀️')
  await shot('after-04-failed')

  // Save stores what is in the field as a plain icon.
  await page.click('[data-testid="folder-config-submit"]')
  await page.waitForSelector(MODAL, { state: 'detached', timeout: 5000 })
  assert('after: Save sends the field as icon', JSON.stringify(patches[0]) === JSON.stringify({ icon: '☀️' }), JSON.stringify(patches))

  await browser.close(); srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })
