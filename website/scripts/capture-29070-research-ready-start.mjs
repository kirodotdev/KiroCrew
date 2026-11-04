/**
 * Screenshot harness: a ready Research Lab campaign (created, never started)
 * offers Start in its detail view.
 *
 * Runs the REAL built SPA behind the shared loopback static server, every
 * /api/** call answered from fixtures (gateway-free). The campaign stub is
 * STATEFUL: an accepted start flips it to running, so the re-reads that follow
 * show what the server would.
 *
 * MODE=before shows the detail view of the build without the control; MODE=after
 * (default) shows Start, presses it, checks the request and the running view, then
 * goes Back and checks the list shows the campaign as active; MODE=blocked opens
 * the ready campaign while another one runs; MODE=elsewhere starts another
 * campaign behind the page's back (the list it already read shows none) and checks
 * Start re-reads the list and sends nothing; MODE=refused answers the start with
 * the gateway's 409. THEME picks dark (default) or light. RECORD=1 records the
 * run as <MODE>-flow-<THEME>.webm instead of taking stills, slowed so each step
 * stays on screen long enough to read.
 *
 * Usage: DIST=<dist dir> MODE=after THEME=dark [RECORD=1] node scripts/capture-29070-research-ready-start.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/29070'
const MODE = process.env.MODE || 'after'
const THEME = process.env.THEME || 'dark'
const RECORD = process.env.RECORD === '1'
const VIEWPORT = { width: 1280, height: 760 }
mkdirSync(OUT, { recursive: true })

function assert(label, ok, detail = '') {
  console.log(`${label}: ${ok ? 'OK' : 'FAIL'}${detail ? ` — ${detail}` : ''}`)
  if (!ok) throw new Error(`${label} failed${detail ? `: ${detail}` : ''}`)
}

const BASE_CAMPAIGN = { sub_questions: '[]', sources: '["web"]', max_cycles: 30, idle_secs: 60, findings: [] }
const ready = {
  ...BASE_CAMPAIGN, id: 'cafe2907', name: 'Outage-tolerant queues',
  question: 'Which queueing designs survive a regional outage without losing messages?',
  status: 'ready', total_cycles: 0,
}
const other = {
  ...BASE_CAMPAIGN, id: 'cafe0001', name: 'Rate limiting research',
  question: 'How do other teams handle API rate limiting effectively?',
  status: 'running', total_cycles: 3,
}
const campaigns = MODE === 'blocked' ? [other, ready] : [ready]
const actions = []

const extra = (path, route) => {
  const method = route.request().method()
  // The nav dock's roster; the generic [] guess raises its load-failed notice.
  if (path === '/api/members') return json(route, { members: [], default_agent: 'kirocrew' }), true
  if (path === '/api/apps/auto-research/campaigns') return json(route, campaigns), true
  if (path === `/api/apps/auto-research/campaigns/${ready.id}`) {
    if (method === 'PATCH') {
      const body = route.request().postDataJSON()
      actions.push(body.action)
      if (MODE === 'refused') return json(route, { error: "Cannot start a campaign in 'running' state" }, 409), true
      ready.status = 'running'
      return json(route, { id: ready.id, status: 'running' }), true
    }
    return json(route, ready), true
  }
  if (path === `/api/apps/auto-research/campaigns/${other.id}`) return json(route, other), true
  return false
}

const ONE_AT_A_TIME = 'One campaign at a time — research benefits from focused depth.'

async function shot(page, name) {
  if (RECORD) return
  const path = `${OUT}/${name}-${THEME}.png`
  await page.screenshot({ path })
  console.log('wrote', path)
}

async function main() {
  const { srv, base } = await serveDist(process.env.DIST || undefined)
  const browser = await chromium.launch(RECORD ? { slowMo: 600 } : {})
  const context = await browser.newContext({
    viewport: VIEWPORT, deviceScaleFactor: 1,
    ...(RECORD ? { recordVideo: { dir: OUT, size: VIEWPORT } } : {}),
  })
  const page = await context.newPage()
  await stubDashboardApi(page, { theme: THEME, extra })
  logPageProblems(page)
  try {
    await page.goto(base + '/auto-research', { waitUntil: 'domcontentloaded' })
    await page.getByText(ready.question).click()
    await page.getByRole('heading', { name: ready.name }).waitFor()
    const start = page.getByRole('button', { name: 'Start Campaign', exact: true })
    const resume = page.getByRole('button', { name: 'Resume', exact: true })

    if (MODE === 'before') {
      await page.getByText('First cycle in progress...').waitFor()
      assert('no Start control', await start.count() === 0)
      assert('no Resume control', await resume.count() === 0)
      await shot(page, 'before-ready')
    } else if (MODE === 'blocked') {
      await start.waitFor()
      assert('Start disabled', await start.isDisabled())
      await page.getByText(ONE_AT_A_TIME, { exact: true }).waitFor()
      const describedBy = await start.getAttribute('aria-describedby')
      const described = describedBy && await page.locator(`[id="${describedBy}"]`).innerText()
      assert('reason describes Start', described?.trim() === ONE_AT_A_TIME, described)
      await shot(page, 'blocked-ready')
    } else if (MODE === 'elsewhere') {
      await start.waitFor()
      assert('Start enabled', await start.isEnabled())
      campaigns.unshift(other) // started through the API after the page's read
      await start.click()
      await page.getByText(ONE_AT_A_TIME, { exact: true }).waitFor()
      assert('Start disabled', await start.isDisabled())
      assert('no start sent', actions.length === 0, actions.join(','))
      await shot(page, 'elsewhere-ready')
    } else if (MODE === 'refused') {
      await start.click()
      await page.getByText("Cannot start a campaign in 'running' state").waitFor()
      assert('one start sent', actions.join(',') === 'start', actions.join(','))
      assert('Start still enabled', await start.isEnabled())
      await shot(page, 'refused-start')
    } else {
      await start.waitFor()
      await page.getByText('Not started yet.', { exact: true }).waitFor()
      assert('Start enabled', await start.isEnabled())
      assert('no Resume control', await resume.count() === 0)
      await shot(page, 'after-01-ready')
      await start.click()
      await page.getByRole('button', { name: 'Pause', exact: true }).waitFor()
      assert('one start sent', actions.join(',') === 'start', actions.join(','))
      assert('Start gone', await start.count() === 0)
      await page.getByText('First cycle in progress...').waitFor()
      await shot(page, 'after-02-started')
      await page.getByRole('button', { name: '← Back' }).click()
      await page.getByText('ACTIVE', { exact: true }).waitFor()
      assert('History empty', await page.getByText('HISTORY', { exact: true }).count() === 0)
      assert('New Campaign disabled', await page.getByRole('button', { name: '+ New Campaign' }).isDisabled())
      await shot(page, 'after-03-list')
    }
  } finally {
    const video = page.video()
    await context.close()
    if (video) {
      await video.saveAs(`${OUT}/${MODE}-flow-${THEME}.webm`)
      await video.delete()
      console.log('wrote', `${OUT}/${MODE}-flow-${THEME}.webm`)
    }
    await browser.close()
    srv.close()
  }
}

main().catch(e => { console.error(e); process.exit(1) })
