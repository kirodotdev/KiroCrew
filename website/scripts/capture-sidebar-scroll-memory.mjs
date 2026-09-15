// Rendered evidence for the session-lane scroll memory: scroll the sessions
// sidebar deep into a long list, collapse it with the "Hide sessions" toggle
// (which UNMOUNTS ChatSidebar), reopen it, and screenshot before/after. Runs
// against the built SPA on the gateway-free fixture harness.
//
// Usage:
//   npm run build
//   OUT_DIR="$KIROCREW_SCRATCH/sidebar-scroll-memory" node scripts/capture-sidebar-scroll-memory.mjs
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'fs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.env.OUT_DIR || `${process.env.TMPDIR || '.'}/sidebar-scroll-memory`
const SESSIONS = Number(process.env.SESSIONS || 120)
mkdirSync(OUT, { recursive: true })

const slots = Array.from({ length: SESSIONS }, (_, i) => ({
  key: `chat-1-${String(i).padStart(3, '0')}`,
  title: `Session ${i} — demo work item`,
  running: false,
  messages: 3 + (i % 9),
  agent: i % 3 === 0 ? 'kirocrew' : 'kirocrew-lite',
  last_ts: new Date(Date.now() - (i + 1) * 60_000).toISOString(),
}))

const { srv, base } = await serveDist()
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined })
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: process.env.REDUCED_MOTION === '1' ? 'reduce' : 'no-preference' })
const page = await ctx.newPage()
await stubDashboardApi(page, { slots, folders: [] })
await page.goto(base)
await page.waitForSelector('[data-slot-key]', { timeout: 20_000 })
await page.waitForTimeout(500)

const LANE = '[data-testid="tree-view-lane"]'
const laneState = () => page.evaluate(sel => {
  const el = document.querySelector(sel)
  if (!el) return null
  const c = el.getBoundingClientRect()
  const firstVisible = Array.from(el.querySelectorAll('[data-slot-key]')).find(row => row.getBoundingClientRect().bottom > c.top + 4)
  return { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight, firstVisible: firstVisible?.getAttribute('data-slot-key') ?? null }
}, LANE)

// Scroll deep into the list via a real wheel gesture so the lane's own scroll
// event fires and the position is recorded exactly as a user's would be.
await page.locator(LANE).hover()
for (let i = 0; i < 12; i++) await page.mouse.wheel(0, 400)
await page.waitForTimeout(300)
const before = await laneState()
await page.screenshot({ path: `${OUT}/1-scrolled.png` })

// Collapse (unmounts ChatSidebar), then reopen.
await page.getByLabel('Hide sessions sidebar').click()
await page.waitForSelector(LANE, { state: 'detached', timeout: 5_000 })
await page.waitForTimeout(300)
await page.screenshot({ path: `${OUT}/2-collapsed.png` })
await page.getByLabel('Show sessions sidebar').click()
await page.waitForSelector(LANE, { timeout: 5_000 })
await page.waitForTimeout(500)
const after = await laneState()
await page.screenshot({ path: `${OUT}/3-reopened.png` })

// Rows are content-visibility:auto, so scrollTop is not comparable across
// mounts (placeholder heights); the user-visible contract is the same row at
// the top of the lane.
const restored = !!before && !!after && before.scrollTop > 0 && before.firstVisible === after.firstVisible
const report = { sessions: SESSIONS, before, after, restored }
writeFileSync(`${OUT}/report.json`, JSON.stringify(report, null, 2))
console.log(JSON.stringify(report, null, 2))
await browser.close()
srv.close()
if (!restored) process.exit(1)
