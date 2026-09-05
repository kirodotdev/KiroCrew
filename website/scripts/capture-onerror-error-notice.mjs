/**
 * Capture + regression harness for "user-initiated mutation failures that only
 * reached console.error" (#8625): a rejected mid-turn steer on the chat page,
 * and a rejected Export YAML on the project detail page. Both now render
 * through the shared ErrorNotice surface instead of dying in DevTools.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback server with
 * /api/** answered by the shared fixture stub. Only the network is stubbed:
 * the steer POST is refused with the same JSON envelope the real handler
 * emits, and the plan.yaml GET returns the handler's literal 409 body
 * ({"error": "no plan to export"}).
 *
 * Photographs the flows AND asserts what matters, exiting non-zero on failure:
 *   1. A rejected steer renders the refused-press notice above the composer,
 *      titled "Couldn't steer", carrying the server reason AND the agent
 *      hand-off (nothing to destroy: the hand-off stages a fresh session).
 *   2. Its ✕ dismisses the notice.
 *   3. A rejected export renders the plan-export notice under the tab bar with
 *      role="alert", the action-naming lead, and the unwrapped server reason —
 *      with NO agent hand-off (the page holds unsaved plan edits).
 *   4. Its ✕ dismisses the notice.
 *
 * Usage: node scripts/capture-onerror-error-notice.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/onerror-error-notice-8625'
const VIEW = { width: 1400, height: 900 }
const STEER_REFUSAL = 'No running turn to steer — the turn ended before the message arrived'

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

const slots = [{
  key: 'chat-a', title: 'Deploy pipeline', running: true, last_message: '',
  messages: 2, agent: 'kirocrew', memory_mode: 'persistent', project: '',
  modified: now, tags: [], source_links: [], source_links_total: 0,
}]

const chatDetail = {
  messages: [
    { role: 'user', content: 'Roll the canary out to 10% and watch the error rate.', cls: 'msg msg-u' },
    { role: 'assistant', content: 'Starting the canary rollout — dialing traffic to 10% now and tailing the error-rate dashboard.', cls: 'msg msg-a' },
  ],
  running: true, has_more: false, total: 2,
}

const run = {
  task_id: 'run-1', name: 'Checkout revamp', running: false, status: 'completed',
  steps: 3, completed: 3, failed: 0, skipped: 0, current_step: 3,
  spec: 'checkout.md', spec_name: 'Checkout', error: '',
  tokens_used: 1000, replan_count: 0,
  started_at: now - 120, finished_at: now - 30,
  work_dir: '/tmp/checkout', branch_name: 'main', spec_content: '# Checkout revamp',
  lessons_learned: [], commits: 1, original_input: 'revamp checkout', source: 'text',
  groups: [[1, 2], [3]],
  task_details: [
    { index: 1, title: 'Scaffold', description: 'Set up the module', status: 'passed', error: '', result: 'done', attempts: 1, depends_on: [], requires_approval: false },
    { index: 2, title: 'Wire payments', description: 'Connect the provider', status: 'passed', error: '', result: 'ok', attempts: 1, depends_on: [], requires_approval: false },
    { index: 3, title: 'Verify', description: 'End-to-end pass', status: 'passed', error: '', result: 'pass', attempts: 1, depends_on: [1, 2], requires_approval: false },
  ],
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch({ args: ['--no-sandbox'] })
  const results = []
  const record = (name, pass, note = '') => {
    results.push({ name, pass, note })
    console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${note ? ` — ${note}` : ''}`)
  }

  const extra = async (path, route) => {
    const method = route.request().method()
    // The active slot's transcript — running, so Enter routes to steer.
    if (path === '/api/chat/slots/chat-a' && method === 'GET') {
      await json(route, chatDetail)
      return true
    }
    // The steer POST, refused the way the gateway refuses it: a JSON error
    // envelope that api client's failure chokepoint unwraps for display.
    if (path === '/api/chat' && method === 'POST') {
      await json(route, { error: STEER_REFUSAL }, 409)
      return true
    }
    if (path === '/api/taskrunner' && method === 'GET') {
      await json(route, { runs: [run] })
      return true
    }
    // The export, refused with the handler's literal no-plan body
    // (api_taskrunner_export_yaml returns exactly this at 409).
    if (path === '/api/taskrunner/run-1/plan.yaml' && method === 'GET') {
      await json(route, { error: 'no plan to export' }, 409)
      return true
    }
    return false
  }

  const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots, extra })

  const shot = (name) => page.screenshot({ path: `${OUT}/${name}.png` })

  // ── Scene 1: rejected mid-turn steer on the chat page ──
  await page.goto(base + '/chat/chat-a', { waitUntil: 'domcontentloaded' })
  const input = page.getByLabel('Message input')
  await input.waitFor({ timeout: 12000 })
  await page.waitForTimeout(500)

  await input.fill('Hold the rollout — revert to 5% first')
  await input.press('Enter')

  const steerNotice = page.locator('[data-testid="refused-press-error"]')
  await steerNotice.waitFor({ timeout: 12000 })
  await page.waitForTimeout(300)
  await shot('01-steer-refused-notice-above-composer')

  const steerText = (await steerNotice.textContent()) ?? ''
  record('steer notice titles the refused action', steerText.includes("Couldn't steer"), steerText.slice(0, 120))
  record('steer notice carries the server reason', steerText.includes(STEER_REFUSAL))
  record('steer notice is an alert', (await steerNotice.locator('[role="alert"]').count()) === 1)
  // The hand-off stages a fresh session and per-slot drafts survive the
  // switch, so the refused-press surface opts INTO askAgent.
  record('steer notice offers the agent hand-off', (await steerNotice.getByRole('button', { name: 'Ask the agent' }).count()) === 1)
  // The rejection must not destroy the user's only copy: steer() cleared the
  // composer at send time and the optimistic bubble is retracted, so the text
  // is restored into the composer draft.
  record('rejected steer text restored to composer', ((await input.inputValue()) || '').includes('Hold the rollout'))

  await steerNotice.getByLabel('Dismiss').click()
  await page.waitForTimeout(200)
  record('✕ dismisses the steer notice', (await steerNotice.count()) === 0)
  await shot('02-steer-notice-dismissed')

  // ── Scene 2: rejected Export YAML on the project detail page ──
  await page.goto(base + '/projects?applied=run-1', { waitUntil: 'domcontentloaded' })
  const exportBtn = page.getByText('Export YAML')
  await exportBtn.waitFor({ timeout: 12000 })
  await page.waitForTimeout(500)
  await exportBtn.click()

  const exportNotice = page.locator('[data-testid="plan-export-error"]')
  await exportNotice.waitFor({ timeout: 12000 })
  await page.waitForTimeout(300)
  await shot('03-plan-export-error-below-tab-bar')

  const exportText = (await exportNotice.textContent()) ?? ''
  record('export notice leads with the action', exportText.includes('Could not export the plan YAML'), exportText.slice(0, 120))
  record('export notice unwraps the server reason', exportText.includes('no plan to export') && !exportText.includes('{"error"'), exportText.slice(0, 120))
  record('export notice is an alert', (await exportNotice.getAttribute('role')) === 'alert')
  // Deliberately NO hand-off: the page holds unsaved plan edits (pendingEdits)
  // and the navigation would unmount them.
  record('export notice has no agent hand-off', (await exportNotice.getByRole('button', { name: 'Ask the agent' }).count()) === 0)

  await exportNotice.getByLabel('Dismiss').click()
  await page.waitForTimeout(200)
  record('✕ dismisses the export notice', (await exportNotice.count()) === 0)
  await shot('04-plan-export-notice-dismissed')

  await browser.close()
  srv.close()
  const failed = results.filter(r => !r.pass)
  if (failed.length) {
    console.error(`\n${failed.length} assertion(s) failed`)
    process.exit(1)
  }
  console.log('\nall assertions passed; screenshots in', OUT)
}

main().catch(err => { console.error(err); process.exit(1) })
