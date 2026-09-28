/**
 * Screenshot runner for capture/thinking-bursts.html.
 *
 * Two shells, from website/:
 *   npx vite --host 127.0.0.1 --port 6811 --strictPort
 *   node scripts/capture-thinking-bursts.mjs http://127.0.0.1:6811 ../temp-screenshots/thinking-bursts before
 *
 * The third argument is only a filename prefix, so the same runner photographs
 * the fixed tree and the reverted tree without editing anything.
 *
 * Every reasoning block is EXPANDED by clicking its real toggle, because the
 * whole question is which text ended up inside which block. The runner also
 * prints the row trace and the per-block text it read out of the DOM, so the
 * claim does not rest on the picture alone.
 */
import { chromium } from 'playwright'
import { mkdir } from 'node:fs/promises'

const BASE = process.argv[2] || 'http://127.0.0.1:6811'
const OUT = process.argv[3] || '../temp-screenshots/thinking-bursts'
const PREFIX = process.argv[4] || 'shot'

await mkdir(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

for (const refresh of ['0', '1']) {
  const ctx = await browser.newContext({
    viewport: { width: 900, height: 900 },
    deviceScaleFactor: 2,
    colorScheme: 'dark',
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  await page.goto(`${BASE}/capture/thinking-bursts.html?theme=dark&refresh=${refresh}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-capture-root]', { timeout: 15000 })

  // Expand every reasoning block through its own button — the disclosure is
  // real state, so this is the only honest way to photograph the contents.
  const toggles = await page.$$('[data-row="thinking"] button[aria-expanded]')
  for (const t of toggles) await t.click()
  await page.waitForTimeout(700)

  const read = await page.evaluate(() => {
    const root = document.querySelector('[data-capture-root]')
    return {
      trace: root.getAttribute('data-trace'),
      bursts: Number(root.getAttribute('data-bursts')),
      blocks: [...document.querySelectorAll('[data-row="thinking"]')]
        .map(el => (el.textContent || '').replace(/^Thinking/, '').trim()),
    }
  })

  const name = `${PREFIX}${refresh === '1' ? '-after-refresh' : ''}.png`
  const target = await page.$('[data-capture-root]')
  await target.screenshot({ path: `${OUT}/${name}` })
  const box = await target.boundingBox()

  console.log(`\n  ${name}  ${Math.round(box.width)}x${Math.round(box.height)} css`)
  console.log(`    rows   : ${read.trace}`)
  console.log(`    blocks : ${read.bursts}`)
  read.blocks.forEach((b, i) => console.log(`      [${i + 1}] ${b.slice(0, 120)}`))

  if (errors.length) {
    failed++
    console.error(`FAIL ${name}: ${errors.length} page error(s)\n  ${errors.join('\n  ')}`)
  }
  await ctx.close()
}

await browser.close()
if (failed) {
  console.error(`\n${failed} frame(s) rendered with page errors — not trustworthy.`)
  process.exit(1)
}
