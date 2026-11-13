/** Real-browser evidence for #32280: the publication badge names its provider,
 *  shows that name as a hover tooltip, and a failed sync shows its error text
 *  through ErrorNotice on the gallery card and the library row.
 *
 * Drives website/capture/publication-badge-provider.html (the REAL
 * LibraryMasonry card and ArtifactRow over a fetch stub). Asserted per theme:
 *  1-library: the badge is labelled "Published to Acme Drive (private)", and
 *             the failed artifact shows its sync error on the card and the row.
 *  2-hover:   hovering the card badge leaves it the topmost element there, so
 *             the native tooltip can show.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6825 --strictPort    # in another shell (website/)
 *   node scripts/capture-32280-publication-badge-provider.mjs http://127.0.0.1:6825 ../temp-screenshots/32280
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6825'
const OUT = process.argv[3] || '../temp-screenshots/32280'
mkdirSync(OUT, { recursive: true })

const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok) => {
  console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`)
  if (!ok) failures++
}

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: { width: 1140, height: 760 } })
  page.on('pageerror', e => { console.error(`[${theme}] pageerror:`, e.message); failures++ })
  await page.goto(`${BASE}/capture/publication-badge-provider.html?theme=${theme}`, { waitUntil: 'networkidle' })
  const gallery = page.getByTestId('gallery')
  const table = page.getByTestId('table')
  await gallery.getByLabel('Published to Acme Drive (private)').first().waitFor({ state: 'visible', timeout: 15000 })
  check(`[${theme}] row badge names the provider`,
    await table.getByLabel('Published to Acme Drive (private)').first().isVisible())
  for (const [name, host] of [['card', gallery], ['row', table]]) {
    const notice = host.getByTestId('publication-sync-error')
    check(`[${theme}] ${name}: sync error shown`,
      (await notice.count()) === 1 && (await notice.textContent())?.includes('destination conflict'))
  }
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/${theme}-1-library.png`, fullPage: true })

  const badge = gallery.getByTestId('publication-badge').first()
  const box = await badge.boundingBox()
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  const topmost = await page.evaluate(([x, y]) => {
    const el = document.elementFromPoint(x, y)
    return !!el?.closest('[data-testid="publication-badge"]')
  }, [box.x + box.width / 2, box.y + box.height / 2])
  check(`[${theme}] card badge is topmost under the pointer`, topmost)
  await page.screenshot({ path: `${OUT}/${theme}-2-hover.png`, clip: { x: 0, y: 0, width: 640, height: 360 } })
  await page.close()
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
