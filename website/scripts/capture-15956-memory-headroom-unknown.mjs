/**
 * Screenshot harness + assertions for the wave chip's queued reason when the
 * cgroup memory usage file is unreadable (#15956).
 *
 * The gate defers a start whose cgroup has a finite memory limit but no readable
 * usage. That headroom is UNKNOWN, yet the chip used to read the 0 the probe
 * contributes as a measurement: "needs 4.5 GB of free memory, 0 GB free now".
 * The gate now labels that wait with `cause: cgroup_usage_unreadable` and no
 * `available_gb`, and the chip says free memory is unknown.
 *
 * Drives the REAL built SPA (website/dist), pushes the `subagent_queued` frames
 * the gateway sends, and exits non-zero unless:
 *   - the unreadable-usage frame renders the unknown-headroom sentence with no
 *     "free now" figure, and
 *   - a measured low-memory frame still renders its figures.
 *
 * Usage: node scripts/capture-15956-memory-headroom-unknown.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/15956-memory-headroom-unknown'
const SLOT = 'chat-headroom'
const PROJECT = '/home/user/workspace/uploader'
const RUNNING_ID = 'a1b2c3d4'

const slots = [{
  key: SLOT,
  title: 'Audit the uploader retry ladder',
  running: true,
  last_message: 'Spawned 3 subagent(s).',
  messages: 2,
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
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 600, content: 'Audit the uploader retry ladder in three parts.' },
    { role: 'assistant', ts: Date.now() / 1000 - 60, content: 'Splitting this into three sub-agents.' },
  ],
}

const SCENES = [
  {
    name: 'unknown-headroom',
    frame: { reason: 'low_memory', required_gb: 4.5, cause: 'cgroup_usage_unreadable', max_wait_secs: 1800 },
    expect: text => /free memory is unknown/.test(text) && !/free now/.test(text),
  },
  {
    name: 'measured-low-memory',
    frame: { reason: 'low_memory', available_gb: 1.2, required_gb: 4.5, max_wait_secs: 1800 },
    expect: text => /free now/.test(text) && !/unknown/.test(text),
  },
]

async function main() {
  mkdirSync(OUT, { recursive: true })
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
  const results = []
  try {
    for (const theme of ['dark', 'light']) {
      for (const scene of SCENES) {
        const page = await context.newPage()
        logPageProblems(page)
        const extra = async (path, route) => {
          if (path === '/api/spawn') { await json(route, { agents: [{ id: RUNNING_ID, done: false, parent: `dashboard:${SLOT}` }] }); return true }
          if (path === '/api/tips/status') { await json(route, { enabled: false }); return true }
          if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
          return false
        }
        await stubDashboardApi(page, { slots, theme, extra })
        // After the shared stub so this wins: the harness needs the socket.
        let wsServer = null
        await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })
        await page.addInitScript(slot => localStorage.setItem('mc-active-slot', slot), SLOT)
        await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
        await page.waitForTimeout(2500)
        if (!wsServer) throw new Error('websocket route never bound')
        const send = (type, data) => wsServer.send(JSON.stringify({ type, data }))
        send('subagent_spawn', { slot: SLOT, id: RUNNING_ID, task: 'audit the retry ladder in the uploader', agent: 'kirocrew' })
        send('subagent_queued', { slot: SLOT, queued: 2, ...scene.frame })
        const line = page.getByTestId('subagent-wait-reason').first()
        await line.waitFor({ state: 'visible', timeout: 10_000 })
        const text = ((await line.textContent()) || '').trim()
        const pass = scene.expect(text)
        results.push({ theme, scene: scene.name, pass, text })
        const chip = page.getByTestId('subagent-histogram')
          .locator('xpath=ancestor::div[contains(@class,"animate-slide-up")][1]').first()
        const path = `${OUT}/${theme}-${scene.name}.png`
        if (await chip.count()) await chip.screenshot({ path })
        else await page.screenshot({ path })
        console.log('wrote', path)
        await page.close()
      }
    }
  } finally {
    await browser.close()
    srv.close()
  }
  for (const r of results) console.log(`${r.pass ? 'PASS' : 'FAIL'}  ${r.theme} ${r.scene}: ${r.text}`)
  if (results.some(r => !r.pass)) process.exit(1)
}

main().catch(e => { console.error(e); process.exit(1) })
