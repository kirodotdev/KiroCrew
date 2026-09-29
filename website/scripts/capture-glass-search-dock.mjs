/**
 * Screenshot harness for the LIST-PANEL SEARCH DOCK: the Sessions sidebar's and
 * the Crew Members roster's shared search row (components/SearchFilterBar) as a
 * Liquid Glass capsule floating over the list (components/ListDock), with the
 * rows scrolling under it. Photographs the REAL SPA over the shared stubbed
 * dashboard API in both polarities, at rest and scrolled, and asserts that the
 * capsule carries a live backdrop-blur layer and that the first row starts
 * below the dock at rest. Nothing in CI runs this file.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6811 --strictPort   # in another shell
 *   node scripts/capture-glass-search-dock.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { screenshotWithCaret } from './lib/screenshot-with-caret.mjs'


const BASE = process.env.BASE || 'http://127.0.0.1:6811'
const OUT = process.argv[2] || '../temp-screenshots/glass-search-dock'
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const TITLES = [
  'GUI user-test standing reds fix', 'Talos review kiro-drive', 'Remove Cloud button from Members header',
  'Kiro Drive phone Details bottom sheet', 'Identity-sweep livelock fix', 'Haptic feedback (phone taps)',
  'Liquid glass composer dock', 'Settings capsule search on mobile', 'Members page filters parity',
  'Crewmate chat autoscroll', 'Shelf fade under the composer', 'File card type icons', 'Cron template picker',
  'Effort back in model picker', 'Fake ACP list models', 'Agent plugins std MCP', 'Cron store fsync',
  'Customize page crewmate vocab', 'Portability warning for agent templates', 'Watchdog orphan queue read',
]
const slots = TITLES.map((title, i) => ({
  key: `chat-${i}`, title, running: i === 0 || i === 6, last_message: 'Latest turn.', messages: 4 + i,
  agent: 'kirocrew', memory_mode: 'persistent', project: '', folder_id: '',
  modified: now - i * 3600, source_links: [], source_links_total: 0,
}))

const MATES = [
  ['Radar', 'Triage new GitHub issues every morning', 'Two issues need a decision this morning.'],
  ['Fixer', 'Turns a triaged issue into a green PR', 'PR #14844 is on CI round 2.'],
  ['Scout', 'Reads the docs and the web before anyone else', 'Nothing new on the glass front.'],
  ['Scribe', 'Keeps the notes and the changelog honest', 'Notes synced.'],
  ['Conductor', 'Runs the pipeline and wakes the others', 'Sleeping until the next stage.'],
  ['Reviewer', 'Reads every diff as a new user would', 'Approved the details sheet.'],
  ['Ops', 'Watches the fleet and the watchdog', 'Fleet is quiet.'],
  ['Archivist', 'Files what is finished', 'Archived 3 sessions.'],
  ['Gardener', 'Prunes stale worktrees', 'Two worktrees are stale.'],
  ['Translator', 'Keeps the 13 locales in step', 'ja is 2 keys behind.'],
  ['Auditor', 'Checks the security invariants', 'All green.'],
  ['Greeter', 'Welcomes a new user on first run', 'No new users today.'],
  ['Librarian', 'Indexes the knowledge base', 'Two new documents filed.'],
  ['Courier', 'Relays channel messages', 'Slack quiet since noon.'],
  ['Planner', 'Turns a goal into a task list', 'Sprint plan drafted.'],
  ['Tester', 'Writes the missing test first', 'Coverage up 0.4%.'],
  ['Painter', 'Keeps the themes in step', 'Ocean theme retinted.'],
  ['Historian', 'Summarises long sessions', 'Three summaries queued.'],
  ['Dispatcher', 'Routes work to crewmates', 'Idle.'],
  ['Sentinel', 'Watches the security invariants', 'All green.'],
  ['Cartographer', 'Maps the feature surface', 'Feature map current.'],
  ['Herald', 'Posts the release notes', 'rc1 notes drafted.'],
  ['Mechanic', 'Repairs the dev fleet', 'Two pods rebuilt.'],
  ['Weaver', 'Stitches workflows together', 'Idle.'],
]
const members = MATES.map(([name, description, last_message], i) => ({
  name, slug: name.toLowerCase(), bound: true, slot_key: `member-${name.toLowerCase()}`, running: i === 1 || i === 6,
  kiro_agent: 'kirocrew', workspace: 'default', memory_store: name.toLowerCase(), memory_version: 2, memory_owner: name,
  model: '', description, source: 'kirocrew', last_active_ts: now - i * 1800, last_message,
}))

const THEME = { mode: 'dark' }
const extra = async (path, route) => {
  // Dev server: the stub's `**/api/**` glob also catches `/src/api/*.ts` modules.
  if (!path.startsWith('/api/')) { await route.continue(); return true }
  if (path === '/api/members') { await json(route, { members, default_agent: 'kirocrew' }); return true }
  if (/^\/api\/members\/[^/]+\/(thread|activity|panel)$/.test(path)) {
    await json(route, path.endsWith('thread') ? { messages: [], has_more: false } : path.endsWith('activity') ? { entries: [], capped: false } : { panel: null, html: null })
    return true
  }
  // The roster's teams query reads `.teams` off the body: the shared stub's
  // guessed `[]` makes it resolve to undefined and the "Could not load teams"
  // notice would land in the frame as if the product had failed.
  if (path === '/api/teams') { await json(route, { teams: [] }); return true }
  // `/api/theme/boot` also carries the first-run flags; without `crewmates_onboarded`
  // the Meet CrewMates dialog opens over /members and intercepts the star click.
  if (path === '/api/theme/boot') { await json(route, { mode: THEME.mode, theme: '', onboarded: true, crewmates_onboarded: true, privacy_acked: true }); return true }
  if (path === '/api/chat/pins') { await json(route, { pins: [] }); return true }
  if (path === '/api/crons') { await json(route, { jobs: [] }); return true }
  if (path === '/api/webhooks') { await json(route, { tokens: [] }); return true }
  if (path === '/api/autonudge') { await json(route, { enabled: true, loops: [] }); return true }
  if (path.startsWith('/api/chat/slots/')) { await json(route, { running: false, has_more: false, total: 0, queue: [], messages: [] }); return true }
  return false
}

/** The glass is live when an effect layer under `root` carries a backdrop blur. */
async function assertGlass(page, label) {
  const filters = await page.$$eval('[data-testid="search-field-glass"] *', els => els
    .map(n => getComputedStyle(n).backdropFilter).filter(f => f && f !== 'none'))
  if (!filters.some(f => /blur\(/.test(f))) throw new Error(`${label}: no backdrop blur layer rendered`)
}

/** At rest the first row's top must sit at or below the dock's bottom edge. */
async function assertRowBelowDock(page, scroller, label) {
  const dock = await page.getByTestId('list-dock').first().boundingBox()
  const pad = await page.locator(scroller).first().evaluate(el => parseFloat(getComputedStyle(el).paddingTop))
  if (!dock || pad < dock.height - 1) throw new Error(`${label}: scroller pads ${pad}px under a ${dock?.height}px dock`)
  console.log(label, `dock ${Math.round(dock.height)}px, scroller pad ${pad}px`)
}

/** The shelf is opaque and inert: the space beside a chip must land on the
 *  dock, never on a row the scrim hides. */
async function assertChipRowInert(page, label) {
  const hit = await page.evaluate(() => {
    const row = document.querySelector('[data-testid="list-dock"] [data-testid$="filter-chips"], [data-testid="list-dock"] .flex-wrap')
    if (!row) return 'no chip row'
    const r = row.getBoundingClientRect()
    const el = document.elementFromPoint(r.right - 6, r.top + r.height / 2)
    return el && el.closest('[data-testid="list-dock"]') ? 'dock' : 'below'
  })
  if (hit !== 'dock') throw new Error(`${label}: click beside the chips lands on the ${hit}, not the inert shelf`)
}

/** Every focusable inside the dock must hit-test to itself: the band is
 *  click-through, so a control that forgot to re-arm dead-clicks silently. */
async function assertDockControlsHit(page, label) {
  const misses = await page.evaluate(() => {
    const dock = document.querySelector('[data-testid="list-dock"]')
    const out = []
    for (const el of dock.querySelectorAll('button, a[href], input, select, textarea, [tabindex]:not([tabindex="-1"]), [role="button"]')) {
      const r = el.getBoundingClientRect()
      if (r.width < 2 || r.height < 2) continue
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
      if (!hit || !(el === hit || el.contains(hit))) out.push(`${el.tagName.toLowerCase()}[${el.getAttribute('data-testid') || el.getAttribute('aria-label') || ''}] -> ${hit ? hit.tagName.toLowerCase() : 'nothing'}`)
    }
    return out
  })
  if (misses.length) throw new Error(`${label}: dock controls not hit-testable: ${misses.join('; ')}`)
}

/** The extra scenes the UX lane asked for: the dock in its GROWN states over a
 *  scrolling list, a pinned folder header on the pane's edge, a notice, the board. */
async function scenes(browser, theme) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
  logPageProblems(page)
  const sidebar = page.locator('.sidebar-inner').first()

  // Folders + the running-only filter chip active: chips float over the rows.
  const folders = [{ id: 'fa', name: 'Design', order: 0, collapsed: false }, { id: 'fb', name: 'Kiro Drive', order: 1, collapsed: false }]
  const foldered = slots.map((s, i) => ({ ...s, running: i % 2 === 0, folder_id: i < 8 ? 'fa' : i < 14 ? 'fb' : '' }))
  await stubDashboardApi(page, { slots: foldered, folders, theme, extra, localStorageEntries: { 'mc-session-running-only': '1' } })
  await page.goto(`${BASE}/`, { waitUntil: 'networkidle' })
  const lane = page.getByTestId('tree-view-lane')
  await lane.waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(700)
  await assertRowBelowDock(page, '[data-testid="tree-view-lane"]', `sessions-chips/${theme}`)
  await assertChipRowInert(page, `sessions-chips/${theme}`)
  await assertDockControlsHit(page, `sessions-chips/${theme}`)
  await sidebar.screenshot({ path: `${OUT}/sessions-chips-rest-${theme}.png` })
  await lane.evaluate(el => { el.scrollTop = 60 })
  await page.waitForTimeout(400)
  await sidebar.screenshot({ path: `${OUT}/sessions-chips-scrolled-${theme}.png` })
  await page.close()

  // Deeper, no chip: the first folder's header pins on the dock's bottom edge
  // (the pane plus the shelf's 4px fade).
  // A fresh page, because the stub re-seeds its localStorage entries on every load.
  const p1 = await browser.newPage({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
  logPageProblems(p1)
  await stubDashboardApi(p1, { slots: foldered, folders, theme, extra })
  await p1.goto(`${BASE}/`, { waitUntil: 'networkidle' })
  const lane1 = p1.getByTestId('tree-view-lane')
  await lane1.waitFor({ state: 'visible', timeout: 20000 })
  await p1.waitForTimeout(700)
  await lane1.evaluate(el => { el.scrollTop = 140 })
  await p1.waitForTimeout(400)
  const pin = await p1.evaluate(() => {
    const lane = document.querySelector('[data-testid="tree-view-lane"]')
    const dock = document.querySelector('[data-testid="list-dock"]').getBoundingClientRect().bottom
    const head = document.querySelector('.folder-row-sticky').getBoundingClientRect().top
    return { dockBottom: dock - lane.getBoundingClientRect().top, headerTop: head - lane.getBoundingClientRect().top }
  })
  if (Math.abs(pin.dockBottom - pin.headerTop) > 1) throw new Error(`sessions-pinned/${theme}: header ${pin.headerTop} vs dock bottom ${pin.dockBottom}`)
  console.log(`sessions-pinned/${theme}`, pin)
  await p1.locator('.sidebar-inner').first().screenshot({ path: `${OUT}/sessions-pinned-folder-${theme}.png` })
  await p1.close()

  // A list-level notice in the dock: the remote-instances read fails.
  const p2 = await browser.newPage({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
  logPageProblems(p2)
  await stubDashboardApi(p2, {
    slots, theme, localStorageEntries: { 'mc-preview-instance-sessions': '1' },
    extra: async (path, route) => {
      if (path === '/api/instances') { await route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"tunnel closed"}' }); return true }
      return extra(path, route)
    },
  })
  await p2.goto(`${BASE}/`, { waitUntil: 'networkidle' })
  await p2.getByTestId('instance-sessions-error').waitFor({ state: 'visible', timeout: 20000 })
  await p2.waitForTimeout(600)
  await assertRowBelowDock(p2, '[data-testid="tree-view-lane"]', `sessions-notice/${theme}`)
  await assertDockControlsHit(p2, `sessions-notice/${theme}`)
  await p2.getByTestId('tree-view-lane').evaluate(el => { el.scrollTop = 60 })
  await p2.waitForTimeout(400)
  await p2.locator('.sidebar-inner').first().screenshot({ path: `${OUT}/sessions-notice-${theme}.png` })
  await p2.close()

  // The board lane steps below the dock (columns scroll on their own).
  const p3 = await browser.newPage({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
  logPageProblems(p3)
  const tags = [{ id: 'todo', name: 'ToDo', color: '#3b82f6', order: 0, status: true }, { id: 'done', name: 'Done', color: '#10b981', order: 1, status: true }]
  const columns = [{ id: 'c1', name: 'To do', tag_ids: ['todo'], mode: 'any', order: 0, include_untagged: true }, { id: 'c2', name: 'Done', tag_ids: ['done'], mode: 'any', order: 1, include_untagged: false }]
  await stubDashboardApi(p3, {
    slots, theme, localStorageEntries: { 'mc-chat-config': JSON.stringify({ tagColumnsEnabled: true }) },
    extra: async (path, route) => {
      if (path === '/api/chat/tags') { await json(route, tags); return true }
      if (path === '/api/chat/tag-columns') { await json(route, columns); return true }
      return extra(path, route)
    },
  })
  await p3.goto(`${BASE}/`, { waitUntil: 'networkidle' })
  await p3.getByTestId('column-strip').waitFor({ state: 'visible', timeout: 20000 })
  await p3.waitForTimeout(700)
  await p3.locator('.sidebar-inner').first().screenshot({ path: `${OUT}/sessions-board-${theme}.png` })
  await p3.close()

  // The roster's notice in the dock: a star write that the gateway refused.
  const p4 = await browser.newPage({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
  logPageProblems(p4)
  await stubDashboardApi(p4, {
    slots, theme,
    extra: async (path, route) => {
      if (/^\/api\/(config\/kirocrew\/)?agents\/[^/]+$/.test(path) && route.request().method() !== 'GET') { await route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"config file is read-only"}' }); return true }
      return extra(path, route)
    },
  })
  await p4.goto(`${BASE}/members`, { waitUntil: 'networkidle' })
  const roster4 = p4.locator('#main-content ul').first()
  await roster4.waitFor({ state: 'visible', timeout: 20000 })
  await p4.waitForTimeout(700)
  // The star fades in on row hover; the face beside it animates, so hover the
  // row and click the star without waiting for it to be "stable".
  await p4.locator('#main-content ul li').first().hover()
  await p4.getByTestId('member-star-radar').click({ force: true })
  await p4.getByTestId('member-star-error').waitFor({ state: 'visible', timeout: 10000 })
  await p4.waitForTimeout(600)
  await assertRowBelowDock(p4, '#main-content ul', `members-notice/${theme}`)
  await assertDockControlsHit(p4, `members-notice/${theme}`)
  await roster4.evaluate(el => { el.scrollTop = 60 })
  await p4.mouse.move(5, 5)
  await p4.waitForTimeout(400)
  await p4.locator('#main-content .sidebar-inner').first().screenshot({ path: `${OUT}/members-notice-${theme}.png` })
  await p4.close()
}

const browser = await chromium.launch()
let failed = false
try {
  for (const theme of ['dark', 'light']) {
    THEME.mode = theme
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
    logPageProblems(page)
    await stubDashboardApi(page, { slots, theme, extra })

    // Sessions sidebar
    await page.goto(`${BASE}/`, { waitUntil: 'networkidle' })
    const lane = page.getByTestId('tree-view-lane')
    await lane.waitFor({ state: 'visible', timeout: 20000 })
    await page.waitForTimeout(700)
    await assertGlass(page, `sessions/${theme}`)
    await assertRowBelowDock(page, '[data-testid="tree-view-lane"]', `sessions/${theme}`)
    await assertDockControlsHit(page, `sessions/${theme}`)
    const sidebar = page.locator('.sidebar-inner').first()
    await sidebar.screenshot({ path: `${OUT}/sessions-rest-${theme}.png` })
    // Focus changes nothing on the glass field (maintainer decision): same
    // shadow, same side line, same tint; the caret is the indicator. Read the
    // pane before and after the input takes focus and photograph the focused
    // state beside the resting one.
    {
      const pane = page.getByTestId('search-field-glass').first()
      const read = () => pane.evaluate(el => ({ shadow: getComputedStyle(el).boxShadow, edge: getComputedStyle(el).getPropertyValue('--glass-edge').trim(), tint: getComputedStyle(el).getPropertyValue('--glass-tint').trim() }))
      const rest = await read()
      await page.getByPlaceholder('Search sessions').focus()
      await page.waitForTimeout(300)
      const focus = await read()
      if (!(await page.getByPlaceholder('Search sessions').evaluate(el => document.activeElement === el))) throw new Error(`sessions/${theme}: search input did not take focus`)
      for (const k of ['shadow', 'edge', 'tint']) if (focus[k] !== rest[k]) throw new Error(`sessions/${theme}: pane ${k} changed on focus (${rest[k]} -> ${focus[k]}); a glass pane must not change on focus`)
      if (!(await screenshotWithCaret(sidebar, { path: `${OUT}/sessions-focused-${theme}.png` }, page.getByPlaceholder('Search sessions')))) throw new Error(`sessions/${theme}: no caret caught in the focused field frame`)
      console.log(`sessions/${theme}: focus leaves the field unchanged -- shadow ${focus.shadow}; edge ${focus.edge}; tint ${focus.tint}`)
      await page.mouse.click(640, 400)
      await page.waitForTimeout(200)
    }
    await lane.evaluate(el => { el.scrollTop = 34 })
    await page.waitForTimeout(400)
    await sidebar.screenshot({ path: `${OUT}/sessions-scrolled-${theme}.png` })
    await page.getByPlaceholder('Search sessions').fill('glass')
    await page.waitForTimeout(400)
    await sidebar.screenshot({ path: `${OUT}/sessions-typing-${theme}.png` })

    // Crew Members roster
    await page.goto(`${BASE}/members`, { waitUntil: 'networkidle' })
    const roster = page.locator('#main-content ul').first()
    await roster.waitFor({ state: 'visible', timeout: 20000 })
    await page.waitForTimeout(700)
    await assertGlass(page, `members/${theme}`)
    await assertRowBelowDock(page, '#main-content ul', `members/${theme}`)
    const panel = page.locator('#main-content .sidebar-inner').first()
    await panel.screenshot({ path: `${OUT}/members-rest-${theme}.png` })
    await roster.evaluate(el => { el.scrollTop = 120 })
    await page.waitForTimeout(400)
    await panel.screenshot({ path: `${OUT}/members-scrolled-${theme}.png` })
    await page.screenshot({ path: `${OUT}/members-page-${theme}.png` })
    console.log('wrote', theme)
    await page.close()
    await scenes(browser, theme)
  }
} catch (e) {
  failed = true
  console.error(e)
} finally {
  await browser.close()
}
process.exit(failed ? 1 : 0)
