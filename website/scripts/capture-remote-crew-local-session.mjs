/**
 * Screenshot harness for "a local session bound to a remote crew as executor".
 *
 * Two surfaces, because the feature is a pair: the entry point that creates the
 * binding, and the marker that tells you a session HAS one.
 *
 *  1. `new-chat-on-crew` — the create-caret submenu listing this machine's
 *     connected crews. Picking one creates a session that stays in THIS list
 *     instead of switching to that crew's iframe pane, which is the behaviour
 *     change the feature exists for; the menu is where a user meets it.
 *  2. The sidebar chip on a bound row — `RemoteCrewChip`, info-tinted with a
 *     `Server` glyph, first in the chip strip because it qualifies the whole
 *     row. Shot beside an ordinary local row and beside a row carrying the
 *     other chips, since the only claim worth verifying is that a reader can
 *     tell the two kinds of row apart at a glance.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures — no gateway, no
 * kiro-cli, and deliberately no peer: the chip renders off the `executor` and
 * `instance_id` fields of the session projection plus the crew roster, so the
 * at-rest surface is reproducible on a machine with no reachable crew. What
 * this therefore does NOT show is a relayed turn streaming back; that needs two
 * hosts on the same build.
 *
 * The preview flag is seeded because the whole surface is gated on it — without
 * it the submenu does not render and the frame would show the unflagged menu.
 *
 * Usage: npm run build && node scripts/capture-remote-crew-local-session.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/remote-crew-local-session'
mkdirSync(OUT, { recursive: true })

/** Mirrors `PREVIEW_FLAG_PREFIX + 'remote-crew-chat'` in utils/previewFlags.ts. */
const PREVIEW_REMOTE_CREW_CHAT = 'mc-preview-remote-crew-chat'

const crew = (id, name, sshHost, port) => ({
  id, name, ssh_host: sshHost, remote_port: 7777, local_port: port, ttl: '20h',
  remote_bin: '', connection_method: 'ssh', ssm_target: '', ssm_run_as: '',
  aws_profile: '', aws_region: '', was_connected: false,
  status: { instance_id: id, state: 'connected', local_port: port, remote_port: 7777 },
})

const CREWS = [
  crew('nobita', 'nobita', 'nobita-alias', 7801),
  crew('shizuka', 'shizuka', 'shizuka-alias', 7802),
  crew('gian', 'gian-eu-west-1', 'gian-euw1-alias', 7803),
]
const SSO = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }

const slot = (key, title, extra = {}) => ({
  key, title, messages: 8, running: false, agent: 'kirocrew', mode: '',
  memory_mode: 'persistent', folder_id: '', last_message: '',
  source_links: [], source_links_total: 0,
  created: '2026-09-01T01:00:00Z', last_ts: '2026-09-03T20:00:00Z',
  modified: Math.floor(Date.now() / 1000),
  executor: 'local', ...extra,
})

const SLOTS = [
  // The bound row. `executor: 'remote'` is what the row reads to decide whether
  // to render the chip; `instance_id` is what resolves its name from the roster.
  slot('s-remote', 'Rebuild the index on the big box',
    { executor: 'remote', instance_id: 'nobita', last_ts: '2026-09-03T21:00:00Z' }),
  // A bound row whose crew name is long enough to hit the chip's 7rem cap, so
  // the frame also shows the truncation rather than only the happy width.
  slot('s-remote-long', 'Profile the ingest path',
    { executor: 'remote', instance_id: 'gian', last_ts: '2026-09-03T20:40:00Z' }),
  // Ordinary local rows for contrast, including one carrying other chips so the
  // runs-elsewhere marker is seen competing for the same strip.
  slot('s-local', 'Wire the settings search', { last_ts: '2026-09-03T20:20:00Z' }),
  slot('s-local-chips', 'Draft the migration note',
    { memory_mode: 'incognito', mode: 'orchestrator', last_ts: '2026-09-03T19:00:00Z' }),
  slot('s-local-2', 'Fix the lockfile floor', { last_ts: '2026-09-03T18:00:00Z' }),
]

/**
 * The peer's roster reply. Every field the type declares is present, because a
 * partial payload here is not a smaller fixture — the shelf reads the rosters
 * through it, so an absent `agents` or `models` crashes the pane and the frame
 * would show an error boundary instead of the surface.
 */
const capabilities = (id) => ({
  instance_id: id,
  version: '0.5.0',
  local_version: '0.5.0',
  version_match: true,
  agents: [
    { name: 'kirocrew', description: "The crew's default agent", scope: 'global', model: 'auto' },
    { name: 'indexer', description: 'Owns the search index', scope: 'global', model: 'auto' },
  ],
  default_agent: 'kirocrew',
  models: [
    { model_name: 'auto', display_name: 'Auto', description: 'Let the crew choose', context_window: 0 },
    { model_name: 'opus', display_name: 'Opus 4.8', description: '', context_window: 200000 },
  ],
  effort_levels: ['low', 'medium', 'high'],
  workspaces: [{ name: 'kirocrew', path: '/home/crew/kirocrew' }],
  default_workspace: '/home/crew/kirocrew',
  unavailable: {},
})

const extra = async (path, route) => {
  if (path === '/api/instances') {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ active: true, instances: CREWS, warm_set_cap: 5, sso: SSO }),
    })
    return true
  }
  const caps = /^\/api\/instances\/([^/]+)\/capabilities$/.exec(path)
  if (caps) {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(capabilities(decodeURIComponent(caps[1]))),
    })
    return true
  }
  const tunnel = /^\/api\/instances\/([^/]+)\/(connect|refresh-token|status)$/.exec(path)
  if (tunnel) {
    const found = CREWS.find((c) => c.id === decodeURIComponent(tunnel[1]))
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ...(found ? found.status : { state: 'connected' }), token: 'stub-token' }),
    })
    return true
  }
  return false
}

let failures = 0
const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1400, height: 900 },
  deviceScaleFactor: 2, // 10px chip type renders soft at 1x on GitHub
})
const page = await context.newPage()

// The crew panes are iframes pointed at a tunnel port that does not exist here.
await page.route(/127\.0\.0\.1:78\d\d/, (route) =>
  route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>pane</title>' }))
await stubDashboardApi(page, {
  slots: SLOTS,
  extra,
  localStorageEntries: { [PREVIEW_REMOTE_CREW_CHAT]: '1' },
})
logPageProblems(page)

await page.goto(`${base}/chat`, { waitUntil: 'domcontentloaded' })
await page.waitForSelector('[data-testid="remote-crew-chip"]', { timeout: 20000 })
await page.waitForTimeout(600)

// ---- Frame 1: the sidebar, chip visible on the bound rows -------------------

const chips = page.locator('[data-testid="remote-crew-chip"]')
const chipCount = await chips.count()
if (chipCount !== 2) {
  fail(`expected the chip on exactly the 2 rows whose executor is "remote", saw ${chipCount}`)
}
const chipNames = (await chips.allInnerTexts()).map((s) => s.trim())
console.log('CHIP LABELS', JSON.stringify(chipNames))
// The name comes from the roster, not from `instance_id` — a chip reading
// "gian" instead of "gian-eu-west-1" would mean the lookup silently fell back.
if (chipNames[0] !== 'nobita') {
  fail(`the first chip reads ${JSON.stringify(chipNames[0])}, not the crew's roster name "nobita"`)
}

// The row is a fixed height with the timestamp pinned at its end; an unclamped
// chip pushes the timestamp out. Assert the chip stayed inside its cap and that
// nothing on the row overflows it.
const geom = await page.evaluate(() => {
  const chip = document.querySelector('[data-testid="remote-crew-chip"]')
  const row = chip.closest('[data-slot-key]')
  const cr = chip.getBoundingClientRect()
  const rr = row.getBoundingClientRect()
  return {
    chipWidth: +cr.width.toFixed(1),
    capPx: parseFloat(getComputedStyle(chip).maxWidth),
    rowWidth: +rr.width.toFixed(1),
    chipRightOverflow: +(cr.right - rr.right).toFixed(1),
    rowScrollOverflow: row.scrollWidth - row.clientWidth,
  }
})
console.log('CHIP GEOMETRY', JSON.stringify(geom))
if (geom.chipWidth > geom.capPx + 0.5) {
  fail(`the chip is ${geom.chipWidth}px wide, past its own ${geom.capPx}px cap`)
}
if (geom.chipRightOverflow > 0) {
  fail(`the chip paints ${geom.chipRightOverflow}px past the row's right edge`)
}
if (geom.rowScrollOverflow > 1) {
  fail(`the row overflows by ${geom.rowScrollOverflow}px with the chip present`)
}

// The session list has no single addressable wrapper, so the clip is the union
// of the rows themselves — which is the surface under review anyway, and keeps
// the 10px chip type legible instead of shrinking it inside a full-window frame.
const sidebar = await page.evaluate(() => {
  const rows = [...document.querySelectorAll('[data-slot-key]')]
  const rects = rows.map((k) => k.getBoundingClientRect()).filter((r) => r.width && r.height)
  return {
    x: Math.min(...rects.map((r) => r.x)),
    y: Math.min(...rects.map((r) => r.y)),
    right: Math.max(...rects.map((r) => r.right)),
    bottom: Math.max(...rects.map((r) => r.bottom)),
  }
})
const pad = 10
const clip = {
  x: Math.max(0, sidebar.x - pad),
  y: Math.max(0, sidebar.y - pad),
  width: Math.min(1400, sidebar.right + pad) - Math.max(0, sidebar.x - pad),
  height: Math.min(900, sidebar.bottom + pad) - Math.max(0, sidebar.y - pad),
}
console.log('SIDEBAR CLIP', JSON.stringify(clip))
await page.screenshot({ path: `${OUT}/01-sidebar-bound-session-chip.png`, clip })
console.log('wrote', `${OUT}/01-sidebar-bound-session-chip.png`)

// ---- Frame 2: the "New chat on crew" submenu --------------------------------

const caret = page.locator('button[aria-label="More create options"]').first()
await caret.waitFor({ state: 'visible', timeout: 15000 })
await caret.click()
const menu = page.locator('[role="menu"]').first()
await menu.waitFor({ state: 'visible', timeout: 10000 })

const trigger = page.locator('[data-testid="new-chat-on-crew"]').first()
await trigger.waitFor({ state: 'visible', timeout: 10000 })
await trigger.hover()
// Radix portals the submenu to <body>; wait for one of its rows, not the trigger.
const crewItem = page.locator('[data-testid^="new-chat-on-crew-"]').first()
await crewItem.waitFor({ state: 'visible', timeout: 10000 })
await page.waitForTimeout(400) // let the open animation settle

const offered = (await page.locator('[data-testid^="new-chat-on-crew-"]').allInnerTexts())
  .map((s) => s.trim())
console.log('CREWS OFFERED', JSON.stringify(offered))
if (offered.length !== CREWS.length) {
  fail(`the submenu offers ${offered.length} crews, not the ${CREWS.length} connected ones`)
}

const boxes = await Promise.all(
  ['[data-create-menu]', '[role="menu"]'].map((sel) =>
    page.locator(sel).first().boundingBox()),
)
const subBox = await page.locator('[data-testid^="new-chat-on-crew-"]').first()
  .evaluate((el) => {
    const r = (el.closest('[role="menu"]') || el).getBoundingClientRect()
    return { x: r.x, y: r.y, width: r.width, height: r.height }
  })
const all = [...boxes.filter(Boolean), subBox]
const x0 = Math.max(0, Math.min(...all.map((b) => b.x)) - 18)
const y0 = Math.max(0, Math.min(...all.map((b) => b.y)) - 18)
const x1 = Math.min(1400, Math.max(...all.map((b) => b.x + b.width)) + 18)
const y1 = Math.min(900, Math.max(...all.map((b) => b.y + b.height)) + 18)
await page.screenshot({
  path: `${OUT}/02-new-chat-on-crew-menu.png`,
  clip: { x: x0, y: y0, width: x1 - x0, height: y1 - y0 },
})
console.log('wrote', `${OUT}/02-new-chat-on-crew-menu.png`)

await page.close()
await context.close()
await browser.close()
srv.close()

if (failures) {
  console.error(`\n${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('\nALL GREEN')
