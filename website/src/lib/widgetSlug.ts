/**
 * Stable widget-slug derivation.
 *
 * Every `<mcwidget>` impression in chat is bound to an artifact identity
 * via a slug. The agent normally emits an explicit `slug=` attribute when
 * re-rendering a saved artifact; for brand-new emissions the slug is
 * derived deterministically from the message location so that:
 *
 *   - Same message + same widget position → same slug → same binding.
 *   - Save once, refresh, click again → no duplicate created (the second
 *     POST goes to the same slug, server returns 409, frontend reconciles
 *     the bookmark icon to "filled").
 *   - Legacy widgets (rendered before this scheme existed) still get a
 *     stable identity from their message_ts so bookmark state survives
 *     refreshes.
 *
 * The hash function is FNV-1a-like — fast, deterministic, no crypto
 * properties needed (we just want unique-enough opaque IDs). Output is
 * 16 lowercase hex chars, well within the slug regex
 * (`^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$`) used by the artifact store.
 */

const HEX = '0123456789abcdef'

function hexFromUint32(n: number): string {
  // Unsigned 32-bit integer -> 8 hex chars.
  const u = n >>> 0
  let out = ''
  for (let shift = 28; shift >= 0; shift -= 4) {
    out += HEX[(u >> shift) & 0xf]
  }
  return out
}

/**
 * Compute a 16-hex-char deterministic slug from a message timestamp and
 * a widget's 0-based index within that message. Two FNV-1a passes with
 * the standard 32-bit prime but different starting offset bases give us
 * 64 bits of namespace at zero crypto cost.
 *
 * Note: we use the 32-bit FNV prime (0x01000193) for BOTH passes, not
 * the 64-bit prime (0x100000001b3). Math.imul truncates its operands to
 * 32-bit signed integers before multiplying, so the 64-bit prime would
 * silently get truncated to 0x1b3 (435 decimal) — collapsing the second
 * pass to a weak `multiply-by-435` hash with poor avalanche behavior.
 * The two different offset bases (0x811c9dc5 and 0x62b82175) are enough
 * to guarantee independence of the two passes given the same input.
 *
 * @param messageTs Slack-style timestamp (e.g. "1779995123.456789") or any
 *                  string identifier for the parent message.
 * @param widgetIndex 0-based ordinal of the widget within the message.
 */
export function deriveWidgetSlug(messageTs: string, widgetIndex: number): string {
  const seed = `${messageTs}#${widgetIndex}`
  // Two independent FNV-1a passes — 32-bit prime, different offset bases.
  let h1 = 0x811c9dc5 >>> 0
  let h2 = 0x62b82175 >>> 0
  for (let i = 0; i < seed.length; i++) {
    const c = seed.charCodeAt(i)
    h1 = Math.imul(h1 ^ c, 0x01000193) >>> 0
    h2 = Math.imul(h2 ^ c, 0x01000193) >>> 0
  }
  return hexFromUint32(h1) + hexFromUint32(h2)
}

/**
 * Pick the effective slug for a widget impression — explicit attribute
 * wins; otherwise derived from message location. Returns null only when
 * neither input is available (e.g. a streaming or detached widget that
 * has no parent message context yet).
 */
export function effectiveWidgetSlug(opts: {
  explicitSlug?: string | null
  messageTs?: string | null
  widgetIndex?: number | null
}): string | null {
  if (opts.explicitSlug) return opts.explicitSlug
  if (opts.messageTs && typeof opts.widgetIndex === 'number') {
    return deriveWidgetSlug(opts.messageTs, opts.widgetIndex)
  }
  return null
}
