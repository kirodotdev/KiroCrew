/**
 * Screenshot harness for `<details>` collapsible blocks in the Notes app.
 *
 * The feature renders a closed `<details>…</details>` HTML block as a native
 * disclosure while keeping the app's core promise intact: every byte of the
 * content is escaped (no raw HTML passes through) and the SOURCE stays one
 * click away. Frames, one claim each:
 *
 *   01 rendered  - two disclosures (one open-by-default-collapsed with a
 *                  summary, one summary-less showing the localized fallback)
 *                  drawn as native <details> between ordinary markdown blocks
 *   02 editing   - clicking a disclosure's body opens the whole HTML source in
 *                  the block editor, not a half-block
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** and /apps/md-notebook/api/** call answered from
 * fixtures. No gateway, no token.
 *
 * Usage: node scripts/capture-mdnb-details.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { MDNB_VAULT_ID, mdnbApiStub, mdnbNoteDoc, mdnbNotesList, notePaneClip } from './lib/mdnb-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/mdnb-details'
mkdirSync(OUT, { recursive: true })

const NOTE_PATH = 'faq.md'
const NOTE_TITLE = 'Deployment FAQ'

const NOTE = `# ${NOTE_TITLE}

Answers fold away so the page reads as a short index first.

<details>
<summary>How do I roll back a bad deploy?</summary>
Re-run the pipeline pinned to the **previous** green tag, then confirm the
health check flips back to \`green\` before you close the incident.
</details>

A disclosure with no summary falls back to a localized label:

<details>
Anything here stays hidden until the reader opens it, even with no summary
line of its own.
</details>

The source is always one click away — clicking a block opens exactly its lines.
`

async function shoot(browser, base, doc, { file, edit = false }) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    deviceScaleFactor: 2,
    locale: 'en-US',
  })
  const page = await context.newPage()
  await stubDashboardApi(page, {
    theme: 'dark',
    extra: mdnbApiStub({ notes: mdnbNotesList(NOTE_PATH, NOTE_TITLE), doc }),
    localStorageEntries: { 'mdnb-active-vault': MDNB_VAULT_ID },
  })
  logPageProblems(page)

  await page.goto(base + '/md-notebook', { waitUntil: 'domcontentloaded' })
  await page.getByText(NOTE_TITLE).first().waitFor({ timeout: 15000 })
  await page.getByText(NOTE_TITLE).first().click()
  // Resolves once the first summary has rendered as a real <summary> element.
  await page.locator('summary').first().waitFor({ timeout: 15000 })
  await page.waitForTimeout(600)

  const applied = await page.evaluate(() => document.documentElement.dataset.theme || '')
  if (applied !== 'kiro-dark') throw new Error(`theme mismatch: wanted kiro-dark, got ${applied || '(none)'}`)

  if (edit) {
    // Expand the first disclosure (clicking its summary only toggles — the
    // click is stopped from reaching the editor), then click its now-visible
    // body text to open the whole block's source in the editor.
    await page.getByText('How do I roll back a bad deploy?').first().click()
    const body = page.getByText('Re-run the pipeline', { exact: false }).first()
    await body.waitFor({ state: 'visible', timeout: 5000 })
    await body.click()
    await page.locator('textarea').first().waitFor({ timeout: 5000 })
    await page.waitForTimeout(300)
  }

  await page.screenshot({ path: `${OUT}/${file}`, clip: await notePaneClip(page) })
  console.log('wrote', `${OUT}/${file}`)
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const doc = mdnbNoteDoc(NOTE_PATH, NOTE)
  try {
    await shoot(browser, base, doc, { file: '01-collapsibles-rendered.png' })
    await shoot(browser, base, doc, { file: '02-click-body-opens-source.png', edit: true })
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
