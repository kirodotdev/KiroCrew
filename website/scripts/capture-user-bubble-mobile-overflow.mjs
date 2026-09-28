/**
 * Narrow-width measurement + screenshots for the user bubble that hangs off the
 * LEFT edge of a phone viewport when it contains a fenced code block.
 *
 * Drives the ISOLATED capture entry (website/capture/user-bubble-mobile-overflow.html),
 * which rebuilds the transcript's box chain with its production class strings and
 * mounts the real UserMessage inside it, then exposes window.__measure(). This is
 * the only check that exercises REAL layout: the unit suite
 * (src/test/userBubbleMobileOverflow.test.tsx) pins the class contract, but
 * happy-dom computes no layout, so a regression that reintroduces the clip while
 * leaving the classes intact — a new uncapped wrapper spliced into the chain, a
 * host that forgets the wrapper cap — is only caught here.
 *
 * Assertions:
 *  - fix=on: at every width and in every scene the bubble's left edge stays
 *    inside the viewport (unreachableLeft === 0), and the fenced block keeps its
 *    own horizontal scroll (preScrolls) so per-block scrolling is not the thing
 *    that got "fixed".
 *  - fix=off (the before state, the three caps reverted): at every width every
 *    scene must reproduce the clip. A before frame identical to the after frame
 *    is exactly what a toggle that silently failed to apply would produce, so the
 *    reproduction is asserted, not assumed.
 *
 * Scenes: `plain` is the reported defect; `steer` adds the intermediate
 * animation wrapper the steered-message path renders between the wrapper and the
 * bubble, which is a second uncapped box in the same chain. `edit` measures the
 * edit-mode box, which carried the same fixed cap — but it CANNOT reproduce the
 * clip and so is captured for the fixed state only: that box is auto-sized by a
 * hidden text mirror whose `overflow-wrap: anywhere` (index.css `.edit-grow`)
 * makes it wrap at any width, so its fit-content width never reaches the cap.
 * Its cap was made viewport-relative for consistency, and running this arm is
 * what shows that change is a no-op rather than assuming it.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6811 --strictPort   # in another shell
 *   node scripts/capture-user-bubble-mobile-overflow.mjs http://127.0.0.1:6811 ../temp-screenshots/user-bubble-mobile-overflow
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6811'
const OUT = process.argv[3] || '../temp-screenshots/user-bubble-mobile-overflow'
mkdirSync(OUT, { recursive: true })

/** iPhone SE and iPhone 13 CSS widths — the two narrowest common phones. */
const WIDTHS = [320, 390]
const SCENES = ['plain', 'steer', 'edit']
/** The scenes whose box can exceed the cap, so only these can reproduce. */
const REPRO_SCENES = ['plain', 'steer']
/** Screenshot evidence comes from the reported path only; the rest is asserted. */
const SHOT_SCENE = 'plain'

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

for (const fix of ['off', 'on']) {
  for (const w of WIDTHS) {
    for (const scene of SCENES) {
      const page = await browser.newPage({ viewport: { width: w, height: 640 } })
      await page.goto(`${BASE}/capture/user-bubble-mobile-overflow.html?theme=dark&w=${w}&scene=${scene}&fix=${fix}`, { waitUntil: 'networkidle' })
      await page.waitForSelector('[data-role="user"] .message-bubble')
      if (scene === 'edit') {
        // The edit box only exists once the footer's pencil is pressed; the
        // footer is opacity-0 until hover but still hit-testable.
        await page.click('[data-role="user"] button[aria-label="Edit & Resend"]')
        await page.waitForSelector('[data-role="user"] .edit-grow')
      }
      const m = await page.evaluate(() => window.__measure())
      const clipped = m.unreachableLeft > 0
      console.log(
        `fix=${fix} w=${w} ${scene.padEnd(5)}: bubble=${String(m.bubbleWidth).padStart(3)} ` +
        `left=${String(m.bubbleLeft).padStart(5)} right=${String(m.bubbleRight).padStart(3)} ` +
        `docScrollW=${m.docScrollWidth} hScroll=${m.hasHorizontalScroll} preScrolls=${m.preScrolls} ` +
        `→ ${clipped ? `UNREACHABLE ${m.unreachableLeft}px` : 'fits'}`,
      )
      if (fix === 'on') {
        if (clipped) {
          console.error(`FAIL: ${scene} still spills ${m.unreachableLeft}px off the left edge at ${w}px with the caps applied`)
          failures++
        }
        if (m.bubbleRight > w) {
          console.error(`FAIL: ${scene} right edge ${m.bubbleRight} escapes the ${w}px viewport — right-alignment broke`)
          failures++
        }
        // Losing the per-block scroll would mean the code is unreadable a
        // different way, so the fix has to leave it intact. Edit mode renders a
        // textarea, not a <pre>, so there is nothing to scroll there.
        if (scene !== 'edit' && !m.preScrolls) {
          console.error(`FAIL: ${scene} fenced block lost its own horizontal scroll at ${w}px`)
          failures++
        }
      }
      if (fix === 'off' && REPRO_SCENES.includes(scene) && !clipped) {
        console.error(`FAIL: ${scene} did not reproduce the pre-fix clip at ${w}px — before/after evidence would be meaningless`)
        failures++
      }
      if (scene === SHOT_SCENE) {
        await page.screenshot({ path: `${OUT}/${fix === 'off' ? 'before' : 'after'}-${w}px.png` })
      }
      await page.close()
    }
  }
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('ALL GREEN')
