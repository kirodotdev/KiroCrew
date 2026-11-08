/**
 * Screenshots of the Dashboard tab loading state (#24610), before and after, in
 * both themes. Boots Vite in-process (no external server), loads the
 * `capture/dashboard-loading.html` harness for each variant/theme, and writes a
 * PNG per combination.
 *
 * Usage: node scripts/capture-dashboard-loading.mjs [outDir]
 */
import { createServer } from 'vite'
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = join(HERE, '..')
const OUT = process.argv[2] || join(ROOT, '..', 'temp-screenshots', 'dashboard-loading')
mkdirSync(OUT, { recursive: true })

const server = await createServer({ root: ROOT, server: { host: '127.0.0.1', port: 6841, strictPort: true } })
await server.listen()
const base = `http://127.0.0.1:6841`

const browser = await chromium.launch()

async function shoot(motion) {
  const page = await browser.newPage({
    viewport: { width: 520, height: 640 },
    deviceScaleFactor: 2,
    reducedMotion: motion,
  })
  // `before` and `after` in each theme under default motion; plus the `after`
  // treatment under reduced motion, to show the pulse freezes to a still box.
  const variants = motion === 'reduce' ? ['after'] : ['before', 'after']
  for (const variant of variants) {
    for (const theme of ['light', 'dark']) {
      await page.goto(`${base}/capture/dashboard-loading.html?variant=${variant}&theme=${theme}`, { waitUntil: 'networkidle' })
      await page.waitForTimeout(300)
      const suffix = motion === 'reduce' ? '-reduced-motion' : ''
      const file = join(OUT, `${variant}-${theme}${suffix}.png`)
      await page.screenshot({ path: file })
      console.log(`wrote ${file}`)
    }
  }
  await page.close()
}

await shoot('no-preference')
await shoot('reduce')

await browser.close()
await server.close()
