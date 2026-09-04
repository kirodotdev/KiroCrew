/**
 * Screenshot harness for the Older Sessions search-result ordering fix.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with all /api/** answered from fixtures (gateway-free). Seeds a
 * relevance-ranked /api/sessions/search response in which the exact TITLE
 * match is the OLDEST session — the shape that reproduces the bug: pre-fix,
 * the pane re-sorts search results into date-desc and the title match renders
 * LAST; post-fix it renders FIRST (backend relevance order preserved).
 *
 * Usage: node scripts/capture-history-search-order.mjs [outDir] [prefix]
 *   Run against the branch build (prefix "after") and against a build with
 *   origin/main's ChatSidebar.tsx (prefix "before").
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/history-search-order'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const NOW = Date.parse('2026-08-08T15:00:00Z') / 1000

// One open session so the sidebar renders normally.
const slots = [{
  key: 's1', title: 'Current work session', messages: 4, running: false,
  agent: 'kirocrew', created: '2026-08-08T01:00:00Z', last_ts: '2026-08-08T14:00:00Z', folder_id: '',
}]

// History list (pre-search): a few sessions, newest first.
const history = [
  { key: 'dashboard_chat-9', title: 'Fresh unrelated session', modified: NOW - 3600 },
  { key: 'dashboard_chat-8', title: 'Middle unrelated session', modified: NOW - 7200 },
  { key: 'cron_target', title: 'Cron job bug fixes investigation', modified: NOW - 86400 * 2 },
]

// Relevance-ranked search response: exact title match FIRST but OLDEST.
// The backend's search_sessions() applies a 10x title boost, so this is the
// order the real endpoint returns for the query below.
const searchResults = [
  { key: 'cron_target', title: 'Cron job bug fixes investigation', modified: NOW - 86400 * 2 },
  { key: 'dashboard_chat-9', title: 'Fresh unrelated session', modified: NOW - 3600, snippet: 'we went over the cron job bug fixes and what shipped' },
  { key: 'dashboard_chat-8', title: 'Middle unrelated session', modified: NOW - 7200, snippet: 'notes referencing cron job bug fixes follow-ups' },
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2, // sidebar type renders soft at 1x on GitHub
  })
  const page = await context.newPage()

  await stubDashboardApi(page, {
    folders: [], slots,
    extra: async (path, route) => {
      if (path === '/api/sessions/search') {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ sessions: searchResults }) })
        return true
      }
      if (path === '/api/sessions') {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ sessions: history, has_more: false }) })
        return true
      }
      return false
    },
  })
  logPageProblems(page)

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  // Expand the (collapsed-by-default) Older Sessions pane.
  await page.getByRole('button', { name: /older sessions/i }).click()
  await page.waitForTimeout(400)

  // Type the query; the debounced (250ms) search then hits the stubbed endpoint.
  await page.getByPlaceholder(/search older sessions/i).fill('cron job bug fixes')
  await page.waitForTimeout(1200)

  // Probe the rendered order before trusting the pixels: collect the
  // history-pane row titles in DOM order.
  const order = await page.evaluate(() => {
    const rows = [...document.querySelectorAll('div[role="button"][title]')]
    return rows.map(r => r.getAttribute('title')).filter(t =>
      ['Cron job bug fixes investigation', 'Fresh unrelated session', 'Middle unrelated session'].includes(t))
  })
  console.log(`ORDER(${PREFIX}):`, JSON.stringify(order))

  // Clip to the sidebar so the delta is legible at PR-review size.
  await page.screenshot({ path: `${OUT}/${PREFIX}-dark.png`, clip: { x: 0, y: 0, width: 480, height: 900 } })
  console.log(`saved ${OUT}/${PREFIX}-dark.png`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
