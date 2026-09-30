/**
 * Real-layout measurement + screenshots for the reply-thread footer that floats
 * on the far right of a user bubble CLI UI mode has already moved to the left.
 *
 * Drives the ISOLATED capture entry (website/capture/thread-footer-cli-align.html),
 * which rebuilds the transcript's box chain with its production class strings and
 * mounts the real UserMessage, AssistantMessage and ThreadFooter inside it, then
 * exposes window.__measure(). This is the only check that exercises the real
 * CASCADE: the unit suite (src/test/cliModeThreadFooter.test.ts) pins the rule's
 * source text, but happy-dom resolves neither `:has()` nor `align-self` against
 * `align-items`, so a rule that is present and yet loses -- a specificity slip, a
 * wrapper that stops matching, a `!important` dropped -- is only caught here.
 *
 * Arms:
 *   ui=cli  fix=on  -- the footer's mark sits on the bubble's own left edge.
 *   ui=cli  fix=off -- the BEFORE state. Must reproduce: the mark lands hundreds
 *                      of px right of the bubble's left edge. A before frame
 *                      equal to the after frame is what a toggle that silently
 *                      failed to apply looks like, so reproduction is asserted.
 *   ui=normal       -- the untouched theme. The user footer must stay on the
 *                      RIGHT (its right edge tracking the bubble's) and the
 *                      assistant footer on the LEFT, so the fix is proven to be
 *                      scoped to CLI mode rather than a global re-alignment.
 *
 * On every arm the assistant footer must stay shrink-wrapped: its `self-start`
 * is what escapes its column's default `align-items: stretch`, and a footer as
 * wide as the column is the regression that dropping that class would cause.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6814 --strictPort   # in another shell
 *   node scripts/capture-thread-footer-cli-align.mjs http://127.0.0.1:6814 ../temp-screenshots/thread-footer-cli
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6814'
const OUT = process.argv[3] || '../temp-screenshots/thread-footer-cli'
mkdirSync(OUT, { recursive: true })

/** How close to the bubble's edge counts as "lined up", in px. */
const TOLERANCE = 2
/** The before state must be wrong by much more than a rounding wobble. */
const REPRO_MIN_OFFSET = 100
/** The assistant footer's before state is off by the indent it misses: the
 *  bubble root's 6px double bar plus its 10px padding, less a rounding wobble. */
const REPRO_MIN_INDENT = 12

const ARMS = [
  { ui: 'cli', fix: 'on', shot: 'after-cli' },
  { ui: 'cli', fix: 'off', shot: 'before-cli' },
  { ui: 'normal', fix: 'on', shot: 'after-normal-theme' },
]

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }

for (const arm of ARMS) {
  const page = await browser.newPage({ viewport: { width: 900, height: 460 } })
  // A render that threw leaves an empty #root, which times out below as if the
  // page were merely slow; surfacing the console says which it was.
  page.on('pageerror', e => console.error(`  pageerror: ${e.message}`))
  page.on('console', msg => { if (msg.type() === 'error') console.error(`  console: ${msg.text()}`) })
  await page.goto(`${BASE}/capture/thread-footer-cli-align.html?theme=dark&ui=${arm.ui}&fix=${arm.fix}`, { waitUntil: 'networkidle' })
  // Both footers are SIBLINGS of their bubble, not descendants -- that is the
  // whole reason the wrapper's alignment is what places them.
  await page.waitForSelector('[data-role="assistant"]')
  await page.waitForFunction(() => document.querySelectorAll('[data-testid="thread-footer"]').length === 2)
  const m = await page.evaluate(() => window.__measure())

  for (const side of ['user', 'assistant']) {
    const s = m[side]
    console.log(
      `ui=${arm.ui.padEnd(6)} fix=${arm.fix.padEnd(3)} ${side.padEnd(9)}: ` +
      `alignSelf=${s.alignSelf.padEnd(10)} mark=[${String(s.markLeft).padStart(4)},${String(s.markRight).padStart(4)}] ` +
      `bubble=[${String(s.bubbleLeft).padStart(3)},${String(s.bubbleRight).padStart(3)}] ` +
      `footerW=${String(s.footerWidth).padStart(3)}/colW=${s.columnWidth} ` +
      `→ offL=${s.offsetFromBubble} offR=${s.offsetFromBubbleRight}`,
    )
  }

  // The assistant footer is shrink-wrapped on EVERY arm, the before state
  // included: `self-start` is what escapes its column's default stretch, and no
  // arm of this harness touches that, so a wide footer here means the class was
  // lost in the component rather than by the mode.
  if (m.assistant.footerWidth >= m.assistant.columnWidth) {
    fail(`ui=${arm.ui} fix=${arm.fix}: assistant footer stretched to ${m.assistant.footerWidth}px of a ${m.assistant.columnWidth}px column — self-start lost`)
  }

  if (arm.fix === 'on') {
    // Every footer lines up with its own bubble, on the edge its row is aligned
    // to: the left one everywhere except the normal theme's user row, which is
    // right-aligned by design and is read against the bubble's right edge.
    const rightAligned = arm.ui !== 'cli' ? ['user'] : []
    for (const side of ['user', 'assistant']) {
      const onRight = rightAligned.includes(side)
      const off = onRight ? m[side].offsetFromBubbleRight : m[side].offsetFromBubble
      if (Math.abs(off) > TOLERANCE) {
        fail(`ui=${arm.ui}: ${side} footer sits ${off}px off its bubble's ${onRight ? 'right' : 'left'} edge`)
      }
    }
  }

  if (arm.ui === 'cli' && arm.fix === 'on') {
    if (m.user.alignSelf !== 'flex-start') {
      fail(`cli fix=on: user footer resolved align-self=${m.user.alignSelf} — the cli-mode rule lost the cascade`)
    }
  }

  if (arm.ui === 'cli' && arm.fix === 'off') {
    // Both halves of the before state must reproduce, or the paired screenshots
    // would be evidence of nothing. The user footer is off by hundreds of px;
    // the assistant one by exactly the bar-and-padding indent it misses, so it
    // gets its own floor rather than sharing the big one.
    if (m.user.offsetFromBubble < REPRO_MIN_OFFSET) {
      fail(`cli fix=off: user footer only ${m.user.offsetFromBubble}px off the bubble — the before state did not reproduce`)
    }
    if (m.assistant.offsetFromBubble > -REPRO_MIN_INDENT) {
      fail(`cli fix=off: assistant footer only ${m.assistant.offsetFromBubble}px off the bubble — the before state did not reproduce`)
    }
  }

  if (arm.ui === 'normal') {
    // The normal theme is untouched: the user's bubble is on the right, and its
    // footer's RIGHT edge is what tracks the bubble there.
    if (m.user.alignSelf !== 'flex-end') {
      fail(`normal theme: user footer resolved align-self=${m.user.alignSelf} — the CLI rule leaked out of its scope`)
    }
    if (m.user.markLeft <= m.user.bubbleLeft) {
      fail(`normal theme: user footer mark at ${m.user.markLeft} is not right of its bubble's left edge ${m.user.bubbleLeft} — right placement broke`)
    }
  }

  await page.screenshot({ path: `${OUT}/${arm.shot}.png` })
  await page.close()
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('ALL GREEN')
