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
 *  null and the caller posts the complete input immediately.
 */

/** Gap between frames, per element.
 *
 *  excalidraw's own reference harness (`dev-mock.ts`
 *  `streamElements(elements, intervalMs = 120)`) posts one element per 120ms
 *  with no total budget, and its tool description promises "elements stream in
 *  one by one with draw-on animations". We deliberately run SLOWER than that
 *  reference: at 120ms a 10-element diagram was over in 1.0s, which reads as a
 *  flash rather than as drawing. 200ms keeps each element individually legible.
 *
 *  An earlier revision used a fixed ~700ms TOTAL budget instead of a per-element
 *  step. That squeezed a whole diagram into under a second and batched several
 *  elements per frame — the animation was present but not perceptible. Pace per
 *  element, not per payload.
 *
 *  PROVENANCE (this is a sibling checkout, NOT a dependency — nothing in CI can
 *  notice if these upstream numbers change, so they are pinned here by hand):
 *  github.com/excalidraw/excalidraw-mcp @ 157aa23 — `src/dev-mock.ts:105`
 *  (intervalMs = 120), `src/server.ts:12` (MAX_INPUT_BYTES = 5MB),
 *  `src/mcp-app.tsx:32` (excludeIncompleteLastItem).
 *
 *  ONE DELIBERATE DEVIATION from that reference: the harness increments before
 *  posting, so it emits prefixes 1..n — the complete array included, as a
 *  "partial". We never do: a partial equal to the whole payload would tell an
 *  app that only listens to partials that it had final state. Combined with the
 *  app dropping each frame's last element, the visible consequence is that our
 *  final partial shows total-2 elements and the complete `tool-input` then
 *  jumps to total — the tail lands two elements at once where the reference
 *  lands one. That is the one place this cadence provably differs.
 *
 *  WHY THE REVEAL IS GATED ON ARGUMENT SHAPE AND NOT APP CAPABILITY (kept here
 *  so the gap is not re-litigated): `ui/initialize` params DO carry
 *  `appCapabilities`, so a real capability gate looks available. It is not —
 *  excalidraw, the reference partial-aware consumer, declares `capabilities: {}`
 *  while implementing `ontoolinputpartial`, so gating on a declared capability
 *  would switch the animation off for the very app that implements it. The cost
 *  of shape-gating is that an app which ignores partials still waits out the
 *  reveal for its complete input; REVEAL_MAX_TOTAL_MS bounds that wait. */
export const REVEAL_STEP_MS = 200
/** Ceiling on the whole reveal, so a 400-element diagram does not hold the
 *  app's complete input for the best part of a minute. Past this, frames carry
 *  more than one element each (see prefixLengths).
 *
 *  Sized so that BATCHING is the exception, not the rule: at 200ms this allows
 *  80 frames, so every diagram up to ~80 elements reveals exactly one element
 *  per step. Batching is what actually destroys the sense of progress — two or
 *  five elements appearing together reads as a jump, not as drawing — so the
 *  ceiling has to be high enough that realistic diagrams never reach it. */
export const REVEAL_MAX_TOTAL_MS = 16_000
/** Frame ceiling implied by the interval and the total cap. */
export const REVEAL_MAX_FRAMES = Math.floor(REVEAL_MAX_TOTAL_MS / REVEAL_STEP_MS)
/** Cap on the encoded size of the WHOLE arguments object.
 *
 *  It must be the whole object, not just the revealed array: every frame is
 *  `{...toolInput, [key]: prefix}`, so each frame structured-clones every
 *  SIBLING argument too. Measuring only the array would let a small array
 *  beside a multi-megabyte sibling ship (frames x sibling) bytes through
 *  postMessage.
 *
 *  1MB, not the 256KB a previous revision used: the excalidraw server itself
 *  accepts up to 5MB of elements (`MAX_INPUT_BYTES`), so a much tighter host
 *  cap meant large-but-legal diagrams silently got no animation at all. Total
 *  cloned bytes stay bounded by REVEAL_MAX_CLONE_BYTES below rather than by
 *  making the accepted payload small. */
export const REVEAL_MAX_SOURCE_BYTES = 1_000_000
/** Budget for total bytes handed to postMessage across the whole reveal.
 *
 *  Frame count and payload size multiply, so bounding either alone is not
 *  enough. This trades frames against size: a typical 40KB diagram gets the
 *  full per-element cadence, while a 1MB one animates in fewer, larger steps
 *  instead of cloning 60MB. */
export const REVEAL_MAX_CLONE_BYTES = 24_000_000

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

/** Prefix lengths to reveal, strictly increasing and never including `total`.
 *
 *  Starts at TWO, not one, because the app drops the last element of every
 *  partial frame on purpose (`excludeIncompleteLastItem` in excalidraw's
 *  DiagramView — during real streaming that element is truncated mid-JSON). A
 *  1-element frame therefore renders nothing, so it would waste the first step.
 *
 *  `maxFrames` collapses steps when the payload is large: one element per frame
 *  is the ideal, but frames x payload bytes has to stay bounded. */
function prefixLengths(total: number, maxFrames: number): number[] {
  const last = total - 1
  if (last < 2) return []
  // Available lengths are 2..last inclusive; `span` is the largest increment
  // above the starting length, so the final frame lands exactly on `last` and
  // never on `total`.
  const span = last - 2
  const steps = Math.max(1, Math.min(maxFrames, last - 1))
  const out: number[] = []
  for (let i = 0; i < steps; i++) {
    const len = steps === 1 ? last : 2 + Math.round((i * span) / (steps - 1))
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
  // Fewer than three items yields no useful intermediate state: the app drops
  // the last element of each partial, so only a 3+ element array can show a
  // frame with something in it that is also short of the whole diagram.
  if (!best || best.items.length < 3) return null

  // Trade frames against payload size so total cloned bytes stay bounded.
  const byteCappedFrames = Math.max(1, Math.floor(REVEAL_MAX_CLONE_BYTES / Math.max(1, encodedBytes)))
  const lengths = prefixLengths(best.items.length, Math.min(REVEAL_MAX_FRAMES, byteCappedFrames))
  if (lengths.length === 0) return null

  const frames = lengths.map((len) => ({
    ...toolInput,
    [best.key]: encodeItems(best.items.slice(0, len), best.encoding),
  }))

  return {
    key: best.key,
    frames,
    stepMs: REVEAL_STEP_MS,
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
