// Rendered evidence that a hidden session list stays hidden across a page
// refresh. The stored choice is "hidden" (`mc-sidebar-pinned=false`), and the
// slot list is held back for a moment the way it is on a real reload, so the
// page first mounts with an empty, not-yet-loaded list. Shots are taken while
// the list is still loading and after it lands. Runs against a built SPA on the
// gateway-free fixture harness.
//
// Usage:
//   npm run build
//   OUT_DIR="$KIROCREW_SCRATCH/sidebar-hidden-refresh" node scripts/capture-sidebar-hidden-refresh.mjs
//   DIST=<another dist dir> captures a different build (e.g. the base) for comparison.
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.env.OUT_DIR || `${process.env.TMPDIR || '.'}/sidebar-hidden-refresh`
const SLOT_DELAY_MS = Number(process.env.SLOT_DELAY_MS || 2000)
mkdirSync(OUT, { recursive: true })

const slots = Array.from({ length: 6 }, (_, i) => ({
  key: `chat-1-${i}`,
  title: `Session ${i}`,
  running: false,
  messages: 3,
  agent: 'kirocrew',
  last_ts: new Date(Date.now() - (i + 1) * 60_000).toISOString(),
}))

const { srv, base } = await serveDist(process.env.DIST || DEFAULT_DIST)
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined })
const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage()
await stubDashboardApi(page, {
  slots,
  localStorageEntries: { 'mc-sidebar-pinned': 'false' },
  extra: async (path, route) => {
    if (path !== '/api/chat/slots') return false
    await new Promise(r => setTimeout(r, SLOT_DELAY_MS))
    await json(route, slots)
    return true
  },
})

const state = () => page.evaluate(() => ({
  rows: document.querySelectorAll('[data-slot-key]').length,
  stored: localStorage.getItem('mc-sidebar-pinned'),
}))

await page.goto(base)
await page.waitForTimeout(Math.min(800, SLOT_DELAY_MS / 2))
const loading = await state()
await page.screenshot({ path: `${OUT}/1-loading.png` })

await page.waitForSelector('[aria-label="Show sessions sidebar"], [aria-label="Hide sessions sidebar"]', { timeout: 20_000 })
await page.waitForTimeout(800)
const loaded = await state()
const toggle = await page.locator('[aria-label="Show sessions sidebar"]').count() ? 'show' : 'hide'
await page.screenshot({ path: `${OUT}/2-loaded.png` })

const hidden = loading.rows === 0 && loaded.rows === 0 && toggle === 'show' && loaded.stored === 'false'
const report = { slotDelayMs: SLOT_DELAY_MS, loading, loaded, toggle, hidden }
writeFileSync(`${OUT}/report.json`, JSON.stringify(report, null, 2))
console.log(JSON.stringify(report))
await browser.close()
srv.close()
if (!hidden) process.exit(1)
