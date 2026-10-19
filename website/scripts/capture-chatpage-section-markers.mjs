/**
 * Screenshots of section markers inside the REAL ChatPage transcript, both themes.
 *
 * Every frame asserts before it is written: two marker rows mounted through the
 * renderer registry, the labelled one carrying its label, and no assistant bubble
 * holding the marker's fallback text, which is what an unregistered role draws.
 *
 * Usage (from website/):
 *   npx vite --host 127.0.0.1 --port 6875 --strictPort   # in another shell
 *   node scripts/capture-chatpage-section-markers.mjs [base] [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6875'
const OUT = process.argv[3] || '../temp-screenshots/transcript-section-markers'
mkdirSync(OUT, { recursive: true })

const browserEnv = { ...process.env }
delete browserEnv.LD_LIBRARY_PATH
const browser = await chromium.launch({
  env: browserEnv,
  args: ['--disable-gpu', '--no-sandbox', '--disable-dev-shm-usage', '--use-gl=swiftshader'],
})
let failed = false
const check = (name, ok, detail) => {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
}

// The fixture module is served by vite, so the route answers with the exact rows
// the entry preloads rather than a copy that can drift.
const probe = await browser.newPage()
await probe.goto(`${BASE}/capture/chatpage-section-markers.html`)
const fixture = await probe.evaluate(async () => {
  const mod = await import('/capture/chatpage-section-markers-fixture.ts')
  return mod.markerFixture()
})
await probe.close()

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 2 })
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ key: 'slot-a', title: 'Review queue', running: false, has_more: false, total: fixture.length, messages: fixture }),
      })
    }
    if (/\/api\/chat\/(tags|pins|folders|tag-columns)$/.test(path)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    }
    const isList = /commands|skills|agents$|sessions|files|history|models|artifacts|folders|slots$/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/chatpage-section-markers.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('Starting on item-43 now.').first().waitFor()
  await page.waitForTimeout(300)

  const applied = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
  check(`${theme} theme applied`, applied === theme || !!applied?.endsWith(`-${theme}`), `data-theme=${applied}`)
  const rows = page.getByTestId('section-marker-row')
  const count = await rows.count()
  check(`${theme} marker rows in ChatPage`, count === 2, `rows=${count}`)
  const labelled = count ? await rows.first().getAttribute('aria-label') : ''
  check(`${theme} labelled row carries its label`, !!labelled && labelled.includes('item-41'), `aria-label=${labelled}`)
  const bubbled = await page.locator('[data-capture-root] .msg-a, [data-capture-root] .msg').filter({ hasText: 'End of section' }).count()
  check(`${theme} no marker drawn as a bubble`, bubbled === 0, `bubbles=${bubbled}`)

  await page.screenshot({ path: `${OUT}/chatpage-section-markers-${theme}.png` })
  await page.close()
}

await browser.close()
if (failed) {
  console.error('CAPTURE FAILED: a frame did not match its asserted state')
  process.exit(1)
}
console.log(`frames written to ${OUT}`)
