/**
 * Screenshot harness for the composer context meter's unknown state.
 *
 * A slot with no context reading yet renders as unknown, so "not measured
 * yet" reads differently from a measured 0%: a dashed track, `—` in place
 * of the figures, and a "No context reading yet" tooltip. States:
 *
 *  - `unknown-*`: the slot-detail payload carries no context fields (a session
 *    with no reading), so the meter renders unknown.
 *  - `measured-*`: the payload carries the issue's own reading (45.9% of
 *    262,144 tokens), so the meter renders the number.
 *
 * Each state is shot with the chip closed (default settings: the bar alone),
 * with the inline percentage readout turned on, and with the popover open, in
 * both themes. Runs the REAL built SPA (website/dist) with every /api/** call
 * answered from fixtures, gateway-free. Run it against a main build for the
 * "before" frames.
 *
 * Usage: node scripts/capture-context-meter-unknown.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/context-meter-unknown'
mkdirSync(OUT, { recursive: true })

const SLOT = 'context-meter-demo'
const now = Math.floor(Date.now() / 1000)

const slots = [{
  key: SLOT,
  title: 'Long tool-calling session',
  running: false,
  last_message: 'Reading the repository.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  modified: now,
  source_links: [],
  source_links_total: 0,
}]

const baseDetail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  messages: [
    { role: 'user', ts: now - 600, content: 'Audit every caller of the session pool.' },
    { role: 'assistant', ts: now - 30, content: 'Read 14 files; the pool is reached from three entry points.' },
  ],
}

const STATES = {
  unknown: baseDetail,
  measured: { ...baseDetail, context_pct: 45.9, context_used_tokens: 120_390, context_window_tokens: 262_144 },
}

async function shoot(browser, base, theme, name, detail, showPct, open) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    slots,
    theme,
    localStorageEntries: showPct ? { 'mc-chat-config': JSON.stringify({ showContextPct: true, showContextTokens: true }) } : null,
    extra: async (path, route) => {
      if (path === `/api/chat/slots/${SLOT}/autocompact`) { await json(route, { pct: null, global_pct: 70, min: 5, max: 90 }); return true }
      if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
      return false
    },
  })
  await page.goto(base + `/chat?sid=${encodeURIComponent(SLOT)}`, { waitUntil: 'domcontentloaded' })
  const chip = page.getByLabel('Context usage')
  await chip.waitFor({ timeout: 15000 })
  await page.getByText(baseDetail.messages[1].content).waitFor({ timeout: 15000 })
  if (open) {
    await chip.click()
    await page.getByText('Remaining', { exact: true }).waitFor({ timeout: 5000 })
    await page.waitForTimeout(300) // popover slide-up settles
  }
  const box = await chip.boundingBox()
  if (!box) throw new Error('context chip has no box')
  // The composer's lower right corner, chip and popover both inside.
  const x = Math.max(box.x - 300, 0)
  const clip = { x, y: Math.max(box.y - 260, 0), width: Math.min(560, 1280 - x), height: 310 }
  const suffix = open ? 'popover' : showPct ? 'readout' : 'bar'
  await page.screenshot({ path: `${OUT}/${name}-${suffix}-${theme}.png`, clip })
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  let n = 0
  for (const theme of ['dark', 'light']) {
    for (const [name, detail] of Object.entries(STATES)) {
      await shoot(browser, base, theme, name, detail, false, false); n++
      await shoot(browser, base, theme, name, detail, true, false); n++
      await shoot(browser, base, theme, name, detail, false, true); n++
    }
  }
  await browser.close()
  srv.close()
  console.log(`wrote ${n} frames to ${OUT}`)
}

main().catch(err => { console.error(err); process.exit(1) })
