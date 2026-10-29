// Evidence capture: the Crew Mode dispatch hint in a conductor's chat, and the
// side panel its button opens on the Crew tab.
//
// Runs the REAL built SPA (website/dist) with the dashboard API stubbed: one Crew
// Mode root whose transcript holds the gateway's `crew_dispatch_hint` notice, and
// the crew member it started. Not a test: crewDispatchHint.test.tsx pins the row.
//
//   node scripts/capture-crew-dispatch-hint.mjs [outDir] [lang]
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/crew-dispatch-hint'
const LANG = process.argv[3] || 'en'
mkdirSync(OUT, { recursive: true })

const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const catalog = JSON.parse(readFileSync(LOCALES + (LANG === 'en' ? 'en.manual.json' : `${LANG}.json`), 'utf-8'))
const OPEN = catalog.commandCenter.dispatch_hint_open
if (!OPEN) throw new Error(`dispatch_hint_open missing from the ${LANG} catalog`)

const ROOT = 'chat-1-100'
const WORKER = 'chat-2-200'
const now = Math.floor(Date.now() / 1000)
const conductor = { agent: 'kirocrew-conductor', agent_kind: 'template', crew_mode_agent: 'kirocrew-conductor' }
const slots = [
  {
    key: ROOT, title: 'Fix the flaky login test', running: false, stop_state: 'idle',
    last_message: 'A crew member is on it.', messages: 4, memory_mode: 'persistent',
    modified: now, source_links: [], source_links_total: 0, crew_mode_root: true, ...conductor,
  },
  {
    key: WORKER, title: 'Reproduce the login flake', running: true, stop_state: 'running',
    last_message: 'Running the test 50 times.', messages: 2, agent: 'kirocrew-worker', memory_mode: 'persistent',
    modified: now, source_links: [], source_links_total: 0, created_by: ROOT, crew_root: ROOT,
  },
]
const detail = {
  running: false, has_more: false, total: 4, queue: [], ...conductor,
  messages: [
    { role: 'user', ts: now - 120, content: 'The login test fails about one run in ten. Find out why.' },
    { role: 'tool', ts: now - 100, content: '🔧 session_create', meta: { tool_name: 'session_create', output: `{"ok": true, "target": "${WORKER}", "crew_mode": true}` } },
    {
      role: 'notice', ts: now - 99, cls: 'msg msg-info',
      content: 'Crew started work. Open the Crew tab in the right-side panel to follow it.',
      meta: { kind: 'crew_dispatch_hint', mid: 'm-hint' },
    },
    { role: 'assistant', ts: now - 90, content: 'A crew member is reproducing the flake now. I will report back when it has a cause.' },
  ],
}
const workers = {
  root: ROOT, root_closed: false, workers: [
    { key: WORKER, title: 'Reproduce the login flake', agent: 'kirocrew-worker', group: 'working', status: 'working', summary: 'Running the test 50 times.' },
  ],
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1440, height: 860 }, deviceScaleFactor: 2, locale: LANG })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    slots,
    localStorageEntries: LANG === 'en' ? null : { 'mc-language': LANG, i18nextLng: LANG },
    extra: async (path, route) => {
      if (path === `/api/chat/slots/${ROOT}/crew-workers`) { await json(route, workers); return true }
      if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
      return false
    },
  })
  await page.goto(base + `/chat?sid=${encodeURIComponent(ROOT)}`, { waitUntil: 'domcontentloaded' })
  const hint = page.getByTestId('crew-dispatch-hint')
  await hint.waitFor({ timeout: 20000 })
  await page.waitForTimeout(500)
  await page.screenshot({ path: `${OUT}/${LANG}-hint.png` })

  await page.getByRole('button', { name: OPEN }).click()
  await page.waitForTimeout(1200)
  await page.screenshot({ path: `${OUT}/${LANG}-crew-tab-open.png` })
  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}/${LANG}-{hint,crew-tab-open}.png`)
}

main().catch(err => { console.error(err); process.exit(1) })
