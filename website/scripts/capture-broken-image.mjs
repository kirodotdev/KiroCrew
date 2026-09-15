/**
 * Screenshots of the broken-image fallback chip (capture/broken-image.html).
 *
 * Self-checking: asserts the three fallback chips render with the right wording
 * (missing-file vs generic load failure), the control image did NOT fall back,
 * and the click-to-copy affordance flashes its confirmation — then screenshots.
 * A screenshot of the wrong state is worse evidence than none.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6817 --strictPort    # in another shell
 *   node scripts/capture-broken-image.mjs http://127.0.0.1:6817 ../temp-screenshots/broken-image-chip
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6817'
const OUT = process.argv[3] || '../temp-screenshots/broken-image-chip'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 860, height: 900 }, deviceScaleFactor: 2 })

for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/broken-image.html?theme=${theme}`)
  // The control image must LOAD (naturalWidth > 0) — otherwise every image on
  // the page failed and the scene shows nothing but fallbacks.
  await page.waitForFunction(() => {
    const img = document.querySelector('img')
    return img && img.naturalWidth > 0
  })
  // All three broken images must have fallen back to the chip.
  await page.waitForFunction(() =>
    document.body.innerText.includes('图片文件已不存在'))
  const missing = await page.getByText('图片文件已不存在').count()
  const remote = await page.getByText('图片加载失败').count()
  if (missing !== 2) throw new Error(`expected 2 missing-file chips, got ${missing}`)
  if (remote !== 1) throw new Error(`expected 1 remote-failure chip, got ${remote}`)
  if ((await page.locator('img').count()) !== 1) throw new Error('a broken <img> survived')
  await page.screenshot({ path: join(OUT, `chips-${theme}.png`) })
  console.log(`captured chips-${theme}.png`)
}

// Copied-state frame: click the first missing-file chip, catch the green check.
await page.goto(`${BASE}/capture/broken-image.html?theme=dark`)
await page.waitForFunction(() => document.body.innerText.includes('图片文件已不存在'))
const chip = page.locator('[role="button"]').filter({ hasText: '图片文件已不存在' }).first()
await chip.click()
const title = await chip.getAttribute('title')
if (title !== '已复制！') throw new Error(`copied title flash missing, got: ${title}`)
await page.screenshot({ path: join(OUT, 'chip-copied-dark.png') })
console.log('captured chip-copied-dark.png')

await browser.close()
console.log(`done → ${OUT}`)
