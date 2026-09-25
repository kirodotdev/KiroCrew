/**
 * Screenshot harness for the config-defined backends feature.
 *
 * Fifteen frames, one per user-visible surface the change adds, plus a frame
 * sequence of the Verify success transition (verify-seq/, assembled into a GIF
 * by the caller):
 *   1. Settings -> Backends: the registry listing, with one config-defined
 *      backend selectable beside the builtins, one INVALID descriptor and one
 *      UNROUTABLE descriptor each shown with its reason.
 *   2. New chat: the welcome-screen backend picker open, offering the
 *      config-defined backend beside the builtins with the default flagged.
 *   3. An open chat pinned to that backend: the backend chip in the
 *      composer shelf beside the agent chip.
 *   4. The same chip clicked: its inline explanation (fixed at creation; pick
 *      on a new chat's welcome screen) -- the chip opens no picker.
 *   5. An UNPINNED chat's chip clicked: no lock glyph, and the explanation says
 *      the chat follows the default backend.
 *   6. Settings -> Backends: a routed descriptor whose routing is not yet
 *      verified, with its "Routing not verified" badge and the Verify action.
 *   7. The same row after a VERIFIED probe: it keeps its place; the badge is
 *      gone and the success line sits under it.
 *   8. The same row after a FAILED probe (the probe file was written despite
 *      denial): the outcome rendered under the row.
 *   9./10. Settings -> Backends with nothing registered / with a listing that
 *      did not answer.
 *  11./12. The welcome-screen picker in the same two states.
 *  13. The welcome picker's pick refused by the server: ErrorNotice under it.
 *  14. Settings -> Backends: an INCONCLUSIVE probe outcome under the row.
 *  15. Settings -> Backends: VERIFIED but denied by the agent_backend policy.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures: no gateway, no
 * dashboard token, no provider CLI.
 *
 * Usage: node scripts/capture-backends-feature.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/backends-feature'
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

// The listing the registry endpoint serves: builtins plus one config-defined
// backend ("acme", from harnesses.json) that registered as selectable, one
// descriptor that failed validation, and one that declared no verified routing.
const BACKENDS = {
  backends: [
    { id: '', label: 'Kiro CLI', is_global_default: true },
    { id: 'claude', label: 'Claude Code', is_global_default: false },
    { id: 'kas', label: 'Kiro Agent Server', is_global_default: false },
    { id: 'codex', label: 'Codex', is_global_default: false },
    { id: 'acme', label: 'Acme Agent', is_global_default: false },
  ],
  invalid: [
    // An invalid entry is listed under its FILE POSITION (never its key, which
    // is operator text), and its reasons locate it the same way.
    { id: '#3', label: '#3', reasons: ['entry #3: argv[0] must be "{executable}"', 'entry #3: model_source is \'static\' but no models are declared'] },
  ],
  unroutable: [
    { id: 'shadow', label: 'Shadow Agent', reason: 'descriptor declares no routing, so nothing establishes its tool calls reach the permission gate', verifiable: false },
    // A routed descriptor whose only missing piece is the end-to-end routing
    // attestation: the row Settings offers a Verify action for.
    { id: 'orbit', label: 'Orbit Agent', reason: 'routing not yet verified end to end: run Verify in Settings → AI backends (the gateway spawns the harness once and checks that a file edit and a shell command each ask for permission before acting)', verifiable: true },
  ],
}

// What POST /api/backends/orbit/verify answers in frames 7 and 8. Toggled per
// frame; the listing after a verified verdict moves orbit into `backends`.
let VERIFY_ANSWER = 'verified'
const VERIFY_RESULTS = {
  verified: {
    verdict: 'verified', verified: true, permission_requests: 1, probe_file_written: false, elapsed_secs: 14.2, selectable: true,
    reason: 'the host asked for permission 1 time(s) before acting and honoured the refusal (nothing was written)',
  },
  violation: {
    verdict: 'violation', verified: false, permission_requests: 0, probe_file_written: true, elapsed_secs: 9.8, selectable: false,
    reason: 'KIROCREW_ROUTING_PROBE.txt was written although every permission request was denied (0 received): a tool call executed without going through the permission gate, so this backend\'s routing is not what its descriptor declares',
  },
  policy_denied: {
    verdict: 'verified', verified: true, permission_requests: 1, probe_file_written: false, elapsed_secs: 12.7, selectable: false, policy_denied: true,
    reason: 'the host asked for permission 1 time(s) before acting and honoured the refusal (nothing was written); routing verified, but this deployment\'s agent_backend policy does not permit the backend, so it is not selectable',
  },
  inconclusive: {
    verdict: 'inconclusive', verified: false, permission_requests: 0, probe_file_written: false, elapsed_secs: 31.4, selectable: false,
    reason: 'the host raised 2 permission request(s), none of them for the probe write (KIROCREW_ROUTING_PROBE.txt), and nothing was written: the write the probe asked for was never attempted, so routing was neither proven nor disproven -- run it again',
  },
}
// When true, POST /api/chat/slots/<slot>/backend refuses (frame 13: the welcome
// picker's refused pick rendered through ErrorNotice).
let REFUSE_BACKEND_SWITCH = false
// After a verified-but-policy-denied probe: orbit stays unroutable with the
// policy named as its reason and no Verify action (verification is not what it lacks).
const POLICY_DENIED_BACKENDS = {
  ...BACKENDS,
  unroutable: BACKENDS.unroutable.map(r => r.id !== 'orbit' ? r : {
    ...r, verifiable: false,
    reason: "routing verified, but this deployment's agent_backend policy does not permit the backend, so it is not selectable",
  }),
}
const VERIFIED_BACKENDS = {
  ...BACKENDS,
  backends: [...BACKENDS.backends, { id: 'orbit', label: 'Orbit Agent', is_global_default: false }],
  unroutable: BACKENDS.unroutable.filter(r => r.id !== 'orbit'),
}
let verifyDone = false
// What GET /api/backends answers: the fixture listing, an empty one (frames
// 09/11: the "no backends registered" states), or a 500 (frames 10/12: the
// "listing unavailable" states of the panel and the picker).
let LISTING_MODE = 'normal'
const EMPTY_BACKENDS = { backends: [], unroutable: [], invalid: [] }

const MODELS_BY_BACKEND = {
  '': [{ model_name: 'auto', description: 'Models chosen by task' }, { model_name: 'claude-opus-5', description: 'Claude Opus 5' }],
  acme: [{ model_name: 'acme-large', description: 'Acme Large' }, { model_name: 'acme-mini', description: 'Acme Mini' }],
}

const EMPTY_SLOT = 'chat-new'
const PINNED_SLOT = 'chat-pinned'
const UNPINNED_SLOT = 'chat-unpinned'

const SLOTS = [
  {
    key: EMPTY_SLOT, title: '', running: false, messages: 0, agent: 'kirocrew', model: 'auto',
    acp_backend: '', modified: now, last_ts: '2026-09-18T16:00:00Z', folder_id: '',
  },
  {
    key: PINNED_SLOT, title: 'Draft the release notes', running: false, messages: 2, agent: 'kirocrew',
    model: 'acme-large', acp_backend: 'acme', modified: now - 300, last_ts: '2026-09-18T15:55:00Z', folder_id: '',
    last_message: 'Here is a first draft of the notes.',
  },
  {
    key: UNPINNED_SLOT, title: 'Summarize the incident review', running: false, messages: 2, agent: 'kirocrew',
    model: 'auto', acp_backend: null, modified: now - 600, last_ts: '2026-09-18T15:50:00Z', folder_id: '',
    last_message: 'Three action items came out of the review.',
  },
]

const pinnedDetail = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', content: 'Draft the release notes for 0.8.', ts: '2026-09-18T15:54:00Z' },
    { role: 'assistant', content: 'Here is a first draft of the notes.', ts: '2026-09-18T15:55:00Z' },
  ],
}
const unpinnedDetail = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', content: 'Summarize the incident review.', ts: '2026-09-18T15:49:00Z' },
    { role: 'assistant', content: 'Three action items came out of the review.', ts: '2026-09-18T15:50:00Z' },
  ],
}
const emptyDetail = { running: false, has_more: false, total: 0, queue: [], messages: [] }

const extra = async (path, route) => {
  const url = new URL(route.request().url())
  if (path === '/api/backends/orbit/verify') {
    const answer = VERIFY_RESULTS[VERIFY_ANSWER]
    if (answer.verified) verifyDone = answer.policy_denied ? 'policy' : true
    return await json(route, answer), true
  }
  if (path === '/api/backends') {
    if (LISTING_MODE === 'error') {
      await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'boom' }) })
      return true
    }
    if (LISTING_MODE === 'empty') return await json(route, EMPTY_BACKENDS), true
    return await json(route, verifyDone === 'policy' ? POLICY_DENIED_BACKENDS : verifyDone ? VERIFIED_BACKENDS : BACKENDS), true
  }
  if (path === '/api/chat/slots') return await json(route, SLOTS), true
  if (path.endsWith('/backend') && route.request().method() === 'POST') {
    if (REFUSE_BACKEND_SWITCH) {
      return await json(route, { ok: false, error: "backend 'orbit' is not selectable: routing not yet verified end to end" }), true
    }
    const body = route.request().postDataJSON()
    return await json(route, { ok: true, backend: body?.backend ?? '' }), true
  }
  if (path.startsWith('/api/chat/slots/' + PINNED_SLOT)) return await json(route, pinnedDetail), true
  if (path.startsWith('/api/chat/slots/' + UNPINNED_SLOT)) return await json(route, unpinnedDetail), true
  if (path.startsWith('/api/chat/slots/')) return await json(route, emptyDetail), true
  if (path === '/api/models') {
    const b = url.searchParams.get('backend') || ''
    return await json(route, MODELS_BY_BACKEND[b] || MODELS_BY_BACKEND['']), true
  }
  if (path.startsWith('/api/agents/') && path.endsWith('/resolved-model')) return await json(route, { model: 'auto' }), true
  return false
}

async function newPage(browser, activeSlot) {
  const context = await browser.newContext({ viewport: { width: 1500, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme: 'dark',
    extra,
    localStorageEntries: {
      'mc-lang': 'en',
      'mc-active-slot': activeSlot,
      'mc-privacy-notice-v1': '1',
      'mc-sidebar-pinned': 'true',
    },
  })
  return { context, page }
}

async function shot(target, name) {
  const out = join(OUT, name)
  await target.screenshot({ path: out })
  console.log('wrote', out)
}

async function captureSettings(browser, base) {
  const { context, page } = await newPage(browser, PINNED_SLOT)
  await page.goto(base + '/settings/backends', { waitUntil: 'domcontentloaded' })
  await page.getByText('Acme Agent', { exact: true }).first().waitFor({ state: 'visible', timeout: 20000 })
  await page.getByText('Shadow Agent', { exact: true }).first().waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(500)
  await shot(page, '01-settings-backends.png')
  await context.close()
}

async function captureVerifyFlow(browser, base) {
  // Frame 6: a routed descriptor whose routing is not yet verified -- the row
  // carries the "Routing not verified" badge and the one action the panel adds.
  verifyDone = false
  VERIFY_ANSWER = 'violation'
  let { context, page } = await newPage(browser, PINNED_SLOT)
  await page.goto(base + '/settings/backends', { waitUntil: 'domcontentloaded' })
  const btn = page.getByTestId('backend-verify-orbit')
  await btn.waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(400)
  await shot(page, '06-settings-backend-unverified.png')
  // Frame 8 (same page): the failed outcome -- the probe file was written
  // despite denial -- rendered under the row through ErrorNotice.
  await btn.click()
  await page.getByTestId('backend-verify-outcome-orbit').waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(400)
  await shot(page, '08-settings-backend-verify-failed.png')
  await context.close()

  // Frame 7: the verified outcome. The row keeps its place in the list; its
  // badge goes and the success line appears under it (the listing refetches,
  // Orbit is now selectable, and nothing on screen jumps).
  VERIFY_ANSWER = 'verified'
  ;({ context, page } = await newPage(browser, PINNED_SLOT))
  await page.goto(base + '/settings/backends', { waitUntil: 'domcontentloaded' })
  await page.getByTestId('backend-verify-orbit').waitFor({ state: 'visible', timeout: 20000 })
  await page.getByTestId('backend-verify-orbit').click()
  await page.getByTestId('backend-row-orbit').waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(400)
  await shot(page, '07-settings-backend-verified.png')
  await context.close()
  verifyDone = false
}

async function captureVerifyRecording(browser, base) {
  // A frame sequence of the Verify success transition (assembled into a GIF by
  // the caller): before, pressed (verifying), and after -- the row does not
  // move, its badge flips and the success line appears in place.
  verifyDone = false
  VERIFY_ANSWER = 'verified'
  const { context, page } = await newPage(browser, PINNED_SLOT)
  await page.goto(base + '/settings/backends', { waitUntil: 'domcontentloaded' })
  const btn = page.getByTestId('backend-verify-orbit')
  await btn.waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(400)
  mkdirSync(join(OUT, 'verify-seq'), { recursive: true })
  await shot(page, 'verify-seq/00.png')
  await btn.hover()
  await page.waitForTimeout(150)
  await shot(page, 'verify-seq/01.png')
  await btn.click()
  await page.getByTestId('backend-row-orbit').waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(150)
  await shot(page, 'verify-seq/02.png')
  await page.waitForTimeout(400)
  await shot(page, 'verify-seq/03.png')
  await context.close()
  verifyDone = false
}

async function captureEmptyAndErrorStates(browser, base) {
  // Frames 9/10: the panel with nothing registered, and with a listing that
  // did not answer (ErrorNotice + Ask the agent). Frames 11/12: the same two
  // states of the welcome-screen picker.
  LISTING_MODE = 'empty'
  let { context, page } = await newPage(browser, PINNED_SLOT)
  await page.goto(base + '/settings/backends', { waitUntil: 'domcontentloaded' })
  await page.getByText('No backends registered', { exact: false }).first().waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(300)
  await shot(page, '09-settings-backends-empty.png')
  await context.close()

  LISTING_MODE = 'error'
  ;({ context, page } = await newPage(browser, PINNED_SLOT))
  await page.goto(base + '/settings/backends', { waitUntil: 'domcontentloaded' })
  await page.getByTestId('backends-panel-list-error').waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(300)
  await shot(page, '10-settings-backends-unavailable.png')
  await context.close()

  LISTING_MODE = 'empty'
  ;({ context, page } = await newPage(browser, EMPTY_SLOT))
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  let trigger = page.getByLabel(/^Backend: /).first()
  await trigger.waitFor({ state: 'visible', timeout: 20000 })
  await trigger.click()
  await page.getByText('No backends registered', { exact: false }).first().waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(300)
  await shot(page, '11-new-chat-backend-picker-empty.png')
  await context.close()

  LISTING_MODE = 'error'
  ;({ context, page } = await newPage(browser, EMPTY_SLOT))
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  trigger = page.getByLabel(/^Backend: /).first()
  await trigger.waitFor({ state: 'visible', timeout: 20000 })
  await trigger.click()
  await page.getByText('unavailable', { exact: false }).first().waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(300)
  await shot(page, '12-new-chat-backend-picker-unavailable.png')
  await context.close()
  LISTING_MODE = 'normal'
}

async function capturePicker(browser, base) {
  const { context, page } = await newPage(browser, EMPTY_SLOT)
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  const trigger = page.getByLabel(/^Backend: /).first()
  await trigger.waitFor({ state: 'visible', timeout: 20000 })
  await trigger.click()
  await page.getByRole('option', { name: /Acme Agent/ }).waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(400)
  await shot(page, '02-new-chat-backend-picker.png')
  // Frame 16: the same list scrolled to its end, where the diagnostic rows
  // live -- the invalid entry under its position id with each reason through
  // ErrorNotice (Ask the agent attached), and the unroutable entry's reason.
  await page.getByRole('listbox').evaluate(el => { el.scrollTop = el.scrollHeight })
  await page.waitForTimeout(300)
  await shot(page, '16-new-chat-backend-picker-diagnostics.png')
  await context.close()
}

async function captureInheritedChip(browser, base) {
  // Frame 5: an UNPINNED chat's chip, on a chat that has messages (the welcome
  // surface shows no chip: the picker there is the control). It follows the
  // configured default, so it carries no lock glyph and its click-to-explain
  // caption says so -- the counterpart to frame 4's pinned explanation.
  const { context, page } = await newPage(browser, UNPINNED_SLOT)
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  const row = page.getByText('Summarize the incident review', { exact: true }).first()
  await row.waitFor({ state: 'visible', timeout: 20000 })
  await row.click()
  const chip = page.getByTestId('chat-input-backend-chip')
  await chip.waitFor({ state: 'visible', timeout: 20000 })
  await chip.click()
  await page.getByTestId('chat-input-backend-chip-explained').waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(300)
  await shot(page, '05-chat-backend-chip-inherited.png')
  await context.close()
}

async function captureRefusedPick(browser, base) {
  // Frame 13: the welcome picker's pick is refused by the server; the failure
  // renders under the picker through ErrorNotice (with Ask the agent), never
  // the hand-written switch toast. No chip is shown on this surface.
  REFUSE_BACKEND_SWITCH = true
  const { context, page } = await newPage(browser, EMPTY_SLOT)
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  const trigger = page.getByLabel(/^Backend: /).first()
  await trigger.waitFor({ state: 'visible', timeout: 20000 })
  await trigger.click()
  await page.getByRole('option', { name: /Acme Agent/ }).click()
  await page.getByTestId('welcome-backend-switch-error').waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(400)
  await shot(page, '13-new-chat-backend-pick-refused.png')
  await context.close()
  REFUSE_BACKEND_SWITCH = false
}

async function capturePolicyDenied(browser, base) {
  // Frame 15: the probe VERIFIED the routing, but the deployment's agent_backend
  // policy denies the backend: the outcome names the policy (never "can now be
  // picked"), the row stays Not selectable, and no Verify action is offered.
  verifyDone = false
  VERIFY_ANSWER = 'policy_denied'
  const { context, page } = await newPage(browser, PINNED_SLOT)
  await page.goto(base + '/settings/backends', { waitUntil: 'domcontentloaded' })
  const btn = page.getByTestId('backend-verify-orbit')
  await btn.waitFor({ state: 'visible', timeout: 20000 })
  await btn.click()
  await page.getByTestId('backend-verify-outcome-orbit').waitFor({ state: 'visible', timeout: 20000 })
  await page.getByTestId('backend-verify-orbit').waitFor({ state: 'hidden', timeout: 20000 })
  await page.waitForTimeout(400)
  await shot(page, '15-settings-backend-verify-policy-denied.png')
  await context.close()
  verifyDone = false
  VERIFY_ANSWER = 'verified'
}

async function captureInconclusive(browser, base) {
  // Frame 14: a probe that proved nothing (the host asked about other things
  // and never attempted the write): the inconclusive outcome under the row; the
  // backend stays unverified with its Verify action.
  verifyDone = false
  VERIFY_ANSWER = 'inconclusive'
  const { context, page } = await newPage(browser, PINNED_SLOT)
  await page.goto(base + '/settings/backends', { waitUntil: 'domcontentloaded' })
  const btn = page.getByTestId('backend-verify-orbit')
  await btn.waitFor({ state: 'visible', timeout: 20000 })
  await btn.click()
  await page.getByTestId('backend-verify-outcome-orbit').waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(400)
  await shot(page, '14-settings-backend-verify-inconclusive.png')
  await context.close()
  VERIFY_ANSWER = 'verified'
}

async function captureChip(browser, base) {
  const { context, page } = await newPage(browser, PINNED_SLOT)
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  // Open the pinned chat from the session list: the stored active-slot key is
  // not authoritative on a cold load, and the transcript preview in the list
  // would satisfy a bare text wait without the chat ever being opened.
  const row = page.getByText('Draft the release notes', { exact: true }).first()
  await row.waitFor({ state: 'visible', timeout: 20000 })
  await row.click()
  const chip = page.getByTestId('chat-input-backend-chip')
  await chip.waitFor({ state: 'visible', timeout: 20000 })
  // The assertion the frame exists for: the chip names the PINNED backend.
  await chip.getByText('Backend: Acme Agent', { exact: true }).waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(500)
  await shot(page, '03-chat-backend-chip.png')
  // Frame 4: the chip answers a click by explaining itself inline (it opens no
  // picker -- the binding is fixed at creation), so the expanded state is a
  // surface of its own.
  await chip.click()
  await page.getByTestId('chat-input-backend-chip-explained').waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(300)
  await shot(page, '04-chat-backend-chip-explained.png')
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    await captureSettings(browser, base)
    await captureVerifyFlow(browser, base)
    await captureVerifyRecording(browser, base)
    await captureEmptyAndErrorStates(browser, base)
    await capturePicker(browser, base)
    await captureChip(browser, base)
    await captureInheritedChip(browser, base)
    await captureRefusedPick(browser, base)
    await captureInconclusive(browser, base)
    await capturePolicyDenied(browser, base)
  } finally {
    await browser.close()
    srv.close()
  }
}

await main()
