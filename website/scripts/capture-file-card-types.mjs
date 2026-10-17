/**
 * Screenshot runner for capture/file-card-types.html.
 *
 * Run from website/:
 *   node scripts/capture-file-card-types.mjs [base url] [outdir]
 *
 * With no base URL, the runner starts a loopback-only Vite server and closes
 * it after capture. One PNG is written per theme. The capture includes every
 * file family plus the real audio, video and image branches.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { createServer } from 'vite'

let base = process.argv[2]
const OUT = process.argv[3] || '../temp-screenshots/file-card-types'
mkdirSync(OUT, { recursive: true })

let server
if (!base) {
  server = await createServer({
    logLevel: 'error',
    server: { host: '127.0.0.1', port: 41739, strictPort: false },
  })
  await server.listen()
  base = server.resolvedUrls?.local[0]
  if (!base) throw new Error('Vite did not report a loopback URL')
}

const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
  'base64',
)

const browser = await chromium.launch()
let failed = 0
for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({ viewport: { width: 740, height: 900 }, deviceScaleFactor: 2, colorScheme: theme })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  await page.route('**/api/outbox/*', async route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('.png')) {
      await route.fulfill({ contentType: 'image/png', body: PNG })
      return
    }
    if (path.endsWith('.mp4')) {
      await route.fulfill({ contentType: 'video/mp4', body: '' })
      return
    }
    if (path.endsWith('.mp3')) {
      await route.fulfill({ contentType: 'audio/mpeg', body: '' })
      return
    }
    await route.fulfill({ status: 404, body: '' })
  })
  try {
    await page.goto(`${base}/capture/file-card-types.html?theme=${theme}`, { waitUntil: 'networkidle' })
    await page.locator('[data-block]').first().waitFor({ timeout: 15000 })
    if (errors.length) throw new Error(`page errors: ${errors.join(' | ')}`)
    if (await page.locator('[role="dialog"]').count()) throw new Error('unexpected dialog')
    const families = await page.locator('[data-testid="file-card-glyph"]').evaluateAll(els => els.map(e => e.dataset.family))
    const blocks = await page.locator('[data-block]').count()
    if (families.length !== blocks) throw new Error(`${blocks} cards but ${families.length} glyphs`)
    const unknown = families.filter(f => f === 'unknown').length
    if (unknown !== 1) throw new Error(`expected exactly one 'unknown' card, got ${unknown}: ${families.join(',')}`)
    for (const family of ['image', 'video', 'audio']) {
      if (!families.includes(family)) throw new Error(`missing ${family} media card`)
    }
    console.log(`${theme}: ${families.join(' ')}`)
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/file-card-types-${theme}.png` })
  } catch (e) {
    console.error(`${theme}: FAILED — ${e}`)
    failed++
  } finally {
    await ctx.close()
  }
}
await browser.close()
await server?.close()
process.exit(failed ? 1 : 0)
