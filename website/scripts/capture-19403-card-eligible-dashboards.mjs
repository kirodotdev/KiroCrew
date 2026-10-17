/**
 * Screenshot harness for which sessions the All Dashboards page cards (#19403).
 *
 * `/session-dashboards` built its card list from every `kind === 'session'` node and
 * applied no eligibility test at all, so it offered an entry to a session that has no
 * card to show -- an ADOPTED worker, an incognito session, a remote one -- on a page
 * whose own wording promised each entry a dashboard of its own.
 *
 * The page now asks the one question the frame under each card asks (`canCarryCard`:
 * not private, not remote, no session-tree `parent`). That deliberately KEEPS a
 * dispatched conductor: `GET .../dashboard-card` has no root test and serves it a
 * derived board, and this page is the only surface that mounts one.
 *
 * The seed is one root, one dispatched conductor with a published board, one adopted
 * worker, one incognito session and one remote session, with a question waiting in the
 * adopted worker. Three things must be true in the frame: the root and the conductor
 * are carded, the other three are not, and the adopted worker's question is still in
 * the Needs you inbox, because that is the decision this page exists to carry.
 *
 * The frames cannot lie -- the script ASSERTS the card set by `data-slot`, the exact
 * set of `dashboard-card` reads and the inbox contents, and exits non-zero if any of
 * that is wrong, so the PNGs are not citable unless the page really behaves.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server
 * and answers every /api/** call from fixtures through `stubDashboardApi`.
 *
 * Usage: node scripts/capture-19403-card-eligible-dashboards.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/19403-root-only-dashboards'

mkdirSync(OUT, { recursive: true })

const SLOTS = [
  { key: 'chat-10', title: 'Release the dashboard fix', messages: 8, running: true },
  // Dispatched, no session-tree parent: the card route serves this one a derived board.
  { key: 'chat-11', title: 'Pipeline conductor: ship the wave', messages: 6, running: false, created_by: 'chat-10' },
  // Adopted: a member of someone else's team, so it carries no board of its own.
  { key: 'chat-12', title: 'Worker: capture the evidence', messages: 4, running: false, parent: { slot: 'chat-10', key: 'chat-10' } },
  { key: 'chat-13', title: 'Incognito session', messages: 3, running: false, memory_mode: 'incognito' },
  { key: 'chat-14', title: 'Remote session', messages: 3, running: false, executor: 'remote' },
]

const CARDED = ['chat-10', 'chat-11']

const QUESTION = {
  slot: 'chat-12', ask_id: 'ask-19403',
  questions: [{ question: 'Rebase onto main, or hold the head?', options: [{ label: 'Rebase onto main' }, { label: 'Hold the head' }] }],
}

/** The dispatched conductor has a published DERIVED board; the root has none yet. */
const CARDS = {
  'chat-11': {
    card: { html: '<section><h3 data-dashboard-field="lede">Board</h3><p data-dashboard-field="you"></p></section>',
      data: { lede: 'Five QA fixes in flight', you: 'Two PRs are waiting on your approval' } },
    status: 'published', published_at: 1_762_000_000, content_event_at: 1_762_000_000, stale: false,
  },
}
const NO_CARD = { card: null, status: 'waiting', published_at: null, content_event_at: null, stale: false }

/** One intent per session, so each card shows a real saved summary. */
const summary = slot => ({
  enabled: true, stale: false, constraints: [], generated_at: 1_762_000_000,
  user_turns: 4, last_activity: null,
  intents: [{
    title: slot === 'chat-10' ? 'Ship the All Dashboards fix' : 'Drive the wave to green',
    initial_intent: 'Ship the fix', progress: ['Tests pass on the page test file'], next_steps: [],
    ranges: [[1, 4]], status: 'active', verified: null, state: 'in-progress', last_touched_turn: 4, origin_turn: 1,
  }],
})

const cardReads = []
const { srv, base } = await serveDist()
const browser = await chromium.launch()
const failures = []

try {
  const context = await browser.newContext({
    // 1x, per the image-read ceiling the sibling harnesses observe.
    viewport: { width: 1500, height: 1040 },
    deviceScaleFactor: 1,
  })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    slots: SLOTS,
    // The page is a Feature Preview surface; the operator has it on.
    localStorageEntries: { 'mc-lang': 'en' },
    extra: async (path, route) => {
      if (path === '/api/ask-question/pending') {
        await json(route, [QUESTION])
        return true
      }
      if (path === '/api/approvals') {
        await json(route, [])
        return true
      }
      if (path === '/api/workflows/runs') {
        await json(route, { runs: [] })
        return true
      }
      // The card's sandboxed document: the gateway mints a one-shot URL for the posted
      // srcdoc; here the document itself is handed back as a data URL, as the sibling
      // harnesses do. Without it the board never paints and the frame proves nothing.
      if (path === '/api/sandbox-doc') {
        const body = JSON.parse(route.request().postData() || '{}')
        await json(route, { url: 'data:text/html;base64,' + Buffer.from(String(body.html || ''), 'utf8').toString('base64') })
        return true
      }
      const match = /^\/api\/chat\/slots\/([^/]+)\/(summary|dashboard-card)$/.exec(path)
      if (!match) return false
      const slot = decodeURIComponent(match[1])
      if (match[2] === 'summary') {
        await json(route, summary(slot))
        return true
      }
      cardReads.push(slot)
      await json(route, CARDS[slot] || NO_CARD)
      return true
    },
  })

  await page.goto(`${base}/session-dashboards`)
  await page.getByRole('region', { name: 'Needs you' }).waitFor()
  await page.getByText('Ship the All Dashboards fix').first().waitFor()
  await page.waitForTimeout(800)

  const cards = await page.locator('[data-testid="session-dashboard-card"]').evaluateAll(
    nodes => nodes.map(node => node.getAttribute('data-slot')))
  if ([...cards].sort().join(',') !== CARDED.join(',')) failures.push(`cards are [${cards}], expected [${CARDED}]`)
  if ([...cardReads].sort().join(',') !== CARDED.join(',')) failures.push(`dashboard-card was read for [${cardReads}], expected [${CARDED}]`)

  // The board is the whole point of keeping this card: assert the document really
  // mounted, so a frame showing only the "Content published" line cannot stand in for it.
  const board = page.locator('[data-testid="session-dashboard-card"][data-slot="chat-11"] iframe')
  await board.waitFor({ timeout: 10_000 }).catch(() => failures.push('the dispatched conductor card mounted no board document'))
  const boardText = await board.first().contentFrame()?.locator('body').innerText().catch(() => '')
  if (!(boardText || '').includes('Two PRs are waiting on your approval')) {
    failures.push(`the board document does not carry its data: ${JSON.stringify((boardText || '').slice(0, 120))}`)
  }

  const inbox = page.getByRole('region', { name: 'Needs you' })
  if (!await inbox.getByText('Rebase onto main, or hold the head?').isVisible()) {
    failures.push('the adopted worker question is missing from the inbox')
  }
  if (!await inbox.getByText('Worker: capture the evidence').isVisible()) {
    failures.push('the inbox does not name the session the question came from')
  }

  await page.screenshot({ path: join(OUT, 'card-eligible-gallery.png'), fullPage: true })
  await page.locator('[data-testid="session-dashboard-card"][data-slot="chat-11"]').screenshot({ path: join(OUT, 'dispatched-conductor-board.png') })
  await inbox.screenshot({ path: join(OUT, 'needs-you-keeps-the-adopted-worker.png') })

  // Second scene, the one iamwhatever's review named: every open session is one this
  // page draws no card for. The empty state must say THAT, never "No sessions", and
  // the muted line must still account for the sessions that are open.
  const hidden = await context.newPage()
  logPageProblems(hidden)
  await stubDashboardApi(hidden, {
    slots: SLOTS.filter(slot => !CARDED.includes(slot.key)),
    localStorageEntries: { 'mc-lang': 'en' },
    extra: async (path, route) => {
      if (path === '/api/ask-question/pending') {
        await json(route, [QUESTION])
        return true
      }
      if (path === '/api/approvals') {
        await json(route, [])
        return true
      }
      if (path === '/api/workflows/runs') {
        await json(route, { runs: [] })
        return true
      }
      return false
    },
  })
  await hidden.goto(`${base}/session-dashboards`)
  const emptyTitle = hidden.locator('[data-testid="empty-state-title"]')
  await emptyTitle.waitFor({ timeout: 10_000 }).catch(() => failures.push('no empty state rendered for an all-withheld fleet'))
  const emptyText = await emptyTitle.innerText().catch(() => '')
  if (emptyText !== 'None of your open sessions has a status card.') {
    failures.push(`empty title reads ${JSON.stringify(emptyText)}`)
  }
  if (!await hidden.getByText('3 sessions without a status card are not shown.').isVisible()) {
    failures.push('the withheld-session count is missing')
  }
  if (await hidden.locator('[data-testid="session-dashboard-card"]').count()) {
    failures.push('a card rendered for a session with no card to show')
  }
  await hidden.screenshot({ path: join(OUT, 'all-withheld-empty-state.png'), fullPage: true })
} finally {
  await browser.close()
  srv.close()
}

if (failures.length) {
  console.error('FAILED:')
  for (const line of failures) console.error(` - ${line}`)
  process.exit(1)
}
console.log(`wrote 4 frames to ${OUT}; dashboard-card read for: ${cardReads.join(', ')}`)
