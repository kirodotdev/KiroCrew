/**
 * Screenshot harness for the bounded pane hydrate + its earlier-messages marker.
 *
 * The row appears on a BACKGROUND pane only. The active pane renders the store's
 * full history rather than its own bounded page, so a row there would be false and
 * is suppressed — which is why the split is anchored at the SHORT session.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with all /api/** answered from fixtures (gateway-free).
 *
 * Shape that reaches the change: split view tiles two session panes. The LEFT
 * session is longer than the hydrate bound, so the server answers `has_more:
 * true` and the pane renders the marker; the RIGHT one fits inside the bound and
 * must render no marker. Both in one frame, so the marker's presence and its
 * absence are the same evidence.
 *
 * Split view is reached the way a user reaches it: `mc-split-layouts` holds a
 * two-session layout anchored at the left slot, and landing on that anchor
 * auto-enters split. The feature is gated on `dashboard.session_grid`, so the
 * config fixture turns it on.
 *
 * Usage: node scripts/capture-pane-earlier-marker.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/pane-earlier-marker'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const LONG = 'chat-long'
const SHORT = 'chat-short'
const BG = 'chat-background'

const slots = [
  {
    key: LONG, title: 'Release checklist review', messages: 120, running: false,
    agent: 'kirocrew', created: '2026-06-01T09:00:00Z', last_ts: '2026-08-13T10:00:00Z', folder_id: '',
  },
  {
    key: SHORT, title: 'Pipeline triage', messages: 6, running: false,
    agent: 'oncall', created: '2026-08-12T09:00:00Z', last_ts: '2026-08-13T09:30:00Z', folder_id: '',
  },
  {
    key: BG, title: 'Migration notes', messages: 90, running: false,
    agent: 'kirocrew', created: '2026-07-10T09:00:00Z', last_ts: '2026-08-13T08:00:00Z', folder_id: '',
  },
]

// The layout a user's ⌘D would have persisted. Anchor = first session leaf.
const LAYOUT = {
  [LONG]: {
    type: 'split', id: 'sp-1', dir: 'row', sizes: [0.5, 0.5],
    children: [
      // LONG is the anchor, and only landing on the ANCHOR auto-enters split — a
      // member slot stays in single chat with an "In split" badge. LONG is therefore
      // the ACTIVE pane, which renders the store's full history rather than its own
      // bounded page, so its row is suppressed by design. BG is the background pane
      // that earns the row, so one frame carries both the row and its suppression.
      { type: 'leaf', id: 'lf-1', kind: 'session', slot: LONG },
      { type: 'leaf', id: 'lf-2', kind: 'session', slot: BG },
    ],
  },
}

const longTail = [
  { role: 'user', content: 'Which checklist items are still open?', ts: '2026-08-13T09:56:00Z', meta: { mid: 'm-117' } },
  { role: 'assistant', content: 'The changelog entry, the migration note, and the smoke run.', ts: '2026-08-13T09:57:00Z', meta: { mid: 'm-118' } },
  { role: 'user', content: 'Which one is blocking the release?', ts: '2026-08-13T09:58:00Z', meta: { mid: 'm-119' } },
  { role: 'assistant', content: 'The smoke run — it needs the staging deploy to finish first.', ts: '2026-08-13T09:59:00Z', meta: { mid: 'm-120' } },
]

const shortAll = [
  { role: 'user', content: 'Anything paging overnight?', ts: '2026-08-13T09:28:00Z', meta: { mid: 's-1' } },
  { role: 'assistant', content: 'Nothing paged. One warning cleared itself at 03:10.', ts: '2026-08-13T09:29:00Z', meta: { mid: 's-2' } },
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()

  const limits = []

  await stubDashboardApi(page, {
    folders: [], slots,
    extra: async (path, route) => {
      if (path === '/api/dashboard/config') {
        // session_grid gates split view; the rest mirrors the shared default.
        await json(route, {
          restore_sessions: false, restore_window_minutes: 30,
          merge_queued_messages: false, widget_density: 'more', session_grid: true,
        })
        return true
      }
      if (path === `/api/chat/slots/${LONG}`) {
        limits.push(new URL(route.request().url()).searchParams.get('limit'))
        // Longer than the bound: the server reports there is older history.
        await json(route, { messages: longTail, has_more: true, total: 120 })
        return true
      }
      if (path === `/api/chat/slots/${BG}`) {
        limits.push(new URL(route.request().url()).searchParams.get('limit'))
        await json(route, { messages: longTail, has_more: true, total: 90 })
        return true
      }
      if (path === `/api/chat/slots/${SHORT}`) {
        await json(route, { messages: shortAll, has_more: false, total: shortAll.length })
        return true
      }
      if (path === '/api/sessions') { await json(route, { sessions: [], has_more: false }); return true }
      if (path === '/api/chat/pins') { await json(route, { pins: [] }); return true }
      return false
    },
  })
  // Added after stubDashboardApi so it runs after that script's localStorage.clear().
  await page.addInitScript(layout => {
    localStorage.setItem('mc-split-layouts', JSON.stringify(layout))
  }, LAYOUT)
  logPageProblems(page)

  await page.goto(`${base}/chat/${LONG}`, { waitUntil: 'domcontentloaded' })
  await page.getByText('Migration notes').first().waitFor({ timeout: 20_000 })
  await page.waitForTimeout(2500)

  const marker = page.getByRole('button', { name: /earlier messages/i })
  await marker.waitFor({ state: 'visible', timeout: 10_000 })

  // The bound is what makes the marker necessary, so assert it was actually sent
  // rather than trusting that the pane asked for a page at all.
  if (!limits.some(l => l && Number(l) > 0)) {
    throw new Error(`pane did not request a bounded page (limits=${JSON.stringify(limits)})`)
  }
  // Exactly one: the active pane is suppressed by design and the short session fits
  // inside the bound, so a second marker would mean the row renders unconditionally
  // and the frame would prove nothing.
  const count = await marker.count()
  if (count !== 1) throw new Error(`expected exactly 1 marker (long pane only), got ${count}`)

  // Scroll the MARKER itself into view, not a scroller picked by heuristic: message
  // rows carry their own buttons, so "the scroller containing a button" matches the
  // first pane and silently leaves the marker's pane pinned to its newest message —
  // the assertions below then pass while the captured pixels omit the row entirely.
  await marker.scrollIntoViewIfNeeded()
  await page.waitForTimeout(600)

  const box = await marker.boundingBox()
  const vp = page.viewportSize()
  if (!box || box.y < 0 || box.y > vp.height) {
    throw new Error(`marker is outside the viewport (box=${JSON.stringify(box)}) — the frame would not show it`)
  }

  await page.screenshot({ path: `${OUT}/${PREFIX}-1-split-panes.png` })
  await marker.screenshot({ path: `${OUT}/${PREFIX}-2-marker.png` })

  console.log(`limits=${JSON.stringify(limits)} markers=${count} markerY=${Math.round(box.y)}`)
  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}/${PREFIX}-{1-split-panes,2-marker}.png`)
}

main().catch(err => { console.error(err); process.exit(1) })
