/**
 * Screenshot runner for capture/img-loading-skeleton.html.
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6835 --strictPort
 *   node scripts/capture-img-loading-skeleton.mjs http://127.0.0.1:6835 <outdir> <loaded.png>
 *
 * The loading states are produced by HOLDING the pending-* image requests
 * open (route handler that never resolves), so onLoad/onError never fire —
 * the exact in-flight state the fix is about. The loaded.png route is
 * fulfilled from a real PNG on disk. deviceScaleFactor 1, element-scoped
 * frames under 2000px on both edges.
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6835'
const OUT = process.argv[3] || '../temp-screenshots/img-loading-skeleton'
const LOADED_PNG = process.argv[4]

mkdirSync(OUT, { recursive: true })
const loadedBytes = LOADED_PNG ? readFileSync(LOADED_PNG) : null

const browser = await chromium.launch()
let failed = 0

for (const theme of ['light', 'dark']) {
  const ctx = await browser.newContext({
    viewport: { width: 920, height: 1600 },
    deviceScaleFactor: 1,
    colorScheme: theme,
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push(String(e)))
  // Hold every pending-* image request open forever: the loading state.
  await page.route('https://cap.test/pending-*.png', () => {})
  await page.route('https://cap.test/loaded.png', (route) =>
    loadedBytes
      ? route.fulfill({ contentType: 'image/png', body: loadedBytes })
      : route.abort(),
  )
  try {
    await page.goto(`${BASE}/capture/img-loading-skeleton.html?theme=${theme}`, {
      waitUntil: 'domcontentloaded',
    })
    await page.waitForSelector('[data-capture-root]', { timeout: 30000 })
    // Let the loaded.png decode and its skeleton release.
    await page.waitForTimeout(800)
    if (errors.length) throw new Error(`page errors: ${errors.join(' | ')}`)

    const probe = await page.evaluate(() => {
      const box = (sel) => {
        const el = document.querySelector(sel)
        if (!el) return null
        const r = el.getBoundingClientRect()
        return { w: Math.round(r.width), h: Math.round(r.height) }
      }
      const overlaySel = 'span[aria-hidden="true"].pointer-events-none'
      return {
        overlays: document.querySelectorAll(overlaySel).length,
        pendingBox: box(`[data-episode="pending"] ${overlaySel}`),
        compactBox: box(`[data-episode="pending-compact"] ${overlaySel}`),
        learnedBox: box(`[data-episode="learned"] ${overlaySel}`),
        loadedOverlay: document.querySelector(`[data-episode="loaded"] ${overlaySel}`) !== null,
        loadedImgW: box('[data-episode="loaded"] img')?.w ?? 0,
        icons: document.querySelectorAll(`${overlaySel} svg.lucide-image`).length,
        pulses: document.querySelectorAll(`${overlaySel} .animate-pulse`).length,
        // Computed styles prove Tailwind compiled the recipe — DOM counts
        // alone stay green when the stylesheet is missing entirely.
        overlayBg: (() => {
          const el = document.querySelector(overlaySel)
          return el ? getComputedStyle(el).backgroundColor : ''
        })(),
        compactEnd: (() => {
          const el = document.querySelector(`[data-episode="pending-compact"] ${overlaySel}`)
          const img = document.querySelector('[data-episode="pending-compact"] img')
          if (!el || !img) return NaN
          return Math.abs(el.getBoundingClientRect().right - img.getBoundingClientRect().right)
        })(),
      }
    })
    // 4 skeletons: pending-a, pending-b, compact, learned; loaded released its own.
    if (probe.overlays !== 4) throw new Error(`expected 4 overlays, got ${probe.overlays}`)
    if (probe.loadedOverlay) throw new Error('loaded image still shows its skeleton')
    if (!probe.pendingBox || probe.pendingBox.w !== 420 || probe.pendingBox.h !== 236)
      throw new Error(`pending box ${JSON.stringify(probe.pendingBox)}, expected 420x236`)
    if (!probe.compactBox || probe.compactBox.w !== 240 || probe.compactBox.h !== 180)
      throw new Error(`compact box ${JSON.stringify(probe.compactBox)}, expected 240x180`)
    if (!probe.learnedBox || probe.learnedBox.w !== 640 || probe.learnedBox.h !== 360)
      throw new Error(`learned box ${JSON.stringify(probe.learnedBox)}, expected 640x360`)
    if (probe.icons !== 4) throw new Error(`expected 4 lucide image icons, got ${probe.icons}`)
    if (probe.pulses < 4) throw new Error(`expected >=4 pulse layers, got ${probe.pulses}`)
    if (!probe.overlayBg || probe.overlayBg === 'rgba(0, 0, 0, 0)')
      throw new Error(`overlay bg not compiled (${probe.overlayBg})`)
    if (!(probe.compactEnd < 1)) throw new Error(`compact overlay misaligned by ${probe.compactEnd}px`)
    if (!(probe.loadedImgW > 400)) throw new Error(`loaded img width ${probe.loadedImgW}, expected natural layout`)

    await page.locator('[data-capture-group="loading"]').screenshot({
      path: `${OUT}/img-skeleton-loading-${theme}.png`,
    })
    await page.locator('[data-capture-group="loaded"]').screenshot({
      path: `${OUT}/img-skeleton-loaded-${theme}.png`,
    })
    console.log(`ok ${theme}: boxes 420x236 / 240x180 / 640x360, loaded released, bg=${probe.overlayBg}`)
  } catch (e) {
    failed = 1
    console.error(`FAIL ${theme}:`, e.message)
  } finally {
    await ctx.close()
  }
}

await browser.close()
process.exit(failed)
