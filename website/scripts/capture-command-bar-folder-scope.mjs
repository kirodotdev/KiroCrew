/**
 * Screenshot harness for the Command Bar's FOLDERS scope.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server and
 * answers every /api/** call from fixtures through `stubDashboardApi`. No gateway,
 * no dashboard auth, no kiro-cli.
 *
 * What the frames evidence is one claim: a folder is reached the way a session is
 * — press Enter on a row, then type — and NOT by folder rows spread through the
 * first page, and not from a second surface either. So the frames are the states of
 * that path plus the surface it is NOT on:
 *   1. the root page, where Search Sessions and Search Folders sit as siblings
 *      and no folder NAME appears
 *   2. inside the folders scope with an empty query: the breadcrumb names it, the
 *      placeholder changes, the whole corpus is listed in sidebar order
 *   3. inside the scope with a query: narrowed rows, each with its ancestry path
 *   4. the host quick-search palette, reached by turning the launcher OFF: typing
 *      "fold" raises no Folders scope, and Tab adopts none
 *   5. the root's other door: the tail row "Search folders for <query>", which
 *      carries the typed text into the view the row in frame 1 enters empty
 *   6. inside the scope with nothing filed: "No folders yet", in folder words
 *   7. inside the scope when the read fails: the failure said by `ErrorNotice`
 *      above the list, Retry still an option inside it
 *   8. inside the scope with a query matching nothing: the full list offered back
 *      as a row, and the footer verb that names it
 *
 * Frame 1 is the load-bearing one: it is the frame that fails if the flat folder
 * group ever comes back. Frame 4 is its counterpart for the other direction — it
 * fails if a second folder search is added back to the surface underneath. Frames
 * 5-8 are the states a reader meets when the happy path does not hold, which is
 * where a launcher usually goes quiet.
 *
 * Usage: node scripts/capture-command-bar-folder-scope.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/command-bar-folder-scope'

mkdirSync(OUT, { recursive: true })

const SLOT = 'chat-1'

/**
 * The launcher is a builtin so it claims the quick-search slot — without this the
 * chord opens the old quick-search overlay and every frame is of the wrong surface.
 */
const APPS = [
  {
    name: 'command-bar',
    displayName: 'Command Bar',
    enabled: true,
    origin: 'builtin',
    source: 'builtin',
    version: '0.1.0',
    manifest: {
      name: 'command-bar',
      displayName: 'Command Bar',
      version: '0.1.0',
      ui: { overlays: [{ id: 'command-bar', replaces: 'quick-search' }] },
    },
  },
]

/**
 * The same app, DISABLED, which is how frame 4 reaches the surface underneath: a
 * disabled app claims no slot, so the chord opens the host's own quick-search.
 * Deliberately not an empty list — the app being absent and the app being off must
 * look the same to the slot resolver, and using the off case exercises that.
 */
const APPS_LAUNCHER_OFF = [{ ...APPS[0], enabled: false }]

/**
 * A NESTED tree, not a flat list: the breadcrumb under a row is the thing a flat
 * fixture cannot photograph, and it is also what makes a name like "oss" readable
 * when two parents both hold one.
 */
const FOLDERS = [
  { id: 'f-kirocrew', name: 'kirocrew', order: 0, collapsed: false },
  { id: 'f-oss', name: 'oss', order: 0, collapsed: false, parent_id: 'f-kirocrew' },
  { id: 'f-reviews', name: 'reviews', order: 1, collapsed: false, parent_id: 'f-kirocrew' },
  { id: 'f-personal', name: 'personal', order: 1, collapsed: false },
  { id: 'f-finance', name: 'finance', order: 0, collapsed: false, parent_id: 'f-personal' },
  { id: 'f-travel', name: 'travel', order: 1, collapsed: false, parent_id: 'f-personal' },
]

function assert(label, ok, detail = '') {
  console.log(`${label}: ${ok ? 'OK' : 'FAIL'}${detail ? ` — ${detail}` : ''}`)
  if (!ok) throw new Error(`${label} failed${detail ? `: ${detail}` : ''}`)
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

async function openBar(apps = APPS, folders = FOLDERS, opts = {}) {
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 1 })
  const page = await context.newPage()

  // `/api/apps` is what decides WHICH overlay the chord opens. Without it the stub
  // answers an empty list, the launcher never claims `quick-search`, and Control+K
  // opens the OLD Search Everywhere palette — whose rows look plausible enough that
  // the frames would be of the wrong surface. The assertions below are what caught it.
  const extra = async (path, route) => {
    if (path === '/api/apps') {
      await json(route, apps)
      return true
    }
    // The failed-read frame needs the folder endpoint to REJECT, which the fixture
    // path cannot express: answered before the stub's own handler so the same
    // endpoint the sidebar reads is the one that fails, as it would in production.
    if (opts.failFolders && path === '/api/chat/folders') {
      await route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'folder listing unavailable' }),
      })
      return true
    }
    return false
  }

  await stubDashboardApi(page, {
    folders,
    slots: [{ key: SLOT, messages: 0, running: false, agent: 'default', mode: '' }],
    extra,
  })

  await page.goto(`${base}/chat`)
  await page.waitForLoadState('networkidle')
  await page.keyboard.press('Control+k')
  await page.waitForSelector('[role="dialog"]', { timeout: 10_000 })
  return { context, page }
}

/** The query field. Addressed by its attribute rather than its computed ARIA role:
 *  the role is what the overlay sets, and matching the attribute cannot be thrown
 *  off by how a browser build maps `combobox` on a text input. */
const box = page => page.locator('[role="dialog"] input').first()

/** Enter the folders scope the way the list binds activation: mousedown on the row. */
async function enterFolders(page) {
  await box(page).fill('folders')
  const row = page.getByRole('option').filter({ hasText: 'Search Folders' }).first()
  await row.dispatchEvent('mousedown')
  // The breadcrumb is the scope's own proof: wait for it, not for a timeout.
  await page.getByText('Search Folders', { exact: true }).first().waitFor({ timeout: 5_000 })
}

async function shot(page, name) {
  await page.waitForTimeout(350)
  const file = join(OUT, name)
  await page.screenshot({ path: file })
  console.log(`wrote ${file}`)
}

// ── 1. the root page: one row per corpus, zero folder names ────────────
{
  const { context, page } = await openBar()
  // A query is required: with an empty box the root page is recents + New Session,
  // and the Commands group (which BOTH corpus rows live in) is not listed at all.
  // That is the same for Sessions and Folders, which is the point of the frame.
  await box(page).fill('search')
  await page.getByRole('option').filter({ hasText: 'Search Folders' }).first().waitFor({ timeout: 5_000 })
  const rows = await page.getByRole('option').allTextContents()
  const joined = rows.join(' | ')
  assert('root offers Search Folders', /Search Folders/.test(joined), joined.slice(0, 200))
  assert('root offers Search Sessions', /Search Sessions/.test(joined), joined.slice(0, 200))
  await shot(page, '1-root-corpus-rows.png')

  // The flat group is gone: typing a FOLDER's own name at the root must not put
  // that folder in the list. Checked against the fixture's names so a renamed
  // fixture cannot pass vacuously.
  await box(page).fill('oss')
  await page.waitForTimeout(400)
  const afterName = await page.getByRole('option').allTextContents()
  const leaked = FOLDERS.map(f => f.name).filter(n => afterName.some(r => r.trim().startsWith(n)))
  assert('no folder row at root', leaked.length === 0, `leaked: ${leaked.join(', ')}`)
  await context.close()
}

// ── 2. inside the scope, empty query: whole corpus, in sidebar order ────────
{
  const { context, page } = await openBar()
  await enterFolders(page)
  await box(page).fill('')
  await page.getByRole('option').filter({ hasText: 'travel' }).first().waitFor({ timeout: 5_000 })
  const placeholder = await box(page).getAttribute('placeholder')
  assert('placeholder switched to folders', placeholder === 'Search all folders…', String(placeholder))
  const rows = await page.getByRole('option').allTextContents()
  assert('empty query lists the corpus', rows.length >= FOLDERS.length, `${rows.length} rows`)
  await shot(page, '2-scope-empty-query.png')
  await context.close()
}

// ── 3. inside the scope, a query: narrowed rows with their ancestry path ────
{
  const { context, page } = await openBar()
  await enterFolders(page)
  await box(page).fill('oss')
  // Wait for the FILTER, not for a row that was already on screen: the query is
  // debounced, so `oss` matches the stale `oss` row instantly and reading here
  // returned the whole unfiltered corpus. A row that must LEAVE is the only wait
  // that proves the new query was applied.
  await page.getByRole('option').filter({ hasText: 'travel' }).first().waitFor({ state: 'detached', timeout: 5_000 })
  const rows = await page.getByRole('option').allTextContents()
  assert('query narrows the corpus', rows.length < FOLDERS.length, `${rows.length} rows`)
  assert('row carries its parent path', /kirocrew/.test(rows.join(' | ')), rows.join(' | ').slice(0, 200))
  await shot(page, '3-scope-query-narrowed.png')
  await context.close()
}

// ── 4. the surface underneath: the host palette has no folders scope ────────
{
  const { context, page } = await openBar(APPS_LAUNCHER_OFF)
  const input = box(page)
  // Prove WHICH surface this is before asserting anything about it. The launcher's
  // own field never carries this placeholder, so a frame of the wrong overlay fails
  // here instead of photographing a vacuous pass.
  assert(
    'launcher off, host palette open',
    (await input.getAttribute('placeholder')) === 'Search for anything',
    String(await input.getAttribute('placeholder')),
  )

  // "fold" uniquely prefixes the Folders scope this host used to carry, so it is the
  // query that would raise the Tab hint if the scope came back.
  await input.fill('fold')
  await page.waitForTimeout(500)
  const dialog = await page.locator('[role="dialog"]').innerText()
  assert('no folders scope hint', !/Folders/i.test(dialog), dialog.replace(/\n/g, ' | ').slice(0, 200))
  await shot(page, '4-host-palette-no-folders-scope.png')

  // Tab is what ADOPTS a hinted scope. With no folders provider there is nothing to
  // adopt, so the placeholder must not narrow and the query must survive.
  await page.keyboard.press('Tab')
  await page.waitForTimeout(300)
  assert(
    'Tab adopts no folders scope',
    (await input.getAttribute('placeholder')) === 'Search for anything',
    String(await input.getAttribute('placeholder')),
  )
  assert('query survives the Tab', (await input.inputValue()) === 'fold', await input.inputValue())
  await context.close()
}

// ── 5. the root's OTHER door into the same view: the tail row carrying the query ─
{
  const { context, page } = await openBar()
  // A typed folder name leaves the root with no folder row (frame 1) — this is the
  // row that says where that name CAN be searched, and it carries the text along.
  await box(page).fill('oss')
  const tail = page.getByRole('option').filter({ hasText: 'Search folders for' }).first()
  await tail.waitFor({ timeout: 5_000 })
  const tailText = (await tail.textContent()) || ''
  assert('tail row carries the typed query', tailText.includes('oss'), tailText)
  // The row sits below the two sibling fallbacks, so the frame has to be scrolled to
  // it: an unscrolled shot is what the review could not read it from.
  await tail.scrollIntoViewIfNeeded()
  await shot(page, '5-root-tail-row-carries-query.png')
  await context.close()
}

// ── 6. inside the scope with NOTHING filed: an empty corpus, in folder words ─────
{
  const { context, page } = await openBar(APPS, [])
  await enterFolders(page)
  await box(page).fill('')
  await page.getByText(/No folders yet/).first().waitFor({ timeout: 5_000 })
  const dialog = await page.locator('[role="dialog"]').innerText()
  // The sessions copy reports a match against a query the reader never typed, which
  // is the mistake this state exists to avoid.
  assert('empty corpus speaks of folders', /No folders yet/.test(dialog), dialog.slice(0, 160))
  assert('not the sessions empty copy', !/No sessions match/.test(dialog), dialog.slice(0, 160))
  await shot(page, '6-scope-empty-corpus.png')
  await context.close()
}

// ── 7. inside the scope when the read FAILS: the notice above, Retry in the list ─
{
  const { context, page } = await openBar(APPS, FOLDERS, { failFolders: true })
  await enterFolders(page)
  // Scoped to the DIALOG: the sidebar reads the same endpoint, so it raises its own
  // ErrorNotice behind the overlay, and an unscoped query asserts against that one.
  const alert = page.locator('[role="dialog"]').getByRole('alert').first()
  await alert.waitFor({ timeout: 5_000 })
  const alertText = (await alert.textContent()) || ''
  assert('failure renders through ErrorNotice', /Search failed/.test(alertText), alertText.slice(0, 160))
  // The backend's own wording never reaches the copy.
  assert('backend detail stays out of the copy', !/unavailable/i.test(alertText), alertText.slice(0, 160))
  const rows = await page.getByRole('option').allTextContents()
  assert('Retry stays an option in the list', rows.some(r => /Retry/.test(r)), rows.join(' | ').slice(0, 160))
  assert('failure text is not a row', !rows.some(r => /Search failed/.test(r)), rows.join(' | ').slice(0, 160))
  await shot(page, '7-scope-read-failed.png')
  await context.close()
}

// ── 8. inside the scope with a query that matches nothing: the way back is a row ─
{
  const { context, page } = await openBar()
  await enterFolders(page)
  await box(page).fill('zzzznope')
  const dead = page.getByRole('option').filter({ hasText: 'No folders match' }).first()
  await dead.waitFor({ timeout: 5_000 })
  const deadText = (await dead.textContent()) || ''
  assert('dead end names the query', deadText.includes('zzzznope'), deadText)
  // Its Enter verb names the list it returns to, which is not recency.
  const dialog = await page.locator('[role="dialog"]').innerText()
  assert('footer verb is Show All Folders', /Show All Folders/.test(dialog), dialog.slice(0, 200))
  await shot(page, '8-scope-no-match.png')
  await context.close()
}

await browser.close()
srv.close()