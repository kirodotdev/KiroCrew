/**
 * Stable body-keyed widget-slug derivation.
 *
 * Every `<mcwidget>` impression in chat is bound to an artifact identity via a
 * slug. The agent normally emits an explicit `slug=` attribute when rendering a
 * saved artifact; otherwise the slug is derived deterministically from the
 * message timestamp and body so that:
 *
 *   - A slug hit implies that the stored body equals the rendered body.
 *   - Save once, refresh, click again → no duplicate created (the second
 *     POST goes to the same slug, server returns 409, frontend reconciles
 *     the bookmark icon to "filled").
 *   - Identical bodies in one message deliberately share a slug because they
 *     represent the same artifact content.
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

function fnvPair(seed: string): [number, number] {
  // Two independent FNV-1a passes — 32-bit prime, different offset bases.
  let h1 = 0x811c9dc5 >>> 0
  let h2 = 0x62b82175 >>> 0
  for (let i = 0; i < seed.length; i++) {
    const c = seed.charCodeAt(i)
    h1 = Math.imul(h1 ^ c, 0x01000193) >>> 0
    h2 = Math.imul(h2 ^ c, 0x01000193) >>> 0
  }
  return [h1, h2]
}

/** Derive a widget artifact slug from its parent message and exact body. */
export function deriveWidgetBodySlug(messageTs: string, body: string): string {
  const [h1, h2] = fnvPair(`${messageTs}#w:${body}`)
  return hexFromUint32(h1) + hexFromUint32(h2)
}

/**
 * Pick the effective slug for a widget impression — explicit attribute wins;
 * otherwise derive from message timestamp and body. A derived-slug hit implies
 * content equality. Identical bodies in one message deliberately share a slug.
 * Returns null when either derivation input is unavailable.
 */
export function effectiveWidgetSlug(opts: {
  explicitSlug?: string | null
  messageTs?: string | null
  body?: string | null
}): string | null {
  if (opts.explicitSlug) return opts.explicitSlug
  if (opts.messageTs && typeof opts.body === 'string') {
    return deriveWidgetBodySlug(opts.messageTs, opts.body)
  }
  return null
}
