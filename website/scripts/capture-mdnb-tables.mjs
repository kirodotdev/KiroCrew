/**
 * Screenshot harness for markdown tables in the Notes app.
 *
 * The claim needing evidence is that a table stops arriving as literal pipes and
 * becomes a real table WITHOUT costing the app its defining gesture — clicking a
 * block opens the markdown source. Two frames, both on the shipped default
 * theme, because that is what the change is about:
 *
 *   01 rendered - alignment, inline markup inside cells and an escaped pipe
 *   02 editing  - the table clicked open: the whole source, pipes aligned
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures. No gateway, no token.
 *
 * Usage: node scripts/capture-mdnb-tables.mjs [outDir]
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

const OUT = process.argv[2] || '../temp-screenshots/mdnb-tables'
mkdirSync(OUT, { recursive: true })

const VIEW = { width: 1280, height: 900 }

const NOTE_PATH = 'table-demo.md'
const NOTE_TITLE = 'Gateway Comparison'

// One note carrying every case the parser has to get right: default and
// explicit alignment, inline code / bold / wikilink inside cells, an escaped
// pipe, and a short row that must be padded to the header's width.
const NOTE_CONTENT = `# ${NOTE_TITLE}

Two candidates, scored against the same criteria.

| Edition | Model | Notes |
| --- | :-: | --- |
| **Kong OSS** | self-hosted | Free, \`nginx\`-based, plugins included |
| **Kong Konnect** | SaaS | Cloud control plane, billed per gateway \\| per request |
| **API Gateway** | managed | REST, HTTP and WebSocket; see [[Quotas]] |

Costs below are list price, one region, per million requests.

| Tier | Requests | Price | Cache | Notes |
| :-- | --: | --: | :-: | --- |
| First 333M | 333,000,000 | $3.50 | included | Volume tiers apply automatically per account and region |
| Next 667M | 667,000,000 | $2.80 | included | |
| Over 20B | 20,000,000,000 | $1.51 | extra | Contact sales before committing to a private pricing tier |

The cache column is a per-hour charge, not per request.
`

const NOTES_LIST = mdnbNotesList(NOTE_PATH, NOTE_TITLE)
const NOTE_DOC = mdnbNoteDoc(NOTE_PATH, NOTE_CONTENT)
const mdnbApi = mdnbApiStub({ notes: NOTES_LIST, doc: NOTE_DOC })

/**
 * Open the fixture note and photograph it.
 *
 * The shared stub clears localStorage in its own init script, so the active
 * vault is written in a LATER init script to survive it.
 */
async function shoot(browser, base, { file, edit = false }) {
  const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2, locale: 'en-US' })
  const page = await context.newPage()
  await stubDashboardApi(page, { theme: 'dark', extra: mdnbApi })
  logPageProblems(page)
  await page.addInitScript(vaultId => {
    localStorage.setItem('mdnb-active-vault', vaultId)
  }, MDNB_VAULT_ID)

  await page.goto(base + '/md-notebook', { waitUntil: 'domcontentloaded' })
  await page.getByText(NOTE_TITLE).first().waitFor({ timeout: 15000 })
  await page.getByText(NOTE_TITLE).first().click()
  await page.getByRole('table').first().waitFor({ timeout: 15000 })
  await page.waitForTimeout(700)

  if (edit) {
    await page.getByRole('table').first().click()
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
  try {
    await shoot(browser, base, { file: '01-rendered.png' })
    await shoot(browser, base, { file: '02-editing-the-source.png', edit: true })
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
