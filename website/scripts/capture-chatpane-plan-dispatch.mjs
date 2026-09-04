/**
 * Screenshot harness for grid-pane orchestrator plan dispatch (#5893).
 *
 * `deriveFollowUpOptions` flags plan follow-ups (`followUpIsPlan`), and
 * ChatPage dispatches them to `POST /api/chat/slots/{slot}/plan-action`. The
 * pane hosts used to drop the flag, so clicking a plan chip (e.g. `Go`) in a grid
 * pane fell through to the composer-append path — the approval label ended up
 * as composer text, one Enter away from being sent to the agent as an ordinary
 * chat message. The frames prove the fix: the LEFT pane belongs to an
 * orchestrator session mid-plan; clicking its approve chip fires the
 * plan-action POST (asserted, with the pane's own slot in the URL) and leaves
 * the composer EMPTY.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with all /api/** answered from fixtures (gateway-free).
 *
 * Usage: node scripts/capture-chatpane-plan-dispatch.mjs [outDir] [prefix]
 *   prefix 'before' (run against a pre-fix build) inverts the assertions:
 *   the click must append the label to the composer and fire NO plan POST.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, json } from './lib/stub-dashboard-api.mjs'
import { stubSplitPanes } from './lib/split-pane-fixture.mjs'

const OUT = process.argv[2] || '../temp-screenshots/chatpane-plan-dispatch'
const PREFIX = process.argv[3] || 'after'
const EXPECT_DISPATCH = PREFIX !== 'before'

mkdirSync(OUT, { recursive: true })

const LEFT = 'chat-orchestrator'
const RIGHT = 'chat-plain'
const APPROVE = 'Go'
const PLAN_OPTIONS = [APPROVE, 'Go All', 'Cancel']

const slots = [
  {
    key: LEFT, title: 'Release train — autopilot', messages: 4, running: false, mode: 'orchestrator',
    agent: 'kirocrew', created: '2026-08-20T09:00:00Z', last_ts: '2026-08-26T08:00:00Z', folder_id: '',
  },
  {
    key: RIGHT, title: 'Pipeline triage', messages: 2, running: false,
    agent: 'oncall', created: '2026-08-24T09:00:00Z', last_ts: '2026-08-26T07:30:00Z', folder_id: '',
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

/** A plan needs BOTH the header and a stage line for parseOptions to set isPlan,
 *  and the marker has to close its own line for OPTION_MARKER_RE to match. */
const leftMessages = [
  { role: 'user', content: 'Plan the 2.4 release rollout.', ts: '2026-08-26T07:58:00Z', meta: { mid: 'l-1' } },
  {
    role: 'assistant',
    content: `📋 Plan for: 2.4 release rollout\n\nStage 1: tag the release candidate and run the full gate suite\nStage 2: canary to 5% for 2 hours\nStage 3: full rollout + changelog\n\n[OPTION: ${PLAN_OPTIONS.join(' | ')}]`,
    ts: '2026-08-26T07:59:00Z', meta: { mid: 'l-2' },
  },
]

const rightMessages = [
  { role: 'user', content: 'Anything paging overnight?', ts: '2026-08-26T07:28:00Z', meta: { mid: 'r-1' } },
  { role: 'assistant', content: 'Nothing paged. One warning cleared itself at 03:10.', ts: '2026-08-26T07:29:00Z', meta: { mid: 'r-2' } },
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()

  /** Every plan-action POST the SPA fires, so the dispatch is asserted, not assumed. */
  const planPosts = []

  await stubSplitPanes(page, {
    slots,
    transcripts: { [LEFT]: leftMessages, [RIGHT]: rightMessages },
    layout: LAYOUT,
    extra: async (path, route) => {
      const m = path.match(/^\/api\/chat\/slots\/([^/]+)\/plan-action$/)
      if (m && route.request().method() === 'POST') {
        planPosts.push({ slot: decodeURIComponent(m[1]), body: route.request().postData() })
        await json(route, { ok: true })
        return true
      }
      return false
    },
  })
  logPageProblems(page)

  await page.goto(`${base}/chat/${LEFT}`, { waitUntil: 'domcontentloaded' })
  await page.getByText('Nothing paged.').first().waitFor({ timeout: 20_000 })

  // The evidence is only valid if SPLIT VIEW actually materialized: single-chat
  // fallback would photograph ChatPage's own (already working) plan dispatch.
  await page.locator('[data-chat-pane]').first().waitFor({ timeout: 10_000 })
  const paneCount = await page.locator('[data-chat-pane]').count()
  if (paneCount !== 2) {
    throw new Error(`expected exactly 2 [data-chat-pane] boundaries (split view), got ${paneCount} — the frame would show single chat, not ChatPane`)
  }
  const leftPane = page.locator('[data-chat-pane]').first()
  const approveChip = leftPane.getByRole('button', { name: APPROVE, exact: true })
  await approveChip.waitFor({ state: 'visible', timeout: 10_000 })
  const chipCount = await page.getByRole('button', { name: APPROVE, exact: true }).count()
  if (chipCount !== 1) throw new Error(`expected exactly 1 ${JSON.stringify(APPROVE)} chip, got ${chipCount}`)

  // The chips' staggered entrance is still translating them for ~750ms after mount.
  await page.waitForTimeout(1000)
  await page.screenshot({ path: `${OUT}/${PREFIX}-1-plan-in-pane.png` })

  // Single click; the chip debounces ~220ms before onSelect fires (double-click window).
  await approveChip.click()
  await page.waitForTimeout(700)

  const composerValue = await leftPane.getByRole('textbox').first().inputValue()
  if (EXPECT_DISPATCH) {
    if (planPosts.length !== 1) throw new Error(`expected exactly 1 plan-action POST, saw ${planPosts.length}`)
    if (planPosts[0].slot !== LEFT) throw new Error(`plan-action POST hit slot ${JSON.stringify(planPosts[0].slot)}, expected ${JSON.stringify(LEFT)} (the pane's own slot)`)
    if (!(planPosts[0].body || '').includes(APPROVE)) throw new Error(`plan-action POST body ${JSON.stringify(planPosts[0].body)} does not carry the action ${JSON.stringify(APPROVE)}`)
    if (composerValue !== '') throw new Error(`composer should stay EMPTY after a plan dispatch, but contains ${JSON.stringify(composerValue)}`)
  } else {
    if (planPosts.length !== 0) throw new Error(`pre-fix build should fire NO plan-action POST, saw ${planPosts.length}`)
    if (composerValue !== APPROVE) throw new Error(`pre-fix build should append the label to the composer, but it contains ${JSON.stringify(composerValue)}`)
  }

  await page.screenshot({ path: `${OUT}/${PREFIX}-2-after-click.png` })
  // Close-up: the left pane's composer band — empty after dispatch (fix) or
  // carrying the literal approval label (pre-fix).
  const composerBox = await leftPane.getByRole('textbox').first().boundingBox()
  const vp = page.viewportSize()
  if (composerBox) {
    await page.screenshot({
      path: `${OUT}/${PREFIX}-3-composer-closeup.png`,
      clip: { x: 0, y: Math.max(0, composerBox.y - 120), width: vp.width / 2, height: Math.min(vp.height - Math.max(0, composerBox.y - 120), 300) },
    })
  }

  console.log(`mode=${PREFIX} planPosts=${JSON.stringify(planPosts)} composer=${JSON.stringify(composerValue)}`)
  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}/${PREFIX}-{1-plan-in-pane,2-after-click,3-composer-closeup}.png`)
}

main().catch(err => { console.error(err); process.exit(1) })
