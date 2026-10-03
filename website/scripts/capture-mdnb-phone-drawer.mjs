/**
 * Screenshot harness for the Notes app's PHONE drawer (#10254).
 *
 * At a phone width the notes panel is a `width: 100%` drawer. Its flex sibling,
 * the main column, was left 0px wide — and that column's header controls are
 * `position: absolute` at its right edge, so they were painted leftward OVER
 * the drawer: at 390px the *Commit* / *Save locally* pill sat on the right end
 * of the FIRST tree row (measured: row y 131–159, pill y 130–158, x 292–370),
 * and the Settings page's close button leaked the same way. The fix takes the
 * main column out of the flow while the drawer owns the pane.
 *
 * What needs evidence is the drawer at 390px with nothing painted over it, and
 * that the two things the pane shows without the drawer — the editor and the
 * Settings page — still arrive intact once the drawer steps aside. So:
 *
 *   01 — the drawer, no note open: the first row must be fully visible and NO
 *        sync pill may be on screen (asserted, not just photographed).
 *   02 — a note tapped: the drawer closes, the editor shows with its toolbar.
 *   03 — Settings tapped from the reopened drawer: the page owns the pane.
 *
 * Emulated through Playwright's iPhone 13 descriptor (isMobile + hasTouch),
 * which is what flips both `useIsMobile` and the `(hover: none)` query.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures. No gateway, no token.
 *
 * Usage: node scripts/capture-mdnb-phone-drawer.mjs [outDir]
 */
import { chromium, devices } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { MDNB_VAULT_ID, mdnbApiStub, mdnbNoteDoc } from './lib/mdnb-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/mdnb-phone-drawer'
mkdirSync(OUT, { recursive: true })

const OPEN_PATH = 'Design/type-ramp-audit.md'
const OPEN_TITLE = 'Type ramp audit'

/**
 * Two folders on purpose: the defect was on the FIRST row, and the earlier
 * phone frame in the new-folder harness had to tap the second one to dodge it.
 */
const NOTES = [
  { path: OPEN_PATH, title: OPEN_TITLE, modifiedAt: Date.now() - 6e5, syncStatus: 'synced' },
  { path: 'Design/icon craft.md', title: 'Icon craft', modifiedAt: Date.now() - 8.6e7, syncStatus: 'synced' },
  { path: 'Meetings/design review 2026-08-18.md', title: 'Design review 2026-08-18', modifiedAt: Date.now() - 1.3e5, syncStatus: 'synced' },
  { path: 'Inbox.md', title: 'Inbox', modifiedAt: Date.now() - 2.6e6, syncStatus: 'synced' },
]

const CONTENT = `# ${OPEN_TITLE}

The rail borrows the sessions list ramp rather than declaring a second one.
`

const mdnbApi = mdnbApiStub({ notes: NOTES, doc: mdnbNoteDoc(OPEN_PATH, CONTENT) })

/** The sync pill, whatever its label reads in this vault's state. */
const syncPill = page => page.getByRole('button', { name: /^(Save locally|Sync|Synced|Syncing)/ })

/**
 * Anything painted on the drawer's first row other than the row itself. Walks the
 * row's right edge with `elementFromPoint`, which is the same test a finger makes.
 */
async function coveringFirstRow(page, row) {
  const box = await row.boundingBox()
  if (!box) return ['row-not-found']
  return row.evaluate((el, b) => {
    const y = b.y + b.height / 2
    const strangers = new Set()
    for (let x = b.x + b.width - 4; x > b.x; x -= 8) {
      const hit = document.elementFromPoint(x, y)
      if (hit && !el.contains(hit) && !hit.contains(el)) {
        strangers.add(`${hit.tagName.toLowerCase()}${hit.textContent ? ':' + hit.textContent.trim().slice(0, 24) : ''}`)
      }
    }
    return [...strangers]
  }, box)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    const phone = await browser.newContext({ ...devices['iPhone 13'], locale: 'en-US' })
    const page = await phone.newPage()
    await stubDashboardApi(page, { theme: 'dark', extra: mdnbApi })
    logPageProblems(page)
    // Written in a LATER init script: the shared stub clears localStorage in its
    // own. `mdnb-list-view` = folders is what puts folder rows on screen.
    await page.addInitScript(vaultId => {
      localStorage.setItem('mdnb-active-vault', vaultId)
      localStorage.setItem('mdnb-list-view', 'folders')
      localStorage.setItem('mc-color-theme', 'kiro')
    }, MDNB_VAULT_ID)

    await page.goto(base + '/md-notebook', { waitUntil: 'domcontentloaded' })
    await page.getByRole('button', { name: 'Design', exact: true }).waitFor({ timeout: 15000 })
    await page.waitForTimeout(500)
    if (!(await page.evaluate(() => matchMedia('(hover: none)').matches))) {
      throw new Error('phone emulation did not report (hover: none)')
    }

    // 01 — the drawer alone. The pill must not be on screen at all: `display:
    // none` reports a 0x0 rect, and nothing but the row may answer a tap on it.
    const pillBox = await syncPill(page).boundingBox().catch(() => null)
    if (pillBox && pillBox.width > 0) {
      throw new Error(`sync pill painted over the drawer at ${JSON.stringify(pillBox)}`)
    }
    const covering = await coveringFirstRow(page, page.getByRole('button', { name: 'Design', exact: true }))
    if (covering.length) throw new Error(`first row covered by: ${covering.join(', ')}`)
    await page.screenshot({ path: `${OUT}/01-drawer-390.png` })
    console.log('wrote', `${OUT}/01-drawer-390.png`)

    // 02 — picking a note closes the drawer; the editor and its toolbar return.
    await page.getByText(OPEN_TITLE).first().tap()
    await syncPill(page).waitFor({ timeout: 5000 })
    await page.waitForTimeout(500)
    const editorPill = await syncPill(page).boundingBox()
    if (!editorPill || editorPill.width === 0) throw new Error('sync pill missing from the editor')
    await page.screenshot({ path: `${OUT}/02-editor-390.png` })
    console.log('wrote', `${OUT}/02-editor-390.png`)

    // 03 — Settings from the reopened drawer. Before, the drawer stayed forced
    // open (no note picked) and the page rendered 0px wide behind it.
    await page.getByRole('button', { name: 'Show notes panel' }).tap()
    await page.getByRole('button', { name: 'Design', exact: true }).waitFor({ timeout: 5000 })
    await page.locator('.mdnb-row[aria-label="Settings"]').tap()
    const close = page.getByRole('button', { name: 'Close settings' })
    await close.waitFor({ timeout: 5000 })
    await page.waitForTimeout(400)
    const closeBox = await close.boundingBox()
    if (!closeBox || closeBox.width === 0) throw new Error('Settings close button not painted')
    if (await page.getByRole('button', { name: 'Design', exact: true }).isVisible()) {
      throw new Error('the drawer must step aside for the Settings page it opened')
    }
    await page.screenshot({ path: `${OUT}/03-settings-390.png` })
    console.log('wrote', `${OUT}/03-settings-390.png`)

    await phone.close()
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
