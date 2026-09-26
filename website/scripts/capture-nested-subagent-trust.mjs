/**
 * Screenshot harness + assertions for NESTED SUB-AGENT TRUST ROUTING (#13109).
 *
 * A subagent's own key is ``subagent:<id>``, which no dashboard tab shows. Before
 * the fix, a depth-two run's spawn-approval prompt and every lifecycle frame were
 * keyed on that literal parent, so the prompt surfaced nowhere the user looked
 * and the run's card never appeared in any tab. The fix routes both to the tab of
 * the chat at the ROOT of the spawn tree, and the composer chip's 30-second
 * reconcile matches `/api/spawn` entries on their `slot` rather than `parent`, so
 * the nested card is not evicted half a minute later.
 *
 * Seven scenes, each asserted (a PNG cannot fail) against the REAL built SPA
 * (website/dist) fed the same WS frames the gateway sends:
 *
 *   1. prompt   -- a depth-two spawn's approval, addressed to the root chat's tab,
 *                  renders there as a parked card ("1 awaiting" on the chip);
 *   2. lifecycle -- after approval the run's spawn/tool frames (root-slotted)
 *                  advance that card, it SURVIVES a reconcile tick whose
 *                  `/api/spawn` entry has `parent: subagent:<child>` but
 *                  `slot: <slot>` (its root tab), and its done frame finishes it;
 *   3. contested -- a spawn prompt for a CONTESTED conversation carries no slot
 *                  and lands on the global feed titled and explained.
 *
 * Usage: node scripts/capture-nested-subagent-trust.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/nested-subagent-trust'
const SLOT = 'chat-coordinator'
const PROJECT = '/home/user/workspace/uploader'

/** Hex-shaped ids: the launch-card parser only recognises the real id pattern. */
const CHILD_ID = 'c0ffee01'
const GRAND_ID = 'deadbe02'
const CONTESTED_ID = 'ba5eba03'
const GRAND_TASK = 'run the migration dry-run against the staging schema'
// The pump's two-line description, split the way the gateway's spawn approver
// hands it over: first line the title, the rest the purpose.
const CONTESTED_STATE = 'This run has no single owning chat'
const CONTESTED_TITLE = 'spawn_run(grandchild task)'
const CONTESTED_PURPOSE = `${CONTESTED_STATE}. Start this task again from a single chat, or approve this request. Its task was continued from a chat that did not start it.`
const FILE_NAME = 'migration-dry-run.log'
const TOOL_CMD = 'psql -h staging-db -f migrations/0042_pin_schema.sql --dry-run'

const slots = [{
  key: SLOT,
  title: 'Coordinate the uploader migration',
  running: true,
  last_message: 'Spawned 1 subagent(s).',
  messages: 3,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: true,
  has_more: false,
  total: 3,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 600, content: 'Coordinate the uploader migration end to end.' },
    { role: 'assistant', ts: Date.now() / 1000 - 60, content: 'Delegating to a coordinator sub-agent, which will fan out further.' },
    {
      role: 'tool',
      ts: Date.now() / 1000 - 55,
      content: '\u{1F527} spawn_run',
      cls: '',
      meta: {
        tool_call_id: 'tc_spawn_13109',
        input: '{}',
        output: `Spawned 1 subagent(s). Results will arrive as completion events:\n  ${CHILD_ID} (kirocrew): coordinate the migration`,
      },
    },
  ],
}

/** What `/api/spawn` reports once the grandchild runs: its literal parent is the
 *  depth-one run's ``subagent:`` key, and its ``slot`` is the root chat's tab. A reconcile
 *  keyed on ``parent`` would evict the card; keyed on ``slot`` it keeps it. */
let grandDone = false
const spawnList = () => ({
  agents: [
    { id: CHILD_ID, done: false, parent: `dashboard:${SLOT}`, slot: SLOT },
    { id: GRAND_ID, done: grandDone, parent: `subagent:${CHILD_ID}`, slot: SLOT },
  ],
})

async function main() {
  mkdirSync(OUT, { recursive: true })
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)

  let spawnListHits = 0
  const extra = async (path, route) => {
    if (path === '/api/spawn') { spawnListHits += 1; await json(route, spawnList()); return true }
    if (path === '/api/tips/status') { await json(route, { enabled: false }); return true }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }
  await stubDashboardApi(page, { slots, extra })
  let wsServer = null
  await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })
  await page.addInitScript(slot => localStorage.setItem('mc-active-slot', slot), SLOT)
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  if (!wsServer) throw new Error('websocket route never bound')

  const send = async (type, data, settle = 350) => {
    wsServer.send(JSON.stringify({ type, data }))
    await page.waitForTimeout(settle)
  }
  const text = async (testId) => {
    const el = page.getByTestId(testId).first()
    return (await el.count()) ? (await el.textContent() || '').trim() : null
  }
  const chip = () => page.getByTestId('subagent-histogram').locator('xpath=ancestor::div[contains(@class,"animate-slide-up")][1]')
  const shotChip = async (name) => { if (await chip().count()) await chip().first().screenshot({ path: `${OUT}/${name}` }) }

  // The depth-one run is live, as the gateway's frame for it says.
  await send('subagent_spawn', { slot: SLOT, id: CHILD_ID, task: 'coordinate the migration', agent: 'kirocrew' })
  await send('subagent_tool', { slot: SLOT, id: CHILD_ID, tool: 'spawn_run', tool_count: 1 })

  // ── Scene 1: the grandchild's spawn prompt, addressed to the ROOT tab ──────
  // Verbatim the frame `_interactive_approval` broadcasts once
  // `_spawn_approval_slot` resolved the run's stamped root to this tab.
  await send('approval', {
    id: `spawn:${GRAND_ID}`, slot: SLOT, tool: `spawn_run(${GRAND_TASK})`, source: 'subagent', ts: Date.now() / 1000,
  }, 900)
  const scene1 = {
    running: await text('subagent-running-count'),
    awaiting: await text('subagent-awaiting-count'),
    parkedRow: await page.getByText('Waiting for your approval to start').first().count() > 0,
  }
  await page.screenshot({ path: `${OUT}/1-nested-prompt-in-root-tab.png` })
  await shotChip('1-chip-awaiting.png')

  // ── Scene 2: approved; lifecycle frames advance the card; reconcile keeps it ─
  await send('approval_resolved', { id: `spawn:${GRAND_ID}`, slot: SLOT, approved: true })
  await send('subagent_spawn', { slot: SLOT, id: GRAND_ID, task: GRAND_TASK, agent: 'kirocrew' })
  await send('subagent_tool', { slot: SLOT, id: GRAND_ID, tool: 'Reading: migrations/0042_pin_schema.sql', tool_count: 3 })
  const rowsBefore = await page.getByTestId('subagent-row').count()
  await shotChip('2a-chip-two-running.png')
  // The chip reconciles every 30s against /api/spawn; wait for at least one
  // tick to have run with the grandchild reported under its ``subagent:`` parent.
  const hitsBefore = spawnListHits
  await page.waitForFunction(() => true) // yield
  await page.waitForTimeout(31_500)
  const reconciled = spawnListHits > hitsBefore
  const rowsAfter = await page.getByTestId('subagent-row').count()
  const runningAfter = await text('subagent-running-count')
  await page.screenshot({ path: `${OUT}/2b-nested-card-survives-reconcile.png` })

  // ── Scene 2d: the nested run sends a file; its card lands in the ROOT tab ──
  // Verbatim the `chat_message` frame `append_and_surface` broadcasts once
  // `_subagent_parent_session_key` resolved the depth-two run's stamped root to
  // this slot (before the fix the run's literal parent, `subagent:c0ffee01`,
  // named no tab and the card was suppressed).
  await send('chat_message', {
    slot: SLOT,
    role: 'file',
    ts: Date.now() / 1000,
    content: JSON.stringify({
      filename: FILE_NAME,
      path: `/home/user/.kiro/crew/outbox/${FILE_NAME}`,
      description: 'Dry-run output against the staging schema',
      size: 20480,
      content_type: 'text/plain',
    }),
  }, 900)
  const fileCard = page.getByText(FILE_NAME, { exact: true }).first()
  const fileCardShown = await fileCard.count() > 0
  await page.screenshot({ path: `${OUT}/2d-nested-file-card-in-root-tab.png` })
  if (fileCardShown) {
    await fileCard.locator('xpath=ancestor::a[1]').screenshot({ path: `${OUT}/2d-nested-file-card.png` })
  }

  // ── Scene 2e: the nested run's TOOL prompt lands in the ROOT tab ──────────
  // Verbatim the `approval` frame `request_approval` broadcasts for a mid-run
  // tool call once the run loop handed the run's stamped root key (this tab)
  // to `_on_tool_approval`. The root chat is untrusted here, so the prompt is
  // interactive: an inline permission card in the tab that can answer it.
  await send('approval', {
    id: `tool:${GRAND_ID}:7`, slot: SLOT, tool: `shell(${TOOL_CMD})`, tool_input: TOOL_CMD,
    tool_purpose: 'Dry-run the pinned schema migration against staging before applying it',
    source: 'subagent', ts: Date.now() / 1000,
  }, 900)
  const toolPrompt = page.getByText(TOOL_CMD, { exact: false }).first()
  const toolPromptShown = await toolPrompt.count() > 0
  await page.screenshot({ path: `${OUT}/2e-nested-tool-prompt-in-root-tab.png` })
  await send('approval_resolved', { id: `tool:${GRAND_ID}:7`, slot: SLOT, approved: true }, 600)
  grandDone = true
  await send('subagent_done', { slot: SLOT, id: GRAND_ID, elapsed: 42, outcome: 'completed' }, 900)
  const runningDone = await text('subagent-running-count')
  await page.screenshot({ path: `${OUT}/2c-nested-card-done.png` })
  await shotChip('2c-chip-after-done.png')

  // ── Scene 3: a contested conversation's prompt lands on the global feed ────
  // `slot: ""` -- the marker names no tab -- and the pump's label on the entry.
  await send('approval', {
    id: `spawn:${CONTESTED_ID}`, slot: '', tool: CONTESTED_TITLE, tool_purpose: CONTESTED_PURPOSE,
    source: 'subagent', ts: Date.now() / 1000,
  }, 1200)
  const banner = page.getByTestId('notification-banner-card').first()
  const bannerShown = await banner.count() > 0
  const bannerText = bannerShown ? (await banner.textContent() || '') : ''
  await page.screenshot({ path: `${OUT}/3-contested-prompt-global-feed.png` })
  if (bannerShown) await banner.screenshot({ path: `${OUT}/3-contested-feed-entry.png` })

  // ── Scene 3b: Review opens the entry where the user decides; the copy is whole ─
  let reviewShown = false
  let reviewText = ''
  if (bannerShown) {
    await banner.getByRole('button', { name: 'Review' }).click()
    await page.waitForTimeout(900)
    const panelTitle = page.getByText(`Tool approval: ${CONTESTED_TITLE}`, { exact: true }).last()
    reviewShown = await panelTitle.count() > 0
    if (reviewShown) {
      const panel = panelTitle.locator('xpath=ancestor::div[contains(@class,"h-full")][1]')
      reviewText = (await panel.textContent()) || ''
      await panel.screenshot({ path: `${OUT}/3b-contested-review-panel.png` })
    }
    await page.screenshot({ path: `${OUT}/3b-contested-review-open.png` })
  }

  // ── Scene 3c: the contested run's mid-run TOOL prompt on the feed ─────────
  // Verbatim the `approval` frame for a tool call once `_label_contested_prompt`
  // put the state sentence ahead of the tool's own purpose: the title stays the
  // tool, the body says why the prompt has no chat and what the tool is for.
  // Escape once leaves the detail panel, once more closes the popover.
  await page.keyboard.press('Escape')
  await page.waitForTimeout(400)
  await page.keyboard.press('Escape')
  await page.waitForTimeout(600)
  await send('approval', {
    id: `tool:${CONTESTED_ID}:3`, slot: '', tool: `shell(${TOOL_CMD})`, tool_input: TOOL_CMD,
    tool_purpose: `${CONTESTED_STATE}.\n\n> Dry-run the pinned schema migration against staging before applying it.\n\nIts task was continued from a chat that did not start it. Start this task again from a single chat, or approve this request.`,
    source: 'subagent', ts: Date.now() / 1000,
  }, 1200)
  const toolBanner = page.getByTestId('notification-banner-card').first()
  const toolBannerShown = await toolBanner.count() > 0
  const toolBannerText = toolBannerShown ? (await toolBanner.textContent() || '') : ''
  if (toolBannerShown) await toolBanner.screenshot({ path: `${OUT}/3c-contested-tool-prompt-feed-entry.png` })
  let toolReviewShown = false
  let toolReviewText = ''
  let toolReviewQuotes = 0
  if (toolBannerShown) {
    await toolBanner.getByRole('button', { name: 'Review' }).click()
    await page.waitForTimeout(900)
    const panelTitle = page.getByText(`Tool approval: shell(${TOOL_CMD})`, { exact: true }).last()
    toolReviewShown = await panelTitle.count() > 0
    if (toolReviewShown) {
      const panel = panelTitle.locator('xpath=ancestor::div[contains(@class,"h-full")][1]')
      toolReviewText = (await panel.textContent()) || ''
      toolReviewQuotes = await panel.locator('blockquote').count()
      await panel.screenshot({ path: `${OUT}/3c-contested-tool-prompt-review-panel.png` })
    }
  }

  // ── Scene 3d: the contested entry in the LIGHT theme ───────────────────────
  // A fresh page served the light mode shows the same entry and Review body
  // on the other palette, so the copy is checked against both.
  await page.keyboard.press('Escape')
  await page.waitForTimeout(400)
  await page.keyboard.press('Escape')
  await page.waitForTimeout(400)
  const lightContext = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2, colorScheme: 'light' })
  const light = await lightContext.newPage()
  // The shared stub bakes the mode into `/api/theme/boot` (the server is the
  // source of truth; localStorage is only a render cache).
  await stubDashboardApi(light, { slots, extra, theme: 'light' })
  let lightWs = null
  await light.routeWebSocket(/\/api\/ws/, ws => { lightWs = ws })
  await light.addInitScript(slot => localStorage.setItem('mc-active-slot', slot), SLOT)
  await light.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await light.waitForTimeout(2500)
  let lightShown = false
  if (lightWs) {
    lightWs.send(JSON.stringify({ type: 'approval', data: {
      id: `spawn:${CONTESTED_ID}`, slot: '', tool: CONTESTED_TITLE, tool_purpose: CONTESTED_PURPOSE,
      source: 'subagent', ts: Date.now() / 1000,
    } }))
    await light.waitForTimeout(1200)
    const lightBanner = light.getByTestId('notification-banner-card').first()
    lightShown = await lightBanner.count() > 0
    if (lightShown) {
      await lightBanner.screenshot({ path: `${OUT}/3d-contested-feed-entry-light.png` })
      await lightBanner.getByRole('button', { name: 'Review' }).click()
      await light.waitForTimeout(900)
      const panelTitle = light.getByText(`Tool approval: ${CONTESTED_TITLE}`, { exact: true }).last()
      if (await panelTitle.count()) {
        await panelTitle.locator('xpath=ancestor::div[contains(@class,"h-full")][1]').screenshot({ path: `${OUT}/3d-contested-review-panel-light.png` })
      }
    }
  }

  await browser.close()
  srv.close()

  const observed = {
    scene1,
    scene2: { rowsBefore, reconciled, rowsAfter, runningAfter, fileCardShown, toolPromptShown, runningDone },
    scene3: {
      bannerShown,
      titled: bannerText.includes('spawn_run(grandchild task)'),
      explained: bannerText.includes(CONTESTED_STATE),
      reviewShown,
      reviewWhole: reviewText.includes(CONTESTED_PURPOSE) && reviewText.includes('spawn_run(grandchild task)'),
      lightShown,
      toolBannerShown,
      toolLabeled: toolBannerText.includes('shell(psql'),
      toolCardSaysWhy: toolBannerText.includes(CONTESTED_STATE),
      spawnCardReachesTheRemedy: bannerText.includes('Start this task again'),
      toolCardShowsPurpose: toolBannerText.includes('Dry-run the pinned schema'),
      toolReviewCarriesRemedy: toolReviewText.includes('Start this task again from a single chat, or approve this request'),
      toolReviewAttributesTheRun: toolReviewQuotes > 0,

      toolReviewShown,
    },
  }
  console.log(JSON.stringify(observed, null, 2))
  console.log(`screenshots -> ${OUT}`)

  const ok = scene1.running === '1' && scene1.awaiting === '1' && scene1.parkedRow
    && rowsBefore === 2 && reconciled && rowsAfter === 2 && runningAfter === '2'
    && fileCardShown && toolPromptShown && runningDone === '1'
    && bannerShown && observed.scene3.titled && observed.scene3.explained
    && reviewShown && observed.scene3.reviewWhole && observed.scene3.spawnCardReachesTheRemedy
    && toolBannerShown && observed.scene3.toolLabeled && observed.scene3.toolCardSaysWhy && observed.scene3.toolCardShowsPurpose && toolReviewShown && observed.scene3.toolReviewCarriesRemedy && observed.scene3.toolReviewAttributesTheRun && lightShown
  if (!ok) { console.error('the nested-trust scenes do not show the fix'); process.exit(1) }
  console.log('AFTER: nested spawn and tool prompts in root tab, nested card survives reconcile and finishes, nested file card in root tab, contested entry titled and explained on the feed and whole in Review')
}

main().catch(e => { console.error(e); process.exit(1) })
