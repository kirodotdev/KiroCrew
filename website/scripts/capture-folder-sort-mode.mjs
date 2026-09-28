/**
 * Screenshot probe: the sidebar's folder sort mode (Custom / Name / Date created).
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * and answers every /api/** call from fixtures via Playwright route interception
 * (gateway-free - no kiro-cli, no live backend). The fixture is the reporter's
 * own scheme from #13321: folders numbered 01. .. 04., 98., 99. whose STORED
 * positions were set by placing them, so the custom order draws `99.` between
 * `03.` and `04.` and `98.` below - exactly his screenshot.
 *
 * The mode is driven through the real control: the sort-and-filter menu's
 * "Folder order" rows. A PATCH to /api/config/kirocrew is answered by mutating the
 * fixture the GET serves, the way the real endpoint pair round-trips, so the
 * settle-time refetch sees the saved value.
 *
 * Every frame is asserted before it is written: the rendered folder rows must be
 * in the order the mode promises (or the notice the frame is about must be on
 * screen with the fixture's own words), or the run fails instead of shipping a
 * picture of the wrong state.
 *
 * Frames written:
 *   01-custom-before        the stored order (today's behaviour and the default)
 *   02-menu-custom          the menu with "Sort sessions by" over the session rows and
 *                           "Folder order" -> Custom checked under them
 *   03-name-after           the same folders after picking Name: 01 < 02 < ... < 99
 *   04-menu-name            the menu with By name checked ("By date created (newest
 *                           first)" as the third row -- no label takes a Sort sessions
 *                           by row's own shape)
 *                           and the note under the rows
 *   14-menu-flat-lane       the same menu in the flat lane: the rows are offered where
 *                           no folder tree is drawn (the mode still orders every Move
 *                           to folder picker and the history search's folder groups),
 *                           the drag note is not; the row's PATCH is asserted after
 *                           the frame
 *   05-reorder-hint         after a sibling reorder drag in Name mode: nothing moved,
 *                           the sidebar says where reordering comes back, and offers
 *                           the way there ("Switch to Custom", taken and asserted
 *                           right after the frame)
 *   06-created-after        after picking By date created: newest first
 *   15-menu-created-legacy  the menu in Date created mode with a folder from before
 *                           the stamp in the list: the row's fact line says such
 *                           folders have no date and come last, in the saved order
 *                           (a fresh install never sees it; a pre-upgrade tree does)
 *   07-failed-read-sidebar  the settings GET fails with nothing to fall back on: the
 *                           sidebar's banner over the tree -- the plain title leads,
 *                           the server's words under it as the smaller line, then the
 *                           one subline every surface uses; the session menu on the
 *                           same screen is checked quiet
 *   09-failed-read-suggestion-card  the folder-suggestion card with the sidebar
 *                           COLLAPSED: the banner is off screen, so the card carries
 *                           the same notice above it (no hand-off: its dropdown holds
 *                           an unsaved pick; the subline points at the sidebar)
 *   10-failed-read-job-form      the same state beside the job form's folder picker: the
 *                           same subline, which also names the chat sidebar as where
 *                           "Ask the agent" lives
 *   11-name-command-bar     the Command Bar's Search Folders view in Name mode: the same
 *                           order as the sidebar, read off the same settings entry
 *   12-failed-read-command-bar   the failed read said above that view's list, passive,
 *                           with the same subline-plus-pointer as the job form
 *   13-failed-read-header-menu   the header's session menu with the sidebar collapsed: the
 *                           banner is off screen, so the Move to folder row carries the
 *                           in-menu pair itself (passive notice + the subline, and
 *                           "Ask the agent" as a sibling item -- so no pointer)
 *
 * (08 -- the session menu's own copy of the notice on the sidebar's screen -- was
 * retired: one screen says a failure once, on the sidebar's banner. 09 and 13 are
 * the two surfaces that speak only when that banner is off screen.)
 * Frames 11 and 12 are numbered by kind, not by when they are taken: 11 is shot in
 * phase A right after the Name frames, 09, 12 and 13 in phase B after the sidebar's.
 *
 * Usage: node scripts/capture-folder-sort-mode.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { KIROCREW_CONFIG_FIXTURE, json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/folder-sort-mode'
mkdirSync(OUT, { recursive: true })

// The reporter's sidebar: stored positions from the drags that placed them.
// `created_at` stamps are the order the folders were made in (98 and 99 last).
const folders = [
  { id: 'f01', name: '01. Inbox', order: 0, collapsed: true, created_at: 1_758_600_000 },
  { id: 'f02', name: '02. Research', order: 1, collapsed: true, created_at: 1_758_610_000 },
  { id: 'f03', name: '03. Writing', order: 2, collapsed: true, created_at: 1_758_620_000 },
  { id: 'f99', name: '99. Archive', order: 3, collapsed: true, created_at: 1_758_650_000 },
  { id: 'f04', name: '04. Reviews', order: 4, collapsed: true, created_at: 1_758_630_000 },
  { id: 'f98', name: '98. Someday', order: 5, collapsed: true, created_at: 1_758_640_000 },
]
const slots = [{
  key: 's1', title: 'Weekly planning', messages: 4, running: false, agent: 'kirocrew',
  created: '2026-09-23T09:00:00Z', last_ts: '2026-09-23T09:30:00Z', folder_id: 'f01',
}]

const CUSTOM = ['f01', 'f02', 'f03', 'f99', 'f04', 'f98']
const BY_NAME = ['f01', 'f02', 'f03', 'f04', 'f98', 'f99']
const NEWEST_FIRST = ['f99', 'f98', 'f04', 'f03', 'f02', 'f01']

/** What the gateway says when the settings read fails; the notices must quote it. */
const READ_FAILURE = 'config store unavailable while the gateway restarts'
const MENU_CLIP = { x: 150, y: 60, width: 560, height: 700 }

/**
 * The Command Bar app, enabled, as `/api/apps` lists it: the shell hands its
 * overlay the quick-search slot from this record, so Ctrl+K opens the bar and its
 * Search Folders view -- the one folder list outside the sidebar that is not a
 * picker. Absent from the shared stub (which answers `/api/apps` with `[]`), so
 * both phases add it.
 */
const COMMAND_BAR_APP = {
  name: 'command-bar', version: '1.0.0', enabled: true, origin: 'builtin',
  manifest: { name: 'command-bar', ui: { overlays: [{ id: 'command-bar', replaces: 'quick-search' }] } },
}

async function drawnOrder(page) {
  return page.$$eval('[data-folder-row]', els => els.map(el => el.getAttribute('data-folder-row')))
}

/**
 * Open the Command Bar's Search Folders view (Ctrl+K, then its root row) and
 * return the folder names its option rows show, top to bottom. The bar's list is
 * the sidebar's promise restated -- the same order -- so this is asserted against
 * the sidebar's expected sequence by name.
 */
async function openSearchFolders(page) {
  await page.keyboard.press('Control+k')
  const row = page.getByRole('option', { name: /Search Folders/ })
  await row.first().waitFor({ state: 'visible', timeout: 8000 })
  await row.first().click()
  await page.getByPlaceholder('Search all folders…').waitFor({ state: 'visible', timeout: 8000 })
  await page.waitForTimeout(400)
}

/**
 * Close the bar and WAIT for it to go. Two things make a single `Escape` press
 * insufficient. The key is handled by the DIALOG PANEL's own keydown, so it has to
 * be pressed while focus is inside the panel -- after a real pointer click on an
 * option row focus sits on the document body and the press reaches nothing. And
 * inside a corpus view the first Escape leaves the VIEW (back to the root), only
 * the next one dismisses the overlay. Left open, the overlay's backdrop swallows
 * the clicks the next frame needs.
 */
async function closeCommandBar(page) {
  const bar = page.getByRole('combobox', { name: 'Command Bar' })
  for (let i = 0; i < 4 && await bar.count(); i += 1) {
    await bar.first().focus()
    await page.keyboard.press('Escape')
    await page.waitForTimeout(250)
  }
  await bar.waitFor({ state: 'detached', timeout: 5000 })
}
async function commandBarOrder(page) {
  const rows = await page.$$eval('[role="listbox"] [role="option"]', els => els.map(el => el.textContent || ''))
  return rows.map(text => folders.find(f => text.includes(f.name))?.id).filter(Boolean)
}
/**
 * Clip around the launcher PANEL, measured -- not around the input inside it. The
 * bar is centred and wider than its combobox, so anchoring on the input cuts the
 * folder names off the left edge of the frame.
 */
async function commandBarClip(page) {
  const box = await page.getByRole('dialog').first().boundingBox()
  if (!box) throw new Error('command bar: no dialog box')
  const x = Math.max(0, box.x - 12)
  const y = Math.max(0, box.y - 12)
  return {
    x, y,
    width: Math.min(1100 - x, box.width + 24),
    height: Math.min(760 - y, box.height + 24),
  }
}

async function assertOrder(page, expected, label) {
  await page.waitForFunction(
    exp => JSON.stringify([...document.querySelectorAll('[data-folder-row]')].map(el => el.getAttribute('data-folder-row'))) === JSON.stringify(exp),
    expected,
    { timeout: 8000 },
  ).catch(() => {})
  const got = await drawnOrder(page)
  if (JSON.stringify(got) !== JSON.stringify(expected)) {
    throw new Error(`${label}: rendered order ${JSON.stringify(got)} != expected ${JSON.stringify(expected)}`)
  }
  console.log(`${label}: rendered order OK ${got.join(' ')}`)
}

/** The notice must be on screen AND carry the fixture's own failure text. */
/** The plain line under a folder-order notice: ONE phrase on every surface (the
 *  sidebar's banner, the pickers, the suggestion card) -- a person may see it on
 *  four surfaces at once, and two phrasings for one failure read as two failures.
 *  Where the notice has no hand-off of its own, the line also says where the
 *  hand-off lives. */
const DETAIL = 'All folders are shown, in your Custom order; retries automatically'
const DETAIL_ASK = `${DETAIL}. Open the chat sidebar to ask the agent about it.`

async function assertNotice(page, testId, label, detail) {
  const el = page.locator(`[data-testid="${testId}"]`)
  await el.first().waitFor({ state: 'visible', timeout: 15000 })
  // A surface that mounts its own observer after the failure (the header menu)
  // says the generic words until its mount-time retry lands, then the server's:
  // wait for the settled text rather than reading the first frame.
  await el.first().filter({ hasText: READ_FAILURE }).waitFor({ state: 'visible', timeout: 15000 })
  const text = await el.first().textContent()
  if (!text || !text.includes('Folder order could not be read') || !text.includes(READ_FAILURE)) {
    throw new Error(`${label}: notice text ${JSON.stringify(text)} lacks the title or the server string`)
  }
  // The server's words are the detail, not the lead: their own smaller line under
  // the plain title (`messagePlacement="below"` -- `block` in the banner, a
  // wrapped `basis-full` line in an inline notice).
  const rawClass = await el.first().locator('span', { hasText: READ_FAILURE }).last().getAttribute('class')
  if (!rawClass || !/\b(block|basis-full)\b/.test(rawClass) || !/text-\[1[12]px\]/.test(rawClass)) {
    throw new Error(`${label}: the server string is not the demoted line under the title (class ${JSON.stringify(rawClass)})`)
  }
  // The subline sits inside the block banner (its footer) and directly under an
  // inline notice (a sibling line), so read it off the notice's container.
  const around = await el.first().locator('xpath=..').textContent()
  if (!around || !around.includes(detail)) {
    throw new Error(`${label}: no "${detail}" line under the notice (${JSON.stringify(around)})`)
  }
  if (around.includes('arrangement')) {
    throw new Error(`${label}: one phrase for this failure everywhere -- no surface says "arrangement" (${JSON.stringify(around)})`)
  }
  console.log(`${label}: notice OK (${testId})`)
}

async function openMenu(page) {
  await page.getByRole('button', { name: 'Sort and filter sessions' }).click()
  await page.locator('[data-testid="folder-order-custom"]').waitFor({ state: 'visible', timeout: 5000 })
  await page.waitForTimeout(250)
}

async function sidebarClip(page, height = 300) {
  const row = page.locator('[data-folder-row="f01"]')
  const box = await row.first().boundingBox()
  const x = box ? Math.max(0, box.x - 28) : 0
  return { x, y: 64, width: Math.min(420, 1100 - x), height }
}

async function shot(page, name, clip) {
  await page.screenshot({ path: `${OUT}/${name}.png`, clip })
  console.log('wrote', `${OUT}/${name}.png`)
}

/**
 * A pointer drag of one folder header onto the TOP EDGE of a sibling's header:
 * outside the header's middle nest band, so it is the reorder gesture and not a
 * re-parent. dnd-kit's pointer sensor arms after 5px, hence the small first move.
 */
async function attemptSiblingReorder(page, fromId, toId) {
  const from = await page.locator(`[data-folder-row="${fromId}"]`).first().boundingBox()
  const to = await page.locator(`[data-folder-row="${toId}"]`).first().boundingBox()
  if (!from || !to) throw new Error(`drag: missing row box for ${fromId} -> ${toId}`)
  await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2)
  await page.mouse.down()
  await page.mouse.move(from.x + from.width / 2 + 12, from.y + from.height / 2 + 6, { steps: 4 })
  await page.mouse.move(to.x + to.width / 2, to.y + 2, { steps: 12 })
  await page.waitForTimeout(200)
  await page.mouse.up()
}

/** Phase A: the three modes, the menu, and the withdrawn reorder said at the drop. */
async function captureModes(browser, base) {
  let storedMode = 'custom'
  let folderWrites = 0
  // The folders the stub serves; frame 15 adds a pre-stamp folder for one reload.
  let servedFolders = folders
  const context = await browser.newContext({ viewport: { width: 1100, height: 760 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, {
    folders, slots,
    extra: async (path, route) => {
      const method = route.request().method()
      if (path === '/api/apps') { await json(route, [COMMAND_BAR_APP]); return true }
      if (path === '/api/chat/folders' && method === 'GET') { await json(route, servedFolders); return true }
      if (path.startsWith('/api/chat/folders') && method !== 'GET') {
        folderWrites += 1
        console.log(`STUB: ${method} ${path} (folder write #${folderWrites})`)
        await json(route, { ok: true })
        return true
      }
      if (path !== '/api/config/kirocrew') return false
      if (method === 'PATCH') {
        const body = JSON.parse(route.request().postData() || '{}')
        if (body.path === 'dashboard.folder_sort') storedMode = body.value
        console.log(`STUB: PATCH ${body.path} = ${body.value}`)
        await json(route, { ok: true })
        return true
      }
      await json(route, { ...KIROCREW_CONFIG_FIXTURE, dashboard: { ...(KIROCREW_CONFIG_FIXTURE.dashboard || {}), folder_sort: storedMode } })
      return true
    },
  })
  logPageProblems(page)
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  // 01: the stored order, as today's build draws it (Custom is the default).
  await assertOrder(page, CUSTOM, 'custom (before)')
  await shot(page, '01-custom-before', await sidebarClip(page))

  // 02: the control, Custom checked; nothing to say under the rows.
  await openMenu(page)
  if (await page.locator('[data-testid="folder-order-reorder-note"]').count()) throw new Error('menu (custom): the reorder note must not show while Custom is active')
  await shot(page, '02-menu-custom', MENU_CLIP)

  // 03: pick Name -> PATCH -> the tree re-sorts, positions untouched.
  await page.locator('[data-testid="folder-order-name"]').click()
  await page.keyboard.press('Escape')
  await assertOrder(page, BY_NAME, 'name (after)')
  if (storedMode !== 'name') throw new Error(`the menu did not PATCH dashboard.folder_sort=name (stored: ${storedMode})`)
  await shot(page, '03-name-after', await sidebarClip(page))

  // 04: the control again, By name checked, "By date created (newest first)" as
  // the third row -- the direction spelled, as the session rows spell theirs; no
  // Folder order label may take a session row's own shape ("Created (Newest)"),
  // which read as one setting in two lists
  // and the note under the rows: a fact about the modes, not an inert copy of
  // the sidebar hint's actionable sentence.
  await openMenu(page)
  const rowLabels = {}
  for (const mode of ['custom', 'name', 'created']) {
    rowLabels[mode] = (await page.locator(`[data-testid="folder-order-${mode}"]`).textContent())?.trim()
  }
  // "Custom", not "Custom order": under the "Folder order" heading a third
  // "order" (the failed-read line says "saved order") had to be read around.
  const EXPECTED_LABELS = { custom: 'Custom', name: 'By name', created: 'By date created (newest first)' }
  for (const [mode, expected] of Object.entries(EXPECTED_LABELS)) {
    if (rowLabels[mode] !== expected) throw new Error(`menu (name): ${mode} row reads ${JSON.stringify(rowLabels[mode])}, expected ${JSON.stringify(expected)}`)
  }
  const sortByLabels = await page.getByRole('menu').first().locator('[role="menuitemradio"], [role="menuitem"]').allTextContents()
  for (const label of Object.values(rowLabels)) {
    // `sortByLabels` lists every row of the menu, the Folder order rows included,
    // so a label seen twice is one a session row also carries.
    if (/Created \(/.test(label) || sortByLabels.filter(l => l === label).length > 1) throw new Error(`menu (name): Folder order label ${JSON.stringify(label)} takes a Sort sessions by row's shape`)
  }
  if (!sortByLabels.some(l => /Created \(Newest\)/.test(l))) throw new Error(`menu (name): the Sort by rows should still read "Created (Newest)" (${JSON.stringify(sortByLabels)})`)
  // The sessions sort names its object: two orderings in one menu ("Sort by" over
  // one list, "Folder order" over another) read as sorting twice.
  const menuText = await page.getByRole('menu').first().textContent()
  if (!menuText.includes('Sort sessions by')) throw new Error('menu (name): the sessions sort heading should read "Sort sessions by"')
  if (menuText.includes('Sort by')) throw new Error('menu (name): a bare "Sort by" heading is still in the menu')
  if (await page.locator('[data-testid="folder-order-unstamped-note"]').count()) throw new Error('menu (name): the unstamped-folder line must not show outside Date created mode')
  const note = await page.locator('[data-testid="folder-order-reorder-note"]').textContent()
  if (note !== 'Folders can be dragged into place in Custom order only') throw new Error(`menu (name): note reads ${JSON.stringify(note)}`)
  await shot(page, '04-menu-name', MENU_CLIP)
  await page.keyboard.press('Escape')
  await page.waitForTimeout(250)

  // 14: the flat lane. No folder tree is drawn, but the mode still orders every
  // Move to folder picker and the history search's folder groups, so the rows are
  // offered here too (Name still checked) -- and the drag note is not, since
  // there is no folder row here to drag.
  await page.locator('[data-testid="flat-view-toggle"]').click()
  await page.locator('[data-testid="flat-view-lane"]').waitFor({ state: 'visible', timeout: 5000 })
  if (await page.locator('[data-folder-row]').count()) throw new Error('flat lane: folder rows are still drawn')
  await openMenu(page)
  const flatChecked = await page.locator('[data-testid="folder-order-name"] svg.text-accent').count()
  if (flatChecked !== 1) throw new Error(`menu (flat lane): Name is not the checked row (${flatChecked} check marks)`)
  if (await page.locator('[data-testid="folder-order-reorder-note"]').count()) throw new Error('menu (flat lane): the reorder note must not show where no folder row is drawn')
  await shot(page, '14-menu-flat-lane', MENU_CLIP)
  // The row writes from here too: pick Custom, then back to Name for the frames below.
  await page.locator('[data-testid="folder-order-custom"]').click()
  await page.keyboard.press('Escape')
  await page.waitForFunction(() => document.querySelector('[data-testid="folder-order-custom"]') === null, null, { timeout: 4000 })
  await page.waitForTimeout(400)
  if (storedMode !== 'custom') throw new Error(`the flat lane's row did not PATCH dashboard.folder_sort=custom (stored: ${storedMode})`)
  await openMenu(page)
  await page.locator('[data-testid="folder-order-name"]').click()
  await page.keyboard.press('Escape')
  await page.waitForTimeout(400)
  if (storedMode !== 'name') throw new Error(`the flat lane's row did not PATCH dashboard.folder_sort=name (stored: ${storedMode})`)
  console.log('menu (flat lane): rows offered, note withheld, PATCH OK')
  // Back to the tree lane (the toggle cycles tree <-> flat here: no lineage, no board).
  await page.locator('[data-testid="flat-view-toggle"]').click()
  await page.locator('[data-testid="flat-view-lane"]').waitFor({ state: 'detached', timeout: 5000 })
  await assertOrder(page, BY_NAME, 'name (back in the tree lane)')

  // 05: a sibling reorder drag in Name mode. The row lifts (the same gesture
  // re-parents), no slot opens, nothing is written, and the sidebar says why --
  // with the way there beside it: a "Switch to Custom" action on the line.
  await attemptSiblingReorder(page, 'f01', 'f99')
  await page.locator('[data-testid="folder-reorder-hint"]').waitFor({ state: 'visible', timeout: 4000 })
  const hint = await page.locator('[data-testid="folder-reorder-hint"] span').textContent()
  if (hint !== 'Switch to Custom to reorder folders') throw new Error(`drop hint reads ${JSON.stringify(hint)}`)
  const hintAction = page.locator('[data-testid="folder-reorder-hint-switch"]')
  if ((await hintAction.textContent()) !== 'Switch to Custom') throw new Error(`drop hint action reads ${JSON.stringify(await hintAction.textContent())}`)
  await assertOrder(page, BY_NAME, 'name (after the withdrawn drag)')
  if (folderWrites !== 0) throw new Error(`the withdrawn drag wrote ${folderWrites} folder update(s)`)
  await page.waitForTimeout(200)
  await shot(page, '05-reorder-hint', await sidebarClip(page, 340))

  // The hint's action (taken within the line's six-second life): one PATCH of
  // dashboard.folder_sort=custom through the menu's own write path, the stored
  // order back, no folder rewritten, and the line gone because reordering is back.
  await hintAction.click()
  await assertOrder(page, CUSTOM, 'custom (via the hint action)')
  if (storedMode !== 'custom') throw new Error(`the hint action did not PATCH dashboard.folder_sort=custom (stored: ${storedMode})`)
  if (folderWrites !== 0) throw new Error(`the hint action wrote ${folderWrites} folder update(s)`)
  await page.locator('[data-testid="folder-reorder-hint"]').waitFor({ state: 'detached', timeout: 4000 })
  console.log('hint action: order restored, hint cleared')

  // Back to Name for the Command Bar frame.
  await openMenu(page)
  await page.locator('[data-testid="folder-order-name"]').click()
  await page.keyboard.press('Escape')
  await assertOrder(page, BY_NAME, 'name (again)')

  // 11: the Command Bar's Search Folders view in the same state: the one folder
  // list outside the sidebar that is not a picker, and it lists in Name order too
  // -- read off the same settings entry, with no request of its own.
  await openSearchFolders(page)
  const barOrder = await commandBarOrder(page)
  if (barOrder.join(',') !== BY_NAME.join(',')) throw new Error(`command bar (name): lists ${barOrder.join(',')}, expected ${BY_NAME.join(',')}`)
  console.log('command bar (name): order OK')
  await shot(page, '11-name-command-bar', await commandBarClip(page))
  await closeCommandBar(page)

  // 06: Date created (newest first).
  await openMenu(page)
  await page.locator('[data-testid="folder-order-created"]').click()
  await page.keyboard.press('Escape')
  await assertOrder(page, NEWEST_FIRST, 'created (after)')
  if (storedMode !== 'created') throw new Error(`the menu did not PATCH dashboard.folder_sort=created (stored: ${storedMode})`)
  await shot(page, '06-created-after', await sidebarClip(page))
  // Every fixture folder is stamped, so the menu has no unstamped-folder line here.
  await openMenu(page)
  if (await page.locator('[data-testid="folder-order-unstamped-note"]').count()) throw new Error('menu (created): the unstamped-folder line must not show while every folder is stamped')
  await page.keyboard.press('Escape')
  await page.waitForTimeout(250)

  // 15: a pre-upgrade tree. A folder from before `created_at` existed has no
  // stamp, so Date created puts it after every stamped row, in the stored order --
  // on an existing install that is the order the person already had, and the pick
  // looks like nothing happened unless the menu says why. Served for one reload.
  const LEGACY = { id: 'f00', name: 'Legacy', order: 6, collapsed: true }
  servedFolders = [...folders, LEGACY]
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)
  await assertOrder(page, [...NEWEST_FIRST, 'f00'], 'created (pre-stamp folder last)')
  await openMenu(page)
  const unstamped = await page.locator('[data-testid="folder-order-unstamped-note"]').textContent()
  if (unstamped !== 'Folders made before dates were recorded have no date; they come last, in your Custom order') throw new Error(`menu (created, legacy): fact line reads ${JSON.stringify(unstamped)}`)
  if (!(await page.locator('[data-testid="folder-order-reorder-note"]').count())) throw new Error('menu (created, legacy): the drag note should still be there beside the fact line')
  await shot(page, '15-menu-created-legacy', MENU_CLIP)
  await page.keyboard.press('Escape')
  await page.waitForTimeout(250)
  servedFolders = folders
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)
  await assertOrder(page, NEWEST_FIRST, 'created (fixture folders again)')

  // And back to Custom: the stored order returns exactly, since nothing rewrote it.
  await openMenu(page)
  await page.locator('[data-testid="folder-order-custom"]').click()
  await page.keyboard.press('Escape')
  await assertOrder(page, CUSTOM, 'custom (restored)')
  await context.close()
}

/**
 * Phase B: the settings GET fails with nothing to fall back on. The surfaces that
 * draw the folder tree on a screen with no sidebar to say it -- the job form on the
 * Schedule page, the Command Bar -- say so with the server's words, as the sidebar
 * does over its own tree; the session menu and the suggestion card on the sidebar's
 * screen stay quiet (one screen, one banner).
 */
async function captureFailedRead(browser, base) {
  const context = await browser.newContext({ viewport: { width: 1100, height: 760 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  // The session's folder starts open so its row is on screen for the menu check.
  const openFolders = folders.map(f => (f.id === 'f01' ? { ...f, collapsed: false } : f))
  await stubDashboardApi(page, {
    folders: openFolders, slots,
    extra: async (path, route) => {
      const method = route.request().method()
      if (path === '/api/apps') { await json(route, [COMMAND_BAR_APP]); return true }
      if (path.startsWith('/api/chat/folders') && method !== 'GET') { await json(route, { ok: true }); return true }
      if (path !== '/api/config/kirocrew') return false
      // The one read that fails; nothing else about the dashboard is broken.
      await json(route, { error: READ_FAILURE }, 503)
      return true
    },
  })
  // Registered AFTER the shared stub so this handler wins: the stub swallows
  // /api/ws to stop a retry-storm, but frame 09 needs the socket handle to push
  // the folder-suggestion card exactly as `maybe_suggest_folder` broadcasts it.
  let wsServer = null
  await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })
  logPageProblems(page)
  await page.goto(base + '/chat?sid=s1', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  // 07: the sidebar's own banner over the tree, drawn in the stored order, with
  // the plain line under it. The session menu and the folder-suggestion card on
  // this same screen say nothing of their own: one screen, one banner.
  await assertNotice(page, 'folder-order-unavailable', 'sidebar', DETAIL)
  await assertOrder(page, CUSTOM, 'failed read (stored order fallback)')
  const sessionRow = page.locator('[data-session-row="s1"]').first()
  await sessionRow.waitFor({ state: 'visible', timeout: 5000 })
  await sessionRow.click({ button: 'right' })
  await page.getByRole('menu').first().waitFor({ state: 'visible', timeout: 5000 })
  if (await page.getByRole('menu').first().getByRole('alert').count()) throw new Error('session menu: must carry no folder-order notice of its own')
  console.log('session menu: quiet OK')
  await page.keyboard.press('Escape')
  await page.waitForTimeout(250)
  await shot(page, '07-failed-read-sidebar', await sidebarClip(page, 420))

  // 13: the header's session menu with the sidebar COLLAPSED -- the banner is
  // off screen, so the Move to folder row carries the rule's in-menu pair itself:
  // the passive notice with the server's words, the plain subline under it, and
  // "Ask the agent" as a sibling menu item described by the notice.
  await page.getByRole('button', { name: 'Hide sessions sidebar' }).click()
  await page.locator('[data-testid="folder-order-unavailable"]').waitFor({ state: 'hidden', timeout: 5000 })
  // The pointer is still over the collapsed toggle, whose hover opens the
  // recents flyout over the header; park it elsewhere and wait for the flyout
  // to go before reaching for the header menu.
  await page.mouse.move(900, 600)
  await page.getByRole('menu', { name: 'Recent sessions' }).waitFor({ state: 'hidden', timeout: 5000 }).catch(() => {})
  await page.getByRole('button', { name: 'Session options' }).click()
  const headerMenu = page.getByRole('menu').first()
  await headerMenu.waitFor({ state: 'visible', timeout: 5000 })
  await assertNotice(page, 'session-menu-folder-order-unavailable', 'header menu (sidebar collapsed)', DETAIL)
  const menuHandoff = headerMenu.getByRole('menuitem', { name: 'Ask the agent' })
  await menuHandoff.waitFor({ state: 'visible', timeout: 5000 })
  const menuNoticeId = await headerMenu.locator('[data-testid="session-menu-folder-order-unavailable"]').getAttribute('id')
  const menuDescribedBy = await menuHandoff.getAttribute('aria-describedby')
  if (!menuNoticeId || menuDescribedBy !== menuNoticeId) throw new Error(`header menu: hand-off aria-describedby ${JSON.stringify(menuDescribedBy)} != notice id ${JSON.stringify(menuNoticeId)}`)
  if (await headerMenu.locator('[data-testid="session-menu-folder-order-unavailable"] button').count()) throw new Error('header menu: the passive notice must carry no button of its own')
  // The block is closed after the hand-off: a separator before the regular items.
  const afterHandoff = await menuHandoff.evaluate(el => el.nextElementSibling?.getAttribute('role'))
  if (afterHandoff !== 'separator') throw new Error(`header menu: expected a separator after Ask the agent, found ${JSON.stringify(afterHandoff)}`)
  await page.waitForTimeout(250)
  const headerMenuBox = await headerMenu.boundingBox()
  if (!headerMenuBox) throw new Error('header menu: no menu box')
  const hx = Math.max(0, headerMenuBox.x - 12)
  const hy = Math.max(0, headerMenuBox.y - 44)
  await shot(page, '13-failed-read-header-menu', { x: hx, y: hy, width: Math.min(1100 - hx, headerMenuBox.width + 24), height: Math.min(760 - hy, headerMenuBox.height + 56) })
  await page.keyboard.press('Escape')
  await page.waitForTimeout(250)

  // 09: the folder-suggestion card, sidebar still COLLAPSED. The card's dropdown
  // lists the folders in the mode, so with the banner off screen the same notice
  // sits above the card (full width of the chat column) -- and carries no
  // hand-off, because the dropdown holds a pick that is not saved until Accept
  // and the hand-off would navigate away from it; the subline points at the
  // sidebar instead. The dropdown itself lists the stored order, as every
  // surface does while the mode cannot be read.
  if (!wsServer) throw new Error('suggestion card: the websocket route never bound')
  wsServer.send(JSON.stringify({
    type: 'slot_folder_suggestion',
    data: { slot: 's1', folder_id: 'f02', folder_name: '02. Research', breadcrumb: '02. Research', ts: Date.now() / 1000 },
  }))
  const card = page.getByTestId('folder-suggestion-card')
  await card.waitFor({ state: 'visible', timeout: 8000 })
  await assertNotice(page, 'folder-suggestion-order-unavailable', 'suggestion card (sidebar collapsed)', DETAIL_ASK)
  if (await page.locator('[data-testid="folder-suggestion-order-unavailable"] button, [data-testid="folder-suggestion-order-unavailable"] a').count()) {
    throw new Error('suggestion card: the order notice must carry no hand-off')
  }
  const cardOptions = await page.getByTestId('folder-suggestion-select').locator('option').allTextContents()
  const storedNames = CUSTOM.map(id => folders.find(f => f.id === id).name)
  if (cardOptions.join('|') !== storedNames.join('|')) throw new Error(`suggestion card: options ${JSON.stringify(cardOptions)}, expected the stored order ${JSON.stringify(storedNames)}`)
  const noticeBox = await page.locator('[data-testid="folder-suggestion-order-unavailable"]').boundingBox()
  const cardBox = await card.boundingBox()
  if (!noticeBox || !cardBox) throw new Error('suggestion card: no box for the notice or the card')
  const sy = Math.max(0, noticeBox.y - 16)
  const sx = Math.max(0, Math.min(noticeBox.x, cardBox.x) - 16)
  const sw = Math.min(1100 - sx, Math.max(noticeBox.width, cardBox.width) + 32)
  await shot(page, '09-failed-read-suggestion-card', { x: sx, y: sy, width: sw, height: Math.min(760 - sy, cardBox.y + cardBox.height + 16 - sy) })
  await page.getByTestId('folder-suggestion-decline').click()
  await card.waitFor({ state: 'hidden', timeout: 5000 })
  // Sidebar back for the frames below.
  await page.getByRole('button', { name: 'Show sessions sidebar' }).click()
  await page.locator('[data-testid="folder-order-unavailable"]').waitFor({ state: 'visible', timeout: 5000 })

  // 12: the Command Bar's Search Folders view: the same notice above its list,
  // passive (the typed query is unsaved), and the list in the stored order.
  await openSearchFolders(page)
  await assertNotice(page, 'command-bar-folder-order-unavailable', 'command bar', DETAIL_ASK)
  const barOrder = await commandBarOrder(page)
  if (barOrder.join(',') !== CUSTOM.join(',')) throw new Error(`command bar (failed read): lists ${barOrder.join(',')}, expected ${CUSTOM.join(',')}`)
  if (await page.locator('[data-testid="command-bar-folder-order-unavailable"] button, [data-testid="command-bar-folder-order-unavailable"] a').count()) throw new Error('command bar: the order notice must carry no hand-off')
  await shot(page, '12-failed-read-command-bar', await commandBarClip(page))
  await closeCommandBar(page)

  // 10: the job form's folder picker on the Schedule page, the notice beside it.
  await page.goto(base + '/schedule', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1500)
  await page.getByRole('button', { name: 'Create your first job' }).click()
  await page.getByLabel('Chat folder').waitFor({ state: 'visible', timeout: 8000 })
  await assertNotice(page, 'job-folder-order-unavailable', 'job form', DETAIL_ASK)
  // The picker sits below the dialog's fold: bring it up before measuring, and
  // keep the clip inside the viewport.
  await page.locator('[data-testid="job-folder-order-unavailable"]').scrollIntoViewIfNeeded()
  await page.waitForTimeout(250)
  const picker = await page.getByLabel('Chat folder').boundingBox()
  if (!picker) throw new Error('job form: the Chat folder picker has no box')
  // As wide as the picker itself (plus margins): the subline now runs two
  // sentences, and a fixed 560px clip cut it at the frame's edge.
  const pw = Math.min(1100, Math.ceil(picker.width) + 48)
  const px = Math.max(0, Math.min(picker.x - 24, 1100 - pw))
  const py = Math.max(0, Math.min(picker.y - 60, 760 - 200))
  await shot(page, '10-failed-read-job-form', { x: px, y: py, width: pw, height: 200 })
  await context.close()
}

async function main() {
  const served = await serveDist()
  const browser = await chromium.launch()
  try {
    await captureModes(browser, served.base)
    await captureFailedRead(browser, served.base)
  } finally {
    await browser.close()
    served.srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
