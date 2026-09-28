/**
 * Photograph both collapsed-tool-group affordances in the app-sdk host (#9699).
 *
 * Drives capture/tool-group-affordance.html on the isolated dev server: a
 * settled turn (TurnBlock's fold toggle) above a reasoning run
 * (CollapsibleToolGroup), both saying "2 tool calls". Four frames — dark and
 * light, collapsed and expanded — and a DOM probe that FAILS the run if the two
 * toggles do not share a class list, so a frame cannot pass by looking alike
 * while diverging in code.
 *
 *   node scripts/capture-tool-group-affordance.mjs <base-url> [outDir]
 *
 * Run through scripts/capture-tool-group-affordance-run.sh, which owns the
 * server lifecycle.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2]
const OUT = process.argv[3] || '../temp-screenshots/tool-group-affordance'
if (!BASE) {
  console.error('usage: node scripts/capture-tool-group-affordance.mjs <base-url> [outDir]')
  process.exit(2)
}

mkdirSync(OUT, { recursive: true })

const TOGGLE = '[data-testid="tool-group-toggle"]'

const browser = await chromium.launch()
let failed = 0

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({
    viewport: { width: 760, height: 520 },
    deviceScaleFactor: 2,
    colorScheme: theme,
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))

  // MarkdownRenderer probes path-like inline code and unfurls links; neither
  // endpoint exists on the capture server and a pending probe leaves a chip
  // mid-load, so both are answered here.
  await page.route('**/api/file-read**', route => route.fulfill({ status: 200, headers: { 'X-Path-Kind': 'file' }, body: '' }))
  await page.route('**/api/link-meta**', route => route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }))

  try {
    await page.goto(`${BASE}/capture/tool-group-affordance.html?theme=${theme}`, { waitUntil: 'networkidle' })
    await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
    await page.waitForSelector(TOGGLE, { timeout: 15000 })
    await page.waitForTimeout(900)

    const probe = () => page.evaluate(sel => {
      const btns = [...document.querySelectorAll(sel)]
      return btns.map(b => ({
        surface: b.closest('[data-capture-surface]')?.getAttribute('data-capture-surface'),
        className: b.className,
        text: b.textContent,
        expanded: b.getAttribute('aria-expanded'),
        label: b.getAttribute('aria-label'),
        glyphRotated: !!b.querySelector('.rotate-90'),
        lucideChevron: !!b.querySelector('svg.lucide-chevron-right'),
      }))
    }, TOGGLE)

    const collapsed = await probe()
    console.log(`[${theme}] collapsed:`, JSON.stringify(collapsed, null, 1))
    if (collapsed.length !== 2) { console.error(`FAIL [${theme}]: expected 2 toggles, saw ${collapsed.length}`); failed++ }
    if (collapsed.length === 2 && collapsed[0].className !== collapsed[1].className) {
      console.error(`FAIL [${theme}]: the two toggles have different class lists`); failed++
    }
    for (const t of collapsed) {
      if (!t.text.includes('2 tool calls')) { console.error(`FAIL [${theme}] ${t.surface}: label is not "2 tool calls"`); failed++ }
      if (t.expanded !== 'false') { console.error(`FAIL [${theme}] ${t.surface}: aria-expanded should be false`); failed++ }
      if (t.glyphRotated) { console.error(`FAIL [${theme}] ${t.surface}: glyph rotated while collapsed`); failed++ }
      if (!t.lucideChevron || t.text.includes('▶')) { console.error(`FAIL [${theme}] ${t.surface}: disclosure indicator is not the Lucide chevron`); failed++ }
    }

    await page.mouse.move(0, 0)
    await page.screenshot({ path: `${OUT}/${theme}-collapsed.png`, fullPage: true })
    console.log('wrote', `${OUT}/${theme}-collapsed.png`)

    // Open both: the same click on the same control, on each surface.
    for (const btn of await page.locator(TOGGLE).all()) await btn.click()
    await page.waitForTimeout(700)

    const expanded = await probe()
    console.log(`[${theme}] expanded:`, JSON.stringify(expanded, null, 1))
    for (const t of expanded) {
      if (t.expanded !== 'true') { console.error(`FAIL [${theme}] ${t.surface}: aria-expanded should be true`); failed++ }
      if (!t.glyphRotated) { console.error(`FAIL [${theme}] ${t.surface}: glyph not rotated while expanded`); failed++ }
      // The group's stateless label gets a verb-led name; the turn's label
      // states the action itself and is the name (no aria-label).
      const nameOk = t.surface === 'group' ? t.label?.startsWith('Collapse ') : (t.label === null && t.text.includes('Hide tool calls'))
      if (!nameOk) { console.error(`FAIL [${theme}] ${t.surface}: accessible name shape wrong (aria-label=${t.label})`); failed++ }
    }
    // The turn's fold reveals its two tool rows; the group reveals two
    // reasoning rows. Counted so an expansion that draws nothing cannot pass.
    const toolRows = await page.locator('[data-capture-surface="turn"] button:not([data-testid="tool-group-toggle"])').count()
    console.log(`[${theme}] turn surface rows after expand:`, toolRows)
    if (toolRows < 2) { console.error(`FAIL [${theme}]: expanded turn shows ${toolRows} tool rows, expected >= 2`); failed++ }

    await page.mouse.move(0, 0)
    await page.screenshot({ path: `${OUT}/${theme}-expanded.png`, fullPage: true })
    console.log('wrote', `${OUT}/${theme}-expanded.png`)

    if (errors.length) { console.error(`FAIL [${theme}]: page errors`, errors); failed++ }
  } finally {
    await ctx.close()
  }
}

await browser.close()
if (failed) {
  console.error(`${failed} check(s) failed`)
  process.exit(1)
}
console.log('all checks passed')
