/**
 * Screenshot harness for the Notes app's theme-aware heading chrome.
 *
 * The change under test keeps heading TEXT on `--text-strong` and expresses the
 * active theme's accent as chrome: a rule under h1/h2, a rail beside h3-h6.
 * The claim that needs evidence is therefore not "it looks nice on one theme"
 * but "it follows every theme and stays legible on all of them", so this
 * captures the SAME note across four deliberately chosen palettes:
 *
 *   01 kiro-dark         - the shipped default, green accent
 *   02 monokai-dark      - a saturated warm accent on near-black
 *   03 gruvbox-light     - a light theme with a low-contrast accent
 *   04 everforest-light  - the tightest built-in palette measured: its own
 *                          --text-strong reaches only 7.11:1, which is why
 *                          tinting heading TEXT with the accent was rejected
 *
 * Frame 05 opens a heading for editing, proving the click-to-edit gesture still
 * reaches the markdown SOURCE and keeps the heading's typography.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures. No gateway, no token.
 *
 * Usage: node scripts/capture-mdnb-heading-accent.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import {
  MDNB_VAULT_ID,
  mdnbApiStub,
  mdnbNoteDoc,
  mdnbNotesList,
  notePaneClip,
} from './lib/mdnb-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/mdnb-heading-accent'
mkdirSync(OUT, { recursive: true })

const VIEW = { width: 1280, height: 900 }

// --------------------------------------------------------------------------
// Fixture data
// --------------------------------------------------------------------------


const NOTE_PATH = 'heading-demo.md'
const NOTE_TITLE = 'Release Runbook'

// Every heading level appears, in document order, so one frame shows the whole
// scale: where the rule gives way to the rail, and that h6 still reads as a
// heading rather than bold body text.
const NOTE_CONTENT = `# ${NOTE_TITLE}

Cutting a release touches three systems. Work top to bottom; do not skip the
verification step even when the build looks clean.

## Before you start

Confirm the release branch is green and that no migration is pending.

### Required approvals

- Release manager sign-off
- On-call acknowledgement

#### Escalation path

If the smoke test fails twice, page the on-call rather than retrying.

##### Rollback window

The rollback stays available for six hours after the deploy completes.

###### Recorded by

Platform team, reviewed each quarter.

## Verification

Run the smoke suite against the canary before promoting:

\`\`\`bash
./scripts/smoke.sh --target canary --fail-fast
\`\`\`

> A canary that passes on retry is still a failure — investigate before promoting.

Once the canary holds for ten minutes, promote to the remaining regions.
`

const NOTES_LIST = mdnbNotesList(NOTE_PATH, NOTE_TITLE)
const NOTE_DOC = mdnbNoteDoc(NOTE_PATH, NOTE_CONTENT)
const mdnbApi = mdnbApiStub({ notes: NOTES_LIST, doc: NOTE_DOC })

/**
 * Open the fixture note on one theme and photograph it.
 *
 * `mode` is the light/dark preference (`mc-theme`) and `palette` is the named
 * colour theme (`mc-color-theme`); together they select the `data-theme` the
 * stylesheet keys off. The shared stub clears localStorage in its own init
 * script, so the palette is written in a LATER init script to survive it.
 */
async function shoot(browser, base, { file, mode, palette, edit = false }) {
  const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2, locale: 'en-US' })
  const page = await context.newPage()
  await stubDashboardApi(page, { theme: mode, extra: mdnbApi })
  logPageProblems(page)
  await page.addInitScript(([vaultId, colorTheme]) => {
    localStorage.setItem('mdnb-active-vault', vaultId)
    if (colorTheme) localStorage.setItem('mc-color-theme', colorTheme)
  }, [MDNB_VAULT_ID, palette])

  await page.goto(base + '/md-notebook', { waitUntil: 'domcontentloaded' })
  await page.getByText(NOTE_TITLE).first().waitFor({ timeout: 15000 })
  await page.getByText(NOTE_TITLE).first().click()
  await page.getByText('Verification').first().waitFor({ timeout: 15000 })
  await page.waitForTimeout(700)

  // Fail loudly rather than shipping a frame of the wrong theme.
  const applied = await page.evaluate(() => document.documentElement.dataset.theme || '')
  const want = palette ? `${palette}-${mode}` : mode
  if (applied !== want) throw new Error(`theme mismatch: wanted ${want}, got ${applied || '(none)'}`)

  if (edit) {
    await page.getByText('Required approvals').click()
    await page.locator('textarea').first().waitFor({ timeout: 5000 })
    await page.waitForTimeout(300)
  }

  await page.screenshot({ path: `${OUT}/${file}`, clip: await notePaneClip(page) })
  console.log('wrote', `${OUT}/${file}`, `(${applied})`)
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    await shoot(browser, base, { file: '01-kiro-dark.png', mode: 'dark', palette: 'kiro' })
    await shoot(browser, base, { file: '02-monokai-dark.png', mode: 'dark', palette: 'monokai' })
    await shoot(browser, base, { file: '03-gruvbox-light.png', mode: 'light', palette: 'gruvbox' })
    await shoot(browser, base, { file: '04-everforest-light.png', mode: 'light', palette: 'everforest' })
    await shoot(browser, base, { file: '05-editing-a-heading.png', mode: 'dark', palette: 'kiro', edit: true })
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
