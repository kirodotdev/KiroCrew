/**
 * Screenshot harness for every surface whose left column tracks the sidebar
 * row pad (`ROW_BOX_CLS`) or the folder body inset, beyond the list-view folder
 * header that capture-folder-glyph.mjs already covers:
 *
 *   01-list-rows      list view: pinned divider, row dividers, dormant toggle,
 *                     empty-folder "New chat", hidden-folders reveal (root and
 *                     inside a folder), nested folder bodies
 *   02-flat-view      flat view: date headers plus the pinned divider
 *   03-board          board view: folder bodies nested inside a column
 *   04-members        Crewmates page grouped by team: indented crewmate rows,
 *                     at the default roster width (264) and a wide one (400),
 *                     with one long team name -- it must read whole at both
 *
 * Runs the REAL built SPA behind the shared static server, gateway-free. Point
 * it at two dists (origin/main and the branch) and compare the frames.
 *
 * Usage: node scripts/capture-folder-indent-surfaces.mjs [outDir] [prefix] [distDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/folder-indent-surfaces'
const PREFIX = process.argv[3] || 'after'
const DIST = process.argv[4] || DEFAULT_DIST

mkdirSync(OUT, { recursive: true })

const NOW = Date.now()
const DAY = 24 * 3600 * 1000
const ago = (ms) => new Date(NOW - ms).toISOString()
const agoSec = (ms) => Math.floor((NOW - ms) / 1000)

const slot = (key, title, folder_id, ageMs, extra = {}) => ({
  key, title, messages: 4, running: false, agent: 'kirocrew',
  created: ago(ageMs + DAY), last_ts: ago(ageMs), modified: agoSec(ageMs),
  folder_id, last_message: '', tags: [], source_links: [], source_links_total: 0,
  ...extra,
})

// ── 01 list view ──────────────────────────────────────────────────────────────
// "Kiro" (expanded) holds: two pinned rows, two fresh rows, one dormant row
// (30 days old → the dormant toggle), a nested folder with a row, and a
// subfolder unchecked in the filter menu (→ "1 hidden folder" reveal inside
// the body). "Archive" is empty (→ "New chat in Archive"). The root lane has
// a pinned row, a fresh row, a dormant row and an unchecked root folder (→ the
// reveal row at the root). The filter's unchecked set is the local
// `mc-flat-hidden-folders` preference.
const listFolders = [
  { id: 'f1', name: 'Kiro', icon: '🚀', order: 0, collapsed: false },
  { id: 'f1a', name: 'Sidebar', order: 0, collapsed: false, parent_id: 'f1' },
  { id: 'f1h', name: 'Retired', order: 1, collapsed: true, parent_id: 'f1' },
  { id: 'f2', name: 'Archive', order: 1, collapsed: false },
  { id: 'f3', name: 'Old deals', order: 2, collapsed: true },
]
const listHiddenByFilter = ['f1h', 'f3']
const listSlots = [
  slot('p1', 'Release 0.8 checklist', 'f1', 5 * 3600e3, { pinned: true }),
  slot('p2', 'Folder geometry decision', 'f1', 6 * 3600e3, { pinned: true }),
  slot('s1', 'Replace collapse chevron', 'f1', 2 * 3600e3),
  slot('s2', 'Auto-update install flow', 'f1', 3 * 3600e3),
  slot('s3', 'Tips Kit analyzer', 'f1', 30 * DAY),
  slot('s9', 'Row alignment probe', 'f1a', 4 * 3600e3),
  slot('r1', 'Notification bridge RFC', '', 1 * 3600e3, { pinned: true }),
  slot('r2', 'Weixin QR render fix', '', 7 * 3600e3),
  slot('r3', 'Linux CDN links', '', 40 * DAY),
]

// ── 02 flat view ──────────────────────────────────────────────────────────────
const flatSlots = [
  slot('fp', 'Release 0.8 checklist', 'f1', 5 * 3600e3, { pinned: true }),
  slot('f1', 'Replace collapse chevron', 'f1', 2 * 3600e3),
  slot('f2', 'Auto-update install flow', '', 1 * DAY + 3600e3),
  slot('f3', 'Row alignment probe', 'f1a', 1 * DAY + 2 * 3600e3),
  slot('f4', 'Weixin QR render fix', '', 4 * DAY),
  slot('f5', 'Linux CDN links', '', 5 * DAY),
]

// ── 03 board view ─────────────────────────────────────────────────────────────
const tags = [
  { id: 'todo', name: 'ToDo', color: '#3b82f6', order: 0, status: true },
  { id: 'impl', name: 'Implementation', color: '#8b5cf6', order: 1, status: true },
]
const columns = [
  { id: 'col-todo', name: 'ToDo', tag_ids: ['todo'], mode: 'any', order: 0, include_untagged: false },
  { id: 'col-impl', name: 'Implementation', tag_ids: ['impl'], mode: 'any', order: 1, include_untagged: false },
]
const boardFolders = [
  { id: 'b1', name: 'Kiro', icon: '🚀', order: 0, collapsed: false },
  { id: 'b1a', name: 'Sidebar', order: 0, collapsed: false, parent_id: 'b1' },
  { id: 'b2', name: 'Design', order: 1, collapsed: false },
]
const boardSlots = [
  slot('t1', 'Triage inbox', '', 3600e3, { tags: ['todo'], pinned: true }),
  slot('t2', 'Plan sessions page', 'b1', 2 * 3600e3, { tags: ['todo'] }),
  slot('t3', 'Row alignment probe', 'b1a', 3 * 3600e3, { tags: ['todo'] }),
  slot('t4', 'Spec flat board', 'b2', 4 * 3600e3, { tags: ['todo'] }),
  slot('i1', 'Port claim matcher', 'b1', 5 * 3600e3, { tags: ['impl'] }),
  slot('i2', 'Wire flat lanes', 'b2', 6 * 3600e3, { tags: ['impl'] }),
]

// ── 04 members page ───────────────────────────────────────────────────────────
const member = (name, description, last_message, extra = {}) => ({
  name, slug: name.toLowerCase(), bound: true, slot_key: `member-${name.toLowerCase()}`, running: false,
  kiro_agent: 'kirocrew', workspace: 'default', memory_store: name.toLowerCase(), memory_version: 2, memory_owner: name,
  model: '', description, source: 'kirocrew', last_active_ts: agoSec(30 * 60e3), last_message, ...extra,
})
const members = [
  member('Radar', 'Watches CI and the issue queue', 'Back up since 08:22Z.', { running: true }),
  member('Fixer', 'Opens the fix PRs', 'Two PRs opened for the queue.'),
  member('Scout', 'Reads new issues first', 'Six new issues triaged.'),
  member('Scribe', 'Keeps the decisions log', 'Entry 7 recorded.'),
]
const teams = [
  { id: 'tm-ci', name: 'CI crew', members: ['Radar', 'Fixer'] },
  { id: 'tm-intake', name: 'Intake', members: ['Scout'] },
  // Two long names. The header's name cap must bite only under width pressure,
  // never beside an empty rule: "Docs and support" (~99px) fits the 264 roster
  // and must read whole there, where a 40% cap (80px) clipped it; "Platform
  // reliability engineering" (~175px) is wider than the 264 roster can give a
  // name and may truncate THERE only, reading whole at 400.
  { id: 'tm-docs', name: 'Docs and support', members: ['Scribe'] },
  { id: 'tm-platform', name: 'Platform reliability engineering', members: [] },
]

async function newPage(browser, { viewport, folders, slots, localStorageEntries, extra }) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, { folders, slots, localStorageEntries, extra })
  logPageProblems(page)
  return { context, page }
}

async function clipShot(page, name, clip) {
  await page.screenshot({ path: `${OUT}/${PREFIX}-${name}.png`, clip })
  console.log('wrote', `${OUT}/${PREFIX}-${name}.png`)
}

// Left edges (CSS px) of the surfaces this harness is about, so a before/after
// pair can be compared by number as well as by eye.
async function measure(page, probes) {
  const m = await page.evaluate((probes) => {
    const left = el => (el ? Math.round(el.getBoundingClientRect().left * 10) / 10 : null)
    const out = {}
    for (const [label, sel] of Object.entries(probes)) out[label] = left(document.querySelector(sel))
    return out
  }, probes)
  console.log('MEASURE', JSON.stringify(m))
}

async function must(page, selector, label) {
  const n = await page.locator(selector).count()
  if (n === 0) throw new Error(`${label}: expected ${selector} in frame, found none`)
  console.log(`  ${label}: ${n} × ${selector}`)
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch({ chromiumSandbox: false })

  // 01 — list view
  {
    const { context, page } = await newPage(browser, {
      viewport: { width: 1400, height: 1100 }, folders: listFolders, slots: listSlots,
      localStorageEntries: { 'mc-flat-hidden-folders': JSON.stringify(listHiddenByFilter) },
    })
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForSelector('[data-testid="folder-collapse-f1"]', { timeout: 15000 })
    await page.waitForTimeout(1500)
    await must(page, '[data-pinned-divider]', 'pinned divider')
    await must(page, '[data-row-divider]', 'row divider')
    await must(page, '[data-stale-toggle]', 'dormant toggle')
    await must(page, '[data-folder-new-chat], button[aria-label="New chat in Archive"]', 'empty-folder New chat')
    await must(page, '[data-folder-hidden-reveal]', 'hidden-folders reveal')
    await measure(page, {
      // Root lane: session text, dormant-toggle chevron, hidden-reveal chevron.
      rootRowText: '[data-slot-key="r2"] .session-agent-label',
      rootStaleToggleChevron: '[data-testid="hidden-reveal-root"] ~ * [data-stale-toggle] svg, [data-stale-toggle]:not([data-testid^="folder-children"] [data-stale-toggle]) svg',
      rootHiddenRevealChevron: '[data-testid="hidden-reveal-root"] button svg',
      // Inside "Kiro": glyph, folder name, row text, row divider, pinned divider,
      // dormant toggle, hidden reveal, nested folder glyph and its row text.
      folderGlyph: '[data-testid="folder-collapse-f1"]',
      folderRowText: '[data-slot-key="s1"] .session-agent-label',
      folderRowDivider: '[data-row-divider]',
      folderPinnedDivider: '[data-pinned-divider]',
      folderStaleToggleChevron: '[data-stale-toggle] svg',
      folderHiddenRevealChevron: '[data-testid="hidden-reveal-f1"] button svg',
      emptyFolderNewChat: 'button[aria-label="New chat in Archive"] span',
      nestedGlyph: '[data-testid="folder-collapse-f1a"]',
      nestedRowText: '[data-slot-key="s9"] .session-agent-label',
    })
    await clipShot(page, '01-list-rows', { x: 200, y: 118, width: 380, height: 760 })
    await context.close()
  }

  // 02 — flat view
  {
    const { context, page } = await newPage(browser, {
      viewport: { width: 1400, height: 1000 }, folders: listFolders.slice(0, 2), slots: flatSlots,
      localStorageEntries: { 'mc-sidebar-flat-view': '1' },
    })
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForSelector('[data-testid="date-segment-header"]', { timeout: 15000 })
    await page.waitForTimeout(1200)
    await must(page, '[data-testid="date-segment-header"]', 'date header')
    await must(page, '[data-pinned-divider]', 'pinned divider (flat)')
    await measure(page, {
      dateHeader: '[data-testid="date-segment-header"]',
      rowText: '[data-slot-key="f1"] .session-agent-label',
      pinnedDivider: '[data-pinned-divider]',
    })
    await clipShot(page, '02-flat-view', { x: 200, y: 118, width: 380, height: 520 })
    // A 4x crop of the first date header, the pinned row, the pinned divider and
    // the first automatic row: the 4px shift is not legible at 1x.
    const header = await page.locator('[data-testid="date-segment-header"]').first().boundingBox()
    await page.screenshot({
      path: `${OUT}/${PREFIX}-02b-flat-view-4x.png`,
      clip: { x: header.x - 12, y: header.y - 4, width: 160, height: 150 },
    }).then(() => console.log('wrote', `${OUT}/${PREFIX}-02b-flat-view-4x.png`))
    await context.close()
  }

  // 03 — board view
  {
    const { context, page } = await newPage(browser, {
      viewport: { width: 1500, height: 900 }, folders: boardFolders, slots: boardSlots,
      localStorageEntries: {
        'mc-chat-config': JSON.stringify({ tagColumnsEnabled: true }),
        'mc-sidebar-flat-view': '0',
        'mc-sidebar-width': '620',
      },
      extra: async (path, route) => {
        if (path === '/api/chat/tags') { await json(route, tags); return true }
        if (path === '/api/chat/tag-columns') { await json(route, columns); return true }
        return false
      },
    })
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForSelector('[data-testid="column-strip"]', { timeout: 15000 })
    await page.waitForTimeout(1200)
    await must(page, '[data-testid="column-col-todo"] [data-folder-drop]', 'board folder block')
    await measure(page, {
      columnRootRowText: '[data-testid="column-col-todo"] [data-slot-key="t1"] .session-agent-label',
      columnFolderName: '[data-testid="column-col-todo"] [data-folder-drop] span',
      columnFolderRowText: '[data-testid="column-col-todo"] [data-slot-key="t2"] .session-agent-label',
      columnNestedRowText: '[data-testid="column-col-todo"] [data-slot-key="t3"] .session-agent-label',
    })
    // The rail's hit strip (-5..3 across the body's border) must not sit over a
    // row: a hit at a row's first pixel must land in the row, not the rail. A
    // body with no left pad put the rail over the first 3px of every row and a
    // click there folded the folder instead of opening the session.
    const hit = await page.locator('[data-testid="column-col-todo"] [data-slot-key="t2"]').evaluate(row => {
      const r = row.getBoundingClientRect()
      const probe = [0.5, 1.5, 2.5].map(dx => {
        const el = document.elementFromPoint(r.left + dx, r.top + r.height / 2)
        return { dx, inRow: !!el?.closest('[data-slot-key="t2"]'), onRail: !!el?.closest('.folder-rail') }
      })
      return { rowLeft: r.left, probe }
    })
    console.log(`[${PREFIX}] 03-board first pixels of a folder row`, JSON.stringify(hit))
    if (hit.probe.some(p => p.onRail || !p.inRow)) {
      throw new Error('board: the folder rail covers the first pixels of a session row')
    }
    const strip = await page.locator('[data-testid="column-strip"]').boundingBox()
    await clipShot(page, '03-board', { x: strip.x, y: 118, width: Math.min(strip.width, 640), height: 560 })
    await context.close()
  }

  // 04 — members page, grouped by team, at two roster widths
  for (const [shot, rosterWidth] of [['04-members', 264], ['04b-members-wide', 400]]) {
    const { context, page } = await newPage(browser, {
      viewport: { width: 1280, height: 820 }, folders: [], slots: [],
      // The first visit to the Crewmates page opens the Meet CrewMates flow
      // over the page; the server's `crewmates_onboarded` is what keeps it shut.
      localStorageEntries: { 'mc-crewmates-onboarded': '1', 'mc-members-roster-width': String(rosterWidth) },
      extra: async (path, route) => {
        if (path === '/api/theme/boot') { await json(route, { mode: 'dark', theme: '', crewmates_onboarded: true }); return true }
        if (path === '/api/members') { await json(route, { members, default_agent: 'kirocrew' }); return true }
        if (path === '/api/teams') { await json(route, { teams }); return true }
        if (path === '/api/autonudge') { await json(route, { enabled: true, loops: [] }); return true }
        if (path === '/api/workspaces') { await json(route, { workspaces: [{ name: 'default' }] }); return true }
        if (path === '/api/crons') { await json(route, { jobs: [] }); return true }
        if (path === '/api/webhooks') { await json(route, { tokens: [] }); return true }
        if (/^\/api\/members\/[^/]+\/(thread|activity|panel)$/.test(path)) {
          await json(route, { slot_key: 'member-radar', slug: 'radar', member: 'Radar', created: false, capped: false, entries: [], panel: null, html: null })
          return true
        }
        return false
      },
    })
    // Deep-link into a team: with a crewmate open the roster hides behind the
    // thread, while an open TEAM keeps the roster beside the team view.
    await page.goto(base + '/members?team=tm-ci', { waitUntil: 'domcontentloaded' })
    await page.getByText('CI crew', { exact: true }).first().waitFor({ timeout: 15000 })
    await page.waitForTimeout(1200)
    await must(page, 'li.pl-7, li .pl-7, .pl-7', 'indented crewmate row (pl-7)').catch(async () => {
      await must(page, 'li.pl-6, li .pl-6, .pl-6', 'indented crewmate row (pl-6)')
    })
    await measure(page, {
      teamHeaderIcon: 'li button svg.lucide-users, li button svg.lucide-inline',
      indentedRowAvatar: 'li button.pl-7 > span, li button.pl-6 > span',
    })
    // A team name reads whole (scrollWidth <= clientWidth) unless the header
    // really is too narrow for it: the header's content box less the 100px the
    // icon, the gaps and "Open team" keep (TeamGroupHeader's cap). Anything
    // narrower than that budget that still shows an ellipsis is a cap biting
    // without pressure.
    const names = await page.locator('[data-testid="team-group-header"] span.font-semibold').evaluateAll(els =>
      els.map(el => {
        const p = el.parentElement
        const cs = getComputedStyle(p)
        const budget = p.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight) - 100
        return { text: el.textContent, clientWidth: el.clientWidth, scrollWidth: el.scrollWidth, budget }
      }))
    console.log(`[${PREFIX}] ${shot} roster=${rosterWidth} team names`, JSON.stringify(names))
    const clipped = names.filter(n => n.scrollWidth > n.clientWidth && n.scrollWidth <= n.budget)
    if (clipped.length) {
      throw new Error(`members: team name(s) truncated without width pressure at roster ${rosterWidth}: ${clipped.map(n => JSON.stringify(n.text)).join(', ')}`)
    }
    const flowOpen = await page.locator('[data-testid="meet-crewmates-not-now"]').count()
      + await page.getByText('Give a crewmate a goal to own').count()
      + await page.locator('[role="dialog"]').count()
    if (flowOpen) {
      throw new Error('members: the Meet CrewMates flow is open over the page; the capture would show it instead of the roster')
    }
    const roster = await page.locator('[data-testid="member-roster"]').boundingBox()
    if (!roster || roster.width < 100) throw new Error('members: the roster is not on screen')
    await clipShot(page, shot, { x: roster.x, y: roster.y, width: Math.min(roster.width, 420), height: 420 })
    await context.close()
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
