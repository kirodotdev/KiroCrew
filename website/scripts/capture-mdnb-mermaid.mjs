/**
 * Screenshot harness for Mermaid diagrams in the Notes app.
 *
 * The feature renders a CLOSED ```mermaid fence as a diagram while keeping the
 * app's core promise intact: the SOURCE stays one click away, and a diagram
 * that does not parse degrades to that source instead of an error box. Three
 * frames, one claim each:
 *
 *   01 rendered  - two diagrams (flowchart + sequence) drawn as SVG between
 *                  ordinary markdown blocks
 *   02 editing   - clicking a diagram opens the fenced SOURCE in the mono
 *                  block editor, not the SVG
 *   03 invalid   - a diagram with a syntax error keeps its source visible,
 *                  with the one-line hint underneath
 *
 * kiro-dark only: the diagram palette comes from mermaid's built-in themes
 * keyed on light/dark, not from the dashboard accent, so more themes would
 * demonstrate mermaid's own theming rather than this change.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures. No gateway, no token.
 *
 * Usage: node scripts/capture-mdnb-mermaid.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { MDNB_VAULT_ID, mdnbApiStub, mdnbNoteDoc, mdnbNotesList, notePaneClip } from './lib/mdnb-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/mdnb-mermaid'
mkdirSync(OUT, { recursive: true })

const NOTE_PATH = 'booking-flow.md'
const NOTE_TITLE = 'Booking Flow'

const VALID_NOTE = `# ${NOTE_TITLE}

The orchestrator fans a booking out to payment and fleet before confirming.

\`\`\`mermaid
graph TD
  A[Booking request] --> B{Payment mode?}
  B -->|prepaid| C[Charge via PSP]
  B -->|pay on arrival| D[Hold voucher]
  C --> E[Confirm booking]
  D --> E
\`\`\`

Cancellations follow the reverse path and must release the vehicle hold:

\`\`\`mermaid
sequenceDiagram
  participant C as Client
  participant O as Orchestrator
  participant F as Fleet
  C->>O: cancel(bookingId)
  O->>F: release hold
  F-->>O: released
  O-->>C: cancelled
\`\`\`

Anything still pending after the timeout goes to the manual review queue.
`

const INVALID_NOTE = `# ${NOTE_TITLE}

\`\`\`mermaid
graph TD
  A[Booking request] -=> B{Oops, bad arrow}
\`\`\`

The block above has a syntax error on purpose.
`

async function shoot(browser, base, doc, { file, edit = false, expectError = false }) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    deviceScaleFactor: 2,
    locale: 'en-US',
  })
  const page = await context.newPage()
  await stubDashboardApi(page, {
    theme: 'dark',
    extra: mdnbApiStub({ notes: mdnbNotesList(NOTE_PATH, NOTE_TITLE), doc }),
  })
  logPageProblems(page)
  await page.addInitScript(vaultId => localStorage.setItem('mdnb-active-vault', vaultId), MDNB_VAULT_ID)

  await page.goto(base + '/md-notebook', { waitUntil: 'domcontentloaded' })
  await page.getByText(NOTE_TITLE).first().waitFor({ timeout: 15000 })
  await page.getByText(NOTE_TITLE).first().click()

  if (expectError) {
    // The failure path resolves once the hint appears under the source block.
    await page.getByText('Invalid Mermaid diagram').waitFor({ timeout: 20000 })
  } else {
    // The success path resolves once mermaid's async render lands its SVG —
    // AND its labels: DOMPurify strips <foreignObject>, so a regression that
    // reintroduces HTML labels would pass the svg check but lose every label.
    await page.locator('svg[id^="mdnb-mermaid-"]').first().waitFor({ timeout: 20000 })
    await page.getByText('Booking request').first().waitFor({ timeout: 5000 })
  }
  await page.waitForTimeout(600)

  const applied = await page.evaluate(() => document.documentElement.dataset.theme || '')
  if (applied !== 'kiro-dark') throw new Error(`theme mismatch: wanted kiro-dark, got ${applied || '(none)'}`)

  if (edit) {
    await page.locator('svg[id^="mdnb-mermaid-"]').first().click()
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
  const valid = mdnbNoteDoc(NOTE_PATH, VALID_NOTE)
  const invalid = mdnbNoteDoc(NOTE_PATH, INVALID_NOTE)
  try {
    await shoot(browser, base, valid, { file: '01-diagrams-rendered.png' })
    await shoot(browser, base, valid, { file: '02-click-opens-source.png', edit: true })
    await shoot(browser, base, invalid, { file: '03-invalid-falls-back-to-source.png', expectError: true })
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
