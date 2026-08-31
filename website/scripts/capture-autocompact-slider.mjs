/**
 * Screenshot harness for the per-session auto-compact threshold slider.
 *
 * The context-usage popover (ChatInput) gains a slider section that sets THIS
 * session's compaction threshold, layered over the global
 * `session.autocompact_pct`. Two states carry the whole design:
 *
 *  - `overridden`: the session holds its own threshold (85%), so the value
 *    reads from the override and a "Reset to global (70%)" link renders.
 *  - `following-global`: no override, the slider sits at the global value and
 *    a "Following global (70%)" note replaces the reset link.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures — gateway-free. The slot detail payload seeds the context gauge
 * (72% of 200K) so the chip renders without a live session; the section's own
 * GET /autocompact is stubbed per state via a mutable fixture.
 *
 * Labels are read from the CATALOG, so a key rename breaks the capture loudly
 * instead of silently screenshotting the wrong element.
 *
 * Usage: node scripts/capture-autocompact-slider.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/autocompact-slider'
mkdirSync(OUT, { recursive: true })

const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const manual = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8'))
const ci = manual.components.chatInput
const AUTO_COMPACT_AT = ci.auto_compact_at // "Auto-compact at"
const RESET_TPL = ci.reset_to_global // "Reset to global ({{pct}}%)"
const FOLLOWING_TPL = ci.following_global // "Following global ({{pct}}%)"
if (!AUTO_COMPACT_AT || !RESET_TPL || !FOLLOWING_TPL) {
  throw new Error('components.chatInput auto-compact keys missing — renamed?')
}
const fill = (tpl, pct) => tpl.replace('{{pct}}', String(pct))

const SLOT = 'autocompact-demo'
const now = Math.floor(Date.now() / 1000)

const slots = [{
  key: SLOT,
  title: 'Long investigation session',
  running: false,
  last_message: 'Summarised the findings so far.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  modified: now,
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  messages: [
    { role: 'user', ts: now - 600, content: 'Walk me through the compaction pipeline.' },
    { role: 'assistant', ts: now - 30, content: 'The gate ladder reads the threshold on every context reading.' },
  ],
  // Seeds the context gauge so the chip renders gateway-free: 72% of 200K.
  // Wire shape is flat (`context_pct` etc.) — see fetchSlotDetail, chatSlice.ts.
  context_pct: 72,
  context_used_tokens: 144_000,
  context_window_tokens: 200_000,
}

// Mutable per-state fixture for GET /api/chat/slots/<slot>/autocompact.
let autocompact = { pct: 85, global_pct: 70, min: 5, max: 90 }

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)

  await stubDashboardApi(page, {
    slots,
    extra: async (path, route) => {
      if (path === `/api/chat/slots/${SLOT}/autocompact`) { await json(route, autocompact); return true }
      if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
      return false
    },
  })

  await page.goto(base + `/chat?sid=${encodeURIComponent(SLOT)}`, { waitUntil: 'domcontentloaded' })
  const chip = page.getByLabel('Context usage')
  await chip.waitFor({ timeout: 15000 })

  // State 1 — session override at 85%, reset link present.
  await chip.click()
  await page.getByText(AUTO_COMPACT_AT, { exact: true }).waitFor({ timeout: 5000 })
  await page.getByText(fill(RESET_TPL, 70), { exact: true }).waitFor({ timeout: 5000 })
  await page.waitForTimeout(300) // popover slide-up settles
  await page.screenshot({ path: `${OUT}/overridden.png` })

  // State 2 — no override: slider follows the global, note replaces the link.
  // A fresh page load, not a popover reopen: the section's value lives in the
  // React Query cache (staleTime 30s), so a reopen would render the cached
  // state-1 fixture instead of refetching the mutated one.
  autocompact = { pct: null, global_pct: 70, min: 5, max: 90 }
  await page.reload({ waitUntil: 'domcontentloaded' })
  const chip2 = page.getByLabel('Context usage')
  await chip2.waitFor({ timeout: 15000 })
  await chip2.click()
  await page.getByText(fill(FOLLOWING_TPL, 70), { exact: true }).waitFor({ timeout: 5000 })
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/following-global.png` })

  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}/overridden.png and ${OUT}/following-global.png`)
}

main().catch(err => { console.error(err); process.exit(1) })
