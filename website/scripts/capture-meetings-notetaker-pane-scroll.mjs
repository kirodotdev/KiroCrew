/**
 * Screenshot + measurement runner for capture/meetings-notetaker-pane-scroll.html.
 *
 * Two shells, from website/:
 *   npx vite --host 127.0.0.1 --port 6817 --strictPort
 *   node scripts/capture-meetings-notetaker-pane-scroll.mjs http://127.0.0.1:6817 \
 *     ../temp-screenshots/meetings-notetaker-scroll
 *
 * The frames are evidence, but the ASSERTIONS are the point: `after` must show
 * a pane whose computed overflow-y is `auto` with content taller than the pane
 * and a scrollTop that actually moves; `before` must reproduce the pre-fix
 * clip — the card computes overflow hidden and nothing scrolls. A run that
 * photographs the wrong state exits nonzero instead of emitting a misleading
 * image. The second `after` frame is taken scrolled to the bottom, so the
 * END-OF-NOTES heading (unreachable pre-fix) is visibly on screen.
 *
 * 900px viewport at deviceScaleFactor 2 keeps each frame under 2000px per edge.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6817'
const OUT = process.argv[3] || '../temp-screenshots/meetings-notetaker-scroll'

const PANE = '[data-testid="agent-output-pane"]'

mkdirSync(OUT, { recursive: true })

// The mise-installed node exports an LD_LIBRARY_PATH that shadows the system
// libstdc++ Chromium's GPU stack needs; scrub it from the browser's env.
const env = { ...process.env }
delete env.LD_LIBRARY_PATH

const browser = await chromium.launch({ env })
let failed = 0

for (const theme of ['dark', 'light']) {
  for (const scene of ['before', 'after']) {
    const ctx = await browser.newContext({
      viewport: { width: 900, height: 900 },
      deviceScaleFactor: 2,
      colorScheme: theme,
    })
    const page = await ctx.newPage()
    const errors = []
    page.on('pageerror', e => errors.push(String(e)))

    const name = `${theme}-${scene}.png`
    try {
      // 'load' rather than 'networkidle': a Vite dev server's HMR socket never
      // goes network-idle. The selector wait below is the real readiness signal.
      await page.goto(`${BASE}/capture/meetings-notetaker-pane-scroll.html?scene=${scene}&theme=${theme}`, {
        waitUntil: 'load',
      })
      await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
      await page.waitForSelector(PANE, { timeout: 10000 })
      // animate-rise is the Card's only motion; let it settle.
      await page.waitForTimeout(400)

      const m = await page.evaluate(sel => {
        const pane = document.querySelector(sel)
        const card = pane.closest('.card-glow')
        pane.scrollTop = 1000
        return {
          paneOverflowY: getComputedStyle(pane).overflowY,
          cardOverflowY: getComputedStyle(card).overflowY,
          paneScrollH: pane.scrollHeight,
          paneClientH: pane.clientHeight,
          paneScrolled: pane.scrollTop > 0,
          cardClipped: card.scrollHeight > card.clientHeight + 1,
        }
      }, PANE)

      let frameFailed = 0
      if (scene === 'after') {
        if (m.paneOverflowY !== 'auto') {
          frameFailed++
          console.error(`FAIL ${name}: pane computes overflow-y ${m.paneOverflowY}, not auto — the card-glow clip won again`)
        }
        if (m.paneScrollH <= m.paneClientH + 1 || !m.paneScrolled) {
          frameFailed++
          console.error(`FAIL ${name}: pane ${m.paneClientH}px holds ${m.paneScrollH}px and scrolled=${m.paneScrolled} — nothing to scroll, the scene proves nothing`)
        }
        // Scrolled-to-bottom frame: the content that was unreachable pre-fix.
        await page.evaluate(sel => {
          const pane = document.querySelector(sel)
          pane.scrollTop = pane.scrollHeight
        }, PANE)
        await page.waitForTimeout(150)
        await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${theme}-after-scrolled.png` })
        await page.evaluate(sel => {
          document.querySelector(sel).scrollTop = 0
        }, PANE)
        await page.waitForTimeout(150)
      } else {
        if (m.paneScrolled || m.paneOverflowY === 'auto') {
          frameFailed++
          console.error(`FAIL ${name}: pre-fix scene scrolls (overflow-y ${m.paneOverflowY}) — it did not reproduce the clip`)
        }
        if (m.cardOverflowY !== 'hidden' || !m.cardClipped) {
          frameFailed++
          console.error(`FAIL ${name}: card overflow-y ${m.cardOverflowY}, clipped=${m.cardClipped} — the card-glow clip is not engaging`)
        }
      }
      await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${name}` })
      if (errors.length) {
        frameFailed++
        console.error(`FAIL ${name}: ${errors.length} page error(s)\n  ${errors.join('\n  ')}`)
      }
      failed += frameFailed
      // Only claim a frame is good when nothing about it failed — an `ok` line
      // beside a FAIL line is how a misleading screenshot gets published.
      if (!frameFailed) {
        console.log(
          `ok   ${name}  pane=${m.paneClientH}px content=${m.paneScrollH}px pane-overflow=${m.paneOverflowY} card-overflow=${m.cardOverflowY}`,
        )
      }
    } catch (err) {
      failed++
      console.error(`FAIL ${name}: ${err.message}`)
    }
    await ctx.close()
  }
}

await browser.close()
if (failed) {
  console.error(`\n${failed} assertion(s) failed — the frames do not show the state they claim.`)
  process.exit(1)
}
console.log('\nall scenes match their expected scroll behaviour')
