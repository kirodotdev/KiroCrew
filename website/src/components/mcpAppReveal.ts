/** Host-side progressive reveal of tool-call arguments.
 *
 *  SEP-1865 gives apps two input notifications: `tool-input-partial`
 *  ("Partial tool call arguments (incomplete, may change)") and `tool-input`
 *  ("Complete tool call arguments"). Apps built for streaming hosts listen on
 *  the partial one and redraw per delta — excalidraw's `ontoolinputpartial`
 *  parses a possibly-truncated element array and draws whatever parsed.
 *
 *  WHAT THIS IS, PRECISELY: the pacing here is the HOST'S, not the model's.
 *  kiro-cli 2.16.0 announces a tool call early (`tool_call_chunk`, carrying
 *  only title/kind with `args:{}`) and then delivers arguments WHOLE — it emits
 *  no argument deltas, and the binary carries no `tool_input_partial` /
 *  `input_delta` symbol. So we cannot forward a real token-paced stream; we
 *  take the complete arguments and reveal them in ascending prefixes.
 *
 *  This is not a fake of a feature the app can detect: the notification, its
 *  shape and its "may change" contract are exactly what a genuinely streaming
 *  host sends, and excalidraw's own dev harness (`dev-mock.ts`) fakes it the
 *  same way. The single difference is that the cadence is uniform rather than
 *  tracking generation speed. If a future CLI emits argument deltas, the
 *  frames simply come from the wire instead of from `planReveal`.
 *
 *  Only ARRAY-shaped arguments are revealable: a prefix of a list of diagram
 *  elements is a meaningful intermediate state, whereas a prefix of a string or
 *  a subset of unrelated scalar keys is noise. When nothing qualifies we return
 *  null and the caller posts the complete input immediately, which is exactly
 *  the pre-existing behaviour.
 */

/** Wall-clock budget for the whole reveal. Deliberately short: the reveal is
 *  gated on argument SHAPE, not on app capability, so an app that ignores
 *  `tool-input-partial` still waits this long for its complete input and result.
 *  A capability gate is not available — `ui/initialize` params do carry
 *  `appCapabilities`, but excalidraw (the reference partial-aware consumer)
 *  declares `capabilities: {}`, so gating on a declared capability would switch
 *  the animation off for the very app that implements it. Keeping the budget
 *  small is the honest trade instead. */
export const REVEAL_BUDGET_MS = 700
/** Upper bound on posted frames. A 4000-element diagram must not become 4000
 *  postMessage round-trips; past ~two dozen steps the motion reads identically. */
export const REVEAL_MAX_FRAMES = 24
/** Floor on the gap between frames, so a short list still animates visibly
 *  instead of flashing through in three frames' worth of milliseconds. */
export const REVEAL_MIN_STEP_MS = 45
/** Cap on the encoded size of the WHOLE arguments object.
 *
 *  It must be the whole object, not just the revealed array: every frame is
 *  `{...toolInput, [key]: prefix}`, so each frame structured-clones every
 *  SIBLING argument too. Measuring only the array would let a small array
 *  beside a multi-megabyte sibling ship (frames x sibling) bytes through
 *  postMessage. At this cap the worst case is ~24 x 256KB. */
export const REVEAL_MAX_SOURCE_BYTES = 256_000

/** How the revealed array was carried in the arguments object. Servers differ:
 *  excalidraw passes `elements` as a JSON *string*, others pass a real array. */
type RevealEncoding = 'array' | 'json-string'

export interface RevealPlan {
  /** The argument key being revealed progressively. */
  key: string
  /** Successive partial `arguments` objects, in order. Deliberately EXCLUDES
   *  the complete value: the final state is delivered by the real `tool-input`
   *  notification, keeping "partial ⇒ may change" and "input ⇒ complete" true. */
  frames: Record<string, unknown>[]
  /** Delay between frames, in milliseconds. */
  stepMs: number
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

/** Decode an argument value to an array if it is one, either natively or as a
 *  JSON string. Returns null for anything else. */
function decodeArray(value: unknown): { items: unknown[]; encoding: RevealEncoding } | null {
  if (Array.isArray(value)) return { items: value, encoding: 'array' }
  if (typeof value === 'string') {
    // Cheap prefilters BEFORE parsing: skip values that cannot be an array
    // literal, and skip anything already past the size cap — parsing a
    // multi-megabyte untrusted string just to discover it is too big to reveal
    // is itself the main-thread cost the cap exists to avoid.
    const trimmed = value.trim()
    if (!trimmed.startsWith('[')) return null
    if (trimmed.length > REVEAL_MAX_SOURCE_BYTES) return null
    try {
      const parsed = JSON.parse(trimmed) as unknown
      if (Array.isArray(parsed)) return { items: parsed, encoding: 'json-string' }
    } catch {
      // Not JSON — nothing to reveal. (Unlike the APP side, the host always
      // holds complete arguments, so a parse failure here is a genuine
      // non-array value rather than mid-stream truncation.)
    }
  }
  return null
}

function encodeItems(items: unknown[], encoding: RevealEncoding): unknown {
  return encoding === 'json-string' ? JSON.stringify(items) : items
}

/** Evenly spaced prefix lengths over `1 .. total-1`, at most `REVEAL_MAX_FRAMES`
 *  of them, strictly increasing and never including `total`. */
function prefixLengths(total: number): number[] {
  const steps = Math.min(REVEAL_MAX_FRAMES, total - 1)
  const out: number[] = []
  for (let i = 1; i <= steps; i++) {
    const len = Math.max(1, Math.round((i * (total - 1)) / steps))
    if (out[out.length - 1] !== len) out.push(len)
  }
  return out
}

/** Build a reveal plan for a tool call's arguments, or null when the payload has
 *  no meaningfully revealable array. */
export function planReveal(toolInput: unknown): RevealPlan | null {
  if (!isPlainObject(toolInput)) return null

  // Size-gate the WHOLE arguments object FIRST, before scanning or decoding any
  // value. Every frame carries all sibling arguments, so the per-frame cost is
  // driven by the total payload rather than by the revealed array alone.
  let encodedBytes: number
  try {
    encodedBytes = (JSON.stringify(toolInput) ?? '').length
  } catch {
    // Circular or non-serialisable arguments. structured clone would still copy
    // them, but we cannot bound the per-frame cost, so decline the reveal and
    // let the caller deliver the payload whole.
    return null
  }
  if (encodedBytes > REVEAL_MAX_SOURCE_BYTES) return null

  // Pick the LARGEST array-valued argument: for a diagram call that is the
  // element list, not an incidental two-entry options array. Iteration follows
  // key order and the comparison is strictly-greater, so the choice is stable.
  let best: { key: string; items: unknown[]; encoding: RevealEncoding } | null = null
  for (const key of Object.keys(toolInput)) {
    const decoded = decodeArray(toolInput[key])
    if (!decoded) continue
    if (!best || decoded.items.length > best.items.length) {
      best = { key, items: decoded.items, encoding: decoded.encoding }
    }
  }
  // Fewer than two items yields no intermediate state worth showing.
  if (!best || best.items.length < 2) return null

  const lengths = prefixLengths(best.items.length)
  if (lengths.length === 0) return null

  const frames = lengths.map((len) => ({
    ...toolInput,
    [best.key]: encodeItems(best.items.slice(0, len), best.encoding),
  }))

  return {
    key: best.key,
    frames,
    stepMs: Math.max(REVEAL_MIN_STEP_MS, Math.round(REVEAL_BUDGET_MS / frames.length)),
  }
}

/** Honour the OS/browser reduced-motion preference: a user who has asked for
 *  less motion gets the complete payload at once rather than a draw-on. */
export function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch {
    return false
  }
}

/** Spools already revealed in this page session.
 *
 *  A transcript frame can unmount and remount (virtualisation, navigating back
 *  to an old conversation). Replaying the draw-on every time would animate
 *  history on every scroll, so each spool animates at most once per page. */
const revealedSpools = new Set<string>()
/** Bound the set: transcripts are unbounded, this cache must not be. */
const REVEALED_CAP = 256

export function hasRevealed(spoolId: string): boolean {
  return revealedSpools.has(spoolId)
}

export function markRevealed(spoolId: string): void {
  // Evict the OLDEST entry, not the whole set: clearing wholesale would let
  // every already-seen app re-animate after the 257th render, which is exactly
  // the history-replay this cache exists to prevent. Set preserves insertion
  // order, so the first key is the oldest.
  if (revealedSpools.size >= REVEALED_CAP) {
    const oldest = revealedSpools.values().next()
    if (!oldest.done) revealedSpools.delete(oldest.value)
  }
  revealedSpools.add(spoolId)
}

/** Test seam — reveal state is module-level, so tests must be able to reset it. */
export function __resetRevealedForTests(): void {
  revealedSpools.clear()
}
