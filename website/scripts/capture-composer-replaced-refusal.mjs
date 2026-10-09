/**
 * Frames of the composer approval bar when the user presses Allow once on a
 * coordinator request the server has already replaced (#14711), rendered from
 * the REAL built SPA (website/dist) gateway-free with stubDashboardApi.
 *
 * A coordinator approval's id is the caller's and recurs, so the bar decides
 * against the instance its row names. The stubbed server answers that decide
 * with 404, as it does for an instance it no longer holds. The bar then
 * settles the row as stale, withdraws its buttons and says the request is no
 * longer pending, in the bar's muted status row: an expired request is status,
 * not a failure (only a rejected submit is an error). Each frame asserts its
 * state before it is written, so a regression exits non-zero.
 *
 * Frames:
 *   16-composer-replaced-pending  the bar for request A (instance inst-a),
 *                                 Allow once / Reject live
 *   17-composer-replaced-refused  after Allow once: the decide named inst-a,
 *                                 the server refused it, the buttons are gone
 *                                 and the muted status row explains why
 *   24-composer-cron-timed-out    an unattended (cron) request that already
 *                                 timed out: the same muted status row names
 *                                 the source and says it was denied
 *
 * Usage: npm run build && node scripts/capture-composer-replaced-refusal.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/notification-approval-refusal'
mkdirSync(OUT, { recursive: true })

const SLOT = 'chat-replaced-approval'
const ID = 'apr-nightly-rsync'
const INSTANCE = 'inst-a'
// en.manual.json components.approvalCard.approval_no_longer_pending,
// spelled here so a catalog drift fails the harness instead of moving its bar.
const EXPIRED = 'This approval has expired or was already decided — the agent is no longer waiting on it.'
// en.manual.json components.chatInput.that_request_already_timed_out_and_was_denied, source=cron.
const TIMED_OUT = 'That cron request already timed out and was denied — the job is no longer waiting. Check the approvals feed for the record.'

const now = Date.now() / 1000
const slots = [{
  key: SLOT, title: 'Nightly backup', running: true,
  last_message: 'Waiting on your approval before I copy the data.', messages: 2,
  agent: 'kirocrew', memory_mode: 'persistent', project: '/home/user/workspace/backup',
  folder_id: '', modified: Math.floor(now), source_links: [], source_links_total: 0,
}]
const detail = {
  running: true, has_more: false, total: 2, queue: [], project: '/home/user/workspace/backup',
  messages: [
    { role: 'user', ts: now - 120, content: 'Copy ~/data to the backup volume.' },
    { role: 'assistant', ts: now - 20, content: 'The copy needs a shell step, so I am asking first.' },
  ],
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
let failed = false

async function cleanup() {
  try { await context.close() } catch { /* already closed */ }
  try { await browser.close() } catch { /* already closed */ }
  try { srv.close() } catch { /* already closed */ }
}

/** Open the chat, raise one approval from `source`, and return the page. */
async function raise(source) {
  const page = await context.newPage()
  logPageProblems(page)
  const decides = []
  await stubDashboardApi(page, {
    slots, theme: 'dark', localStorageEntries: { 'mc-active-slot': SLOT },
    extra: async (path, route) => {
      if (path.startsWith('/api/approvals/') && route.request().method() === 'POST') {
        decides.push(route.request().url())
        await json(route, { error: 'not found' }, 404)
        return true
      }
      if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
      return false
    },
  })
  let ws = null
  await page.routeWebSocket(/\/api\/ws/, s => { ws = s })
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  // Let the first-connect approval reconcile (an empty /api/approvals) settle
  // before the approval arrives, or it would retire the row on its own.
  await page.waitForTimeout(2500)
  if (!ws) throw new Error('websocket route never bound')
  ws.send(JSON.stringify({ type: 'approval', data: {
    id: ID, slot: SLOT, instance: INSTANCE, source, tool: 'shell',
    tool_input: 'rsync -a ~/data /mnt/backup', tool_purpose: 'Copy the data to the backup volume', ts: Date.now() / 1000,
  } }))
  await page.waitForTimeout(900)
  return { page, decides }
}

/** The bar's notice: the muted status row, never the ErrorNotice alert. */
async function mutedNotice(area, text) {
  const row = area.getByRole('status').filter({ hasText: text })
  return (await row.count()) === 1 && (await area.getByTestId('approval-decision-error').count()) === 0
}

try {
  const { page, decides } = await raise('agent')

  const area = page.locator('.input-area')
  const controls = () => page.evaluate(() => {
    const el = document.querySelector('.input-area')
    if (!el) return -1
    return Array.from(el.querySelectorAll('button'))
      .filter(b => /^(Allow once|Reject)$/.test(b.textContent.trim())).length
  })
  const box = await area.boundingBox()
  const clip = box ? { x: 0, y: Math.max(0, box.y - 140), width: 1500, height: Math.min(950 - Math.max(0, box.y - 140), box.height + 180) } : undefined

  const pending = await controls()
  const okPending = pending >= 2
  console.log(`16-composer-replaced-pending: ${okPending ? 'OK' : 'MISMATCH'} live controls=${pending}`)
  failed ||= !okPending
  await page.screenshot({ path: `${OUT}/16-composer-replaced-pending.png`, clip })

  await area.getByRole('button', { name: 'Allow once', exact: true }).first().click()
  await page.waitForTimeout(900)
  const after = await controls()
  const named = decides.length === 1 && new URL(decides[0]).searchParams.get('instance') === INSTANCE
  const said = await mutedNotice(area, EXPIRED)
  const okRefused = after === 0 && named && said
  console.log(`17-composer-replaced-refused: ${okRefused ? 'OK' : 'MISMATCH'} controls=${after} decides=${JSON.stringify(decides)} status=${said}`)
  failed ||= !okRefused
  await page.screenshot({ path: `${OUT}/17-composer-replaced-refused.png`, clip })
  await page.close()

  const cron = await raise('cron')
  const cronArea = cron.page.locator('.input-area')
  await cronArea.getByRole('button', { name: 'Allow once', exact: true }).first().click()
  await cron.page.waitForTimeout(900)
  const cronSaid = await mutedNotice(cronArea, TIMED_OUT)
  const okCron = cron.decides.length === 1 && cronSaid
  console.log(`24-composer-cron-timed-out: ${okCron ? 'OK' : 'MISMATCH'} decides=${cron.decides.length} status=${cronSaid}`)
  failed ||= !okCron
  const cronBox = await cronArea.boundingBox()
  const cronClip = cronBox ? { x: 0, y: Math.max(0, cronBox.y - 140), width: 1500, height: Math.min(950 - Math.max(0, cronBox.y - 140), cronBox.height + 180) } : undefined
  await cron.page.screenshot({ path: `${OUT}/24-composer-cron-timed-out.png`, clip: cronClip })
} catch (err) {
  console.error(err)
  failed = true
} finally {
  await cleanup()
}
process.exit(failed ? 1 : 0)
