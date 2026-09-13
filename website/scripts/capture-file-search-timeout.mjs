/**
 * Screenshot harness + behavior check for the BOUNDED @-menu file search.
 *
 * The composer's @ file-mention menu had no deadline on `/api/file-search`, so a
 * wedged gateway left it showing "Searching..." forever with Enter swallowed. The
 * fix bounds the fetch and names the cause: a deadline the client set itself is a
 * KNOWN cause, so that arm says "timed out", while any other rejection (a network
 * drop, a 5xx) can only honestly say "failed".
 *
 * Both arms are photographed because the two strings differ, and a single capture
 * of one arm cannot evidence the other. This asserts as well as photographs,
 * against the REAL built SPA (website/dist): the timeout scene lets the route HANG
 * so the production FILE_SEARCH_TIMEOUT_MS deadline is what fires, rather than a
 * timeout hand-set in the harness. Exits non-zero unless each arm renders its own
 * string. Nothing in CI runs this file -- the CI-enforced half of the invariant is
 * FilePickerMenu.timeout.test.tsx.
 *
 * Usage: node scripts/capture-file-search-timeout.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/file-search-timeout'
const SLOT = 'chat-filesearch'
const PROJECT = '/home/user/workspace/notes'
const MESSAGE = 'Check the release notes in @rele'

// The node toolchain injects its own libstdc++ on LD_LIBRARY_PATH, which the
// bundled Chromium then loads in preference to the system one and fails on.
delete process.env.LD_LIBRARY_PATH

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Release prep',
  running: false,
  last_message: 'Ready when you are.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 600, content: 'Where do the release notes live?' },
    { role: 'assistant', ts: Date.now() / 1000 - 590, content: 'Type `@` and part of the filename.' },
  ],
}

/**
 * `mode` selects which server behaviour the arm reproduces: 'hang' marks the route
 * handled and never fulfills it, so the request stays in flight until the production
 * deadline aborts it; 'error' answers 5xx, which carries no `code`, so the generic
 * failure arm renders; 'denied' and 'not-found' answer the two statuses the handler
 * gives a machine-readable `code`, which is what selects the cause-specific copy.
 *
 * The 403 deliberately carries NO `X-Auth-Required` header: with it the client reads a
 * dashboard-session expiry and falls back to the generic string, so the arm would
 * photograph the wrong copy while still looking like a 403.
 */
async function captureArm(context, base, mode, expected, shot, opts = {}) {
  const page = await context.newPage()
  logPageProblems(page)

  const extra = async (path, route) => {
    if (path === '/api/file-search') {
      if (mode === 'hang') return true
      if (mode === 'empty') { await json(route, { results: [], root: PROJECT }); return true }
      if (mode === 'denied') {
        await route.fulfill({
          status: 403,
          contentType: 'application/json',
          body: JSON.stringify({ error: 'permission denied', code: 'access_denied' }),
        })
        return true
      }
      if (mode === 'not-found') {
        await route.fulfill({
          status: 404,
          contentType: 'application/json',
          body: JSON.stringify({ error: 'project root missing', code: 'project_not_found' }),
        })
        return true
      }
      await route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"boom"}' })
      return true
    }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }

  await stubDashboardApi(page, { slots, theme: 'dark', extra })
  await page.addInitScript(slot => { localStorage.setItem('mc-active-slot', slot) }, SLOT)
  if (opts.ctrlEnter) {
    // The Enter hint has two spellings and the second is a different set of catalog
    // strings, reached only through this persisted setting.
    await page.addInitScript(() => {
      localStorage.setItem('mc-chat-config', JSON.stringify({ sendOnEnter: 'ctrl-enter' }))
    })
  }
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2000)

  const composer = page.locator('textarea').first()
  await composer.click()
  // pressSequentially drives real keydown/input events so the @-trigger detection
  // and the debounced search both run as they do for a person typing.
  await composer.pressSequentially(MESSAGE, { delay: 15 })

  // Generous: the hang arm waits out the real 15s bound (deadline errors are not retried),
  // and the error arm waits out the shared retry policy's one non-deadline retry.
  const found = await page.locator(`text=${expected}`).first()
    .waitFor({ timeout: 30000 }).then(() => true).catch(() => false)
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${shot}.png` })
  console.log('wrote', `${OUT}/${shot}.png`, { mode, found })
  await page.close()
  return found
}

/**
 * The project picker's failure states, which live on a different surface from the @-menu
 * and so need their own driver: open the composer's project chip, with the picker's two
 * reads stubbed to fail.
 *
 * `which` selects WHICH read fails, because the interesting case is not either one alone:
 * a wedged gateway fails BOTH, and the picker then showed two identical Retry buttons with
 * nothing to say which repaired what. 'both' is the capture that evidences the single
 * combined control.
 */
async function capturePickerArm(context, base, which, expected, shot, opts = {}) {
  const page = await context.newPage()
  logPageProblems(page)

  const fail = async route => {
    await route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"boom"}' })
  }
  const extra = async (path, route) => {
    if (path === '/api/recent-projects') {
      // 'recent-hang' leaves the request OPEN, which is the only way to photograph the
      // Loading… state: every settled arm has already left it by the time it paints.
      if (which === 'recent-hang') return true
      if (which === 'recent' || which === 'both') { await fail(route); return true }
      await json(route, { dirs: [PROJECT] })
      return true
    }
    if (path === '/api/browse-dirs') {
      // 'listing-hang' leaves the request open so the CLIENT deadline fires: the 500
      // arm only reaches "Unable to list folder", never the timeout wording.
      if (which === 'listing-hang') return true
      if (which === 'listing' || which === 'both') { await fail(route); return true }
      await json(route, { path: '/home/user', parent: '/home', dirs: [{ name: 'workspace', path: '/home/user/workspace' }] })
      return true
    }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }

  await stubDashboardApi(page, { slots, theme: 'dark', extra })
  await page.addInitScript(slot => { localStorage.setItem('mc-active-slot', slot) }, SLOT)
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2000)

  const chip = page.getByRole('button', { name: `Project: ${PROJECT}` }).first()
  const opened = await chip.click().then(() => true).catch(() => false)
  // Recents that SUCCEED land the picker on the Recent tab, so the Browse tab's listing
  // notice is not mounted at all -- the listing arm has to switch to it to be photographed.
  if (opened && (which === 'listing' || which === 'listing-hang')) {
    await page.waitForTimeout(600)
    await page.getByRole('button', { name: 'Browse' }).first().click().catch(() => {})
  }
  // Both reads run the shared retry policy's one non-deadline retry before they settle.
  const found = opened && await page.locator(`text=${expected}`).first()
    .waitFor({ timeout: 20000 }).then(() => true).catch(() => false)
  await page.waitForTimeout(400)
  // Counted because a screenshot cannot evidence a COUNT: a reader cannot tell one Retry from
  // two, nor "no button" from "button below the fold".
  const retries = await page.getByRole('button', { name: 'Retry' }).count()
  // Reopening also re-reads both sources, but that is not discoverable from the failed state,
  // so each notice carries its own Retry.
  const retryOk = opts.expectRetries === undefined ? true : retries === opts.expectRetries
  await page.screenshot({ path: `${OUT}/${shot}.png` })
  console.log('wrote', `${OUT}/${shot}.png`, { which, opened, found, retries })
  await page.close()
  return found && retryOk
}

/**
 * The file rail's tree-failure notice, which no other arm reaches: the rail is mounted by
 * `SidePanel` only while `useTreeAvailable` says so, and that is now cause-keyed -- a
 * TIMEOUT keeps it mounted so the notice and its Refresh stay reachable, where a refusal
 * still hides it. So this arm has to hang `/api/project/tree` rather than 500 it.
 *
 * The rail hangs off a FILE tab with the rail toggled open, both persisted per slot, so
 * seeding the tab strip plus `mc-files-rail-open` reaches what a real click produces.
 */
async function captureRailArm(context, base, expected, shot) {
  const page = await context.newPage()
  logPageProblems(page)

  const FILE = PROJECT + '/src/index.ts'
  const extra = async (path, route) => {
    // Hang, not 500: a 5xx is the `failed` cause, which also keeps the rail -- but the
    // deadline is the state this PR adds and the one the copy names.
    if (path === '/api/project/tree') return true
    if (path === '/api/project/git/status') {
      await json(route, { repo: true, files: [] })
      return true
    }
    if (path === '/api/file') {
      await json(route, { path: FILE, content: 'export const a = 1\n' })
      return true
    }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }

  await stubDashboardApi(page, { slots, theme: 'dark', extra })
  await page.addInitScript(([slot, file]) => {
    localStorage.setItem('mc-active-slot', slot)
    localStorage.setItem('mc-activity-open:' + slot, 'true')
    localStorage.setItem('mc-files-rail-open', '1')
    localStorage.setItem('mc-panel-tabs:' + slot, JSON.stringify({
      activeId: 'file:' + file,
      tabs: [{ id: 'file:' + file, kind: 'file', title: 'index.ts', path: file, slot }],
    }))
  }, [SLOT, FILE])
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)

  // Waits out the real 10s browse bound before the notice can render.
  const found = await page.locator(`text=${expected}`).first()
    .waitFor({ timeout: 20000 }).then(() => true).catch(() => false)
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${shot}.png` })
  console.log('wrote', `${OUT}/${shot}.png`, { found })
  await page.close()
  return found
}

/**
 * The @-menu state where a refetch fails OVER surviving rows -- the one arm that shows the
 * suffix-free cause copy, because with rows present Enter still picks a row rather than
 * sending, so the notice must NOT promise "Enter sends". It is also where Tab changes
 * meaning: the hand-off gives it to the Retry instead of the highlighted row.
 *
 * Reached the way production reaches it: the first search SUCCEEDS and is cached under its
 * key, then the endpoint starts failing and the same key is re-entered, so react-query
 * serves the cached rows while the refetch rejects.
 */
async function captureStaleRowsArm(context, base, expected, shot) {
  const page = await context.newPage()
  logPageProblems(page)

  let calls = 0
  const extra = async (path, route) => {
    if (path === '/api/file-search') {
      calls += 1
      // First answer seeds the rows; every later one fails, which is what leaves an error
      // sitting over cached results instead of an empty list.
      if (calls === 1) {
        await json(route, {
          results: [{ path: PROJECT + '/src/kept.ts', name: 'kept.ts', size: 12, mtime: 1 }],
          root: PROJECT,
        })
        return true
      }
      await route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"boom"}' })
      return true
    }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }

  await stubDashboardApi(page, { slots, theme: 'dark', extra })
  await page.addInitScript(slot => { localStorage.setItem('mc-active-slot', slot) }, SLOT)
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2000)

  const composer = page.locator('textarea').first()
  await composer.click()
  await composer.pressSequentially(MESSAGE, { delay: 15 })
  await page.locator('text=kept.ts').first().waitFor({ timeout: 20000 }).catch(() => {})

  // Rows survive only on a SAME-key refetch, where the key already holds data -- on a new key
  // an error drops the placeholder. `staleTime` serves the same key from cache until it goes
  // stale, so the wait is what makes re-entering that key fetch again.
  await page.waitForTimeout(11000)
  await composer.press('Backspace')
  await page.waitForTimeout(300)
  await composer.pressSequentially(MESSAGE.slice(-1), { delay: 15 })

  const found = await page.locator(`text=${expected}`).first()
    .waitFor({ timeout: 20000 }).then(() => true).catch(() => false)
  const rowsSurvived = await page.locator('text=kept.ts').first().isVisible().catch(() => false)
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${shot}.png` })
  console.log('wrote', `${OUT}/${shot}.png`, { found, rowsSurvived })
  await page.close()
  return found && rowsSurvived
}

/**
 * The folder panel's four cause-keyed strings, which no other arm reaches.
 *
 * Production opens this from a directory chip: `handleFolderOpen` calls
 * `tabsCtl.openFolder` AND `dispatch(openActivityPanel())`. Both halves persist per slot
 * -- the tab strip under `mc-panel-tabs:<slot>`, the panel's open flag under
 * `mc-activity-open:<slot>` -- so seeding both reaches the state that click produces.
 */
async function captureFolderPanelArm(context, base, mode, expected, shot) {
  const page = await context.newPage()
  logPageProblems(page)

  const DIR = mode === 'tree-hang' ? PROJECT : PROJECT + '/src'
  const extra = async (path, route) => {
    // At the project root the panel renders the TREE, so its own tree-failure notice is only
    // reachable by hanging that read with the panel opened at the root itself.
    if (path === '/api/project/tree' && mode === 'tree-hang') return true
    if (path === '/api/browse-files') {
      if (mode === 'listing-hang') return true
      if (mode === 'listing-error') {
        await route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"boom"}' })
        return true
      }
      await json(route, { path: DIR, parent: PROJECT, dirs: [], files: [] })
      return true
    }
    if (path === '/api/file-search') {
      if (mode === 'search-hang') return true
      if (mode === 'search-denied') {
        await route.fulfill({
          status: 403, contentType: 'application/json',
          body: JSON.stringify({ error: 'permission denied', code: 'access_denied' }),
        })
        return true
      }
      if (mode === 'search-404') {
        await route.fulfill({
          status: 404, contentType: 'application/json',
          body: JSON.stringify({ error: 'project root missing', code: 'project_not_found' }),
        })
        return true
      }
      await json(route, { results: [], root: PROJECT })
      return true
    }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }

  await stubDashboardApi(page, { slots, theme: 'dark', extra })
  await page.addInitScript(([slot, dir]) => {
    localStorage.setItem('mc-active-slot', slot)
    localStorage.setItem('mc-activity-open:' + slot, 'true')
    localStorage.setItem('mc-panel-tabs:' + slot, JSON.stringify({
      activeId: 'folder:' + dir,
      tabs: [{ id: 'folder:' + dir, kind: 'folder', title: 'src', path: dir, slot }],
    }))
  }, [SLOT, DIR])
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)

  if (mode.startsWith('search-')) {
    const box = page.getByPlaceholder('Search files').first()
    await box.waitFor({ timeout: 10000 }).catch(() => {})
    await box.click().catch(() => {})
    await box.pressSequentially('report', { delay: 15 }).catch(() => {})
  }

  const found = await page.locator(`text=${expected}`).first()
    .waitFor({ timeout: 20000 }).then(() => true).catch(() => false)
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${shot}.png` })
  console.log('wrote', `${OUT}/${shot}.png`, { mode, found })
  await page.close()
  return found
}


async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 950 },
    deviceScaleFactor: 2,
    locale: 'en-US',
  })

  const timedOut = await captureArm(
    context, base, 'hang',
    'File search timed out — Enter sends the message', '1-timed-out-enter-sends')
  const failed = await captureArm(
    context, base, 'error',
    'File search failed — Enter sends the message', '2-failed-enter-sends')
  // The settled-empty state is UNCHANGED by this PR and is the contrast that makes the
  // two failure strings legible: a 200 carrying zero rows really is an absence.
  const empty = await captureArm(
    context, base, 'empty',
    'No matching files — Enter sends the message', '3-settled-empty-enter-sends')
  // The two cause-keyed refusals. Each needs its own capture because the whole point of
  // the `code` contract is that these two read differently from the generic failure --
  // one photograph of "failed" cannot evidence either.
  const denied = await captureArm(
    context, base, 'denied',
    'No access to the project folder — Enter sends the message', '4-denied-enter-sends')
  const notFound = await captureArm(
    context, base, 'not-found',
    'Project folder not found — Enter sends the message', '5-not-found-enter-sends')

  // The project picker's own states, on a different surface from the @-menu.
  // The Ctrl+Enter spelling of the same two states.
  const timedOutCtrl = await captureArm(
    context, base, 'hang', 'File search timed out — Ctrl+Enter sends the message',
    '9-timed-out-ctrl-enter-sends', { ctrlEnter: true })
  const deniedCtrl = await captureArm(
    context, base, 'denied', 'No access to the project folder — Ctrl+Enter sends the message',
    '10-denied-ctrl-enter-sends', { ctrlEnter: true })

  const pickerHang = await capturePickerArm(
    context, base, 'listing-hang', 'Folder listing timed out', '11-picker-listing-timed-out',
    { expectRetries: 1 })
  const fpListTimeout = await captureFolderPanelArm(
    context, base, 'listing-hang', 'Folder listing timed out', '12-panel-listing-timed-out')
  const fpSearchTimeout = await captureFolderPanelArm(
    context, base, 'search-hang', 'Search timed out', '13-panel-search-timed-out')
  const fpDenied = await captureFolderPanelArm(
    context, base, 'search-denied', 'No access to this folder', '14-panel-search-denied')
  const fpNotFound = await captureFolderPanelArm(
    context, base, 'search-404', 'Folder not found', '15-panel-search-not-found')
  // Two arms ux-review found missing entirely. `listing-error` was already implemented here but
  // never called, so the non-timeout listing copy had no shot at all.
  const fpListFailed = await captureFolderPanelArm(
    context, base, 'listing-error', 'Unable to list folder', '19-panel-listing-failed')
  const fpTreeError = await captureFolderPanelArm(
    context, base, 'tree-hang', "Couldn't load the file tree", '20-panel-tree-error')

  // The two states that had no capture arm at all. The rail names the TREE, not the listing:
  // it renders `pages.chat.filesHome.tree_error`, the same string FolderPanel's root notice uses.
  const railTree = await captureRailArm(
    context, base, "Couldn't load the file tree", '16-rail-tree-timed-out')
  const staleRows = await captureStaleRowsArm(
    context, base, 'File search failed', '17-menu-failed-over-stale-rows')

  const pickerRecent = await capturePickerArm(
    context, base, 'recent', 'Recent projects unavailable', '6-picker-recent-unavailable')
  const pickerListing = await capturePickerArm(
    context, base, 'listing', 'Unable to list folder', '7-picker-listing-failed')
  const pickerBoth = await capturePickerArm(
    context, base, 'both', 'Recent projects unavailable', '8-picker-both-failed-with-retry',
    { expectRetries: 2 })
  // The pending state UX found in no committed shot. It is only reachable while the read is
  // still open, so it needs the hanging route rather than any settled arm.
  const pickerLoading = await capturePickerArm(
    context, base, 'recent-hang', 'Loading…', '18-picker-recents-loading')

  await browser.close()
  srv.close()

  console.log({ timedOut, failed, empty, denied, notFound, pickerRecent, pickerListing, pickerBoth, pickerLoading })
  if (!timedOut || !failed || !empty || !denied || !notFound
      || !pickerRecent || !pickerListing || !pickerBoth || !pickerLoading
    || !timedOutCtrl || !deniedCtrl || !pickerHang
    || !fpListTimeout || !fpSearchTimeout || !fpDenied || !fpNotFound
    || !fpListFailed || !fpTreeError
    || !railTree || !staleRows) {
    console.error('FAIL: an arm did not render its own copy (flags above)')
    process.exit(1)
  }
  console.log('PASS: deadline names the timeout, 5xx says failed, 200-empty says no '
    + 'matches, and the two coded refusals name their own cause')
}

main().catch(err => { console.error(err); process.exit(1) })
