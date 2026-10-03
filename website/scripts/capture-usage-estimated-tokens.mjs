/**
 * Screenshots of the Usage tab's estimated Kiro CLI tokens, via the
 * capture/usage-estimated-tokens harness (which stubs only /api/usage/kiro and
 * renders the real UsageTab through the real acp adapter).
 *
 * Every frame is asserted before it is shot, so a regression fails the capture
 * instead of shipping a stale-looking frame:
 *  - the Estimated Tokens card shows four rows with a value in both months and
 *    the note saying how the figures are built;
 *  - scene=incomplete: the card carries the unreadable-sessions warning;
 *    scene=full: it does not;
 *  - the Daily History "Est. tokens" column is visible on desktop and hidden at
 *    the phone width, where every day instead carries a visible second line
 *    with the estimate;
 *  - in both tables every visible cell stays on one line and the table never
 *    scrolls sideways (scrollWidth <= clientWidth) -- measured, not eyeballed;
 *  - the text of the "This Month" and "Last Month" headers sits at least 8px
 *    apart, so the two right-aligned headers cannot read as one at the phone
 *    width.
 *
 * Usage: node scripts/capture-usage-estimated-tokens.mjs <viteBase> <outDir>
 */
import { chromium } from 'playwright'
import path from 'node:path'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const outDir = process.argv[3] || '../temp-screenshots/usage-estimated-tokens'

/** Smallest horizontal gap, in CSS px, between the text of two neighbouring headers. */
const MIN_HEADER_GAP = 8

const frames = [
  { scene: 'full', theme: 'dark', width: 760, shot: 'card', name: 'estimated-tokens-card-dark' },
  { scene: 'full', theme: 'light', width: 760, shot: 'card', name: 'estimated-tokens-card-light' },
  { scene: 'incomplete', theme: 'dark', width: 760, shot: 'card', name: 'estimated-tokens-card-incomplete-dark' },
  { scene: 'full', theme: 'dark', width: 760, shot: 'history', name: 'daily-history-est-tokens-dark' },
  { scene: 'full', theme: 'dark', width: 390, shot: 'page', name: 'estimated-tokens-phone-dark' },
]

/** Cells of a table that wrapped onto more than one line (a wrapped cell is taller than its line-height). */
function wrappedCells(table) {
  return table.evaluate(el =>
    [...el.querySelectorAll('th, td')]
      .filter(c => c.getClientRects().length > 0 && c.textContent.trim() !== '')
      .map(c => {
        const cs = getComputedStyle(c)
        const pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom)
        return { text: c.textContent, lines: Math.round((c.getBoundingClientRect().height - pad) / parseFloat(cs.lineHeight)) }
      })
      .filter(c => c.lines > 1)
      .map(c => c.text),
  )
}

async function assertFits(name, label, table) {
  const wrapped = await wrappedCells(table)
  if (wrapped.length) throw new Error(`${name}: ${label} cells wrapped onto more than one line: ${JSON.stringify(wrapped)}`)
  const overflow = await table.evaluate(el => ({ scrollWidth: el.parentElement.scrollWidth, clientWidth: el.parentElement.clientWidth }))
  if (overflow.scrollWidth > overflow.clientWidth) {
    throw new Error(`${name}: ${label} scrolls sideways (${overflow.scrollWidth} > ${overflow.clientWidth})`)
  }
  return overflow.clientWidth
}

/** Horizontal extent of a cell's TEXT (a DOM Range over its contents), not of the cell box: padding is not text. */
function textExtent(cell) {
  return cell.evaluate(el => {
    const range = document.createRange()
    range.selectNodeContents(el)
    const { left, right } = range.getBoundingClientRect()
    return { left, right }
  })
}

/** Gap, in CSS px, between the text of two headers that sit side by side; fails the frame when they could read as one. */
async function assertHeaderGap(name, left, right) {
  const gap = (await textExtent(right)).left - (await textExtent(left)).right
  if (gap < MIN_HEADER_GAP) {
    throw new Error(`${name}: This Month and Last Month headers sit ${gap.toFixed(1)}px apart (min ${MIN_HEADER_GAP}px)`)
  }
  return gap
}

const b = await chromium.launch()
for (const f of frames) {
  const ctx = await b.newContext({ viewport: { width: f.width, height: 1400 }, deviceScaleFactor: 2 })
  const p = await ctx.newPage()
  await p.goto(`${base}/capture/usage-estimated-tokens.html?scene=${f.scene}&theme=${f.theme}`, { waitUntil: 'networkidle' })
  const phone = f.width < 600

  // The Estimated Tokens card.
  const lastMonth = p.getByRole('columnheader', { name: 'Last Month', exact: true })
  await lastMonth.waitFor({ state: 'visible', timeout: 15_000 })
  const estimate = lastMonth.locator('xpath=ancestor::table[1]')
  const estimateRows = await estimate.locator('tbody tr').evaluateAll(rows =>
    rows.map(r => [...r.querySelectorAll('th, td')].map(c => c.textContent.trim())),
  )
  if (estimateRows.length !== 4 || estimateRows.some(r => r.length !== 3 || r.some(c => c === ''))) {
    throw new Error(`${f.name}: estimate table not fully populated: ${JSON.stringify(estimateRows)}`)
  }
  const card = estimate.locator('xpath=ancestor::*[contains(@class,"rounded")][1]')
  if (!(await card.getByText(/does not save token counts/).isVisible())) throw new Error(`${f.name}: estimate note missing`)
  const warned = await card.getByText(/Some sessions could not be read/).isVisible()
  if (warned !== (f.scene === 'incomplete')) throw new Error(`${f.name}: unreadable-sessions warning visible=${warned} in scene ${f.scene}`)
  const estimateWidth = await assertFits(f.name, 'estimate table', estimate)
  const headerGap = await assertHeaderGap(f.name, p.getByRole('columnheader', { name: 'This Month', exact: true }), lastMonth)

  // The Daily History table.
  const history = p.getByRole('columnheader', { name: 'Date', exact: true }).locator('xpath=ancestor::table[1]')
  const estHeader = p.getByRole('columnheader', { name: 'Est. tokens', exact: true, includeHidden: true })
  await estHeader.waitFor({ state: phone ? 'hidden' : 'visible' })
  const dayRows = history.locator('tbody tr:not([data-phone-line])')
  const dayCount = await dayRows.count()
  if (dayCount === 0) throw new Error(`${f.name}: no history rows rendered`)
  const phoneLines = await history
    .locator('tbody tr[data-phone-line]')
    .evaluateAll(rows => rows.filter(r => r.getClientRects().length > 0).map(r => r.textContent))
  if (phone) {
    if (phoneLines.length !== dayCount) throw new Error(`${f.name}: expected ${dayCount} phone lines, saw ${phoneLines.length}`)
    const missing = phoneLines.filter(t => !t.includes('Est. tokens:'))
    if (missing.length) throw new Error(`${f.name}: phone lines without the estimate: ${JSON.stringify(missing)}`)
  } else if (phoneLines.length !== 0) {
    throw new Error(`${f.name}: phone lines leaked onto desktop (${phoneLines.length})`)
  }
  const historyWidth = await assertFits(f.name, 'history table', history)

  const out = path.join(outDir, `${f.name}.png`)
  if (f.shot === 'page') {
    await p.screenshot({ path: out, fullPage: true })
  } else {
    const target = f.shot === 'card' ? estimate : history
    await target.scrollIntoViewIfNeeded()
    await target.locator('xpath=ancestor::*[contains(@class,"rounded")][1]').screenshot({ path: out })
  }
  console.log(
    `captured ${out} (estimate ${estimateWidth}px, headers ${headerGap.toFixed(1)}px apart, history ${dayCount} days ${historyWidth}px, ${phone ? `${phoneLines.length} phone lines` : 'desktop columns'}, no wrap, no sideways scroll)`,
  )
  await ctx.close()
}
await b.close()
