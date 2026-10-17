/**
 * Screenshot harness for the Notes app's auto-sync settings.
 *
 * The claim needing evidence is that auto-sync is now a stored app setting the
 * backend acts on, rather than a browser-only timer, and that the row says so.
 * Two frames on the shipped default theme:
 *
 *   01 off - the switch at rest, with the help line stating that syncing keeps
 *            running with Notes closed while Kiro Crew is open
 *   02 on  - switched on, revealing the interval field the backend loop reads
 *
 * The vault carries a REMOTE, unlike the shared local-only fixture: auto-sync
 * pushes to one, so a local-only vault would photograph a Sync section that
 * cannot describe what this change does.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures. No gateway, no token.
 *
 * Usage: node scripts/capture-mdnb-autosync-settings.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { MDNB_VAULT_ID, MDNB_VAULT, mdnbApiStub, mdnbNoteDoc, mdnbNotesList } from './lib/mdnb-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/mdnb-autosync-settings'
mkdirSync(OUT, { recursive: true })

const VIEW = { width: 1280, height: 900 }

const NOTE_PATH = 'welcome.md'
const NOTE_TITLE = 'Welcome'

// A vault with a remote, so the Sync section reads the way it does for a user
// who can actually push. `localOnly` would relabel the action "Save locally".
const REMOTE_VAULT = {
  ...MDNB_VAULT,
  repo: 'demo/notes',
  remoteUrl: 'https://github.com/demo/notes.git',
  localOnly: false,
}

const NOTES_LIST = mdnbNotesList(NOTE_PATH, NOTE_TITLE)
const NOTE_DOC = mdnbNoteDoc(NOTE_PATH, `# ${NOTE_TITLE}\n\nA note, so the vault is not empty.\n`)

/**
 * Open Settings and photograph the Sync section.
 *
 * The shared stub clears localStorage in its own init script, so the active
 * vault is written in a LATER init script to survive it.
 */
async function shoot(browser, base, { file, on }) {
  const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2, locale: 'en-US' })
  const page = await context.newPage()
  await stubDashboardApi(page, {
    theme: 'dark',
    extra: mdnbApiStub({
      vault: REMOTE_VAULT,
      notes: NOTES_LIST,
      doc: NOTE_DOC,
      settings: { autoSync: on, autoSyncMins: 30 },
    }),
  })
  logPageProblems(page)
  await page.addInitScript(vaultId => {
    localStorage.setItem('mdnb-active-vault', vaultId)
  }, MDNB_VAULT_ID)

  await page.goto(base + '/md-notebook', { waitUntil: 'domcontentloaded' })
  try {
    await page.getByText(NOTE_TITLE).first().waitFor({ timeout: 15000 })
    await page.getByRole('button', { name: 'Settings' }).last().click()
    const help = page.getByText(/Keeps running in the background/i).first()
    await help.waitFor({ timeout: 15000 })
  } catch (err) {
    await page.screenshot({ path: `${OUT}/DEBUG-${file}` })
    const roles = await page.evaluate(() =>
      Array.from(document.querySelectorAll('[role=button],button'))
        .map(b => b.getAttribute('aria-label') || (b.textContent || '').trim())
        .filter(Boolean)
        .slice(0, 30),
    )
    console.error('FAILED', file, String(err).split('\n')[0], '\nbuttons:', roles)
    throw err
  }

  // Assert the state the frame claims, so a stub regression cannot produce a
  // plausible-looking screenshot of the wrong thing.
  const spin = page.getByRole('spinbutton', { name: /auto sync interval/i })
  if (on) {
    await spin.waitFor({ timeout: 15000 })
    const value = await spin.inputValue()
    if (value !== '30') throw new Error(`interval field shows ${value}, expected the stored 30`)
  } else if (await spin.count()) {
    throw new Error('interval field is visible while auto sync is off')
  }

  await page.waitForTimeout(500)
  const section = page
    .getByText(/Keeps running in the background/i)
    .first()
    .locator('xpath=ancestor::*[self::section or self::div][2]')
  await section.screenshot({ path: `${OUT}/${file}` })
  await context.close()
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
try {
  await shoot(browser, base, { file: '01-auto-sync-off.png', on: false })
  await shoot(browser, base, { file: '02-auto-sync-on.png', on: true })
  console.log(`wrote 2 frames to ${OUT}`)
} finally {
  await browser.close()
  srv.close()
}
