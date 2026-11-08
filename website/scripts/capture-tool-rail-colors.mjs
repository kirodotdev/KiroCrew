/**
 * Screenshot + probe for capture/tool-rail-colors.html: the expanded detail
 * rail of every tool status, main chat (ToolCallLine) beside the embed
 * (ToolCallPill).
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6818 --strictPort
 *   node scripts/capture-tool-rail-colors.mjs http://127.0.0.1:6818 \
 *     ../temp-screenshots/tool-rail-colors [label]
 *
 * The probe writes `<label>-<theme>.json`: for each surface, every rail's
 * inline `border-left-color` and its computed colour. Run it on the base and
 * on the change and diff the two files: equal JSON is the proof that sharing
 * the mapping changed no colour, and the frames are what a reader looks at.
 */
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6818'
const OUT = process.argv[3] || '../temp-screenshots/tool-rail-colors'
const LABEL = process.argv[4] || 'after'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0
for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({ viewport: { width: 1100, height: 900 }, deviceScaleFactor: 2, colorScheme: theme })
  const page = await ctx.newPage()
  // A cold dev server pre-bundles dependencies on the first request.
  page.setDefaultTimeout(180000)
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  await page.goto(`${BASE}/capture/tool-rail-colors.html?theme=${theme}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
  // Open every embed pill: it owns its expanded state.
  // Open every closed pill on both surfaces, the first match each time:
  // opening one changes the match set.
  const closed = page.locator('[data-capture-root] button[aria-expanded="false"]')
  for (let i = 0; i < 12 && await closed.count(); i++) {
    await closed.first().click()
    await page.waitForTimeout(150)
  }
  await page.waitForTimeout(1200)
  const rails = await page.evaluate(() => {
    const read = (surface) => [...document.querySelectorAll(`[data-surface="${surface}"] [style*="border-left-color"]`)]
      .map(el => ({ inline: el.style.borderLeftColor, computed: getComputedStyle(el).borderLeftColor }))
    return { main: read('main'), embed: read('embed') }
  })
  writeFileSync(`${OUT}/${LABEL}-${theme}.json`, JSON.stringify(rails, null, 2) + '\n')
  if (rails.main.length !== 5 || rails.embed.length !== 5) {
    console.error(`${theme}: expected 5 rails per surface, got main=${rails.main.length} embed=${rails.embed.length}`)
    failed++
  }
  if (errors.length) { console.error(`${theme}: page errors`, errors); failed++ }
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${LABEL}-${theme}.png` })
  console.log(theme, JSON.stringify(rails))
  await ctx.close()
}
await browser.close()
process.exit(failed ? 1 : 0)
