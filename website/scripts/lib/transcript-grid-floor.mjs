/**
 * The 4px-grid floor that the transcript row harness enforces in CI.
 *
 * `scripts/capture-transcript-row-style.mjs` measures every rendered transcript
 * row off the live DOM. This module decides which of those measurements FAIL. It
 * is a pure function of the probe result, so the decision is unit-tested without a
 * browser (`src/test/transcriptGridFloor.test.ts`), the same split as the i18n
 * render gate's `render-verdict.mjs`.
 *
 * ## What fails
 *
 *  - ZERO matched rows. When the row selector matches nothing, every per-row check
 *    below has nothing to iterate and passes vacuously, so an empty frame is a
 *    failure in its own right. The message names the selector, the candidate count
 *    and the gutter class, which is enough to find the drift that emptied it.
 *  - A row whose box does not start on the column's text edge.
 *  - A row whose card padding is off the 4px grid on any side.
 *  - Any page error while the frame rendered.
 *
 * ## What is reported and never fails
 *
 * Row-to-row pitch and every internal edge offset are printed by the harness but
 * are not part of the floor. Both are off the grid on main for known reasons that
 * need their own visual pass: the 22px chip line box sets the height of the card
 * headers that carry a chip, flex-centred 12-15px glyph boxes have off-grid right
 * and bottom edges by construction, and Markdown prose sets its row height from its
 * line count. Both also follow the runner's font metrics wherever text wraps or
 * sizes a box. Card padding is a computed CSS value, so it reads the same on every
 * machine, and it is the knob a row restyle actually turns.
 */

/** The host row wrappers: every transcript row's wrapper carries the column width. */
export const ROW_SELECTOR = '[style*="--mc-content-width"]'

/** The four padding fields the probe records for each row's painted card. */
export const PAD_SIDES = ['padTop', 'padRight', 'padBottom', 'padLeft']

/** Off-grid amount for a value against the 4px grid, unrounded. */
export const offGrid = v => {
  const m = Math.abs(v) % 4
  return Math.min(m, 4 - m)
}

export const isGrid = v => offGrid(v) < 1e-6

/** A measurement as printed: integers bare, fractions to three places. */
export const fmt = v => (Number.isInteger(v) ? String(v) : v.toFixed(3).replace(/0+$/, ''))

/** The name a report uses for a row: its test id, else its tag and position. */
export const rowName = r => r.testid || `${r.tag}.${r.index}`

/**
 * The floor's failures for one rendered frame.
 *
 * @param {string} frame label for the frame (the theme), prefixed to every message
 * @param {{rows: object[], gutterClass: string, candidateCount: number}} probe
 *   the harness's DOM probe for the frame
 * @param {string[]} [errors] page errors raised while the frame rendered
 * @returns {string[]} one message per failure; empty when the frame holds the floor
 */
export function judgeFrame(frame, probe, errors = []) {
  const failures = []
  if (probe.rows.length === 0) {
    failures.push(
      `${frame}: ZERO transcript rows matched. Selector ${ROW_SELECTOR} found `
        + `${probe.candidateCount} candidate(s) and gutter class '${probe.gutterClass}' kept none, `
        + 'so every per-row check would pass vacuously',
    )
  }
  for (const r of probe.rows) {
    if (Math.abs(r.dx) > 1e-6) {
      failures.push(`${frame}: row ${rowName(r)} box starts at ${fmt(r.dx)}px, not on the column text edge`)
    }
    const off = PAD_SIDES.filter(side => !isGrid(r[side]))
    if (off.length) {
      failures.push(
        `${frame}: row ${rowName(r)} card padding is off the 4px grid: `
          + off.map(side => `${side}=${fmt(r[side])} (off by ${fmt(offGrid(r[side]))})`).join(', '),
      )
    }
  }
  if (errors.length) {
    failures.push(`${frame}: ${errors.length} page error(s)\n  ${errors.join('\n  ')}`)
  }
  return failures
}
