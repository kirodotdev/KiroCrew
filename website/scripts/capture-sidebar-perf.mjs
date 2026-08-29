// Capture evidence for the sidebar perf PR: a small (5-session) sidebar —
// visually unchanged — and a 201-session sidebar rendering correctly with
// content-visibility + the animation gate active. Uses the repo's own
// serve-dist + stub-dashboard-api fixture harness (no gateway, no token).
import { chromium } from 'playwright'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { mkdirSync } from 'fs'

const OUT = process.env.OUT_DIR || '/tmp/pa1-shots'
mkdirSync(OUT, { recursive: true })

const mkSlots = (n) => Array.from({ length: n }, (_, i) => ({
  key: `chat-1-${String(i).padStart(3, '0')}`,
  title: `Session ${i} — demo work item`,
  running: i % 17 === 0,
  messages: 3 + (i % 9),
  agent: i % 3 === 0 ? 'kirocrew' : 'kirocrew-lite',
  last_ts: new Date(Date.now() - i * 3600_000).toISOString(),
}))

const { srv, base } = await serveDist()
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH || undefined,
})

async function shoot(name, slots) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await ctx.newPage()
  await stubDashboardApi(page, { slots, folders: [] })
  await page.goto(base)
  await page.waitForSelector('[data-slot-key]', { timeout: 20000 })
  await page.waitForTimeout(800) // let entrance animation (small list) settle
  await page.screenshot({ path: `${OUT}/${name}.png` })
  const rows = await page.locator('[data-slot-key]').count()
  const withLayoutAnim = await page.evaluate(() =>
    document.querySelectorAll('[data-slot-key][data-projection-id]').length)
  console.log(`${name}: rows=${rows} projection-nodes=${withLayoutAnim}`)
  await ctx.close()
}

await shoot('sidebar-5-sessions', mkSlots(5))
await shoot('sidebar-201-sessions', mkSlots(201))

await browser.close()
srv.close()
console.log('DONE', OUT)
