/**
 * Screenshot harness for the reactive model-access-denial fallback cards (#10815).
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * and answers every /api/** call from fixtures via Playwright route
 * interception (gateway-free — no kiro-cli, no live backend). It exists to give
 * the UX review the two user-visible surfaces this PR adds, which no live
 * capture supplied:
 *
 *   01-notice   The persisted swap NOTICE card the fallback appends when the
 *               account cannot use the configured model and the session is
 *               re-prompted on the first advertised model instead
 *               ("Your account cannot use model '…' — running on '…' instead."),
 *               shown with the header model chip re-synced to the served model.
 *   02-terminal The fail-visible terminal entitlement ERROR card taken when the
 *               provider exposes no set_model seam (or the swap RPC itself
 *               fails): a red error row tagged `model_unentitled`, which the
 *               frontend reads to offer the model picker affordance.
 *
 * The cards render from the seeded transcript exactly as slot.append() writes
 * them (role `notice` -> NoticeCard, role `error` + meta.kind `model_unentitled`
 * -> ErrorCard with the picker affordance). Nothing here re-implements the card;
 * it feeds the real renderers the row shape the backend produces.
 *
 * Usage: node scripts/capture-model-access-fallback.mjs [outDir] [prefix] [distDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/model-access-fallback'
const PREFIX = process.argv[3] || 'after'
const DIST = process.argv[4] || DEFAULT_DIST

mkdirSync(OUT, { recursive: true })

// The configured model the account is NOT entitled to, and the first advertised
// model the swap lands on. The served list is what the header chip and the
// picker show.
const REJECTED = 'auto'
const SERVED = 'claude-opus-4-8'
const ADVERTISED = [SERVED, 'claude-sonnet-4-6']

// Slot 01: the swap happened, so the slot is pinned to the served model (the
// header chip reads SERVED) and the transcript carries the persisted notice.
const NOTICE_SLOT = 'chat-fallback-notice'
// Slot 02: no set_model seam, so the model never changed; the terminal error
// card stands in the transcript.
const TERMINAL_SLOT = 'chat-fallback-terminal'
// Slot 03: the swap applied but its recovery replay was superseded by a newer
// user message, so the replay is dropped at consume and the cancelled-retry
// notice stands in the transcript.
const CANCELLED_SLOT = 'chat-fallback-cancelled'

const slots = [
  {
    key: NOTICE_SLOT,
    title: 'Draft the launch note',
    running: false,
    last_message: "Your account cannot use model 'auto' — running on 'claude-opus-4-8' instead.",
    messages: 3,
    agent: 'kirocrew',
    model: SERVED,
    memory_mode: 'persistent',
    project: '/home/user/workspace/notes',
    folder_id: '',
    modified: Math.floor(Date.now() / 1000),
    source_links: [],
    source_links_total: 0,
  },
  {
    key: TERMINAL_SLOT,
    title: 'Summarize the incident',
    running: false,
    last_message: "This account is not entitled to any served model.",
    messages: 2,
    agent: 'kirocrew',
    model: '',
    memory_mode: 'persistent',
    project: '/home/user/workspace/notes',
    folder_id: '',
    modified: Math.floor(Date.now() / 1000),
    source_links: [],
    source_links_total: 0,
  },
  {
    key: CANCELLED_SLOT,
    title: 'Rename the export columns',
    running: false,
    last_message: "\u2139\uFE0F Model-fallback retry cancelled \u2014 your newer message runs instead.",
    messages: 3,
    agent: 'kirocrew',
    model: SERVED,
    memory_mode: 'persistent',
    project: '/home/user/workspace/notes',
    folder_id: '',
    modified: Math.floor(Date.now() / 1000),
    source_links: [],
    source_links_total: 0,
  },
]

const now = Date.now() / 1000

// Transcript for the notice frame: the user turn, the persisted swap notice
// (exactly as slot.append("notice", …, "msg msg-info")), then the assistant
// reply that the re-prompt on the served model produced.
const noticeDetail = {
  running: false,
  has_more: false,
  total: 3,
  queue: [],
  project: '/home/user/workspace/notes',
  messages: [
    { role: 'user', ts: now - 120, content: 'Draft a short launch note for the new export flow.' },
    {
      role: 'notice',
      ts: now - 118,
      content:
        "\u26A0\uFE0F Your account cannot use model '" + REJECTED + "' \u2014 running on '"
        + SERVED + "' instead.",
    },
    {
      role: 'assistant',
      ts: now - 110,
      content:
        'Here is a first draft:\n\nThe export flow now supports one-click CSV and '
        + 'PDF, with a progress toast while the file builds.',
    },
  ],
}

// Transcript for the terminal frame: the user turn, then the fail-visible
// entitlement error card tagged model_unentitled so the picker affordance
// renders (exactly as slot.append("error", "\u274C …", meta={kind: …})).
const terminalDetail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: '/home/user/workspace/notes',
  messages: [
    { role: 'user', ts: now - 60, content: 'Summarize the incident from this thread.' },
    {
      role: 'error',
      ts: now - 58,
      content:
        "\u274C This account is not entitled to model 'auto'. Served models: "
        + ADVERTISED.join(', ') + '.',
      meta: { kind: 'model_unentitled' },
    },
  ],
}

// Transcript for the cancelled-retry frame: the swap applied and re-queued the
// user's original message, but a newer user message superseded that replay, so
// the recovery is dropped at consume and this notice stands (exactly as
// slot.append("notice", "\u2139\uFE0F Model-fallback retry cancelled \u2014 …", "msg msg-info")).
const cancelledDetail = {
  running: false,
  has_more: false,
  total: 3,
  queue: [],
  project: '/home/user/workspace/notes',
  messages: [
    { role: 'user', ts: now - 90, content: 'Rename the export columns to Title Case.' },
    {
      role: 'notice',
      ts: now - 86,
      content: '\u2139\uFE0F Model-fallback retry cancelled \u2014 your newer message runs instead.',
    },
    {
      role: 'assistant',
      ts: now - 80,
      content: 'Renamed the export columns to Title Case and re-ran the preview.',
    },
  ],
}

const detailFor = key =>
  key === NOTICE_SLOT ? noticeDetail : key === CANCELLED_SLOT ? cancelledDetail : terminalDetail

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    deviceScaleFactor: 2,
  })

  // The roster the header chip and picker read. useAgents reads d.agents /
  // d.default_agent off an OBJECT (the bare-array shape is a shared-stub bug,
  // see issue #11119); answer the object shape here so the served model chip
  // resolves rather than reading empty.
  const extra = async (path, route) => {
    if (path === '/api/agents' || path === '/api/chat/agents') {
      await json(route, {
        agents: [{ name: 'kirocrew', model: SERVED }],
        default_agent: 'kirocrew',
        models: ADVERTISED.map(id => ({ id, name: id })),
      })
      return true
    }
    if (path.startsWith('/api/chat/slots/')) {
      const key = path.split('/api/chat/slots/')[1].split('/')[0]
      await json(route, detailFor(key))
      return true
    }
    return false
  }

  async function shoot(theme, slot, title, name) {
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { slots, theme, extra })
    await page.routeWebSocket(/\/api\/ws/, () => {})
    await page.addInitScript(s => localStorage.setItem('mc-active-slot', s), slot)
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForSelector('[aria-label="Chat messages"]', { timeout: 20_000 })
    // Click the target session row by its title so the correct transcript is
    // shown regardless of which slot the sidebar opened first.
    const row = page.getByText(title, { exact: true }).first()
    await row.click({ timeout: 15_000 })
    // Wait until the header shows this session's title (the transcript swapped).
    await page.waitForFunction(
      t => !!document.querySelector('[aria-label="Chat messages"]')
        && (document.body.textContent || '').includes(t),
      title,
      { timeout: 15_000 },
    )
    await page.waitForTimeout(1200)
    const file = `${OUT}/${PREFIX}-${name}.png`
    await page.screenshot({ path: file })
    console.log('wrote', file)
    await page.close()
  }

  await shoot('dark', NOTICE_SLOT, 'Draft the launch note', '01-notice')
  await shoot('dark', TERMINAL_SLOT, 'Summarize the incident', '02-terminal')
  await shoot('dark', CANCELLED_SLOT, 'Rename the export columns', '03-cancelled')

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
