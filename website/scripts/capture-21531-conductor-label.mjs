/**
 * Screenshot harness for issue #21531: a chat on the `kirocrew-conductor`
 * TEMPLATE is labelled "Conductor" in the session list and the composer, while
 * a crewmate that shares the name keeps it.
 *
 * Runs the REAL built SPA behind the shared loopback static server with every
 * /api/** call answered from fixtures (gateway-free). `DIST` selects the build.
 *
 * Usage: DIST=<dist dir> node scripts/capture-21531-conductor-label.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/21531-conductor-label'
const ACTIVE = 'chat-conductor'

mkdirSync(OUT, { recursive: true })
const now = Math.floor(Date.now() / 1000)

const slots = [
  {
    key: ACTIVE, title: 'Plan the launch week', running: false, messages: 2,
    agent: 'kirocrew-conductor', agent_kind: 'template', modified: now, last_ts: '2026-10-21T00:10:00Z', folder_id: '',
    last_message: 'Split the goal into four tasks.',
  },
  {
    key: 'chat-mate', title: 'Crewmate with the same name', running: false, messages: 2,
    agent: 'kirocrew-conductor', agent_kind: 'member', modified: now - 600, last_ts: '2026-10-21T00:05:00Z', folder_id: '',
    last_message: 'Done.',
  },
  {
    key: 'chat-plain', title: 'Fix the flaky test', running: false, messages: 2,
    agent: 'kirocrew', agent_kind: 'template', modified: now - 900, last_ts: '2026-10-21T00:00:00Z', folder_id: '',
    last_message: 'Pushed the fix.',
  },
]

const detail = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', content: 'Plan the launch week.', ts: now - 120 },
    { role: 'assistant', content: 'Split the goal into four tasks.', ts: now - 60 },
  ],
}

const extra = (path, route) => {
  if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
  return false
}

async function main() {
  const { srv, base } = await serveDist(process.env.DIST || undefined)
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1400, height: 800 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots, theme: 'dark', extra })
  await page.addInitScript(slot => {
    localStorage.setItem('mc-active-slot', slot)
    localStorage.setItem('mc-privacy-notice-v1', '1')
    localStorage.setItem('mc-sidebar-pinned', 'true')
  }, ACTIVE)
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3000)
  await page.screenshot({ path: `${OUT}/after.png` })
  const rows = await page.locator('text=Conductor').count()
  console.log(JSON.stringify({ conductorLabels: rows }))
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
