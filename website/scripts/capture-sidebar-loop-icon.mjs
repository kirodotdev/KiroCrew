/**
 * Screenshot harness for the chat sidebar's goal-loop row (#26579).
 *
 * A looping session's status line used to open with "Loop 7/24" text. The row
 * now shows only the loop glyph and gives the line to the status detail; the
 * count lives on the glyph's tooltip and accessible name. This harness renders
 * three looping rows (capped, uncapped, interrupted) plus a plain row, in light
 * and dark, and prints each loop glyph's accessible name so the moved count is
 * in the evidence too.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures (gateway-free).
 *
 * Usage: [LOOP_ICON_BASELINE=1] node scripts/capture-sidebar-loop-icon.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/sidebar-loop-icon'
/** LOOP_ICON_BASELINE=1 against the pre-change tree writes `before-*` shots. */
const PREFIX = process.env.LOOP_ICON_BASELINE === '1' ? 'before' : 'after'
mkdirSync(OUT, { recursive: true })
const now = Math.floor(Date.now() / 1000)
const ago = mins => new Date((now - mins * 60) * 1000).toISOString()

const slots = [
  {
    key: 'chat-plain', title: 'Plain chat (no loop)', running: false, messages: 3,
    agent: 'default', modified: now + 60, last_ts: ago(0), last_turn_ts: ago(0), folder_id: '',
    last_message: 'Done. The fix is on main.',
  },
  {
    key: 'chat-loop-capped', title: 'Dashboard audit patrol', running: false, messages: 40,
    agent: 'kirocrew', modified: now, last_ts: ago(1), last_turn_ts: ago(1), folder_id: '',
    last_message: 'PR #26295 CI 61 pass, 20 running, 0 red',
  },
  {
    key: 'chat-loop-open', title: 'Security fix patrol', running: false, messages: 22,
    agent: 'kirocrew', modified: now - 120, last_ts: ago(2), last_turn_ts: ago(2), folder_id: '',
    last_message: 'Waiting on review lanes for #24812',
  },
  {
    key: 'chat-loop-stalled', title: 'Release notes watcher', running: false, interrupted: true,
    messages: 9, agent: 'kirocrew', modified: now - 300, last_ts: ago(5), last_turn_ts: ago(5),
    folder_id: '', last_message: 'Model error, retry pending',
  },
]

const loop = (slot, cycle, max) => ({
  id: `loop-${slot}`, slot_key: slot, message: 'Patrol the open PRs.', idle_secs: 300,
  max_cycles: max, cycle_count: cycle, active: true, last_fire_ts: now - 60, next_due_ts: 0,
})
const loops = [
  loop('chat-loop-capped', 7, 24),
  loop('chat-loop-open', 31, 0),
  loop('chat-loop-stalled', 47, 72),
]

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1400, height: 700 }, deviceScaleFactor: 2 })
try {
  for (const theme of ['light', 'dark']) {
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      slots,
      theme,
      localStorageEntries: {
        'mc-active-slot': 'chat-plain',
        'mc-privacy-notice-v1': '1',
        'mc-sidebar-pinned': 'true',
      },
      extra: async (path, route) => {
        if (path === '/api/autonudge') { await json(route, { enabled: true, loops }); return true }
        const m = path.match(/^\/api\/autonudge\/slot\/(.+)$/)
        if (m) { await json(route, { enabled: true, loop: loops.find(l => l.slot_key === decodeURIComponent(m[1])) ?? null }); return true }
        if (path === '/api/monitors') { await json(route, { enabled: true, monitors: [] }); return true }
        if (path.startsWith('/api/monitors/slot/')) { await json(route, { enabled: true, monitor: null }); return true }
        return false
      },
    })
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.locator('.lucide-goal').first().waitFor({ state: 'visible', timeout: 20000 })
    // Open the loop-free chat so every looping row renders as a background row.
    await page.locator('[data-session-row="chat-plain"]').click()
    await page.mouse.move(1300, 650)
    await page.waitForTimeout(1500)
    const names = await page.locator('[data-testid="goal-loop-icon"]').evaluateAll(els => els.map(el => ({
      row: el.closest('[data-session-row]')?.getAttribute('data-session-row'),
      ariaLabel: el.getAttribute('aria-label'),
      title: el.getAttribute('title'),
      line: (el.parentElement?.textContent || '').trim(),
    })))
    console.log(theme, JSON.stringify(names, null, 1))
    const box = await page.locator('.session-row').evaluateAll(els => {
      const rs = els.map(el => el.getBoundingClientRect())
      const x = Math.min(...rs.map(r => r.left)) - 4
      const y = Math.min(...rs.map(r => r.top)) - 4
      const w = Math.max(...rs.map(r => r.right)) - x + 4
      const h = Math.max(...rs.map(r => r.bottom)) - y + 4
      return { x, y, width: w, height: h }
    })
    await page.screenshot({ path: `${OUT}/${PREFIX}-${theme}.png`, clip: box })
    await page.close()
  }
} finally {
  await browser.close()
  srv.close()
}
