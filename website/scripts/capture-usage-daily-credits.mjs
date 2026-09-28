/**
 * Screenshots of the Usage tab's Daily History credits columns (#3371), via the
 * capture/usage-daily-credits harness (which stubs only /api/usage/kiro and
 * renders the real UsageTab through the real acp adapter).
 *
 * Every frame is asserted before it is shot, so a regression fails the capture
 * instead of shipping a stale-looking frame:
 *  - both new column headers are present;
 *  - scene=plan: the over-allowance day reads above 100% (no clamp);
 *  - scene=noplan: the "(%)" column is a dash on every row, never "0%";
 *  - at the phone width the two columns are hidden and each day instead carries
 *    a visible second line, every cell stays on one line, and the table never
 *    scrolls sideways (scrollWidth <= clientWidth) -- measured, not eyeballed.
 *
 * Usage: node scripts/capture-usage-daily-credits.mjs <viteBase> <outDir>
 */
import { chromium } from 'playwright'
import path from 'node:path'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const outDir = process.argv[3] || '../temp-screenshots/usage-daily-credits'

const frames = [
  { scene: 'plan', theme: 'dark', width: 760, name: 'daily-history-credits-plan-dark' },
  { scene: 'plan', theme: 'light', width: 760, name: 'daily-history-credits-plan-light' },
  { scene: 'noplan', theme: 'dark', width: 760, name: 'daily-history-credits-noplan-dark' },
  { scene: 'plan', theme: 'dark', width: 390, name: 'daily-history-credits-plan-phone-dark' },
  { scene: 'plan', theme: 'light', width: 390, name: 'daily-history-credits-plan-phone-light' },
]

const b = await chromium.launch()
for (const f of frames) {
  const ctx = await b.newContext({ viewport: { width: f.width, height: 900 }, deviceScaleFactor: 2 })
  const p = await ctx.newPage()
  await p.goto(`${base}/capture/usage-daily-credits.html?scene=${f.scene}&theme=${f.theme}`, {
    waitUntil: 'networkidle',
  })
  const table = p.getByRole('table')
  await table.waitFor({ state: 'visible', timeout: 15_000 })
  const phone = f.width < 600
  const creditsHeader = p.getByRole('columnheader', { name: 'Credits used', exact: true, includeHidden: true })
  const pctHeader = p.getByRole('columnheader', { name: 'Credits used (%)', exact: true, includeHidden: true })
  await creditsHeader.waitFor({ state: phone ? 'hidden' : 'visible' })
  await pctHeader.waitFor({ state: phone ? 'hidden' : 'visible' })

  const dayRows = table.locator('tbody tr:not([data-phone-line])')
  const pctCells = await dayRows.locator('td:nth-child(6)').allTextContents()
  if (pctCells.length === 0) throw new Error(`${f.name}: no rows rendered`)
  if (f.scene === 'plan') {
    if (!pctCells.some(t => parseFloat(t) > 100)) throw new Error(`${f.name}: no day above 100%: ${pctCells}`)
  } else {
    const bad = pctCells.filter(t => t !== '\u2014')
    if (bad.length) throw new Error(`${f.name}: "(%)" cells must be a dash without a plan: ${bad}`)
  }
  const phoneLines = table.locator('tbody tr[data-phone-line]')
  const visibleLines = await phoneLines.evaluateAll(rows => rows.filter(r => r.getClientRects().length > 0).length)
  if (phone && visibleLines !== pctCells.length) {
    throw new Error(`${f.name}: expected ${pctCells.length} visible phone lines, saw ${visibleLines}`)
  }
  if (!phone && visibleLines !== 0) throw new Error(`${f.name}: phone lines leaked onto desktop (${visibleLines})`)
  // Every visible cell on one line: a wrapped cell is taller than its line-height.
  const wrapped = await table.evaluate(el =>
    [...el.querySelectorAll('th, td')]
      .filter(c => c.getClientRects().length > 0)
      .map(c => {
        const cs = getComputedStyle(c)
        const pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom)
        return { text: c.textContent, lines: Math.round((c.getBoundingClientRect().height - pad) / parseFloat(cs.lineHeight)) }
      })
      .filter(c => c.lines > 1)
      .map(c => c.text),
  )
  if (wrapped.length) throw new Error(`${f.name}: cells wrapped onto more than one line: ${JSON.stringify(wrapped)}`)

  const overflow = await table.evaluate(el => {
    const scroller = el.parentElement
    return { scrollWidth: scroller.scrollWidth, clientWidth: scroller.clientWidth }
  })
  if (overflow.scrollWidth > overflow.clientWidth) {
    throw new Error(`${f.name}: table scrolls sideways (${overflow.scrollWidth} > ${overflow.clientWidth})`)
  }

  // Scroll the table card into view so the frame shows the columns, not the cards above.
  await table.scrollIntoViewIfNeeded()
  const card = table.locator('xpath=ancestor::*[contains(@class,"rounded")][1]')
  const out = path.join(outDir, `${f.name}.png`)
  await (phone ? p : card).screenshot({ path: out })
  console.log(
    `captured ${out} (${pctCells.length} days, ${phone ? `${visibleLines} phone lines` : 'both columns'}, width ${overflow.clientWidth}px, no wrap, no sideways scroll)`,
  )
  await ctx.close()
}
await b.close()
