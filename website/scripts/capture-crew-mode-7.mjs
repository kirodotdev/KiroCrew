/**
 * Screenshot harness for Crew Mode part 7 (the composer chip, the welcome, the
 * conductor's approval card and the crew member view).
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception -- gateway-free, no kiro-cli, no dashboard auth. The slot rows
 * carry the stamps the gateway writes (`crew_mode_root`, `crew_root`,
 * `pending_approval_info`), so every surface below renders from the same data
 * it would read live.
 *
 * Frames (dark, 1280x800, 2x):
 *   01-approval-card.png        the conductor chat with one crew member waiting
 *                               on a tool approval: the card above the composer
 *   02-approval-card-crop.png   the card alone (Approve + Reject in one row,
 *                               Trust this crew member on its own row with its
 *                               scope line beside it)
 *   03-crew-member-waiting.png  that crew member opened in the main pane: the
 *                               breadcrumb, "Waiting for approval in <conductor>"
 *                               with the Open link, no approval band of its own
 *   04-chip-busy.png            a new chat while its reply runs: the chip is
 *                               disabled with "Available after the reply."
 *   05-steer-too-long.png       a steer past the limit: the draft stays, the
 *                               notice says the limit
 *   06-chip-to-mark.webm        pressing the chip: both chips leave, the mark
 *                               appears in the footer as a pressed toggle, then
 *                               pressing the mark brings the chips back
 *
 * Usage (in website/): node scripts/capture-crew-mode-7.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readdirSync, renameSync, rmSync } from 'node:fs'
import { resolve } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/crew-mode-7')
mkdirSync(OUT, { recursive: true })

const ROOT = 'chat-root'
const WORKER = 'chat-w1'
const BUSY = 'chat-busy'
const DONE = 'chat-w2'
const PLAIN = 'chat-plain'
const T0 = '2026-10-28T08:00:00Z'

const APPROVAL = {
  origin: 'native', tool: 'fs_write', tool_kind: 'write', request_id: 'req-rain-1', request_mid: 'mid-rain-1',
  tool_purpose: 'Save the haiku', trust_grantable: '1',
  tool_input: JSON.stringify({ path: '/tmp/cm-haiku-rain.txt', text: 'Soft rain on the roof / each drop taps a quiet song / the gray sky exhales' }, null, 2),
}

const slots = [
  { key: ROOT, title: 'Crew Haiku Sea Trees Rain', messages: 2, running: false, agent: 'kirocrew-conductor', agent_kind: 'template', crew_mode_root: true, mode: '', origin: 'user', memory_mode: 'persistent', created: T0, last_ts: '2026-10-28T08:12:00Z', folder_id: '', workspace: 'default' },
  { key: WORKER, title: 'Rain haiku', messages: 2, running: false, agent: 'kirocrew-worker', agent_kind: 'template', crew_root: ROOT, created_by: ROOT, pending_approval: true, pending_approval_info: APPROVAL, mode: '', origin: 'user', memory_mode: 'persistent', created: T0, last_ts: '2026-10-28T08:11:00Z', folder_id: '', workspace: 'default' },
  { key: DONE, title: 'Tree haiku', messages: 2, running: false, agent: 'kirocrew-worker', agent_kind: 'template', crew_root: ROOT, created_by: ROOT, mode: '', origin: 'user', memory_mode: 'persistent', created: T0, last_ts: '2026-10-28T08:09:00Z', folder_id: '', workspace: 'default' },
  { key: BUSY, title: 'New Session…', messages: 0, running: true, agent: 'kirocrew', agent_kind: 'template', mode: '', origin: 'user', memory_mode: 'persistent', created: T0, last_ts: T0, folder_id: '', workspace: 'default' },
  { key: PLAIN, title: 'New Session…', messages: 0, running: false, agent: 'kirocrew', agent_kind: 'template', mode: '', origin: 'user', memory_mode: 'persistent', created: T0, last_ts: T0, folder_id: '', workspace: 'default' },
]

const CATALOG = {
  agents: [
    { name: 'kirocrew', selection_kind: 'template', kiro_agent: '', workspace: 'default', memory_store: 'default', source: 'builtin' },
    { name: 'kirocrew-conductor', selection_kind: 'template', kiro_agent: '', workspace: 'default', memory_store: 'default', source: 'builtin' },
    { name: 'kirocrew-worker', selection_kind: 'template', kiro_agent: '', workspace: 'default', memory_store: 'default', source: 'builtin' },
  ],
  default_agent: 'kirocrew',
  crew_mode_agent: 'kirocrew-conductor',
  conductor_agents: ['kirocrew-conductor'],
}

const WORKERS = {
  root: ROOT,
  workers: [{
    key: WORKER, title: 'Rain haiku', agent: 'kirocrew-worker', status: 'needs_approval', group: 'approval',
    last_activity: '2026-10-28T08:11:00Z', ledger_item: 'it_rain0001',
    summary: 'Write one short haiku (5-7-5) about rain and save it to a file.',
    created_by: ROOT, dispatched_by: ROOT, approval: APPROVAL,
  }, {
    key: DONE, title: 'Tree haiku', agent: 'kirocrew-worker', status: 'idle', group: 'done',
    last_activity: '2026-10-28T08:09:00Z', ledger_item: 'it_tree0001',
    summary: 'Write one short haiku (5-7-5) about trees and save it to a file.',
    created_by: ROOT, dispatched_by: ROOT,
  }],
}

/** The conductor's tab closed: its list is read from saved data under its saved title. */
const WORKERS_ROOT_CLOSED = { ...WORKERS, root_closed: true, root_title: 'Crew Haiku Sea Trees Rain' }
// Which answer the crew-workers route gives, and which slot rows the list shows.
let workersAnswer = WORKERS
let slotsNow = null

const transcripts = {
  [ROOT]: [
    { role: 'user', content: 'Write three haiku: one about the sea, one about trees, one about rain. One crew member each; each saves its haiku to a file.', ts: '2026-10-28T08:01:00Z', meta: { mid: 'r-1' } },
    { role: 'assistant', content: 'Three crew members are on it: Sea haiku, Tree haiku and Rain haiku. I will report back here when each one is done.', ts: '2026-10-28T08:02:00Z', meta: { mid: 'r-2' } },
  ],
  [WORKER]: [
    { role: 'user', content: `[sent by session ${ROOT} via session_send]\n\nWrite one short haiku (5-7-5) about rain. Save it to /tmp/cm-haiku-rain.txt with your file-write tool, then call work_report with status "done".`, ts: '2026-10-28T08:03:00Z', meta: { mid: 'w-1' } },
    { role: 'assistant', content: 'Soft rain on the roof / each drop taps a quiet song / the gray sky exhales\n\nSaving it now.', ts: '2026-10-28T08:11:00Z', meta: { mid: 'w-2' } },
  ],
  [DONE]: [
    { role: 'user', content: `[sent by session ${ROOT} via session_send]\n\nWrite one short haiku (5-7-5) about trees. Save it to /tmp/cm-haiku-trees.txt, then call work_report with status "done".`, ts: '2026-10-28T08:03:00Z', meta: { mid: 'd-1' } },
    { role: 'assistant', content: 'Old oaks hold the hill / roots remember every rain / leaves let the wind through\n\nSaved to /tmp/cm-haiku-trees.txt and reported done.', ts: '2026-10-28T08:09:00Z', meta: { mid: 'd-2' } },
  ],
  [BUSY]: [],
  [PLAIN]: [],
}

let agentNow = 'kirocrew'

/** Route /api/** -- MUST return truthy once fulfilled (see stubDashboardApi). */
const extra = async (path, route) => {
  const method = route.request().method()
  if (path === '/api/agents/catalog') { await json(route, CATALOG); return true }
  // The slot list follows the switch, as the gateway's frame would: on the
  // conductor the row carries the Crew Mode stamp, so the list row, the side
  // panel and the footer mark all agree with what the press just did.
  if (path === '/api/chat/slots' && method === 'GET') {
    const onConductor = agentNow === 'kirocrew-conductor'
    await json(route, (slotsNow ?? slots).map(s => s.key === PLAIN ? { ...s, agent: agentNow, ...(onConductor ? { crew_mode_root: true } : {}) } : s))
    return true
  }
  if (path === `/api/chat/slots/${ROOT}/crew-workers`) { await json(route, workersAnswer); return true }
  if (path === `/api/chat/slots/${PLAIN}/crew-workers`) { await json(route, { root: PLAIN, workers: [] }); return true }
  if (path === `/api/chat/slots/${PLAIN}/crew-timeline`) { await json(route, { root: PLAIN, lanes: [], gaps: [] }); return true }
  if (path === `/api/chat/slots/${ROOT}/crew-timeline`) { await json(route, { root: ROOT, lanes: [], gaps: [] }); return true }
  if (path === `/api/chat/slots/${PLAIN}/agent` && method === 'POST') {
    const body = route.request().postDataJSON?.() ?? {}
    agentNow = body.agent || 'kirocrew'
    await json(route, { agent: agentNow, agent_kind: 'template', workspace: 'default' })
    return true
  }
  for (const [slot, messages] of Object.entries(transcripts)) {
    if (path === `/api/chat/slots/${slot}`) { await json(route, { messages, has_more: false, total: messages.length }); return true }
  }
  if (path === '/api/sessions') { await json(route, { sessions: [], has_more: false }); return true }
  if (path === '/api/chat/pins') { await json(route, { pins: [] }); return true }
  return false
}

let failures = 0
const assert = (ok, label) => { console.log(`ASSERT ${ok ? 'ok  ' : 'FAIL'}: ${label}`); if (!ok) failures++ }

async function newPage(browser, base, active, record = false) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    deviceScaleFactor: record ? 1 : 2,
    ...(record ? { recordVideo: { dir: resolve(OUT, 'video-raw'), size: { width: 1280, height: 800 } } } : {}),
  })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots: slotsNow ?? slots, theme: 'dark', localStorageEntries: { 'mc-active-slot-chat': active, 'mc-lang': 'en' }, extra })
  await page.goto(`${base}/chat?sid=${active}`, { waitUntil: 'domcontentloaded' })
  return { context, page }
}

async function main() {
  const { srv, base } = await serveDist()
  const { LD_LIBRARY_PATH: _mise, ...env } = process.env
  const browser = await chromium.launch({ env })

  // 01/02: the conductor chat with the approval card.
  {
    const { context, page } = await newPage(browser, base, ROOT)
    const card = page.getByTestId('crew-approval-card')
    await card.waitFor({ timeout: 20_000 })
    await page.waitForTimeout(800)
    assert(await page.getByRole('button', { name: 'Approve' }).isVisible(), 'Approve on the card')
    assert(await page.getByRole('button', { name: 'Reject' }).isVisible(), 'Reject on the card')
    const trust = page.getByRole('button', { name: 'Trust this crew member' })
    assert(await trust.isVisible(), 'Trust on the card')
    const scope = page.getByText('Approves this and lets this crew member run tools without asking until Kiro Crew restarts.')
    assert(await scope.isVisible(), 'Trust scope line visible beside Trust')
    assert((await page.getByTestId('crew-approval-trust-row').getByRole('button').count()) === 1, 'Trust row holds one button')
    assert((await page.getByRole('button', { name: /^Agent: / }).count()) === 0, 'no agent picker on the conductor composer')
    await page.screenshot({ path: `${OUT}/01-approval-card.png` })
    await card.screenshot({ path: `${OUT}/02-approval-card-crop.png` })
    await context.close()
  }

  // 07: the conductor's side panel, Dashboard tab: the crew member's approval is
  // information with a way to the card, no Approve or Reject of its own.
  {
    const { context, page } = await newPage(browser, base, ROOT)
    await page.getByTestId('crew-approval-card').waitFor({ timeout: 20_000 })
    const open = page.getByRole('button', { name: 'Open activity panel' })
    if (await open.count()) await open.first().click()
    await page.waitForTimeout(1000)
    // The panel opens on its tab strip; the Dashboard view is added from the + menu.
    const tab = page.getByRole('tab', { name: 'Dashboard' })
    if ((await tab.count()) === 0) {
      await page.getByRole('button', { name: 'Open side panel tab' }).first().click()
      const item = page.getByRole('menuitem', { name: /^Dashboard\b/ })
      await item.first().waitFor({ timeout: 10_000 })
      await item.first().click()
    }
    await tab.first().waitFor({ timeout: 20_000 })
    await tab.first().click()
    // The Dashboard's Approvals section lists what waits on the person.
    const approvals = page.getByRole('radio', { name: /^Approvals/ })
    await approvals.first().waitFor({ timeout: 20_000 })
    await approvals.first().click()
    const notice = page.getByTestId('crew-approval-notice')
    if (process.env.CM7_DEBUG) { await page.waitForTimeout(3000); await page.screenshot({ path: `${OUT}/debug-07.png` }) }
    await notice.waitFor({ timeout: 20_000 })
    await page.waitForTimeout(800)
    assert(await notice.getByText('Open the approval card').isVisible(), 'Open the approval card link on the notice')
    assert(await notice.getByText('Decide it on the approval card in Crew Haiku Sea Trees Rain.').isVisible(), 'the notice names the conductor')
    assert((await notice.getByRole('button', { name: /^(Approve|Reject)$/ }).count()) === 0, 'no Approve or Reject on the notice')
    await page.screenshot({ path: `${OUT}/07-crew-approval-notice.png` })
    await notice.screenshot({ path: `${OUT}/08-crew-approval-notice-crop.png` })
    await context.close()
  }

  // 03: the crew member in the main pane while its approval waits.
  {
    const { context, page } = await newPage(browser, base, WORKER)
    await page.getByTestId('crew-worker-breadcrumb').waitFor({ timeout: 20_000 })
    await page.getByTestId('crew-worker-approval-waiting').waitFor({ timeout: 20_000 })
    await page.waitForTimeout(800)
    assert(await page.getByText('Waiting for approval in Crew Haiku Sea Trees Rain').isVisible(), 'waiting line names the conductor')
    assert(await page.getByRole('button', { name: 'Open Crew Haiku Sea Trees Rain' }).isVisible(), 'Open <conductor> link')
    assert(await page.getByRole('button', { name: 'Back to main chat' }).isVisible(), 'Back to main chat')
    assert((await page.getByRole('button', { name: /^Approve$/ }).count()) === 0, 'no Approve on the crew member view')
    assert(await page.getByText('From conductor').first().isVisible(), 'From conductor label on the delivered message')
    await page.screenshot({ path: `${OUT}/03-crew-member-waiting.png` })

    // 05: a steer past the limit keeps the draft and says the limit.
    const box = page.getByTestId('crew-steer-composer').getByRole('textbox').first()
    await box.waitFor({ timeout: 10_000 })
    await box.fill('x'.repeat(50_001))
    await box.press('Enter')
    await page.getByTestId('crew-steer-too-long').waitFor({ timeout: 10_000 })
    assert(await page.getByText('A message to Rain haiku can be at most 50,000 characters. Shorten it to send.').isVisible(), 'too-long notice')
    assert((await box.inputValue()).length === 50_001, 'draft kept')
    await page.screenshot({ path: `${OUT}/05-steer-too-long.png` })
    await context.close()
  }

  // 09: a finished crew member: its box still names it and says what a send does.
  {
    const { context, page } = await newPage(browser, base, DONE)
    await page.getByTestId('crew-worker-breadcrumb').waitFor({ timeout: 20_000 })
    const box = page.getByTestId('crew-steer-composer').getByRole('textbox').first()
    await box.waitFor({ timeout: 10_000 })
    await page.waitForTimeout(800)
    assert(await page.getByTestId('crew-worker-breadcrumb').textContent().then(t => t.includes('Done')), 'breadcrumb says Done')
    assert((await box.getAttribute('placeholder')) === 'Message Tree haiku: it reads this before its next step', 'placeholder names the finished crew member')
    assert((await page.getByTestId('crew-worker-root-closed').count()) === 0, 'no closed-conductor line while the conductor is open')
    await page.screenshot({ path: `${OUT}/09-crew-member-done.png` })
    await context.close()
  }

  // 10: a crew member whose conductor's tab is closed: the box is off, the line
  // says what is closed and what opening it again lets the person do.
  {
    workersAnswer = WORKERS_ROOT_CLOSED
    slotsNow = slots.filter(s => s.key !== ROOT)
    const { context, page } = await newPage(browser, base, DONE)
    const gate = page.getByTestId('crew-worker-root-closed')
    await gate.waitFor({ timeout: 20_000 })
    await page.waitForTimeout(800)
    assert(await page.getByText('Main chat is closed. Open it again to message Tree haiku.').isVisible(), 'closed line names the crew member')
    assert((await page.getByTestId('crew-worker-reopen-root').textContent()).trim() === 'Open main chat', 'gate button: Open main chat')
    assert((await page.getByTestId('crew-back-to-conductor').count()) === 0, 'breadcrumb has no button: the Crew tab bar is the way back')
    assert(await page.getByTestId('crew-breadcrumb-root').textContent().then(t => t.includes('Crew Haiku Sea Trees Rain')), 'breadcrumb keeps the saved conductor title')
    const box = page.getByTestId('crew-steer-composer').getByRole('textbox').first()
    assert((await box.getAttribute('aria-disabled')) === 'true', 'box is off under a closed conductor')
    await page.screenshot({ path: `${OUT}/10-conductor-closed.png` })
    await context.close()
    workersAnswer = WORKERS
    slotsNow = null
  }

  // 04: the chip while a reply runs.
  {
    const { context, page } = await newPage(browser, base, BUSY)
    const chip = page.getByTestId('crew-mode-chip')
    await chip.waitFor({ timeout: 20_000 })
    await page.waitForTimeout(800)
    assert((await chip.getAttribute('aria-disabled')) === 'true', 'chip aria-disabled while the reply runs')
    assert(await page.getByText('Available after the reply.').isVisible(), 'busy note visible')
    await page.screenshot({ path: `${OUT}/04-chip-busy.png` })
    await context.close()
  }

  // 06: the chip pressed, the mark in the footer, the mark pressed back (video).
  {
    const { context, page } = await newPage(browser, base, PLAIN, true)
    const chip = page.getByTestId('crew-mode-chip')
    await chip.waitFor({ timeout: 20_000 })
    await page.waitForTimeout(1200)
    await chip.hover()
    await page.waitForTimeout(600)
    await chip.click()
    const mark = page.getByTestId('crew-mode-composer-chip')
    await mark.waitFor({ timeout: 10_000 })
    await page.waitForTimeout(1500)
    assert((await mark.getAttribute('aria-pressed')) === 'true', 'the mark is a pressed toggle before the first message')
    assert((await page.getByTestId('crew-mode-chip').count()) === 0, 'the chip left the row')
    await page.screenshot({ path: `${OUT}/06-mark-pressed.png` })
    await mark.hover()
    await page.waitForTimeout(900)
    await mark.click()
    await page.getByTestId('crew-mode-chip').waitFor({ timeout: 10_000 })
    await page.waitForTimeout(1200)
    assert(await page.getByTestId('crew-mode-chip').isVisible(), 'the chip is back after turning off')
    const video = page.video()
    await context.close()
    const raw = await video.path()
    renameSync(raw, `${OUT}/06-chip-to-mark.webm`)
    for (const f of readdirSync(resolve(OUT, 'video-raw'))) rmSync(resolve(OUT, 'video-raw', f))
    rmSync(resolve(OUT, 'video-raw'), { recursive: true, force: true })
  }

  await browser.close()
  srv.close()
  console.log(failures ? `FAILED: ${failures} assertion(s)` : 'all assertions passed')
  process.exit(failures ? 1 : 0)
}

main().catch(e => { console.error(e); process.exit(2) })
