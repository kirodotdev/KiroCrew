/**
 * Screenshot harness for the sidebar's unresumable-session notice (#3624).
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with all /api/** answered from fixtures (gateway-free).
 *
 * The bug being evidenced: resuming a history row whose surface ChatPage
 * cannot display (e.g. a dashboard session) succeeded over the wire and then
 * silently bounced the user back — the URL/active-slot reverted with no
 * feedback, indistinguishable from a dead click. The fix surfaces an inline
 * ErrorNotice under the history filter naming the session and its surface.
 *
 * The fixture reproduces exactly that: the resume response carries
 * `surface: 'dashboard'`, which `isChatPageSurface` rejects, so the sidebar
 * must now show the notice instead of reverting silently.
 *
 * Frames:
 *   1-history-row   the dashboard session listed in Older Sessions, pre-click
 *   2-notice        the inline notice after clicking the row
 *   3-notice-close  the notice element itself (close-up)
 *
 * Usage: node scripts/capture-sidebar-unresumable-notice.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/sidebar-unresumable-notice'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const DASH_KEY = 'dashboard-ops-triage'
const DASH_TITLE = 'Ops dashboard triage'

// One ordinary open chat slot so the page has content; the dashboard session
// itself must NOT be open — it has to be a history row for the resume path.
const openSlots = [{
  key: 'chat-current', title: 'Scratch', messages: 2, running: false,
  agent: 'kirocrew', created: '2026-08-20T09:00:00Z', last_ts: '2026-08-20T09:30:00Z', folder_id: '',
}]

// The dashboard-surface session as Older Sessions lists it. The 'dashboard'
// key prefix is what drives both the row's surface label and the real
// backend's surface resolution.
const history = [{
  key: DASH_KEY, title: DASH_TITLE, messages: 14,
  created: '2026-08-18T10:00:00Z', modified: 1786000000, agent: 'kirocrew',
  memory_mode: 'persistent',
}]

async function main() {
  const { srv, base } = await serveDist()
  // mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
  // older than the system Mesa needs; children inherit it, so scrub it here.
  const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
  const browser = await chromium.launch({ env: browserEnv })
  const context = await browser.newContext({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2 })
  const page = await context.newPage()

  let resumed = false

  await stubDashboardApi(page, {
    folders: [], slots: openSlots,
    extra: async (path, route) => {
      if (path === '/api/sessions') { await json(route, { sessions: history, has_more: false }); return true }
      if (path === `/api/chat/slots/${DASH_KEY}/resume`) {
        resumed = true
        // What the real backend returns for a dashboard session: the request
        // itself succeeds (`ok: true`) — only the surface reveals that
        // ChatPage cannot show it. `ok` alone cannot distinguish this from a
        // usable resume, which is the whole point of #3624.
        await json(route, {
          ok: true, key: DASH_KEY, surface: 'dashboard', mode: 'dashboard',
          messages: [], has_more: false, total: 14, memory_mode: 'persistent',
        })
        return true
      }
      if (path === '/api/chat/slots/chat-current') {
        await json(route, { messages: [{ role: 'user', content: 'scratch', ts: '2026-08-20T09:30:00Z', meta: { mid: 'sc-1' } }], has_more: false, total: 1 })
        return true
      }
      return false
    },
  })
  logPageProblems(page)
  page.on('pageerror', e => console.log('PAGEERROR', e.message))

  await page.goto(`${base}/chat`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1500)

  // Open the Older Sessions pane so the dashboard session's history row shows.
  await page.getByText(/Older Sessions/i).first().click()
  await page.waitForTimeout(1200)
  const row = page.locator('[role="button"]').filter({ hasText: DASH_TITLE }).first()
  await row.waitFor({ state: 'visible', timeout: 10_000 })
  await page.screenshot({ path: `${OUT}/${PREFIX}-1-history-row.png` })

  await row.click()
  await page.waitForTimeout(1500)
  console.log('resumed=', resumed)
  if (!resumed) throw new Error('history row click did not hit the resume endpoint')

  // The fix: an inline alert naming the session and its surface.
  const notice = page.locator('[role="alert"]').filter({ hasText: DASH_TITLE }).first()
  await notice.waitFor({ state: 'visible', timeout: 10_000 })
  const text = await notice.textContent()
  console.log('notice text:', text)
  if (!/can't be opened from the chat sidebar/i.test(text || '')) {
    throw new Error(`notice text does not carry the expected copy: ${text}`)
  }
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${PREFIX}-2-notice.png` })
  await notice.screenshot({ path: `${OUT}/${PREFIX}-3-notice-close.png` })

  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}/${PREFIX}-{1-history-row,2-notice,3-notice-close}.png`)
}

main().catch(err => { console.error(err); process.exit(1) })
