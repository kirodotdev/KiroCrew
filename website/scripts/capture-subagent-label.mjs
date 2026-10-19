/**
 * Screenshot harness + assertions for the `spawn_run` label in the task box.
 *
 * The wave chip above the composer names each running sub-agent by the first
 * 80 characters of its task text, so two parallel runs of a long orchestrator
 * prompt read the same. A run spawned with a `label` is named by that label;
 * a run without one keeps the task text. This drives the REAL built SPA
 * (website/dist), pushes the snapshot frames the gateway sends for two
 * parallel runs (one labelled, one not), and exits non-zero unless the
 * labelled row reads its label and the other row reads its task text.
 *
 * BEFORE frame: check SubagentProgressBar.tsx out at the base commit, rebuild,
 * and run with --expect-before (both rows then read the task text).
 *
 * Usage: node scripts/capture-subagent-label.mjs [outDir] [--expect-before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const args = process.argv.slice(2).filter(a => a !== '--expect-before')
const EXPECT_BEFORE = process.argv.includes('--expect-before')
const OUT = args[0] || '../temp-screenshots/subagent-label'
const SLOT = 'chat-label'
const PROJECT = '/home/user/workspace/uploader'
const T0 = Date.now()
const TASK = 'You own the retry ladder in the uploader. Read every caller of the retry helper, ' +
  'list each path that retries without a cap, and report file, line and the fix for each.'
const LABEL = 'Retry ladder audit'

const slots = [{
  key: SLOT,
  title: 'Harden the uploader',
  running: true,
  last_message: 'Spawned 2 subagent(s).',
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
    { role: 'user', ts: T0 / 1000 - 60, content: 'Harden the uploader.' },
    { role: 'assistant', ts: T0 / 1000 - 30, content: 'Splitting this into two sub-agents.' },
  ],
}

const spawnList = { agents: [
  { id: 'a1b2c3d4', done: false, parent: `dashboard:${SLOT}` },
  { id: 'e5f6a7b8', done: false, parent: `dashboard:${SLOT}` },
] }

/** Fixture answers for the routes the chat page reads beyond the shared stub. */
const ROUTES = [
  [p => p === '/api/spawn', () => spawnList],
  [p => p === '/api/tips/status', () => ({ enabled: false })],
  [p => p.startsWith('/api/chat/slots/'), () => detail],
]

/** Open the stubbed chat page on SLOT; resolve with the page's WS server end. */
async function openChat(page, base) {
  await stubDashboardApi(page, {
    slots,
    extra: async (path, route) => {
      const hit = ROUTES.find(([match]) => match(path))
      if (!hit) return false
      await json(route, hit[1]())
      return true
    },
  })
  // After the shared stub: the route registered last answers the socket.
  let bound
  const socket = new Promise(resolve => { bound = resolve })
  await page.routeWebSocket(/\/api\/ws/, ws => bound(ws))
  await page.addInitScript(slot => localStorage.setItem('mc-active-slot', slot), SLOT)
  await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })
  const ws = await Promise.race([socket, page.waitForTimeout(10_000).then(() => null)])
  if (!ws) throw new Error('websocket route never bound')
  await page.waitForTimeout(2000)
  return ws
}

async function main() {
  mkdirSync(OUT, { recursive: true })
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const page = await (await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })).newPage()
  logPageProblems(page)
  const wsServer = await openChat(page, base)

  const started = Date.now() / 1000 - 95
  for (const [id, label] of [['a1b2c3d4', LABEL], ['e5f6a7b8', '']]) {
    wsServer.send(JSON.stringify({ type: 'subagent_snapshot', data: {
      slot: SLOT, id, agent: 'kirocrew', task: TASK, ...(label ? { label } : {}),
      last_tool: 'Reading: src/uploader/retry.py', started, streaming: '', tool_count: 4,
    } }))
  }
  await page.waitForTimeout(1500)

  const rows = page.getByTestId('subagent-row')
  const count = await rows.count()
  const texts = []
  for (let i = 0; i < count; i++) texts.push((await rows.nth(i).textContent()) || '')
  const chip = page.getByTestId('subagent-histogram').locator('xpath=ancestor::div[contains(@class,"animate-slide-up")][1]')
  if (await chip.count()) await chip.first().screenshot({ path: `${OUT}/wave-chip${EXPECT_BEFORE ? '-before' : ''}.png` })
  await browser.close()
  srv.close()

  const preview = TASK.slice(0, 40)
  const labelled = texts.filter(t => t.includes(LABEL)).length
  const byTask = texts.filter(t => t.includes(preview)).length
  // The labelled row leads with its label and keeps the task start after it.
  const ledByLabel = texts.filter(t => t.includes(LABEL) && t.indexOf(LABEL) < t.indexOf(preview)).length
  console.log(JSON.stringify({ rows: count, labelled, byTask, ledByLabel }, null, 2))
  console.log(`screenshots -> ${OUT}`)
  if (count !== 2) { console.error(`expected 2 rows, saw ${count}`); process.exit(1) }
  if (EXPECT_BEFORE) {
    if (labelled !== 0 || byTask !== 2) { console.error('BEFORE frame did not reproduce the defect'); process.exit(1) }
    console.log('BEFORE: reproduced -- both rows read the task text')
    return
  }
  if (labelled !== 1 || ledByLabel !== 1) { console.error('AFTER frame does not lead the labelled row with its label'); process.exit(1) }
  console.log('AFTER: labelled row leads with its label, then its task start; the other row reads its task text')
}

main().catch(e => { console.error(e); process.exit(1) })
