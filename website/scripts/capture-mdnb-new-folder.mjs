/**
 * Screenshot harness for creating a FOLDER in the Notes app.
 *
 * The change under review adds a create trigger to folder rows (one `+` on
 * the hover bar, opening a two-item menu: new note in this folder, new
 * subfolder), the same two-item menu behind the header's `+`, and an inline
 * name field rendered where the folder will land. A folder is born with its
 * first note — the tree is derived from note paths — so the evidence is the
 * whole gesture, not one still: the trigger the user discovers on hover, the
 * menu it opens, the field they type into, and the tree after Enter with the
 * new folder holding its opened first note.
 *
 * Frame 01: pointer over the `Design` folder row, its menu open (the row keeps
 *           two actions: its own toggle and this trigger).
 * Frame 02a: the name field open under `Design`, empty — the hint under it is
 *           the in-UI disclosure that a folder is born with an empty note. It is
 *           a hint, not the placeholder: the rail clips a long placeholder and
 *           typing removes it, so a consequence written there is never read.
 * Frame 02: the same field, the name typed, not committed — the hint stays.
 * Frame 02b: a name the cleaner would change (`2026/Q1`): the hint shows the
 *           name that will actually be created, before Enter.
 * Frame 03: after Enter — `Design/Projects` exists, `Untitled` is open.
 * Frame 04: the header's create menu open (one trigger, two items — the row
 *           keeps two controls: vault selector and this).
 * Frame 06: the ROOT name field, placed by the page above the list after
 *           "New folder at the top level" (it shows in list view too).
 * Frame 07: a refused name (`CON`) — the field stays open with the draft and
 *           the reason beside it; nothing is created and no error notice shows.
 * Frame 07b: the other refusal — a name that is nothing but illegal characters
 *           (`?*|`), so nothing usable is left once they are removed.
 * Frame 08: the same validation on the RENAME field (`Inbox` → `CON`): the
 *           reason sits under the note's name field the same way.
 * Frame 05: a 390px touch phone. No hover exists there, so the folder trigger
 *           is shown outright — in the row's flow beside the note count, not
 *           floating over it — and its menu is open from a tap; the script also
 *           asserts the bar's computed opacity is 1 and that its box does not
 *           overlap the count's, since those are the properties a still cannot
 *           prove.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures. No gateway, no token.
 * `/note/new` is answered here, ahead of the shared stub, because the shared
 * stub knows nothing of creation: it returns the path the backend would, and
 * appends the note so the next listing shows the folder.
 *
 * Usage: node scripts/capture-mdnb-new-folder.mjs [outDir]
 */
import { chromium, devices } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { MDNB_VAULT_ID, mdnbApiStub, mdnbNoteDoc } from './lib/mdnb-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/mdnb-new-folder'
mkdirSync(OUT, { recursive: true })

const VIEW = { width: 1280, height: 900 }

const VAULT = {
  id: MDNB_VAULT_ID,
  name: 'My Notes',
  repo: null,
  branch: null,
  localPath: '/srv/notes',
  readOnly: false,
  external: true,
  localOnly: true,
  knowledge: false,
  knowledgeSourceId: null,
}

const OPEN_PATH = 'Design/icon craft.md'
const OPEN_TITLE = 'Icon craft'
const PARENT = 'Design'
const NEW_FOLDER = 'Projects'
const NEW_NOTE_PATH = `${PARENT}/${NEW_FOLDER}/Untitled.md`
/** 120 code points, no space: the cleaner's ceiling, and unbreakable text. */
const LONG_FOLDER = `Zettelkasten-${'x'.repeat(107)}`

/** Mutable on purpose: `/note/new` appends to it, and the stub lists it by reference. */
const NOTES = [
  { path: OPEN_PATH, title: OPEN_TITLE, modifiedAt: Date.now() - 8.6e7, syncStatus: 'synced' },
  { path: 'Design/Sessions/session list rows.md', title: 'Session list rows', modifiedAt: Date.now() - 5.4e6, syncStatus: 'synced' },
  { path: 'Meetings/design review 2026-08-18.md', title: 'Design review 2026-08-18', modifiedAt: Date.now() - 1.3e5, syncStatus: 'synced' },
  { path: 'Inbox.md', title: 'Inbox', modifiedAt: Date.now() - 2.6e6, syncStatus: 'synced' },
  // A folder named with the longest single word the cleaner lets through, so
  // the frames show the label ellipsized rather than pushing the count — and,
  // on the phone, the create trigger — out of the row.
  { path: `${LONG_FOLDER}/scratch.md`, title: 'Scratch', modifiedAt: Date.now() - 9.9e6, syncStatus: 'synced' },
]

const CONTENT = `# ${OPEN_TITLE}

Glyph weight and optical size across the rail.
`

const shared = mdnbApiStub({ vault: VAULT, notes: NOTES, doc: mdnbNoteDoc(OPEN_PATH, CONTENT) })

/** The creation route first, everything else to the shared stub. */
async function mdnbApi(path, route) {
  if (path.startsWith('/apps/md-notebook/api/note/new')) {
    const body = JSON.parse(route.request().postData() || '{}')
    const rel = body.folder ? `${body.folder}/Untitled.md` : 'Untitled.md'
    NOTES.push({ path: rel, title: 'Untitled', modifiedAt: Date.now(), syncStatus: 'synced' })
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ path: rel }) })
    return true
  }
  // The new note is read right after creation: serve it empty, as the backend does.
  if (path.startsWith('/apps/md-notebook/api/note?') && path.includes(encodeURIComponent(NEW_NOTE_PATH))) {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(mdnbNoteDoc(NEW_NOTE_PATH, '')),
    })
    return true
  }
  return shared(path, route)
}

/** Clip the rail alone — same walk as the other Notes harnesses. */
async function railClip(page) {
  return page.evaluate(() => {
    let el = document.querySelector('.mdnb-search')
    while (el && el !== document.body) {
      const s = getComputedStyle(el)
      if (s.borderRadius.startsWith('16px') && s.flexDirection === 'column') break
      el = el.parentElement
    }
    const r = (el && el !== document.body ? el : document.body).getBoundingClientRect()
    return {
      x: Math.max(0, Math.round(r.left) - 12),
      y: Math.max(0, Math.round(r.top) - 12),
      width: Math.round(r.width) + 24,
      height: Math.round(r.height) + 24,
    }
  })
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2, locale: 'en-US' })
    const page = await context.newPage()
    await stubDashboardApi(page, { theme: 'dark', extra: mdnbApi })
    logPageProblems(page)
    await page.addInitScript(vaultId => {
      localStorage.setItem('mdnb-active-vault', vaultId)
      localStorage.setItem('mdnb-list-view', 'folders')
      localStorage.setItem('mc-color-theme', 'kiro')
    }, MDNB_VAULT_ID)

    await page.goto(base + '/md-notebook', { waitUntil: 'domcontentloaded' })
    await page.getByText(OPEN_TITLE).first().waitFor({ timeout: 15000 })
    // Fail loudly rather than shipping a frame of the wrong theme: the palette
    // and the mode compose into the `data-theme` the stylesheet keys off.
    const applied = await page.evaluate(() => document.documentElement.dataset.theme || '')
    if (applied !== 'kiro-dark') throw new Error(`theme mismatch: wanted kiro-dark, got ${applied || '(none)'}`)
    await page.getByText(OPEN_TITLE).first().click()
    await page.waitForTimeout(500)

    // 01 — hover the folder row, open its create menu: that is the discovery.
    const folderRow = page.getByRole('button', { name: PARENT, exact: true })
    await folderRow.hover()
    await folderRow.getByRole('button', { name: 'New note or subfolder here' }).click()
    const subfolderItem = folderRow.getByRole('button', { name: 'New subfolder' })
    await subfolderItem.waitFor({ timeout: 5000 })
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/01-folder-menu.png`, clip: await railClip(page) })
    console.log('wrote', `${OUT}/01-folder-menu.png`)

    // 02 — the name field, in the folder's place, with the name typed.
    await subfolderItem.click()
    const field = page.getByRole('textbox', { name: 'Folder name' })
    await field.waitFor({ timeout: 5000 })
    await page.waitForTimeout(200)
    await page.screenshot({ path: `${OUT}/02a-name-placeholder.png`, clip: await railClip(page) })
    console.log('wrote', `${OUT}/02a-name-placeholder.png`)
    await field.fill(NEW_FOLDER)
    await page.waitForTimeout(200)
    await page.screenshot({ path: `${OUT}/02-name-field.png`, clip: await railClip(page) })
    console.log('wrote', `${OUT}/02-name-field.png`)

    // 02b — the cleaner would change this name: the hint says what it becomes.
    await field.fill('2026/Q1')
    await page.getByText('Will be created as “2026Q1”').waitFor({ timeout: 5000 })
    await page.waitForTimeout(200)
    await page.screenshot({ path: `${OUT}/02b-name-cleaned-preview.png`, clip: await railClip(page) })
    console.log('wrote', `${OUT}/02b-name-cleaned-preview.png`)
    await field.fill(NEW_FOLDER)

    // 03 — committed: the folder exists in the tree, its first note is open.
    await field.press('Enter')
    await page.getByRole('button', { name: NEW_FOLDER, exact: true }).waitFor({ timeout: 5000 })
    await page.getByRole('button', { name: 'Untitled', exact: true }).waitFor({ timeout: 5000 })
    await page.waitForTimeout(500)
    await page.screenshot({ path: `${OUT}/03-created.png`, clip: await railClip(page) })
    console.log('wrote', `${OUT}/03-created.png`)

    // 04 — the header trigger and its two items.
    await page.getByRole('button', { name: 'New note or folder' }).click()
    await page.getByRole('button', { name: 'New folder at the top level' }).waitFor({ timeout: 5000 })
    await page.waitForTimeout(200)
    await page.screenshot({ path: `${OUT}/04-header-menu.png`, clip: await railClip(page) })
    console.log('wrote', `${OUT}/04-header-menu.png`)

    // 06 — the root field: above the list, not inside the tree.
    await page.getByRole('button', { name: 'New folder at the top level' }).click()
    const rootField = page.getByRole('textbox', { name: 'Folder name' })
    await rootField.waitFor({ timeout: 5000 })
    await rootField.fill('Archive')
    await page.waitForTimeout(200)
    await page.screenshot({ path: `${OUT}/06-root-field.png`, clip: await railClip(page) })
    console.log('wrote', `${OUT}/06-root-field.png`)

    // 07 — a refused name: the field keeps the draft, the reason sits under it.
    await rootField.fill('CON')
    await rootField.press('Enter')
    await page.getByText('Windows reserves this name (CON, NUL, LPT1…)').waitFor({ timeout: 5000 })
    if (await page.getByRole('alert').count()) throw new Error('a refused name must not raise the error notice')
    await page.waitForTimeout(200)
    await page.screenshot({ path: `${OUT}/07-name-refused.png`, clip: await railClip(page) })
    console.log('wrote', `${OUT}/07-name-refused.png`)

    // 07b — the other refusal: only illegal characters, nothing left to use.
    await rootField.fill('?*|')
    await rootField.press('Enter')
    await page
      .getByText('Nothing usable is left once illegal characters are removed')
      .waitFor({ timeout: 5000 })
    if (await page.getByRole('alert').count()) throw new Error('a refused name must not raise the error notice')
    await page.waitForTimeout(200)
    await page.screenshot({ path: `${OUT}/07b-name-empty-after-clean.png`, clip: await railClip(page) })
    console.log('wrote', `${OUT}/07b-name-empty-after-clean.png`)
    await rootField.press('Escape')

    // 08 — the rename field refuses the same names the same way.
    const inboxRow = page.getByRole('button', { name: 'Inbox', exact: true })
    await inboxRow.hover()
    await inboxRow.getByRole('button', { name: 'Rename note' }).click()
    const renameField = page.getByRole('textbox', { name: 'Note name' })
    await renameField.waitFor({ timeout: 5000 })
    await renameField.fill('CON')
    await renameField.press('Enter')
    await page.getByText('Windows reserves this name (CON, NUL, LPT1…)').waitFor({ timeout: 5000 })
    if (await page.getByRole('alert').count()) throw new Error('a refused rename must not raise the error notice')
    await page.waitForTimeout(200)
    await page.screenshot({ path: `${OUT}/08-rename-refused.png`, clip: await railClip(page) })
    console.log('wrote', `${OUT}/08-rename-refused.png`)
    await renameField.press('Escape')
    await context.close()

    // 05 — a touch phone: `(hover: none)` holds, so the folder bar is shown
    // without a pointer over it. Emulated through Playwright's device
    // descriptor (isMobile + hasTouch), which is what flips the media query.
    const phone = await browser.newContext({ ...devices['iPhone 13'], locale: 'en-US' })
    const mobile = await phone.newPage()
    await stubDashboardApi(mobile, { theme: 'dark', extra: mdnbApi })
    logPageProblems(mobile)
    await mobile.addInitScript(vaultId => {
      localStorage.setItem('mdnb-active-vault', vaultId)
      localStorage.setItem('mdnb-list-view', 'folders')
      localStorage.setItem('mc-color-theme', 'kiro')
    }, MDNB_VAULT_ID)
    await mobile.goto(base + '/md-notebook', { waitUntil: 'domcontentloaded' })
    await mobile.getByRole('button', { name: PARENT, exact: true }).waitFor({ timeout: 15000 })
    await mobile.waitForTimeout(500)
    const hoverNone = await mobile.evaluate(() => matchMedia('(hover: none)').matches)
    if (!hoverNone) throw new Error('phone emulation did not report (hover: none)')
    const opacity = await mobile.evaluate(() => {
      const bar = document.querySelector('.mdnb-folder-actions')
      return bar ? getComputedStyle(bar).opacity : 'missing'
    })
    if (opacity !== '1') throw new Error(`folder actions not shown on touch: opacity=${opacity}`)
    // Shown outright, the bar must sit BESIDE the note count, not over it: the
    // count is the bar's previous sibling, so their boxes may not intersect.
    const overlap = await mobile.evaluate(() => {
      const bar = document.querySelector('.mdnb-folder-actions')
      const count = bar?.previousElementSibling
      if (!bar || !count) return 'missing'
      const b = bar.getBoundingClientRect()
      const c = count.getBoundingClientRect()
      return b.left < c.right && c.left < b.right && b.top < c.bottom && c.top < b.bottom
    })
    if (overlap !== false) throw new Error(`folder actions overlap the note count on touch: ${overlap}`)
    // And a folder whose name is one unbreakable 120-code-point word keeps its
    // trigger on screen: the label is the part that gives, ellipsized.
    const longRow = await mobile.evaluate(name => {
      const rows = Array.from(document.querySelectorAll('.mdnb-folder-actions'))
      const bar = rows.find(el => el.closest('[role="button"]')?.textContent?.includes(name))
      if (!bar) return 'missing'
      const label = bar.parentElement?.querySelector('span[style*="ellipsis"]')
      return {
        barRight: bar.getBoundingClientRect().right,
        viewport: window.innerWidth,
        ellipsized: label ? label.scrollWidth > label.clientWidth : 'no label',
      }
    }, LONG_FOLDER.slice(0, 20))
    if (typeof longRow !== 'object' || longRow.barRight > longRow.viewport || longRow.ellipsized !== true) {
      throw new Error(`long folder name pushes its trigger off screen: ${JSON.stringify(longRow)}`)
    }
    // And the trigger is tappable there: the menu opens with no hover involved.
    // Tapped on the SECOND folder: at 390px the vault toolbar's Commit button
    // sits over the first row's right end, which predates this change.
    await mobile
      .getByRole('button', { name: 'Meetings', exact: true })
      .getByRole('button', { name: 'New note or subfolder here' })
      .tap()
    await mobile.getByRole('button', { name: 'New subfolder' }).waitFor({ timeout: 5000 })
    await mobile.waitForTimeout(300)
    await mobile.screenshot({ path: `${OUT}/05-touch-390.png`, fullPage: false })
    console.log('wrote', `${OUT}/05-touch-390.png`)
    await phone.close()
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
