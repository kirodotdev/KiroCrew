/**
 * Screenshot + measurement runner for capture/subagent-completion-overflow.html.
 *
 * Two shells, from website/:
 *   npx vite --host 127.0.0.1 --port 6815 --strictPort
 *   node scripts/capture-subagent-completion-overflow.mjs http://127.0.0.1:6815 \
 *     ../temp-screenshots/subagent-completion-overflow
 *
 * The frames are evidence, but the ASSERTIONS are the point: `after` must keep
 * the expanded digest inside a bounded, inner-scrolling body; `before` must
 * show the unbounded body the pre-fix code produced. A run that photographs
 * the wrong state exits nonzero instead of emitting a misleading image.
 *
 * 900px viewport at deviceScaleFactor 2 keeps each frame under 2000px per edge.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6815'
const OUT = process.argv[3] || '../temp-screenshots/subagent-completion-overflow'

// The Tailwind bound the fix puts on the expanded body: max-h-[24rem], which the
// component test pins literally so this constant and the class cannot drift.
const BODY_MAX_PX = 384

const BODY = '[data-testid="subagent-completion-body"]'

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

for (const theme of ['dark', 'light']) {
  for (const scene of ['before', 'after']) {
    const ctx = await browser.newContext({
      viewport: { width: 900, height: 1100 },
      deviceScaleFactor: 2,
      colorScheme: theme,
    })
    const page = await ctx.newPage()
    const errors = []
    page.on('pageerror', e => errors.push(String(e)))

    const name = `${theme}-${scene}.png`
    try {
      // 'load' rather than 'networkidle': a Vite dev server's HMR socket and
      // on-demand module graph never go network-idle, so that wait state is
      // the flaky one. The selector waits below are the real readiness signal.
      await page.goto(`${BASE}/capture/subagent-completion-overflow.html?scene=${scene}&theme=${theme}`, {
        waitUntil: 'load',
      })
      await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
      await page.waitForSelector('[data-testid="subagent-completion-card"]', { timeout: 10000 })
      // A mid-wave chunk's card opens FOLDED (failed=0 until the final chunk),
      // so expand the disclosure the way a reader would.
      const toggle = page.locator('[data-testid="subagent-completion-card"] button[aria-expanded="false"]')
      if (await toggle.count()) await toggle.click()
      await page.waitForSelector(BODY, { timeout: 10000 })
      // The chevron's rotate transition is the only motion; let it finish so
      // the frame is not a mid-rotation blur.
      await page.waitForTimeout(300)

      const m = await page.evaluate(sel => {
        const body = document.querySelector(sel)
        const bb = body.getBoundingClientRect()
        return {
          bodyH: Math.round(bb.height),
          bodyScrollH: body.scrollHeight,
          innerScrolls: body.scrollHeight > body.clientHeight + 1,
        }
      }, BODY)

      await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${name}` })

      let frameFailed = 0
      if (scene === 'after') {
        if (m.bodyH > BODY_MAX_PX + 1) {
          frameFailed++
          console.error(`FAIL ${name}: body ${m.bodyH}px exceeds the ${BODY_MAX_PX}px bound`)
        }
        if (!m.innerScrolls) {
          frameFailed++
          console.error(`FAIL ${name}: digest fits ${m.bodyH}px — too short to prove the inner scroll`)
        }
      } else if (m.bodyH <= BODY_MAX_PX) {
        frameFailed++
        console.error(`FAIL ${name}: pre-fix body is only ${m.bodyH}px — the scene did not reproduce the unbounded state`)
      }
      if (errors.length) {
        frameFailed++
        console.error(`FAIL ${name}: ${errors.length} page error(s)\n  ${errors.join('\n  ')}`)
      }
      failed += frameFailed
      // Only claim a frame is good when nothing about it failed — an `ok` line
      // beside a FAIL line is how a misleading screenshot gets published.
      if (!frameFailed) {
        console.log(`ok   ${name}  body=${m.bodyH}px (content ${m.bodyScrollH}px, inner scroll: ${m.innerScrolls})`)
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
console.log('\nall scenes match their expected containment')
