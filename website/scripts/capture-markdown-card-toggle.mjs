/**
 * Screenshots of the #9196 markdown content card Formatted/Raw toggle
 * (capture/markdown-card-toggle.html).
 *
 * Self-checking, because a screenshot of the wrong state is worse evidence than
 * none: asserts the Formatted default renders a real <h1>/<table> (not the
 * literal "# Release notes" source), that the segmented control offers both
 * modes, and that clicking Raw swaps to the verbatim source. Then screenshots
 * Formatted and Raw in both themes.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6819 --strictPort    # in another shell
 *   node scripts/capture-markdown-card-toggle.mjs http://127.0.0.1:6819 ../temp-screenshots/9196-markdown-card-toggle
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6819'
const OUT = process.argv[3] || '../temp-screenshots/9196-markdown-card-toggle'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 820, height: 640 }, deviceScaleFactor: 2, reducedMotion: 'reduce' })

for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/markdown-card-toggle.html?theme=${theme}`)
  // Formatted default: the heading is a rendered <h1> and it is VISIBLE (both
  // views stay mounted; the inactive one is hidden), and the table exists.
  await page.waitForSelector('h1')
  const h1 = await page.locator('h1').first().innerText()
  if (!h1.includes('Release notes')) throw new Error(`expected rendered <h1>, got: ${h1}`)
  if (!(await page.locator('h1').first().isVisible())) throw new Error('Formatted <h1> should be visible by default')
  if ((await page.locator('table').count()) < 1) throw new Error('expected a rendered <table> in Formatted mode')
  // Both modes are offered by the segmented control.
  if ((await page.getByText('Formatted', { exact: true }).count()) < 1) throw new Error('Formatted segment missing')
  if ((await page.getByText('Raw', { exact: true }).count()) < 1) throw new Error('Raw segment missing')
  await page.waitForTimeout(400)
  await page.screenshot({ path: join(OUT, `formatted-${theme}.png`) })
  console.log(`captured formatted-${theme}.png`)

  // Raw: click it. The rendered <h1> stays in the DOM but becomes hidden and the
  // verbatim source shows. Shoot the plain-<pre> frame: the app's syntax
  // highlighter runs in a Web Worker this isolated capture entry does not boot,
  // so waiting for highlighted output would hang on a swap that never lands here.
  await page.getByText('Raw', { exact: true }).first().click()
  await page.waitForFunction(() => {
    const h1 = document.querySelector('h1')
    const pre = document.querySelector('pre')
    return !!h1 && h1.offsetParent === null && !!pre && pre.innerText.includes('# Release notes')
  })
  await page.screenshot({ path: join(OUT, `raw-${theme}.png`) })
  console.log(`captured raw-${theme}.png`)
}

await browser.close()
console.log(`done -> ${OUT}`)
