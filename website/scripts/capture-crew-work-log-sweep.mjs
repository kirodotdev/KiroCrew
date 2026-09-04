/**
 * Screenshot of the crew work log showing a crew-level `sweep` line beside
 * numbered rows, via the capture/crew-work-log-sweep harness (which stubs only
 * GET /api/apps/issue-radar/crew). Asserts the sweep row's Issue cell holds an em
 * dash and no `#` before shooting -- the whole point of the frame -- so a harness
 * that silently stopped rendering the row cannot produce a green screenshot.
 *
 * Usage: node scripts/capture-crew-work-log-sweep.mjs <viteBase> <outFile>
 */
import { chromium } from 'playwright'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const out = process.argv[3] || '../temp-screenshots/crew-empty-queue/work-log-sweep.png'

const b = await chromium.launch()
const p = await (await b.newContext({ viewport: { width: 1180, height: 900 }, deviceScaleFactor: 2 })).newPage()
await p.goto(`${base}/capture/crew-work-log-sweep.html?theme=dark`, { waitUntil: 'networkidle' })

const sweepRow = p.locator('[data-testid="work-log-row-ev-sweep"]')
await sweepRow.waitFor({ state: 'visible', timeout: 15_000 })
const issueCell = sweepRow.locator('td').nth(1)
const cellText = (await issueCell.textContent())?.trim()
if (cellText !== '\u2014') {
  throw new Error(`sweep row Issue cell is ${JSON.stringify(cellText)}, expected an em dash`)
}
const numbered = await p.locator('[data-testid="work-log-row-ev-ci"] td').nth(1).textContent()
if (numbered?.trim() !== '#2251') {
  throw new Error(`numbered row Issue cell is ${JSON.stringify(numbered)}, expected #2251`)
}

// Both sweep rows must read as PAST INSTANTS. Neither may carry a present-tense
// qualifier: nothing on this page evidences that a crew is alive, so a row claiming
// ongoing activity would misreport a crashed crew as a healthily idle one.
const newestWhen = (await sweepRow.locator('td').nth(0).textContent())?.trim() ?? ''
const olderWhen = (
  await p.locator('[data-testid="work-log-row-ev-sweep-closed"] td').nth(0).textContent()
)?.trim() ?? ''
for (const [label, text] of [['newest', newestWhen], ['older', olderWhen]]) {
  if (/since/i.test(text)) {
    throw new Error(`${label} sweep reads ${JSON.stringify(text)}, expected a bare timestamp`)
  }
  if (!text) {
    throw new Error(`${label} sweep has an empty WHEN cell`)
  }
}

await p.locator('[data-testid="capture-frame"]').screenshot({ path: out })
console.log(`captured ${out} (em dash, numbered sibling, and both sweeps as past instants)`)
await b.close()
