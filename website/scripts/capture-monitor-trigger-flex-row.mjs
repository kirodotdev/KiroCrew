/**
 * Screenshot harness + geometry check for the COMPOSER MONITOR TRIGGER row
 * layout (issue #10183).
 *
 * The bounded-monitor trigger is an `IconButton` whose children are an inline
 * radar glyph and, when a monitor is armed, a probe count. Without a flex row
 * on the button the glyph resolves against the line-box baseline (~2.8px above
 * the button centre) and the count renders flush against the glyph.
 *
 * This ASSERTS as well as photographs: it drives the REAL built SPA
 * (website/dist) behind `serveDist` with every /api/** call answered from
 * fixtures, measures the glyph's vertical centring inside the trigger and the
 * glyph-to-count gap from real bounding boxes, and exits non-zero when either
 * is off. Run it against a build with the fix reverted to photograph "before".
 *
 * Usage: node scripts/capture-monitor-trigger-flex-row.mjs [outDir] [--expect-broken]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { monitorRecordFixture } from './lib/monitor-record-fixture.mjs'

const args = process.argv.slice(2)
const EXPECT_BROKEN = args.includes('--expect-broken')
const OUT = args.find(a => !a.startsWith('--')) || '../temp-screenshots/monitor-trigger-flex-row'
const SLOT = 'chat-mon'
const PROJECT = '/home/user/workspace/uploader'
const LOOP_ID = 'mon-10183'
const PR = 'https://github.com/kirodotdev/KiroCrew/pull/9615'

mkdirSync(OUT, { recursive: true })

const NOW = Math.floor(Date.now() / 1000)
const FIXED_FIRE_TS = Date.UTC(2026, 8, 9, 14, 11, 0) / 1000

const slots = [{
  key: SLOT,
  title: 'Watch PR 9615 to review-ready',
  running: false,
  last_message: 'Monitoring armed.',
  messages: 4,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: NOW,
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: NOW - 900, content: 'Babysit PR 9615 please.' },
    { role: 'assistant', ts: NOW - 120, content: 'Monitoring armed at 300s cadence.' },
  ],
}

/** One LIVE structured monitor, shaped like `GET /api/monitors/slot/{slot}` serves it. */
const liveMonitor = monitorRecordFixture({
  loopId: LOOP_ID, slotKey: SLOT, pr: PR, fireTs: FIXED_FIRE_TS,
})

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1500, height: 950 },
  // 1x renders the 11px mono count too soft to judge the gap on GitHub.
  deviceScaleFactor: 3,
})

const page = await context.newPage()
logPageProblems(page)

await stubDashboardApi(page, {
  slots,
  extra: async (path, route) => {
    if (path === `/api/autonudge/slot/${SLOT}`) { await json(route, { loop: null }); return true }
    if (path === `/api/monitors/slot/${SLOT}`) {
      await json(route, { enabled: true, monitor: liveMonitor })
      return true
    }
    if (path === '/api/autonudge') { await json(route, { enabled: true, loops: [] }); return true }
    if (path === '/api/monitors') {
      await json(route, { enabled: true, monitors: [liveMonitor] })
      return true
    }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  },
  localStorageEntries: { 'mc-active-slot': SLOT, 'mc-lang': 'en' },
})
await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2500)

// The armed trigger's accessible name reads "Monitor status: <status>".
const chip = page.getByRole('button', { name: /^Monitor status: / }).first()
await chip.waitFor({ state: 'visible', timeout: 15000 })

const results = []
const check = (name, ok, detailText) => {
  results.push({ name, ok, detail: detailText })
  if (!ok) console.error(`FAIL ${name}: ${detailText}`)
}

// --- geometry: glyph centring + glyph-to-count gap, from real boxes ---
const chipBox = await chip.boundingBox()
const glyph = chip.locator('svg').first()
const glyphBox = await glyph.boundingBox()
const count = chip.locator('span.font-mono').first()
const countBox = await count.boundingBox()

const chipMid = chipBox.y + chipBox.height / 2
const glyphMid = glyphBox.y + glyphBox.height / 2
const lift = chipMid - glyphMid // positive = glyph sits ABOVE centre
const gap = countBox.x - (glyphBox.x + glyphBox.width)

console.log(`trigger box: ${JSON.stringify(chipBox)}`)
console.log(`glyph lift above centre: ${lift.toFixed(2)}px (0 = centred)`)
console.log(`glyph-to-count gap: ${gap.toFixed(2)}px`)

if (EXPECT_BROKEN) {
  check('glyph sits off the button centre (broken build)', Math.abs(lift) > 1,
    `lift ${lift.toFixed(2)}px -- expected the baseline offset on the unfixed build`)
  check('no gap before the probe count (broken build)', gap < 1,
    `gap ${gap.toFixed(2)}px -- expected the count flush against the glyph`)
} else {
  check('glyph is vertically centred in the trigger', Math.abs(lift) <= 1,
    `lift ${lift.toFixed(2)}px -- the glyph is off the button centre`)
  check('probe count is separated from the glyph', gap >= 3,
    `gap ${gap.toFixed(2)}px -- gap-1 should yield 4px`)
}

// --- frames: the whole composer control row, and the trigger close up ---
const row = chip.locator('xpath=ancestor::div[2]')
const suffix = EXPECT_BROKEN ? 'before' : 'after'
await row.screenshot({ path: join(OUT, `${suffix}-composer-control-row.png`) })
console.log('wrote', join(OUT, `${suffix}-composer-control-row.png`))
const pad = 6
await page.screenshot({
  path: join(OUT, `${suffix}-monitor-trigger-closeup.png`),
  clip: {
    x: chipBox.x - pad, y: chipBox.y - pad,
    width: chipBox.width + pad * 2, height: chipBox.height + pad * 2,
  },
})
console.log('wrote', join(OUT, `${suffix}-monitor-trigger-closeup.png`))

await browser.close()
srv.close()

const failed = results.filter(r => !r.ok)
console.log(`${results.length - failed.length}/${results.length} checks passed`)
process.exit(failed.length ? 1 : 0)
