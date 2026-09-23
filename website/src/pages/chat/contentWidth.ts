/**
 * Column-width scaling for the message font size setting.
 *
 * Kept apart from ChatSettings on purpose: ~90 ChatPage tests replace that
 * module with a `vi.mock` factory that lists its exports by hand, so a helper
 * added there is missing from every one of those mocks and each ChatPage
 * render throws. This module is imported by its callers directly and takes the
 * width table as an argument, so a test that mocks `CONTENT_WIDTH` still sees
 * its own values flow through.
 */

/** Message font size bounds, in px. Mirrors useTerminalFont's
 *  MIN/MAX_TERMINAL_FONT_SIZE bracket — below 12 body text is unreadable,
 *  above 22 a bubble wastes more width wrapping than it gains in legibility. */
export const MIN_MESSAGE_FONT_SIZE = 12
export const MAX_MESSAGE_FONT_SIZE = 22
export const DEFAULT_MESSAGE_FONT_SIZE = 14

export interface ColumnWidth { messages: string; input: string }

/**
 * The column widths to apply, with Compact scaled by the message font size so
 * it holds roughly the same number of characters per line at every size: 800px
 * is a reading width at 14px, and the same 800px at 20px is a narrow one.
 * Comfortable and Full are viewport percentages and already absorb larger text,
 * so any non-compact choice passes through unchanged.
 *
 * Resolved here in JS rather than as a CSS `calc()` in `--mc-content-width`
 * because that var is parsed as a number by consumers (TurnNavigationMinimap
 * reads it with `parseFloat` to place its column overlay), and a `calc()` would
 * make them all fall back to their hardcoded default. Rounded to whole px so the
 * column never lands on a fractional boundary.
 *
 * A size that is not a positive number leaves the base untouched: a config
 * object assembled without the field (a test double, a caller that predates the
 * setting) must never emit `NaNpx`, which CSS drops and consumers misparse.
 */
export function scaleContentWidth(base: ColumnWidth, contentWidth: string, messageFontSize: number | undefined): ColumnWidth {
  if (contentWidth !== 'compact') return base
  const scale = (messageFontSize ?? NaN) / DEFAULT_MESSAGE_FONT_SIZE
  if (!(scale > 0) || scale === 1) return base
  return {
    messages: `${Math.round(parseFloat(base.messages) * scale)}px`,
    input: `${Math.round(parseFloat(base.input) * scale)}px`,
  }
}
