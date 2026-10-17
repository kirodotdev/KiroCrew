/**
 * The longest delay `setTimeout` honours, in ms (2^31 - 1).
 *
 * Browsers convert the delay to a signed 32-bit integer, so a larger value
 * wraps: 2^31 itself becomes 0 and fires at once, and anything past it fires at
 * some arbitrary shorter delay. Clamp to this before arming a timer whose delay
 * is derived from data.
 */
export const MAX_TIMER_DELAY_MS = 2_147_483_647
