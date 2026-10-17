/**
 * Screenshot harness + assertions for the sub-agent elapsed figure past an hour
 * (#19930).
 *
 * The wave chip above the composer printed raw seconds (`4031s`) and the
 * Subagents sidebar card had no hours band (`67m 11s`). Both now read
 * `1h 7m 11s`. This drives the REAL built SPA (website/dist), pushes the same WS
 * frames the gateway sends, jumps the page clock 4031s past the spawn, and exits
 * non-zero unless the chip row and the sidebar card both read `1h 7m 11s`.
 *
 * BEFORE frame: check the two components and format.ts out at the base commit,
 * rebuild, and run with --expect-before (the old strings are then required).
 *
 * Usage: node scripts/capture-subagent-elapsed-hours.mjs [outDir] [--expect-before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const args = process.argv.slice(2).filter(a => a !== '--expect-before')
const EXPECT_BEFORE = process.argv.includes('--expect-before')
const OUT = args[0] || '../temp-screenshots/subagent-elapsed-hours'
const SLOT = 'chat-elapsed'
const PROJECT = '/home/user/workspace/uploader'
const AGENT_ID = 'a1b2c3d4'
const T0 = Date.now()
const RUN_MS = 4_031_000

const slots = [{
  key: SLOT,
  title: 'Audit the uploader retry ladder',
  running: true,
  last_message: 'Spawned 1 subagent(s).',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: Math.floor(T0 / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: true,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: T0 / 1000 - 60, content: 'Audit the uploader retry ladder.' },
    { role: 'assistant', ts: T0 / 1000 - 30, content: 'Handing this to a sub-agent.' },
  ],
}

const spawnList = { agents: [{ id: AGENT_ID, done: false, parent: `dashboard:${SLOT}` }] }

async function main() {
  mkdirSync(OUT, { recursive: true })
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)

  const extra = async (path, route) => {
    if (path === '/api/spawn') { await json(route, spawnList); return true }
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

  // A snapshot frame carries the real start time, as on a reconnect mid-run.
  wsServer.send(JSON.stringify({ type: 'subagent_snapshot', data: {
    slot: SLOT, id: AGENT_ID, agent: 'kirocrew', task: 'audit the retry ladder in the uploader',
    last_tool: 'Reading: src/uploader/retry.py', started: Date.now() / 1000 - RUN_MS / 1000,
    streaming: 'Reading retry.py...\n', tool_count: 27,
  } }))
  await page.waitForTimeout(1500)

  const row = page.getByTestId('subagent-row').first()
  const rowText = (await row.count()) ? (await row.textContent() || '') : ''
  const chip = page.getByTestId('subagent-histogram').locator('xpath=ancestor::div[contains(@class,"animate-slide-up")][1]')
  if (await chip.count()) await chip.first().screenshot({ path: `${OUT}/wave-chip.png` })

  // Open the Subagents sidebar from the row, as a user would.
  await row.getByRole('button').first().click()
  await page.waitForTimeout(1500)
  const region = page.locator('[role="region"][aria-label="Activity"]').first()
  let cardText = ''
  if (await region.count()) {
    cardText = (await region.textContent()) || ''
    await region.screenshot({ path: `${OUT}/sidebar-card.png` })
  }
  await page.screenshot({ path: `${OUT}/full.png` })
  await browser.close()
  srv.close()

  // The figure is 4031s plus the few seconds the harness itself ran.
  const observed = {
    row: rowText.match(/\d+h \d+m \d+s|\d+m \d+s|\d+s/g),
    card: cardText.match(/\d+h \d+m \d+s|\d+m \d+s/g),
  }
  console.log(JSON.stringify(observed, null, 2))
  console.log(`screenshots -> ${OUT}`)
  const hasHours = s => /1h 7m \d+s/.test(s)
  if (EXPECT_BEFORE) {
    const broken = /40\d\ds/.test(rowText) && /67m \d+s/.test(cardText)
    if (!broken) { console.error('BEFORE frame did not reproduce the defect'); process.exit(1) }
    console.log('BEFORE: reproduced -- chip reads raw seconds, card reads 67m')
    return
  }
  if (!hasHours(rowText) || !hasHours(cardText)) { console.error('AFTER frame does not show the hours band'); process.exit(1) }
  console.log('AFTER: chip and card both read 1h 7m Ns')
}

main().catch(e => { console.error(e); process.exit(1) })
