/**
 * Recording runner for capture/steer-requeue-live.html.
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort
 *   node scripts/capture-steer-requeue-live.mjs http://127.0.0.1:6841 <outdir>
 *
 * Two recorded passes on the real virtualized transcript:
 *   bottom   -- the reader follows the tail; steps 0 -> 3 play in order.
 *   scrolled -- the reader is scrolled up into history; the same steps play
 *               and the first visible row must not move.
 * Every animation frame counts the bubbles carrying the steer text. The run
 * bottom pass fails if any frame shows zero or a settled step shows a count
 * other than 1, 1, 2, 1. The scrolled pass fails if its anchor row moves.
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync, writeFileSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/steer-requeue-superseded'
const STEER = 'Also check whether the search cluster is still on the old instance type.'
const SIZE = { width: 940, height: 820 }
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const results = {}
let failed = 0

async function startSampler(page) {
  await page.evaluate(text => {
    window.__counts = []
    const tick = () => {
      const n = [...document.querySelectorAll('[data-role="user"]')].filter(e => e.textContent.includes(text)).length
      window.__counts.push({ step: window.__step, n })
      window.__raf = requestAnimationFrame(tick)
    }
    window.__raf = requestAnimationFrame(tick)
  }, STEER)
}

async function settledCount(page) {
  return page.evaluate(text => [...document.querySelectorAll('[data-role="user"]')].filter(e => e.textContent.includes(text)).length, STEER)
}

async function anchor(page) {
  return page.evaluate(() => {
    const sc = document.querySelector('.chat-container')
    const top = sc.getBoundingClientRect().top
    const rows = [...sc.querySelectorAll('[data-display-index]')]
    const first = rows.find(r => r.getBoundingClientRect().bottom > top + 1)
    return { scrollTop: Math.round(sc.scrollTop), index: first?.dataset.displayIndex, offset: Math.round(first.getBoundingClientRect().top - top) }
  })
}

for (const pass of ['bottom', 'scrolled']) {
  const ctx = await browser.newContext({ viewport: SIZE, deviceScaleFactor: 1, colorScheme: 'dark', recordVideo: { dir: `${OUT}/video-tmp`, size: SIZE } })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  const r = { settled: [], minDuringDrain: null, anchors: [] }
  results[pass] = r
  try {
    await page.goto(`${BASE}/capture/steer-requeue-live.html?theme=dark`, { waitUntil: 'networkidle' })
    await page.locator('.chat-container [data-display-index]').first().waitFor({ timeout: 10000 })
    // Start where a live reader would be: following the tail.
    for (let i = 0; i < 5; i++) {
      await page.evaluate(() => { const sc = document.querySelector('.chat-container'); sc.scrollTop = sc.scrollHeight })
      await page.waitForTimeout(200)
    }
    await page.locator('[data-role="user"]', { hasText: STEER }).first().waitFor({ timeout: 10000 })
    await page.waitForTimeout(600)
    if (pass === 'scrolled') {
      r.scrolledUpBy = await page.evaluate(() => {
        const sc = document.querySelector('.chat-container')
        sc.scrollTop = 400
        return Math.round(sc.scrollHeight - sc.clientHeight - sc.scrollTop)
      })
      await page.waitForTimeout(600)
    }
    await startSampler(page)
    r.settled.push(await settledCount(page))
    r.anchors.push(await anchor(page))
    for (const step of [1, 2, 3]) {
      await page.waitForTimeout(1500)
      await page.evaluate(n => window.__setStep(n), step)
      await page.waitForTimeout(400)
      r.settled.push(await settledCount(page))
      r.anchors.push(await anchor(page))
    }
    await page.waitForTimeout(1500)
    const counts = await page.evaluate(() => { cancelAnimationFrame(window.__raf); return window.__counts })
    r.frames = counts.length
    r.minDuringDrain = Math.min(...counts.filter(c => c.step >= 2).map(c => c.n))
    r.minAnyFrame = Math.min(...counts.map(c => c.n))
    const want = [1, 1, 2, 1]
    // The scrolled reader sits above the tail, so the virtualizer does not
    // mount the rows below the viewport; only its anchor is checked there.
    if (pass === 'bottom' && JSON.stringify(r.settled) !== JSON.stringify(want)) throw new Error(`settled counts ${JSON.stringify(r.settled)}, want ${JSON.stringify(want)}`)
    if (pass === 'bottom' && r.minAnyFrame < 1) throw new Error(`a frame drew the steer text zero times (min ${r.minAnyFrame})`)
    if (pass === 'scrolled') {
      const a0 = r.anchors[0]
      for (const a of r.anchors) {
        if (a.scrollTop !== a0.scrollTop || a.index !== a0.index || a.offset !== a0.offset) {
          throw new Error(`scrolled reader moved: ${JSON.stringify(r.anchors)}`)
        }
      }
    }
    if (errors.length) throw new Error(`page errors: ${errors.join(' | ')}`)
    console.log(`${pass}: OK ${JSON.stringify({ settled: r.settled, minAnyFrame: r.minAnyFrame, frames: r.frames })}`)
  } catch (e) {
    console.error(`${pass}: FAILED ${e}`)
    r.error = String(e)
    failed++
  } finally {
    const video = page.video()
    await ctx.close()
    if (video) renameSync(await video.path(), `${OUT}/steer-requeue-live-${pass}.webm`)
  }
}
await browser.close()
writeFileSync(`${OUT}/steer-requeue-live.json`, JSON.stringify(results, null, 2))
process.exit(failed ? 1 : 0)
