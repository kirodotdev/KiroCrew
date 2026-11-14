/** Real-browser evidence for the conductor mode Board on a long run: every text
 *  the Status view cuts, measured at the side panel's three widths.
 *
 * Drives website/capture/conductor-mode-board-long.html (the REAL panel over
 * fixture reads). For each width it screenshots the Status view and lists each
 * element whose text is cut: one line with an ellipsis (scrollWidth past
 * clientWidth) or a clamp (scrollHeight past clientHeight). With `--strict` it
 * exits non-zero when anything on screen is still cut.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6871 --strictPort    # in another shell (website/)
 *   node scripts/capture-conductor-mode-board-long.mjs http://127.0.0.1:6871 ../temp-screenshots/board-long [--strict] [--tag after]
 */
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6871'
const OUT = process.argv[3] || '../temp-screenshots/board-long'
const strict = process.argv.includes('--strict')
const tagAt = process.argv.indexOf('--tag')
const TAG = tagAt > 0 ? process.argv[tagAt + 1] : 'before'
mkdirSync(OUT, { recursive: true })

const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
const report = {}
let cutTotal = 0
for (const w of [360, 560, 900]) {
  const page = await browser.newPage({ viewport: { width: w + 40, height: 1540 }, deviceScaleFactor: 2 })
  page.on('pageerror', e => { console.error(`[${w}] pageerror:`, e.message); process.exitCode = 1 })
  await page.goto(`${BASE}/capture/conductor-mode-board-long.html?w=${w}`, { waitUntil: 'networkidle' })
  await page.locator('[data-testid="conductor-mode-milestone"][data-state="running"]').first().waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(300)
  const cut = await page.evaluate(() => {
    const status = document.querySelector('[data-testid="conductor-mode-status"]')
    const out = []
    for (const el of status.querySelectorAll('*')) {
      if (!(el instanceof HTMLElement) || !el.textContent?.trim() || el.offsetParent === null) continue
      const s = getComputedStyle(el)
      const wide = el.scrollWidth > el.clientWidth + 1 && (s.textOverflow === 'ellipsis' || s.overflow === 'hidden')
      const tall = el.scrollHeight > el.clientHeight + 1 && s.webkitLineClamp !== 'none' && s.webkitLineClamp !== ''
      const ellipsisInText = /…$/.test(el.textContent.trim()) && el.children.length === 0
      if (!wide && !tall && !ellipsisInText) continue
      const section = el.closest('section')?.getAttribute('data-testid') || ''
      out.push({ section, how: tall ? `line-clamp ${s.webkitLineClamp}` : wide ? 'nowrap+ellipsis' : 'cut in code', shown: el.clientWidth, full: el.scrollWidth, text: el.textContent.trim().slice(0, 80) })
    }
    // A parent and its child cut the same text: keep the innermost.
    return out.filter((o, i) => !out.some((p, j) => j > i && p.text.startsWith(o.text.slice(0, 40)) && p.section === o.section))
  })
  report[w] = cut
  cutTotal += cut.length
  // The frame is taller than any run needs: crop to the Recap's bottom edge.
  const frame = await page.getByTestId('capture-frame').boundingBox()
  const recap = await page.getByTestId('conductor-mode-recap-section').boundingBox()
  await page.screenshot({ path: `${OUT}/${TAG}-${w}.png`, clip: { x: frame.x, y: frame.y, width: frame.width, height: recap.y + recap.height + 16 - frame.y } })
  console.log(`[${w}px] ${cut.length} cut elements`)
  for (const c of cut) console.log(`  ${c.section} | ${c.how} | ${c.text}`)
  await page.close()
}
writeFileSync(`${OUT}/${TAG}-cuts.json`, JSON.stringify(report, null, 2))
await browser.close()
if (strict && cutTotal > 0) {
  console.error(`${cutTotal} cut elements remain`)
  process.exitCode = 1
}
