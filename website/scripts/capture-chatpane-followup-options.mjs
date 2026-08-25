/**
 * Screenshot harness for the grid-pane follow-up [OPTIONS:] pills (#5870).
 *
 * Before this fix a ChatPane stripped the agent's [OPTIONS:] marker from the
 * transcript (ChatMessageList does that by design) but never passed the derived
 * options to its ChatInput, so a grid pane silently dropped the choices. The
 * frame proves the fix: split view tiles two sessions, the LEFT pane's last
 * assistant turn offers three options and the pills render above that pane's
 * composer; the RIGHT pane's last turn offers none and renders no pills, so the
 * presence and the absence are the same evidence.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with all /api/** answered from fixtures (gateway-free).
 *
 * Usage: node scripts/capture-chatpane-followup-options.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/chatpane-followup-options'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const LEFT = 'chat-options'
const RIGHT = 'chat-plain'

const OPTION_LABELS = ['Merge it now', 'Show me the diff', 'Skip the rebase']

const slots = [
  {
    key: LEFT, title: 'Release PR babysit', messages: 4, running: false,
    agent: 'kirocrew', created: '2026-08-20T09:00:00Z', last_ts: '2026-08-25T10:00:00Z', folder_id: '',
  },
  {
    key: RIGHT, title: 'Pipeline triage', messages: 2, running: false,
    agent: 'oncall', created: '2026-08-24T09:00:00Z', last_ts: '2026-08-25T09:30:00Z', folder_id: '',
  },
]

// The layout a user's ⌘D would have persisted. Anchor = first session leaf.
const LAYOUT = {
  [LEFT]: {
    type: 'split', id: 'sp-1', dir: 'row', sizes: [0.5, 0.5],
    children: [
      { type: 'leaf', id: 'lf-1', kind: 'session', slot: LEFT },
      { type: 'leaf', id: 'lf-2', kind: 'session', slot: RIGHT },
    ],
  },
}

/** The marker has to close its own line for OPTION_MARKER_RE to match. */
const leftMessages = [
  { role: 'user', content: 'Is the release PR ready to go?', ts: '2026-08-25T09:58:00Z', meta: { mid: 'l-1' } },
  {
    role: 'assistant',
    content: `All 63 checks are green and the branch is mergeable. One optional rebase would drop the merge commit.\n\n[OPTIONS: ${OPTION_LABELS.join(' | ')}]`,
    ts: '2026-08-25T09:59:00Z', meta: { mid: 'l-2' },
  },
]

const rightMessages = [
  { role: 'user', content: 'Anything paging overnight?', ts: '2026-08-25T09:28:00Z', meta: { mid: 'r-1' } },
  { role: 'assistant', content: 'Nothing paged. One warning cleared itself at 03:10.', ts: '2026-08-25T09:29:00Z', meta: { mid: 'r-2' } },
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()

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
      if (path === `/api/chat/slots/${LEFT}`) {
        await json(route, { messages: leftMessages, has_more: false, total: leftMessages.length })
        return true
      }
      if (path === `/api/chat/slots/${RIGHT}`) {
        await json(route, { messages: rightMessages, has_more: false, total: rightMessages.length })
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

  await page.goto(`${base}/chat/${LEFT}`, { waitUntil: 'domcontentloaded' })
  await page.getByText('Nothing paged.').first().waitFor({ timeout: 20_000 })

  // The evidence is only valid if SPLIT VIEW actually materialized: if the
  // layout seed or the session_grid flag stops working, the page renders
  // single chat and ChatPage's own pills would satisfy every assertion below
  // while photographing a surface that was never broken. Sidebar titles and
  // the sidebar search field can spoof loose global counts, so the guard is
  // scoped to the panes themselves: exactly two [data-chat-pane] boundaries
  // (ChatPane's own wrapper attribute), each containing its own composer, and
  // the options-bearing pane containing the pills.
  await page.locator('[data-chat-pane]').first().waitFor({ timeout: 10_000 })
  const paneCount = await page.locator('[data-chat-pane]').count()
  if (paneCount !== 2) {
    throw new Error(`expected exactly 2 [data-chat-pane] boundaries (split view), got ${paneCount} — the frame would show single chat, not ChatPane`)
  }
  for (let i = 0; i < 2; i++) {
    const composersInPane = await page.locator('[data-chat-pane]').nth(i).getByRole('textbox').count()
    if (composersInPane < 1) throw new Error(`pane ${i} has no composer — not a live ChatPane`)
  }
  const pillsInPanes = await page.locator('[data-chat-pane]').getByRole('button', { name: OPTION_LABELS[0], exact: true }).count()
  if (pillsInPanes !== 1) {
    throw new Error(`expected the ${JSON.stringify(OPTION_LABELS[0])} pill inside exactly 1 pane, got ${pillsInPanes} — the pills may belong to ChatPage's composer, not a ChatPane`)
  }

  // The chips' staggered entrance is still translating them for ~750ms after
  // mount; a screenshot inside that window catches a chip mid-hop.
  const firstPill = page.getByRole('button', { name: OPTION_LABELS[0], exact: true })
  await firstPill.waitFor({ state: 'visible', timeout: 10_000 })
  await page.waitForTimeout(1000)

  // Every offered option renders exactly one pill — a partial row would mean the
  // parse or the pass-through truncated the set.
  for (const label of OPTION_LABELS) {
    const n = await page.getByRole('button', { name: label, exact: true }).count()
    if (n !== 1) throw new Error(`expected exactly 1 pill for ${JSON.stringify(label)}, got ${n}`)
  }
  // The transcript must NOT show the raw marker: pills replace it, never duplicate it.
  const rawMarker = await page.getByText('[OPTIONS:', { exact: false }).count()
  if (rawMarker !== 0) throw new Error(`raw [OPTIONS:] marker leaked into the transcript (${rawMarker} occurrence(s))`)

  const box = await firstPill.boundingBox()
  const vp = page.viewportSize()
  if (!box || box.y < 0 || box.y > vp.height) {
    throw new Error(`pill is outside the viewport (box=${JSON.stringify(box)}) — the frame would not show it`)
  }

  await page.screenshot({ path: `${OUT}/${PREFIX}-1-split-panes.png` })
  // Close-up: the left pane's composer band with the pills.
  await page.screenshot({
    path: `${OUT}/${PREFIX}-2-pills-closeup.png`,
    clip: { x: 0, y: Math.max(0, box.y - 40), width: vp.width / 2, height: Math.min(vp.height - Math.max(0, box.y - 40), 260) },
  })

  console.log(`pills=${OPTION_LABELS.length} pillY=${Math.round(box.y)}`)
  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}/${PREFIX}-{1-split-panes,2-pills-closeup}.png`)
}

main().catch(err => { console.error(err); process.exit(1) })
