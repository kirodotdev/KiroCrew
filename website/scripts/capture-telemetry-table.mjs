/**
 * Screenshot the rebuilt Telemetry page against REAL measured data.
 *
 * The payload (default `/tmp/telemetry-real-measured.json`, override with
 * `PAYLOAD=`) is the actual `GET /api/telemetry/startup` body for this machine,
 * assembled by calling the same four producers the handler calls. Nothing here
 * is fabricated: the credit totals, occupancy samples, turn outcomes, startup
 * phases and instrument names are what the stores hold.
 *
 * Cross-check the row counts against the payload before trusting a shot — a
 * fixture written over the payload path by another process produces a perfectly
 * real screenshot of entirely invented numbers, and nothing about the image
 * says so.
 *
 * Usage: node scripts/capture-telemetry-table.mjs
 */
import { readFileSync, mkdirSync } from 'node:fs'

import { chromium } from 'playwright'

import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.env.OUT || '/tmp/shots/telemetry-impl'
mkdirSync(OUT, { recursive: true })

// Override with PAYLOAD=… . The default is deliberately NOT a generic name: a
// shared scratch path invites another process to overwrite the measured payload
// with a fixture, and the screenshots would then claim to be real data while
// showing someone else's numbers.
const TELEMETRY = JSON.parse(readFileSync(process.env.PAYLOAD || '/tmp/telemetry-real-measured.json', 'utf8'))

const json = (route, body, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1560, height: 1100 },
    // The tables are 12.5px mono; a 1x shot renders soft when shared.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname

    if (path === '/api/telemetry/startup') return json(route, TELEMETRY)

    // The app shell mounts behind this gate and reads status.operation.status —
    // a generic object stub blanks the whole page.
    if (path === '/api/kiro-prerequisite') {
      return json(route, {
        platform: 'linux', installed: true, authenticated: true, ready: true,
        initial_setup_complete: true, can_auto_install: false, can_login: false,
        repair_required: false, docs_url: '', setup_allowed: false,
        operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
      })
    }
    if (path === '/api/status') {
      return json(route, { sessions: 1, crons: 0, lessons: 0, uptime: 120, version: 'dev' })
    }
    if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
    if (path === '/api/theme/boot') return json(route, { mode: 'dark', theme: '' })
    if (path === '/api/themes') return json(route, { themes: [], installed: [] })
    if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
    if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro', avatar: '' })
    // A BARE ARRAY, not {slots:[]}: `api.chatSlots()` does not unwrap, and the
    // fetchSlots reducer maps over the payload directly, so the object form
    // throws `payload.map is not a function` on every load of this harness.
    if (path === '/api/chat/slots') return json(route, [])
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    if (path === '/api/apps') return json(route, { apps: [], serverPlatform: { os: 'linux', arch: 'x64' } })
    if (path === '/api/dashboard/config') {
      return json(route, {
        restore_sessions: false, restore_window_minutes: 30,
        merge_queued_messages: false, widget_density: 'more',
      })
    }
    if (/(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)) return json(route, {})
    return json(route, [])
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => {
    if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 200))
  })

  await page.addInitScript(() => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
  })
  await page.goto(base + '/developer?tab=telemetry', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  const shot = async name => {
    await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: true })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  const rows = async () => (await page.locator('tbody tr').count())

  await shot('spend-by-conversation')
  console.log('  rows:', await rows())

  // Group-by re-keys the same table rather than stacking another section.
  for (const label of ['Model', 'Channel']) {
    const btn = page.getByRole('button', { name: label, exact: true })
    if (await btn.count()) {
      await btn.first().click()
      await page.waitForTimeout(500)
      await shot(`spend-by-${label.toLowerCase()}`)
      console.log('  rows:', await rows())
    } else {
      console.log(`  ! group-by "${label}" not found`)
    }
  }

  // Sorting: flip a column and confirm the caret moved.
  const back = page.getByRole('button', { name: 'Conversation', exact: true })
  if (await back.count()) { await back.first().click(); await page.waitForTimeout(400) }
  const peak = page.getByRole('button', { name: /Peak ctx/ })
  if (await peak.count()) {
    await peak.first().click()
    await page.waitForTimeout(400)
    await shot('spend-sorted-by-peak')
  }

  for (const tab of ['Context', 'Latency', 'Startup']) {
    // Prefix, not exact: a tab carrying the fault-count badge has an accessible
    // name of "Startup16", so an exact match silently skipped that whole tab.
    const btn = page.getByRole('button', { name: new RegExp('^' + tab) })
    if (!(await btn.count())) { console.log(`  ! tab "${tab}" not found`); continue }
    await btn.first().click()
    await page.waitForTimeout(700)
    await shot(`${tab.toLowerCase()}`)
    console.log(`  ${tab} rows:`, await rows())
  }

  // A narrow viewport, to show the summary strip reflowing and the table
  // reaching its horizontal scroll rather than clipping a column.
  await page.setViewportSize({ width: 900, height: 1100 })
  await page.goto(base + '/developer?tab=telemetry', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2200)
  await shot('narrow')
  await page.setViewportSize({ width: 1560, height: 1100 })

  // `telemetry.enabled = false`. Spend and Context are written to the token row
  // store regardless of the OTEL switch, and the switch defaults to OFF — so
  // this is the state most installs actually see, and the surface has to render.
  await page.unroute('**/api/telemetry/startup')
  await page.route('**/api/telemetry/startup', route =>
    json(route, { ...TELEMETRY, enabled: false, startup: null, turn: null, other: [] }),
  )
  await page.goto(base + '/developer?tab=telemetry', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2200)
  await shot('telemetry-off')

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
