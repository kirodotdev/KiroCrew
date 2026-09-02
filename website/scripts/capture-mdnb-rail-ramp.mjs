/**
 * Screenshot harness for the Notes app's LEFT RAIL type ramp.
 *
 * The change under review moves every size in the rail onto `RAIL_TYPE`
 * (apps/md-notebook/constants.ts), which mirrors the sessions list's declared
 * ramp. What needs evidence is not one row but the rail as a SYSTEM: whether the
 * headline still outranks its meta line once the meta line drops to 10px, and
 * whether a folder name at 13px/500 reads as a peer of a note title rather than
 * as chrome.
 *
 * So the frames are the whole rail, not a crop of one row, and the fixture is
 * shaped to put every ramp slot on screen at once: a nested folder tree (folder
 * name + count at two depths), notes with a folder·time meta line, a pending
 * sync badge, and the Settings destination at the foot.
 *
 * Frame 02 opens the sort menu, which is the only place the menu-item step
 * (12 -> 13px) and the uppercase section label are visible.
 *
 * Run it once per side of the diff and label the output directory. Stitching the
 * two sides into one before/after image is done by hand — no composer is checked
 * in, deliberately, because a captioned side-by-side is throwaway PR evidence
 * rather than reusable tooling.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures. No gateway, no token.
 *
 * Usage: node scripts/capture-mdnb-rail-ramp.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { MDNB_VAULT_ID, mdnbApiStub, mdnbNoteDoc } from './lib/mdnb-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/mdnb-rail-ramp'
mkdirSync(OUT, { recursive: true })

const VIEW = { width: 1280, height: 900 }

/**
 * A vault WITH a remote, unlike the shared fixture: `pending` only renders on a
 * vault the note can still be pending to, and the badge is one of the ramp slots
 * under review (`RAIL_TYPE.micro`).
 */
const VAULT = {
  id: MDNB_VAULT_ID,
  name: 'My Notes',
  repo: 'michellemxm/notes',
  branch: 'main',
  localPath: '/Users/demo/notes',
  readOnly: false,
  external: true,
  localOnly: false,
  knowledge: false,
  knowledgeSourceId: null,
}

const OPEN_PATH = 'Design/type-ramp-audit.md'
const OPEN_TITLE = 'Type ramp audit'

/**
 * Nested on purpose. One folder at depth 0 and one at depth 1 show whether the
 * folder step still reads as a folder once it is the same size as a note title,
 * and the counts exercise `RAIL_TYPE.secondary` at both indents.
 */
const NOTES = [
  { path: OPEN_PATH, title: OPEN_TITLE, modifiedAt: Date.now() - 6e5, syncStatus: 'pending' },
  { path: 'Design/Sessions/session list rows.md', title: 'Session list rows', modifiedAt: Date.now() - 5.4e6, syncStatus: 'synced' },
  { path: 'Design/Sessions/folder alignment notes.md', title: 'Folder alignment notes', modifiedAt: Date.now() - 1.7e7, syncStatus: 'synced' },
  { path: 'Design/icon craft.md', title: 'Icon craft', modifiedAt: Date.now() - 8.6e7, syncStatus: 'synced' },
  { path: 'Meetings/design review 2026-08-18.md', title: 'Design review 2026-08-18', modifiedAt: Date.now() - 1.3e5, syncStatus: 'synced' },
  { path: 'Inbox.md', title: 'Inbox', modifiedAt: Date.now() - 2.6e6, syncStatus: 'synced' },
  { path: 'reading-list.md', title: 'Reading list', modifiedAt: Date.now() - 1.9e8, syncStatus: 'synced' },
]

const CONTENT = `# ${OPEN_TITLE}

The rail borrows the sessions list ramp rather than declaring a second one.
`

const mdnbApi = mdnbApiStub({ vault: VAULT, notes: NOTES, doc: mdnbNoteDoc(OPEN_PATH, CONTENT) })

/**
 * Clip the rail alone. Walks up from the search field to the panel card — the
 * panel carries no class or test id, so it is identified by the two properties
 * that are actually its own: the 16px card radius and the column direction.
 */
async function railClip(page) {
  return page.evaluate(() => {
    let el = document.querySelector('.mdnb-search')
    while (el && el !== document.body) {
      const s = getComputedStyle(el)
      if (s.borderRadius.startsWith('16px') && s.flexDirection === 'column') break
      el = el.parentElement
    }
    const r = (el && el !== document.body ? el : document.body).getBoundingClientRect()
    // 12px of bleed so the card's own border and shadow are inside the frame.
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
    // Written in a LATER init script: the shared stub clears localStorage in its
    // own. `mdnb-list-view` = folders is what puts folder rows on screen.
    await page.addInitScript(vaultId => {
      localStorage.setItem('mdnb-active-vault', vaultId)
      localStorage.setItem('mdnb-list-view', 'folders')
      localStorage.setItem('mc-color-theme', 'kiro')
    }, MDNB_VAULT_ID)

    await page.goto(base + '/md-notebook', { waitUntil: 'domcontentloaded' })
    // Folders arrive expanded (no persisted collapse set), so the whole tree —
    // both folder depths and every note row — is on screen without a click.
    // Opening a note is the one gesture needed: it puts the active fill on a row.
    await page.getByText(OPEN_TITLE).first().waitFor({ timeout: 15000 })
    await page.getByText(OPEN_TITLE).first().click()
    await page.waitForTimeout(700)

    await page.screenshot({ path: `${OUT}/01-rail.png`, clip: await railClip(page) })
    console.log('wrote', `${OUT}/01-rail.png`)

    // The sort menu is the only surface showing the menu-item step and the
    // uppercase section label, so it gets its own frame.
    await page.getByLabel('Sort and view').click()
    await page.getByText('File name — A to Z').first().waitFor({ timeout: 5000 })
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/02-sort-menu.png`, clip: await railClip(page) })
    console.log('wrote', `${OUT}/02-sort-menu.png`)

    await context.close()
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
