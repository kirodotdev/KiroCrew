/**
 * Screenshot harness for the approval-expiry retirement (#11178).
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback server, with
 * /api/** answered by the shared fixture stub. No gateway, no approvals raised.
 *
 * The client code under test is unmodified — only the network is stubbed — so
 * the approval bar, the spawn banner and the Subagents card are exercised
 * exactly as they run in production, and driven the way the backend drives
 * them: an `approval` frame raises the card, and an `approval_resolved` frame
 * (the broadcast ApprovalCoordinator.request now emits from its finally when a
 * wait expires) retires it. Three outcomes are pictured:
 *
 *   tool approval + `decision: 'expired'`  → the permission row settles (the
 *     bar with Allow once / Reject unmounts) instead of staying actionable;
 *   spawn approval + `decision: 'expired'` → the pending card terminates with
 *     the catalog sentence "The approval wait expired, so the request was
 *     denied." in its error slot, not the raw `expired` token;
 *   spawn approval + `approved: false`     → the card carries "The approval was
 *     rejected, so the request was denied.", not the raw `rejected` token.
 *
 * Each state is ASSERTED before it is pictured, so the harness exits non-zero
 * when the card keeps a live button or the error slot carries a bare token —
 * a regression test, not just a camera.
 *
 * Usage: node scripts/capture-approval-expiry.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/11178-approval-expiry'
const SLOT = 'chat-approval-expiry'
const PROJECT = '/home/user/workspace/KiroCrew'

/** The catalog sentences the retire path must render — copied from
 *  `hooks.useWebSocket.approval_wait_expired` / `approval_rejected` in
 *  src/i18n/locales/en.manual.json. Spelled here rather than imported so a
 *  drift in the catalog fails this harness instead of moving its bar. */
const EXPIRED_SENTENCE = 'The approval wait expired, so the request was denied.'
const REJECTED_SENTENCE = 'The approval was rejected, so the request was denied.'

/** Bare decision tokens that used to land in the card's error slot. */
const RAW_TOKENS = new Set(['expired', 'rejected', 'stale'])

const TOOL_APPROVAL_ID = 'ap-11178-shell'
const SPAWN_AGENT_ID = '7f3a9c1e'
const SPAWN_APPROVAL_ID = `spawn:${SPAWN_AGENT_ID}`
const SPAWN_TASK = 'Audit the approval-expiry copy across every locale catalog'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Retire the approval card on expiry',
  running: true,
  last_message: 'Waiting on your approval before I touch the build tree.',
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
  running: true,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    {
      role: 'user',
      ts: now - 120,
      content: 'Rebuild the SPA and re-run the locale render gate for the approval copy.',
    },
    {
      role: 'assistant',
      ts: now - 20,
      content:
        'The dist is stale against the new catalog keys, so I need to clear the '
        + 'build tree first. That is a destructive shell step — asking before I run it.',
    },
  ],
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    // The bar, banner and card are dense small type (11–13px); a 1x shot
    // renders them soft on GitHub.
    deviceScaleFactor: 2,
  })

  // Routes the shared stub does not know about: the per-slot detail fetch, and
  // the spawn snapshot SubagentProgressBar reconciles against every 30s (report
  // the parked agent as live so a tick cannot sweep the card mid-capture).
  // Each branch returns an explicit `true` — the stub treats a falsy return as
  // "not handled" and `json()` resolves to undefined.
  const extra = (path, route) => {
    if (path === '/api/spawn') {
      return json(route, {
        agents: [{ id: SPAWN_AGENT_ID, task: SPAWN_TASK, done: false, parent: `dashboard:${SLOT}`, agent: 'kirocrew' }],
      }), true
    }
    if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
    return false
  }

  let page = null
  let wsServer = null

  /** Best-effort teardown for EVERY exit path, so a failing assertion cannot
   *  leave the browser or the static server alive and hang the run. */
  async function cleanup() {
    try { await context.close() } catch { /* already closed */ }
    try { await browser.close() } catch { /* already closed */ }
    try { srv.close() } catch { /* already closed */ }
  }

  try {

  /**
   * A FRESH page per scene. stubDashboardApi installs one `**\/api\/**` handler
   * and bakes the theme into /api/theme/boot, so it cannot be re-installed on
   * the same page; and a fresh page is also what keeps each scene's approval
   * ids out of the previous scene's retired-id log.
   */
  async function load(theme) {
    if (page) await page.close()
    wsServer = null
    page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      slots, theme, extra,
      localStorageEntries: { 'mc-active-slot': SLOT },
    })
    // Registered AFTER the shared stub so this handler wins: the stub swallows
    // /api/ws to stop a retry-storm, but this harness needs the socket handle
    // to push the frames the backend would have sent.
    await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    // Long enough for the first-connect syncPendingApprovals (which reads an
    // empty /api/approvals) to settle BEFORE any approval is injected, or the
    // reconcile would retire the injected card as stale on its own.
    await page.waitForTimeout(2500)
  }

  const push = async (type, data, settle = 900) => {
    if (!wsServer) throw new Error('websocket route never bound')
    wsServer.send(JSON.stringify({ type, data }))
    await page.waitForTimeout(settle)
  }

  /** Exactly what the `approval` broadcast carries for a chat-runner tool. */
  const raiseToolApproval = () => push('approval', {
    id: TOOL_APPROVAL_ID,
    slot: SLOT,
    source: 'agent',
    tool: 'shell',
    tool_input: 'rm -rf website/dist && npx vite build',
    tool_purpose: 'Clear the stale dist and rebuild against the new catalog keys',
    ts: Date.now() / 1000,
  })

  /** A spawn approval: id prefixed `spawn:`, tool spelled as spawn_run(task). */
  const raiseSpawnApproval = () => push('approval', {
    id: SPAWN_APPROVAL_ID,
    slot: SLOT,
    source: 'agent',
    tool: `spawn_run(${SPAWN_TASK})`,
    tool_input: JSON.stringify({ task: SPAWN_TASK }),
    ts: Date.now() / 1000,
  })

  /** The retire broadcast. `decision: 'expired'` is the coordinator's explicit
   *  timeout denial; a plain `approved: false` is a user rejection. */
  const resolve = (id, { approved, decision }) => push('approval_resolved', {
    id, slot: SLOT, approved, ...(decision ? { decision } : {}),
  })

  async function shot(name) {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /** Clip to a fixed rect (reused across before/after so the pair lines up). */
  async function clipShot(name, clip) {
    await page.screenshot({ path: `${OUT}/${name}.png`, clip })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /** Padded rect around a locator, clamped to the viewport. */
  async function rectOf(locator, pad = { x: 24, y: 16, w: 48, h: 40 }) {
    const box = await locator.first().boundingBox()
    if (!box) return null
    const x = Math.max(0, box.x - pad.x)
    const y = Math.max(0, box.y - pad.y)
    return { x, y, width: Math.min(1500 - x, box.width + pad.w), height: Math.min(950 - y, box.height + pad.h) }
  }

  /** Live approval controls in the composer band: the row the bar mounts its
   *  Allow once / Trust / Reject buttons into, plus the spawn banner's own
   *  Approve / Reject. Zero means nothing above the composer is actionable. */
  const composerApprovalControls = () => page.evaluate(() => {
    const area = document.querySelector('.input-area')
    if (!area) return -1
    const buttons = Array.from(area.querySelectorAll('button'))
    return buttons.filter(b => /^(Allow once|Approve|Approve all|Reject|Reject all)$/.test(b.textContent.trim())).length
  })

  /** The Subagents card whose header carries `label`; null when absent. */
  const cardFor = label => page
    .getByTitle(`Subagent ${label}`)
    .locator('xpath=ancestor::div[contains(@class,"bg-card")][1]')

  /** The card's own Approve / Reject. Their text content is `<svg/> Approve`,
   *  so the anchor has to tolerate the leading whitespace the glyph leaves. */
  const CARD_DECISION = /^\s*(Approve|Reject)\s*$/

  /** Every ErrorNotice message on the page, trimmed. The inline notice renders
   *  `role="alert"` > [icon, <span>message</span>, ask-agent button], so the
   *  direct child spans are the message (and, when present, a title). */
  const alertMessages = () => page.evaluate(() =>
    Array.from(document.querySelectorAll('[role="alert"]'))
      .flatMap(el => Array.from(el.querySelectorAll(':scope > span')).map(s => s.textContent.trim()))
      .filter(Boolean))

  const results = {}

  // ── Scene 1: a chat-runner tool approval that expires ─────────────────────
  // BEFORE: the bar above the composer carries live Allow once / Reject.
  await load('dark')
  await raiseToolApproval()
  const toolPendingControls = await composerApprovalControls()
  const toolPendingLabelShown = (await page.locator('.input-area').getByText('shell', { exact: false }).count()) > 0
  const bandRect = await rectOf(page.locator('.input-area'), { x: 0, y: 24, w: 0, h: 40 })
  await shot('01-tool-approval-pending-dark')
  if (bandRect) await clipShot('02-tool-approval-pending-dark-crop', bandRect)

  // AFTER: the coordinator's expiry broadcast. The permission row settles as
  // `stale` (selectSlotPendingApproval no longer returns it), so the bar
  // unmounts and the composer returns to its resting shape — no button that
  // would 404.
  await resolve(TOOL_APPROVAL_ID, { approved: false, decision: 'expired' })
  const toolExpiredControls = await composerApprovalControls()
  const toolExpiredRawToken = (await alertMessages()).some(t => RAW_TOKENS.has(t.toLowerCase()))
  await shot('03-tool-approval-expired-dark')
  if (bandRect) await clipShot('04-tool-approval-expired-dark-crop', bandRect)
  results.tool = { toolPendingControls, toolPendingLabelShown, toolExpiredControls, toolExpiredRawToken }

  // ── Scene 2: a spawn approval that expires ────────────────────────────────
  // BEFORE: the banner "1 sub-agent is awaiting your approval to run" with its
  // Approve / Reject, and the Subagents card in the panel (opened via the
  // banner's own Review in panel) headed "Pending Approval" with its buttons.
  await load('dark')
  await raiseSpawnApproval()
  const bannerShown = (await page.getByText('1 sub-agent is awaiting your approval to run').count()) === 1
  await page.getByRole('button', { name: 'Review in panel' }).click()
  await page.waitForTimeout(1200)
  const pendingCard = cardFor('Pending Approval')
  const pendingCardShown = (await pendingCard.count()) === 1
  const pendingCardButtons = pendingCardShown
    ? (await pendingCard.locator('button', { hasText: CARD_DECISION }).allInnerTexts()).map(t => t.trim())
    : []
  await shot('05-spawn-approval-pending-dark')
  const pendingRect = pendingCardShown ? await rectOf(pendingCard) : null
  if (pendingRect) await clipShot('06-spawn-approval-pending-card-crop', pendingRect)

  // AFTER: the expiry broadcast terminates the card. The banner and the bar
  // both retire, and the card's error slot carries the catalog SENTENCE.
  await resolve(SPAWN_APPROVAL_ID, { approved: false, decision: 'expired' })
  const bannerGoneAfterExpiry = (await page.getByText('1 sub-agent is awaiting your approval to run').count()) === 0
  const spawnExpiredControls = await composerApprovalControls()
  const expiredCard = cardFor('Error')
  const expiredCardShown = (await expiredCard.count()) === 1
  const expiredCardButtons = expiredCardShown
    ? await expiredCard.locator('button', { hasText: CARD_DECISION }).count()
    : -1
  const expiredAlerts = await alertMessages()
  const expiredSentenceShown = expiredAlerts.includes(EXPIRED_SENTENCE)
  const expiredRawToken = expiredAlerts.some(t => RAW_TOKENS.has(t.toLowerCase()))
  await shot('07-spawn-approval-expired-dark')
  const expiredRect = expiredCardShown ? await rectOf(expiredCard) : null
  if (expiredRect) await clipShot('08-spawn-approval-expired-card-crop', expiredRect)
  results.spawnExpired = {
    bannerShown, pendingCardShown, pendingCardButtons,
    bannerGoneAfterExpiry, spawnExpiredControls, expiredCardShown, expiredCardButtons,
    expiredAlerts, expiredSentenceShown, expiredRawToken,
  }

  // ── Scene 3: a spawn approval the user rejects elsewhere ──────────────────
  // A decided frame carries no `decision`; `approved: false` is the rejection.
  // The card must read the rejected SENTENCE, not `rejected`.
  await load('dark')
  await raiseSpawnApproval()
  await page.getByRole('button', { name: 'Review in panel' }).click()
  await page.waitForTimeout(1200)
  await resolve(SPAWN_APPROVAL_ID, { approved: false })
  const rejectedCard = cardFor('Error')
  const rejectedCardShown = (await rejectedCard.count()) === 1
  const rejectedCardButtons = rejectedCardShown
    ? await rejectedCard.locator('button', { hasText: CARD_DECISION }).count()
    : -1
  const rejectedAlerts = await alertMessages()
  const rejectedSentenceShown = rejectedAlerts.includes(REJECTED_SENTENCE)
  const rejectedRawToken = rejectedAlerts.some(t => RAW_TOKENS.has(t.toLowerCase()))
  await shot('09-spawn-approval-rejected-dark')
  const rejectedRect = rejectedCardShown ? await rectOf(rejectedCard) : null
  if (rejectedRect) await clipShot('10-spawn-approval-rejected-card-crop', rejectedRect)
  results.spawnRejected = { rejectedCardShown, rejectedCardButtons, rejectedAlerts, rejectedSentenceShown, rejectedRawToken }

  // ── Scene 4: light-theme parity for the expired card ──────────────────────
  await load('light')
  await raiseSpawnApproval()
  await page.getByRole('button', { name: 'Review in panel' }).click()
  await page.waitForTimeout(1200)
  await resolve(SPAWN_APPROVAL_ID, { approved: false, decision: 'expired' })
  const lightCard = cardFor('Error')
  const lightSentenceShown = (await alertMessages()).includes(EXPIRED_SENTENCE)
  const lightRect = (await lightCard.count()) === 1 ? await rectOf(lightCard) : null
  if (lightRect) await clipShot('11-spawn-approval-expired-light-crop', lightRect)
  else await shot('11-spawn-approval-expired-light')
  results.light = { lightSentenceShown }

  console.log('--- assertions ---')
  console.log('tool approval pending: live composer controls:', toolPendingControls)
  console.log('tool approval pending: bar names the tool:', toolPendingLabelShown)
  console.log('tool approval expired: live composer controls:', toolExpiredControls)
  console.log('tool approval expired: raw token in an error slot:', toolExpiredRawToken)
  console.log('spawn pending: banner shown:', bannerShown)
  console.log('spawn pending: panel card shown with buttons:', pendingCardShown, JSON.stringify(pendingCardButtons))
  console.log('spawn expired: banner gone:', bannerGoneAfterExpiry)
  console.log('spawn expired: live composer controls:', spawnExpiredControls)
  console.log('spawn expired: Error card shown / its Approve+Reject count:', expiredCardShown, expiredCardButtons)
  console.log('spawn expired: error slots:', JSON.stringify(expiredAlerts))
  console.log('spawn expired: catalog sentence present:', expiredSentenceShown)
  console.log('spawn expired: raw token in an error slot:', expiredRawToken)
  console.log('spawn rejected: Error card shown / its Approve+Reject count:', rejectedCardShown, rejectedCardButtons)
  console.log('spawn rejected: error slots:', JSON.stringify(rejectedAlerts))
  console.log('spawn rejected: catalog sentence present:', rejectedSentenceShown)
  console.log('spawn rejected: raw token in an error slot:', rejectedRawToken)
  console.log('light: expired sentence present:', lightSentenceShown)

  await cleanup()

  const ok = toolPendingControls >= 2            // Allow once + Reject were live
    && toolPendingLabelShown
    && toolExpiredControls === 0                 // nothing actionable survives the expiry
    && !toolExpiredRawToken
    && bannerShown
    && pendingCardShown
    && pendingCardButtons.includes('Approve') && pendingCardButtons.includes('Reject')
    && bannerGoneAfterExpiry
    && spawnExpiredControls === 0
    && expiredCardShown && expiredCardButtons === 0
    && expiredSentenceShown && !expiredRawToken
    && rejectedCardShown && rejectedCardButtons === 0
    && rejectedSentenceShown && !rejectedRawToken
    && lightSentenceShown
  if (!ok) {
    // Throw, never process.exit(): exit() would skip the catch below, whose
    // cleanup releases the browser and server this run still holds.
    throw new Error('FAIL: the approval surfaces did not retire as documented\n' + JSON.stringify(results, null, 2))
  }
  console.log('OK')
  } catch (err) {
    await cleanup()
    throw err
  }
}

main().catch(err => { console.error(err); process.exit(1) })
