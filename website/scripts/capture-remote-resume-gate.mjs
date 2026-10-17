/**
 * Screenshot + assertion harness for the Resume gate on a CREW-BOUND session.
 *
 * The defect is an offer that cannot be honoured. `api_chat_slot_continue` runs
 * `remote_bound_refusal` ahead of every other guard and answers 409
 * `remote_action_unsupported` for `executor == "remote"`, because the synthetic
 * turn Continue queues would dispatch on THIS machine and diverge from the
 * peer's transcript. The client offered Resume anyway — and the one path that
 * manufactures the state is `relay_remote_turn`'s failure handler, which appends
 * a trailing `error` row, exactly the shape `selectTurnInterrupted` reads. So a
 * tunnel that dropped mid-stream always landed on a screen whose only offered
 * remedy was guaranteed to fail.
 *
 * Both fixtures carry the SAME interrupted transcript and differ only in
 * `executor`. That pairing is what makes this evidence: the crew-bound slot must
 * offer no Resume, and the local slot must still offer one. A lone frame showing
 * an absent button proves nothing — absence is also what a broken fixture, a
 * failed boot or a mis-seeded active slot looks like, so the local frame is the
 * control that rules those out.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures — no gateway, no peer,
 * no kiro-cli. Both guards read `executor` off the slot projection, so the whole
 * surface is reproducible on a machine with no reachable crew. What this
 * therefore does NOT show is a real relayed turn dying mid-stream; that needs
 * two hosts on the same build.
 *
 * Frames:
 *   01-crew-bound-no-resume        bound + interrupted: marker kept, no Resume
 *   02-local-resume-offered        same transcript, local: Resume still offered
 *   03-sidebar-rows                both rows: only the local one names Resume
 *   04-crew-bound-no-resume-light  light-theme parity on the frame under review
 *
 * Usage: npm run build && node scripts/capture-remote-resume-gate.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/remote-resume-gate'
mkdirSync(OUT, { recursive: true })

/** Mirrors `PREVIEW_FLAG_PREFIX + 'remote-crew-chat'` in utils/previewFlags.ts. */
const PREVIEW_REMOTE_CREW_CHAT = 'mc-preview-remote-crew-chat'

const BOUND = 'chat-bound'
const LOCAL = 'chat-local'
const PROJECT = '/home/user/workspace/KiroCrew'

/** en.json — read from the catalog rather than paraphrased, so a copy change
 *  fails this harness instead of silently passing against stale wording. */
const RESUME_WORD = 'Resume'
const TURN_INTERRUPTED = 'Turn interrupted'
const PRESS_RESUME = 'Last turn was interrupted — press Resume, or just type'

const now = () => Date.now() / 1000
const nowSec = Math.floor(Date.now() / 1000)
const ago = (mins) => new Date((nowSec - mins * 60) * 1000).toISOString()

const slot = (key, title, extra = {}) => ({
  key, title, running: false, messages: 3, agent: 'kirocrew', mode: '',
  memory_mode: 'persistent', folder_id: '', last_message: '', project: PROJECT,
  source_links: [], source_links_total: 0,
  created: '2026-09-13T01:00:00Z', modified: nowSec,
  executor: 'local', ...extra,
})

const SLOTS = [
  // The crew-bound row: `executor` is the field BOTH guards key on, and the one
  // the server's own refusal keys on. `instance_id` resolves the crew chip name.
  slot(BOUND, 'Rebuild the search index on the big box', {
    interrupted: true, executor: 'remote', instance_id: 'nobita',
    last_ts: ago(6), last_turn_ts: ago(6),
  }),
  // The control: identical interrupted transcript, ordinary local execution.
  slot(LOCAL, 'Wire the settings search', {
    interrupted: true, last_ts: ago(9), last_turn_ts: ago(9),
  }),
]

/** The relay-failure shape: a turn that began streaming and stopped on an error
 *  row. This is what `relay_remote_turn`'s failure handler leaves behind, and
 *  what the client reads as an interruption. */
const interruptedDetail = {
  running: false,
  has_more: false,
  total: 3,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: now() - 600, content: 'Rebuild the search index and report the row count.' },
    {
      role: 'assistant',
      ts: now() - 300,
      content: 'Reading the current index manifest first — the row count comes from its footer, so the rebuild can be checked against it.',
    },
    {
      role: 'error',
      ts: now() - 280,
      content: 'The crew running this session stopped responding. The turn may still be running there.',
      cls: 'msg msg-err',
    },
  ],
}

const crew = (id, name, port) => ({
  id, name, ssh_host: `${id}-alias`, remote_port: 7777, local_port: port, ttl: '20h',
  remote_bin: '', connection_method: 'ssh', ssm_target: '', ssm_run_as: '',
  aws_profile: '', aws_region: '', was_connected: false,
  status: { instance_id: id, state: 'connected', local_port: port, remote_port: 7777 },
})
const CREWS = [crew('nobita', 'nobita', 7801)]
const SSO = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }

/**
 * The peer's roster reply. Every field the type declares is present, because a
 * partial payload here is not a smaller fixture: the crew shelf reads the roster
 * through it, so an absent `agents` or `models` throws inside a useMemo and the
 * frame captures an ErrorBoundary instead of the surface under review.
 */
const capabilities = (id) => ({
  instance_id: id,
  version: '0.5.0',
  local_version: '0.5.0',
  version_match: true,
  agents: [{ name: 'kirocrew', description: "The crew's default agent", scope: 'global', model: 'auto' }],
  default_agent: 'kirocrew',
  models: [{ model_name: 'auto', display_name: 'Auto', description: 'Let the crew choose', context_window: 0 }],
  effort_levels: ['low', 'medium', 'high'],
  workspaces: [{ name: 'kirocrew', path: '/workspace/kirocrew' }],
  default_workspace: '/workspace/kirocrew',
  unavailable: {},
})

let failures = 0
const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }
const ok = (msg) => console.log(`  ok  ${msg}`)

const extra = async (path, route) => {
  if (path.startsWith('/api/chat/slots/')) { json(route, interruptedDetail); return true }
  if (path === '/api/instances') {
    json(route, { active: true, instances: CREWS, warm_set_cap: 5, sso: SSO })
    return true
  }
  const caps = /^\/api\/instances\/([^/]+)\/capabilities$/.exec(path)
  if (caps) { json(route, capabilities(decodeURIComponent(caps[1]))); return true }
  const tunnel = /^\/api\/instances\/([^/]+)\/(connect|refresh-token|status)$/.exec(path)
  if (tunnel) {
    const found = CREWS.find((c) => c.id === decodeURIComponent(tunnel[1]))
    json(route, { ...(found ? found.status : { state: 'connected' }), token: 'stub-token' })
    return true
  }
  if (path === '/api/recent-projects') { json(route, { dirs: [PROJECT] }); return true }
  if (path === '/api/chat/nav/resolve-links') { json(route, { summaries: [] }); return true }
  return false
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
// Dense 12–13px composer type; a 1x shot renders it soft on GitHub. Every frame
// is clipped, so the 2x render stays inside the per-edge ceiling.
const context = await browser.newContext({
  viewport: { width: 1500, height: 950 },
  deviceScaleFactor: 2,
})

let page = null

async function load(activeSlot, theme = 'dark') {
  if (page) await page.close()
  page = await context.newPage()
  logPageProblems(page)
  // The crew pane is an iframe pointed at a tunnel port that does not exist
  // here; served locally it is also same-origin, so init scripts stop throwing
  // SecurityError inside it.
  await page.route(/127\.0\.0\.1:78\d\d/, (route) =>
    route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>pane</title>' }))
  // Every storage seed goes through `localStorageEntries`, NOT a second
  // addInitScript: Playwright does not order separately registered init
  // scripts, so a local seed races this stub's own `localStorage.clear()`. A
  // lost `mc-active-slot` would silently point the capture at the wrong slot
  // and every assertion below would be measuring the wrong session. The stub
  // seeds `mc-theme` and `mc-onboarded` itself, and swallows /api/ws.
  await stubDashboardApi(page, {
    slots: SLOTS,
    theme,
    extra,
    localStorageEntries: {
      [PREVIEW_REMOTE_CREW_CHAT]: '1',
      'mc-active-slot': activeSlot,
      'mc-privacy-notice-v1': '1',
      'mc-sidebar-pinned': 'true',
    },
  })
  await page.goto(`${base}/chat`, { waitUntil: 'domcontentloaded' })

  // Select the slot by CLICKING its row rather than trusting the `mc-active-slot`
  // seed. The crew-pane iframe is a different origin, so an init script touching
  // localStorage there throws SecurityError — and when that seed is silently
  // lost the dashboard just auto-selects its newest row instead. That failure is
  // invisible in a screenshot: both scenarios then render the SAME slot and the
  // crew-bound frame "passes" for the wrong reason. A click cannot be lost, and
  // the assertion below proves which row the frame is actually showing.
  const row = page.locator(`[data-session-row="${activeSlot}"]`)
  await row.waitFor({ state: 'visible', timeout: 20000 })
  await row.click()
  await page.locator('[data-testid="error-card"]').first()
    .waitFor({ state: 'visible', timeout: 20000 })
  const activeKey = await page.evaluate(() =>
    document.querySelector('.session-row.session-active')?.getAttribute('data-session-row') || '')
  if (activeKey !== activeSlot) {
    throw new Error(`the active row is ${JSON.stringify(activeKey)}, not the intended `
      + `${JSON.stringify(activeSlot)} — every assertion below would measure the wrong session`)
  }
  await page.waitForTimeout(700)
}

/** Read every Resume affordance in one pass, so a frame and its assertions can
 *  never disagree about what was on screen. */
async function resumeSurfaces() {
  const composer = page.locator('[data-testid="composer-continue"]')
  const cardAction = page.locator('[data-testid="error-card-continue"]')
  return {
    composerButton: await composer.count(),
    cardAction: await cardAction.count(),
    errorCard: await page.locator('[data-testid="error-card"]').count(),
    placeholder: (await page.locator('textarea').first().getAttribute('placeholder')) || '',
  }
}

/** Tight crop over the transcript tail + composer — the whole story in one band. */
async function band(name) {
  const composer = page.locator('textarea').first()
  const box = await composer.boundingBox()
  if (!box) { await page.screenshot({ path: `${OUT}/${name}.png` }); return }
  const rows = page.locator('[data-testid="error-card"], .message-bubble')
  let top = Math.max(0, box.y - 340)
  const count = await rows.count()
  if (count) {
    const first = await rows.nth(0).boundingBox()
    if (first) top = Math.max(0, Math.min(top, first.y - 12))
    const last = await rows.nth(count - 1).boundingBox()
    if (last) top = Math.min(top, Math.max(0, last.y - 12))
  }
  await page.screenshot({
    path: `${OUT}/${name}.png`,
    clip: {
      x: Math.max(0, box.x - 30),
      y: top,
      width: Math.min(1180, box.width + 60),
      height: box.y + box.height + 60 - top,
    },
  })
  console.log('wrote', `${OUT}/${name}.png`)
}

// ---- Frame 1: crew-bound — the interruption is reported, Resume is not -------

await load(BOUND)
const bound = await resumeSurfaces()
console.log('CREW-BOUND SURFACES', JSON.stringify(bound))
if (!bound.errorCard) {
  fail('the crew-bound fixture rendered no error card — the interrupted shape never loaded, '
    + 'so an absent Resume would prove nothing')
} else {
  ok('the interruption is still reported (error card present)')
}
if (bound.composerButton) fail('the composer still offers Resume on a crew-bound slot')
else ok('composer offers no Resume')
if (bound.cardAction) fail('the error card still offers Resume on a crew-bound slot')
else ok('error card offers no Resume action')
if (bound.placeholder.includes(RESUME_WORD)) {
  fail(`the composer placeholder still names Resume on a crew-bound slot: ${JSON.stringify(bound.placeholder)}`)
} else {
  ok(`placeholder does not name Resume (${JSON.stringify(bound.placeholder.slice(0, 60))})`)
}
await band('01-crew-bound-no-resume')

// ---- Frame 2: the control — same transcript, local slot, Resume offered ------

await load(LOCAL)
const local = await resumeSurfaces()
console.log('LOCAL SURFACES', JSON.stringify(local))
if (!local.composerButton) {
  fail('the LOCAL slot lost its Resume button — the guard is over-broad, or the fixture never '
    + 'became continuable, which would also void frame 1')
} else {
  ok('composer still offers Resume on a local slot')
}
if (!local.cardAction) fail('the LOCAL error card lost its Resume action')
else ok('error card still offers its Resume action on a local slot')
if (local.placeholder !== PRESS_RESUME) {
  fail(`the LOCAL placeholder reads ${JSON.stringify(local.placeholder)}, not the press-Resume copy`)
} else {
  ok('placeholder still names Resume on a local slot')
}
await band('02-local-resume-offered')

// ---- Frame 3: the sidebar — only the local row names Resume -----------------

await load(LOCAL)
const rowText = async (key) => {
  const row = page.locator(`[data-session-row="${key}"]`)
  await row.first().waitFor({ state: 'visible', timeout: 15000 })
  return (await row.first().innerText()).replace(/\s+/g, ' ').trim()
}
const boundRow = await rowText(BOUND)
const localRow = await rowText(LOCAL)
console.log('BOUND ROW ', JSON.stringify(boundRow))
console.log('LOCAL ROW ', JSON.stringify(localRow))
if (!boundRow.includes(TURN_INTERRUPTED)) {
  fail('the crew-bound row dropped the interruption marker as well as the instruction')
} else {
  ok('crew-bound row keeps the interruption marker')
}
if (boundRow.includes(RESUME_WORD)) {
  fail('the crew-bound row still instructs the user to press Resume')
} else {
  ok('crew-bound row drops the Resume instruction')
}
if (!localRow.includes(`${TURN_INTERRUPTED} · ${RESUME_WORD}`)) {
  fail(`the local row lost its "${TURN_INTERRUPTED} · ${RESUME_WORD}" label: ${JSON.stringify(localRow)}`)
} else {
  ok('local row keeps the full marker + instruction')
}

const sidebar = await page.evaluate(() => {
  const rows = [...document.querySelectorAll('[data-session-row]')]
  const rects = rows.map((r) => r.getBoundingClientRect()).filter((r) => r.width && r.height)
  return {
    x: Math.min(...rects.map((r) => r.x)),
    y: Math.min(...rects.map((r) => r.y)),
    right: Math.max(...rects.map((r) => r.right)),
    bottom: Math.max(...rects.map((r) => r.bottom)),
  }
})
// Park the pointer clear of the list first: the click that selected the slot
// leaves a row hovered, and its hover toolbar (menu / duplicate / close) paints
// OVER the row's title and status line — the very text this frame exists to show.
await page.mouse.move(1400, 900)
await page.waitForTimeout(400)
const pad = 10
await page.screenshot({
  path: `${OUT}/03-sidebar-rows.png`,
  clip: {
    x: Math.max(0, sidebar.x - pad),
    y: Math.max(0, sidebar.y - pad),
    width: Math.min(1500, sidebar.right + pad) - Math.max(0, sidebar.x - pad),
    height: Math.min(950, sidebar.bottom + pad) - Math.max(0, sidebar.y - pad),
  },
})
console.log('wrote', `${OUT}/03-sidebar-rows.png`)

// ---- Frame 4: light-theme parity on the frame under review -----------------

await load(BOUND, 'light')
const boundLight = await resumeSurfaces()
if (boundLight.composerButton || boundLight.cardAction) {
  fail('light theme still offers Resume on a crew-bound slot')
} else {
  ok('light theme offers no Resume either')
}
await band('04-crew-bound-no-resume-light')

await page.close()
await context.close()
await browser.close()
srv.close()

if (failures) {
  console.error(`\n${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('\nALL GREEN')
