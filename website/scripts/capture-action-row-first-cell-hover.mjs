/**
 * Measurement + screenshots for the message action row's first cell, whose
 * hover paint has to stay inside the `overflow-hidden` message column.
 *
 * Drives the ISOLATED capture entry (website/capture/action-row-first-cell-hover.html),
 * which rebuilds both hosts' row chain from their literal class strings and
 * mounts the real UserMessage and AssistantMessage inside it, then exposes
 * window.__measure(). This is the only check that exercises REAL layout: the
 * unit suite (touchHoverActions.test.tsx, AssistantMessage.test.tsx) pins the
 * class contract of the shared row constant, but happy-dom computes no layout,
 * so a leading cell that is pushed past the column's start edge by some other
 * route -- a new negative margin on a host wrapper, a row that gains its own
 * inset -- is only caught here.
 *
 * Assertions, per row (user and assistant), per theme:
 *  - pull=off (shipped): no pixel of the leading cell lies start-side of the
 *    column (clippedStart === 0), and the glyph is centred in a cell that
 *    starts ON the column (glyphInset === (cellWidth - glyph) / 2 === 7).
 *  - pull=on (the retired negative margin re-applied): both rows must
 *    reproduce the 7px clip. A before frame identical to the after frame is
 *    exactly what a probe that silently failed to apply would produce, so the
 *    reproduction is asserted, not assumed.
 *
 * Screenshots hover the leading cell of each row in turn: one frame per row,
 * arm and theme, cropped to the row's column.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6812 --strictPort   # in another shell
 *   node scripts/capture-action-row-first-cell-hover.mjs http://127.0.0.1:6812 ../temp-screenshots/action-row-first-cell-hover
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6812'
const OUT = process.argv[3] || '../temp-screenshots/action-row-first-cell-hover'
mkdirSync(OUT, { recursive: true })

const THEMES = ['dark', 'light']
const ROWS = ['user', 'assistant']
/** The pointer cell is 28px wide with a 14px glyph: centred, the glyph sits 7px in. */
const GLYPH_INSET = 7
const VIEWPORT_W = 720
const MIN_CROP = 360

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

for (const pull of ['on', 'off']) {
  for (const theme of THEMES) {
    const page = await browser.newPage({ viewport: { width: VIEWPORT_W, height: 240 }, deviceScaleFactor: 2 })
    await page.goto(`${BASE}/capture/action-row-first-cell-hover.html?theme=${theme}&pull=${pull}`, { waitUntil: 'networkidle' })
    await page.waitForSelector('[data-role="assistant"] .message-bubble')
    const m = await page.evaluate(() => window.__measure())
    for (const row of ROWS) {
      const r = m[row]
      console.log(
        `pull=${pull} theme=${theme.padEnd(5)} ${row.padEnd(9)}: column=${r.columnStart} cell=${r.cellStart} w=${r.cellWidth} ` +
        `ms=${r.marginInlineStart} glyphInset=${r.glyphInset} → ${r.clippedStart ? `CLIPPED ${r.clippedStart}px` : 'inside'}`,
      )
      if (pull === 'off') {
        if (r.clippedStart !== 0) {
          console.error(`FAIL: ${row} leading cell still starts ${r.clippedStart}px before its column in ${theme}`)
          failures++
        }
        if (r.glyphInset !== GLYPH_INSET) {
          console.error(`FAIL: ${row} leading glyph sits ${r.glyphInset}px in, expected ${GLYPH_INSET} (cell no longer starts on the column)`)
          failures++
        }
      }
      if (pull === 'on' && r.clippedStart === 0) {
        console.error(`FAIL: ${row} did not reproduce the pre-fix clip in ${theme} — before/after evidence would be meaningless`)
        failures++
      }
      // Hover the leading cell so its paint is in the frame, then crop to that
      // row's column plus a margin that shows where the column edge is.
      const cell = page.locator(`[data-role="${row}"] [class~="gap-x-0"] > button:first-child`)
      await cell.hover()
      await page.waitForTimeout(450) // the row's reveal transition (300ms + delay)
      const column = page.locator(`[data-role="${row}"]`).locator('xpath=ancestor::*[contains(@class,"chat-message-body")]')
      const box = await column.boundingBox()
      // At least MIN_CROP wide so a short user column still reads as a row in
      // the transcript; anchored on the column's end edge so the column, its
      // 24px of surrounding gutter and the leading cell are always in frame.
      const left = Math.max(0, Math.min(box.x - 24, box.x + box.width + 24 - MIN_CROP))
      const width = Math.min(VIEWPORT_W - left, Math.max(box.width + 48, MIN_CROP))
      await page.screenshot({
        path: `${OUT}/${pull === 'on' ? 'before' : 'after'}-${row}-${theme}.png`,
        clip: { x: left, y: Math.max(0, box.y - 4), width, height: box.height + 8 },
      })
    }
    await page.close()
  }
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('ALL GREEN')
