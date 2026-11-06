/**
 * Real-browser evidence on capture/steer-landed-highlight.html: the row a
 * requeued steer's text lands on flashes once, and only once.
 *
 * Serves the dev build itself (vite in-process, loopback, ephemeral port).
 *
 * Per theme (dark, light):
 *   - the reducer stamped exactly one row, the rebuilt one, and the steer row
 *     above it still reads "Turn ended before this applied";
 *   - the flash is `msg-highlight` for 1200 ms; the runner pauses it at 300 ms
 *     and shoots steer-landed-highlight-<theme>.png mid-highlight;
 *   - the mark drops when the animation ends, and a remount of every row (what
 *     the virtualized transcript does on a scroll away and back) draws none.
 * Then once with prefers-reduced-motion: reduce, which draws no flash at all.
 *
 * Usage (in website/):
 *   node scripts/capture-steer-landed-highlight.mjs <outdir>
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync, realpathSync } from 'node:fs'
import { resolve } from 'node:path'

const OUT = resolve(process.argv[2] || '../temp-screenshots/steer-landed-highlight')
mkdirSync(OUT, { recursive: true })
const server = await createServer({
  configFile: './vite.config.ts',
  // A worktree can link node_modules from another checkout; vite then needs
  // that real path on its allow list to serve worker URLs from it.
  server: { host: '127.0.0.1', port: 5199, strictPort: false, fs: { allow: [resolve('.'), realpathSync('node_modules')] } },
  logLevel: 'warn',
})
await server.listen()
const BASE = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok, detail = '') => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? ` ${detail}` : ''}`)
  if (!ok) failures++
}

/** Show the rows, and freeze the flash at `at` ms the moment it starts. */
function showAndFreeze(page, at) {
  return page.evaluate(at => new Promise((done) => {
    const freeze = (el) => {
      const anim = el.getAnimations().find(a => a.animationName)
      if (!anim) return done({ found: true, animation: null })
      anim.pause()
      anim.currentTime = at
      const timing = anim.effect.getComputedTiming()
      done({ found: true, animation: { name: anim.animationName, duration: timing.duration, currentTime: anim.currentTime }, shadow: getComputedStyle(el).boxShadow })
    }
    const obs = new MutationObserver(() => {
      const el = document.querySelector('[data-landed-from-steer]')
      if (el) { obs.disconnect(); freeze(el) }
    })
    obs.observe(document.body, { subtree: true, childList: true, attributes: true })
    window.__show()
    // Two frames after the rows draw with no mark is the reduced-motion answer.
    setTimeout(() => { obs.disconnect(); done({ found: !!document.querySelector('[data-landed-from-steer]') }) }, 1000)
  }), at)
}

const frames = page => page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))))
const marks = page => page.locator('[data-landed-from-steer]').count()

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({ viewport: { width: 940, height: 520 }, deviceScaleFactor: 2, colorScheme: theme })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  await page.goto(`${BASE}/capture/steer-landed-highlight.html?theme=${theme}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-capture-root][data-ready]', { timeout: 60000 })
  const shot = await showAndFreeze(page, 300)
  check(`[${theme}] the rebuilt row flashes`, !!shot.animation, JSON.stringify(shot))
  check(`[${theme}] the flash is msg-highlight for 1200 ms`, shot.animation?.name === 'msg-highlight' && shot.animation?.duration === 1200)
  check(`[${theme}] mid-highlight the ring is drawn`, !!shot.shadow && shot.shadow !== 'none')
  check(`[${theme}] exactly one row carries the mark, the rebuilt one`, (await marks(page)) === 1
    && (await page.locator('[data-row-landed] [data-landed-from-steer]').count()) === 1)
  const users = page.locator('[data-role="user"]')
  check(`[${theme}] two user rows: the requeued steer and its own row`, (await users.count()) === 2)
  check(`[${theme}] the steer row keeps its requeued line`, /Turn ended before this applied/.test(await users.first().innerText()))
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/steer-landed-highlight-${theme}.png` })
  await page.evaluate(() => document.querySelector('[data-landed-from-steer]')?.getAnimations().forEach(a => a.finish()))
  await frames(page)
  check(`[${theme}] the mark drops when the flash ends`, (await marks(page)) === 0)
  await page.evaluate(() => window.__remount())
  await frames(page)
  check(`[${theme}] a remount of every row draws no flash`, (await marks(page)) === 0 && (await users.count()) === 2)
  check(`[${theme}] no page errors`, errors.length === 0, errors.join(' | '))
  await ctx.close()
}

{
  const ctx = await browser.newContext({ viewport: { width: 940, height: 520 }, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  await page.goto(`${BASE}/capture/steer-landed-highlight.html?theme=dark`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-capture-root][data-ready]', { timeout: 60000 })
  const shot = await showAndFreeze(page, 300)
  check('[reduced motion] no flash at all', shot.found === false && (await page.locator('[data-role="user"]').count()) === 2, JSON.stringify(shot))
  await ctx.close()
}

await browser.close()
await server.close()
console.log(failures ? `${failures} check(s) FAILED` : 'all checks passed')
process.exit(failures ? 1 : 0)
