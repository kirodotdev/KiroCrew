/**
 * Wrapped logical-line boundary walks for the xterm buffer — the single source
 * of truth shared by the staged Select key (TerminalKeyBar) and touch
 * range-selection (useTerminalTouchSelection).
 *
 * A single LOGICAL line can span several PHYSICAL rows when it wraps: xterm
 * flags every physical row after the first of a wrapped logical line with
 * `isWrapped === true`. Selecting or skipping a logical line therefore means
 * walking those continuation rows. This walk was previously spelled three times
 * (two in TerminalKeyBar, one in the touch hook); the Design and First
 * Principles reviews of #8070 both flagged that a divergence in any copy would
 * silently make the two selection creators disagree about what a logical line
 * is, with no failing test. Extracting it here makes that impossible.
 *
 * Pure and dependency-light: callers pass a row→isWrapped predicate and the
 * buffer length, so this needs no xterm type and is trivially unit-testable.
 */

/** Predicate: is physical row `r` a wrapped continuation of the row above it? */
export type IsWrapped = (row: number) => boolean

/**
 * Walk UP from `row` to the FIRST physical row of its logical line: while the
 * current row is a wrapped continuation, step up. Returns that top row.
 */
export function logicalLineTop(row: number, isWrapped: IsWrapped): number {
  let top = row
  while (top > 0 && isWrapped(top)) top--
  return top
}

/**
 * Expand `row` to the bounds of its LOGICAL line and return `[top, bottom]`
 * inclusive: walk UP while the current row is a wrapped continuation, and DOWN
 * while the row BELOW is one. `length` is the buffer's physical row count, used
 * to cap the downward walk.
 */
export function logicalLineBounds(row: number, length: number, isWrapped: IsWrapped): [number, number] {
  const top = logicalLineTop(row, isWrapped)
  let bottom = row
  while (bottom + 1 < length && isWrapped(bottom + 1)) bottom++
  return [top, bottom]
}
