/**
 * Screenshot harness for the Research Lab campaign model surfaces.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures, so no gateway or kiro-cli is needed.
 *   01 dark  → list: the running campaign's row names its model and shows the
 *              withhold notice; the finished row names its model
 *   02 light → detail of the running campaign: model line + withhold notice
 *   03 dark  → fork flow: the source campaign's model is pre-selected
 *
 * Usage: node scripts/capture-research-model-surface.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/research-model-surface'

mkdirSync(OUT, { recursive: true })

const base = { sub_questions: '[]', sources: '["web"]', max_cycles: 30, idle_secs: 60, findings: [] }
const RUNNING = {
  ...base, id: 'cafe0002', name: 'Queue backpressure',
  question: 'How do streaming platforms apply backpressure to slow consumers?',
  status: 'running', total_cycles: 4,
  model: 'model-pro', model_withheld: true, served_model: 'model-standard',
}
const DONE = {
  ...base, id: 'cafe0001', name: 'Rate limiting research',
  question: 'How do other teams handle API rate limiting effectively?',
  status: 'complete', total_cycles: 8,
  model: 'model-pro', model_withheld: null, served_model: '',
}
const MODELS = [
  { name: 'auto', description: '' },
  { name: 'model-pro', description: '' },
  { name: 'model-standard', description: '' },
]

const extra = (path, route) => {
  if (path === '/api/apps/auto-research/campaigns') return json(route, [RUNNING, DONE]), true
  if (path === `/api/apps/auto-research/campaigns/${RUNNING.id}`) return json(route, RUNNING), true
  if (path === `/api/apps/auto-research/campaigns/${DONE.id}`) return json(route, DONE), true
  if (path.endsWith('/knowledge-status')) return json(route, { in_library: false }), true
  if (path.endsWith('/report-status')) return json(route, { slug: null }), true
  if (path === '/api/models') return json(route, { models: MODELS }), true
  return false
}

async function open(browser, theme) {
  const context = await browser.newContext({ viewport: { width: 1200, height: 760 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, { theme, extra })
  logPageProblems(page)
  await page.goto(BASE + '/auto-research', { waitUntil: 'domcontentloaded' })
  await page.waitForSelector(`text=${DONE.question}`, { timeout: 15000 })
  return { context, page }
}

async function shot(page, name) {
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log('wrote', `${OUT}/${name}.png`)
}

let BASE
async function main() {
  const { srv, base: url } = await serveDist()
  BASE = url
  const browser = await chromium.launch()

  let { context, page } = await open(browser, 'dark')
  await page.waitForSelector('[role="status"]', { timeout: 10000 })
  await shot(page, '01-list-withheld-dark')
  await context.close()

  ;({ context, page } = await open(browser, 'light'))
  await page.click(`text=${RUNNING.question}`)
  await page.waitForSelector('button:has-text("Pause")', { timeout: 10000 })
  await shot(page, '02-detail-withheld-light')
  await context.close()

  ;({ context, page } = await open(browser, 'dark'))
  await page.click(`text=${DONE.question}`)
  await page.click('button:has-text("Fork & Challenge")')
  await page.waitForSelector('[aria-label="Model"]', { timeout: 10000 })
  await shot(page, '03-fork-model-preselected-dark')
  await context.close()

  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })
