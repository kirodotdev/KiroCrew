/**
 * Screenshot harness for #18203 — a gone approval reads as STATUS, not an error.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback server with
 * /api/** answered by the shared fixture stub. No gateway. The client code is
 * unmodified — only the network is stubbed — so the surface is exercised exactly
 * as it runs in production, and driven the way the backend drives it: an
 * `approval` WS frame raises the spawn banner, "Review in panel" opens the
 * Subagents card, and the card's OWN Approve press is answered 404 "no pending
 * approval" so it finds the request gone.
 *
 * Before #18203 that landed as a red `role="alert"` ErrorNotice with an
 * Ask-the-agent hand-off; after, it is a muted `role="status"` StatusNotice —
 * nothing failed on the user's side, so nothing is dressed as a failure. Both
 * themes are pictured, the state ASSERTED (visible text only) before each shot,
 * so the harness exits non-zero if the gone outcome ever renders as an alert.
 *
 * Usage: node scripts/capture-gone-approval-status.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/18203-gone-approval-status'
const SLOT = 'chat-gone-approval'
const PROJECT = '/home/user/workspace/KiroCrew'

const SPAWN_AGENT_ID = '18203abc'
const SPAWN_APPROVAL_ID = `spawn:${SPAWN_AGENT_ID}`
const SPAWN_TASK = 'Rebuild the SPA and re-run the locale render gate'

/** The gone sentence the surface must render as status (en.manual.json). */
const GONE_FRAGMENT = 'nothing is waiting on it'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'A gone approval reads as status',
  running: true,
  last_message: 'Waiting on your approval before I spawn the worker.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const now = Date.now() / 1000
const detail = {
  running: true, has_more: false, total: 2, queue: [], project: PROJECT,
  messages: [
    { role: 'user', ts: now - 120, content: 'Spawn a worker to re-run the locale render gate.' },
    { role: 'assistant', ts: now - 20, content: 'Spawning the worker needs your approval — asking before I start it.' },
  ],
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })

  // The spawn snapshot SubagentProgressBar reconciles against; report the parked
  // agent live so a tick cannot sweep the card. The per-slot detail fetch. And
  // the resolve POST answered 404 so the card's press finds the request gone.
  const extra = (path, route) => {
    if (path === '/api/spawn') {
      return json(route, { agents: [{ id: SPAWN_AGENT_ID, task: SPAWN_TASK, done: false, parent: `dashboard:${SLOT}`, agent: 'kirocrew', status: 'pending', approval_id: SPAWN_APPROVAL_ID, awaiting_approval: true }] }), true
    }
    if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
    if (/^\/api\/approvals\/.+\/(approve|reject|reject_once)$/.test(path)) {
      return json(route, { error: 'no pending approval' }, 404), true
    }
    return false
  }

  let page = null
  let wsServer = null

  async function cleanup() {
    try { await context.close() } catch { /* already closed */ }
    try { await browser.close() } catch { /* already closed */ }
    try { srv.close() } catch { /* already closed */ }
  }

  try {
    async function load(theme) {
      if (page) await page.close()
      wsServer = null
      page = await context.newPage()
      logPageProblems(page)
      await stubDashboardApi(page, { slots, theme, extra, localStorageEntries: { 'mc-active-slot': SLOT } })
      // Registered AFTER the shared stub so this handler wins: the stub swallows
      // /api/ws, but this harness needs the socket to push the approval frame.
      await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })
      await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
      // Let the first-connect syncPendingApprovals (empty /api/approvals) settle
      // before the approval is injected, or the reconcile retires it as stale.
      await page.waitForTimeout(2500)
    }

    const push = async (type, data, settle = 900) => {
      if (!wsServer) throw new Error('websocket route never bound')
      wsServer.send(JSON.stringify({ type, data }))
      await page.waitForTimeout(settle)
    }

    const raiseSpawnApproval = () => push('approval', {
      id: SPAWN_APPROVAL_ID, slot: SLOT, source: 'agent',
      tool: `spawn_run(${SPAWN_TASK})`, tool_input: JSON.stringify({ task: SPAWN_TASK }),
      ts: Date.now() / 1000,
    })

    const cardFor = label => page
      .getByTitle(`Subagent ${label}`)
      .locator('xpath=ancestor::div[contains(@class,"bg-card")][1]')

    /** VISIBLE status / alert text only (innerText skips the hidden boot-failure
     *  fallback baked into index.html). */
    const visibleByRole = role => page.evaluate(r =>
      Array.from(document.querySelectorAll(`[role="${r}"]`))
        .filter(el => el.offsetParent !== null || el.getClientRects().length)
        .map(el => el.innerText.trim()).filter(Boolean), role)

    async function shot(name) {
      await page.screenshot({ path: `${OUT}/${name}.png` })
      console.log('wrote', `${OUT}/${name}.png`)
    }

    async function driveGone(theme) {
      await load(theme)
      await raiseSpawnApproval()
      await page.getByRole('button', { name: 'Review in panel' }).click()
      await page.waitForTimeout(1200)
      const pending = cardFor('Pending Approval')
      const pendingShown = (await pending.count()) === 1
      await shot(`subagents-pending-${theme}`)
      if (pendingShown) {
        await pending.getByRole('button', { name: /Approve/ }).first().click()
        await page.waitForTimeout(2200)
      }
      const statusText = await visibleByRole('status')
      const alertText = await visibleByRole('alert')
      const goneAsStatus = statusText.some(t => t.includes(GONE_FRAGMENT))
      const goneAsAlert = alertText.some(t => t.includes(GONE_FRAGMENT))
      await shot(`subagents-gone-${theme}`)
      return { pendingShown, goneAsStatus, goneAsAlert, statusText, alertText }
    }

    const dark = await driveGone('dark')
    const light = await driveGone('light')

    console.log('--- assertions ---')
    console.log('dark:', JSON.stringify(dark))
    console.log('light:', JSON.stringify(light))

    await cleanup()

    const ok = dark.pendingShown && dark.goneAsStatus && !dark.goneAsAlert
      && light.pendingShown && light.goneAsStatus && !light.goneAsAlert
    if (!ok) throw new Error('FAIL: the gone approval did not render as status on every theme\n' + JSON.stringify({ dark, light }, null, 2))
    console.log('OK — gone approval renders as status (role=status), never alert, light + dark')
  } catch (err) {
    await cleanup()
    throw err
  }
}

main().catch(err => { console.error(err); process.exit(1) })
