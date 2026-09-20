import { getCurrentSearchRange } from './domHighlight'

/** Threshold (ms) below which consecutive search-nav steps are treated as
 * "rapid stepping" and snap instantly instead of smooth-scrolling. */
export const RAPID_STEP_MS = 250

/** Default wall-clock backstop (ms) for the converge polls below. A far row
 * that is off-window must mount + measure, and a widget target needs ~450ms
 * for its iframe build alone (PROGRAMMATIC_BUILD_DELAY_MS in WidgetFrame), so
 * a short frame-count ceiling gave up before the target ever settled — the
 * jump silently no-op'd and only worked on a second click once cached. ~2s is
 * comfortably past mount + measure + widget build while still guaranteeing a
 * genuinely unreachable target terminates instead of spinning forever. */
export const CONVERGE_MAX_MS = 2000

/** Minimum time (ms) a measurement must stay UNCHANGED before the converge
 * polls accept it as settled. A frame-count streak alone is not sufficient: a
 * widget row reports a static height until its iframe build lands, so a couple
 * of equal frames (~32ms) would end the poll before the growth even started,
 * letting the late resize push the target back off-centre.
 *
 * Calibrated above `MAX_WIDGET_BUILD_WAIT_MS` in WidgetFrame — the worst case
 * for a jump, which is the base build delay PLUS the capped per-widget stagger
 * (clearing only the base delay would let a widget in a later stagger slot
 * settle early). `searchScroll.coupling.test.ts` asserts the
 * relationship, so raising either constant fails CI rather than silently
 * regressing the first-click jump. Still comfortably inside CONVERGE_MAX_MS.
 *
 * Note this delays only the "settled" DECLARATION, not the visible movement:
 * the first step scrolls immediately and later steps merely re-correct, so a
 * longer quiet window costs a few extra rAFs, not perceived latency. */
export const MIN_QUIET_MS = 900

/**
 * Decide how the chat should scroll when the search match changes.
 *
 * While the user steps rapidly (next/prev faster than `thresholdMs`), snap
 * instantly (`'auto'`) — a smooth animation would be interrupted and restarted
 * on every keypress, producing stutter. A lone step (or the final one after a
 * pause) glides smoothly so the landing feels settled.
 */
export function pickSearchScrollBehavior(
  now: number,
  lastStepAt: number,
  thresholdMs: number = RAPID_STEP_MS,
): ScrollBehavior {
  return now - lastStepAt < thresholdMs ? 'auto' : 'smooth'
}

const defaultNow = (): number =>
  typeof performance !== 'undefined' && typeof performance.now === 'function'
    ? performance.now()
    : Date.now()

const defaultRaf: (cb: () => void) => number =
  typeof requestAnimationFrame === 'function'
    ? (cb) => requestAnimationFrame(cb)
    : (cb) => setTimeout(cb, 16) as unknown as number

/**
 * Injectable dependencies for the generic converge poll.
 *
 * The poll is CONDITION-based, not frame-count based. Each frame it reads a
 * monitored quantity via `measure()` (e.g. the target row's height, or the
 * active mark's viewport position):
 *   - `null`  → the target is not mounted yet. The poll waits (runs no `step`)
 *               but the wall-clock backstop keeps ticking, so an absent /
 *               never-mounting target still terminates.
 *   - number  → the target IS present. `step()` runs (e.g. scroll it into
 *               view, re-reading the live offset), and the value is compared
 *               to the previous frame. When it stops moving for `settleFrames`
 *               consecutive frames the row is measured (non-estimated) and the
 *               loop stops. A widget that keeps growing during its build resets
 *               the streak every frame, so the poll naturally re-reads the
 *               offset after the widget-build delay and only settles once done.
 */
export interface SettlePollDeps {
  /** Monitored quantity this frame, or `null` when the target is absent. */
  measure: () => number | null
  /** Side effect to run each frame the target is present (e.g. scroll). */
  step: () => void
  /** Schedule the next frame. */
  raf?: (cb: () => void) => number
  /** Monotonic clock in ms. */
  now?: () => number
  /** Wall-clock backstop in ms. Default `CONVERGE_MAX_MS`. */
  maxMs?: number
  /** Consecutive equal measurements that count as "settled". Default 2. */
  settleFrames?: number
  /**
   * Minimum time in ms the measurement must stay quiet before "settled" is
   * accepted. Default `MIN_QUIET_MS`. A frame-count streak alone is not enough:
   * a widget row reports a static height until its build delay elapses
   * (`PROGRAMMATIC_BUILD_DELAY_MS`, ~450ms), so two equal frames (~32ms) would
   * declare victory, stop polling, and let the later growth push the target
   * back off-centre — reproducing the miss this poll exists to prevent.
   */
  minQuietMs?: number
  /** Invoked exactly once when the loop terminates. */
  onEnd?: (reason: 'settled' | 'timeout' | 'cancelled') => void
}

/**
 * The single in-flight programmatic scroll convergence, if any.
 *
 * Convergence polls are mutually exclusive by nature: they all drive the same
 * scroller, so two running at once fight each other frame by frame. They are
 * also NESTED in practice — navigating to a search result starts a ROW-level
 * poll (centre display index N), and once that row mounts the message component
 * starts a finer MARK-level poll (centre the exact occurrence inside it). The
 * row poll re-scrolls every frame for the whole quiet window, so without a
 * handoff it repeatedly undid the mark centring and left the viewport on the
 * containing turn instead of the match.
 */
let activeScrollOwner: (() => void) | null = null

/**
 * Install `supersede` as the active convergence owner, notifying the previous
 * owner that it has been taken over. Returns a release function that clears
 * ownership only if this owner is still the active one (so a stale poll's
 * teardown can never revoke a newer poll's claim).
 *
 * Exported for the one other scroll driver, the converging glide
 * (utils/convergingGlide), whose eased travel writes `scrollTop` every frame
 * without going through a poll and so must hold the claim itself.
 */
export function claimScrollOwnership(supersede: () => void): () => void {
  const prev = activeScrollOwner
  // Install FIRST: `prev()` may synchronously run its own teardown, whose
  // release must compare against `prev` and therefore no-op.
  activeScrollOwner = supersede
  if (prev && prev !== supersede) prev()
  return () => {
    if (activeScrollOwner === supersede) activeScrollOwner = null
  }
}

/**
 * Frame-driven poll that runs `step()` until the monitored `measure()` value
 * has settled (measured, not estimated), with a wall-clock backstop so a
 * never-present target can't spin. Returns a `cancel()` — idempotent and safe
 * to call after natural termination. Extracted as a pure function (clock, rAF,
 * and DOM access all injected) so the convergence + termination guarantees are
 * unit-testable without a live virtualizer.
 *
 * Starting a poll CLAIMS scroll ownership (see `activeScrollOwner`), so a nested
 * finer-grained poll retires the coarser one that led to it. The retirement is
 * deferred until the superseded poll has completed at least one step, because
 * that first step is what MOUNTS the nested target: cancelling before it would
 * mean the finer poll's element never appears and neither scroll happens.
 */
export function pollRowSettled(deps: SettlePollDeps): () => void {
  const {
    measure,
    step,
    raf = defaultRaf,
    now = defaultNow,
    maxMs = CONVERGE_MAX_MS,
    settleFrames = 2,
    minQuietMs = MIN_QUIET_MS,
  } = deps
  const start = now()
  let last: number | null = null
  let stable = 0
  // When the measurement last CHANGED — the quiet window is timed from here.
  let lastChangeAt = start
  let done = false
  // Whether a step has actually run. A superseding owner waits for this,
  // because the first step is what mounts the nested target.
  let hasStepped = false
  let superseded = false
  let releaseOwnership: () => void = () => {}
  const finish = (reason: 'settled' | 'timeout' | 'cancelled') => {
    if (done) return
    done = true
    releaseOwnership()
    deps.onEnd?.(reason)
  }
  releaseOwnership = claimScrollOwnership(() => {
    if (hasStepped) finish('cancelled')
    else superseded = true
  })
  const tick = () => {
    if (done) return
    const v = measure()
    if (v != null) {
      step()
      hasStepped = true
      // A finer-grained poll is waiting to take over; the target it needs is
      // now mounted and scrolled into place, so stand down.
      if (superseded) return finish('cancelled')
      if (last != null && Math.abs(v - last) < 1) {
        // Require BOTH a frame streak and a real quiet duration, so a row whose
        // growth starts after the streak still converges.
        if (++stable >= settleFrames && now() - lastChangeAt >= minQuietMs) {
          return finish('settled')
        }
      } else {
        stable = 0
        lastChangeAt = now()
      }
      last = v
    }
    if (now() - start >= maxMs) return finish('timeout')
    raf(tick)
  }
  raf(tick)
  return () => finish('cancelled')
}

/**
 * Wraps a scroll callback so only the FIRST invocation may use an animated
 * behavior and every later one snaps instantly.
 *
 * Convergence polls call `step()` once per frame. Re-issuing a smooth scroll
 * cancels the in-flight animation and restarts it from the current position, so
 * a repeatedly-stepped smooth scroll stutters or stalls instead of gliding —
 * the same restart trap the streaming follow pin avoids. The
 * later corrections are sub-pixel-to-few-pixel adjustments as the target row
 * measures in, so making them instant is visually invisible while keeping the
 * initial user-visible movement smooth.
 */
export function glideOnceStep(
  scroll: (behavior: ScrollBehavior) => void,
  firstBehavior: ScrollBehavior,
): () => void {
  let stepped = false
  return () => {
    scroll(stepped ? 'auto' : firstBehavior)
    stepped = true
  }
}

/**
 * Center the active search occurrence (the Range `domHighlight` paints as the
 * current match) in the viewport,
 * re-applying across frames so it CONVERGES as the target settles. A far jump
 * mounts an unmeasured virtualized row, a match inside a collapsed turn
 * triggers a ~300ms expand animation, and a match near a widget shifts as the
 * widget builds (~450ms) — all keep moving layout after an initial scroll, so a
 * single (or short frame-capped) attempt lands on a stale offset (often
 * top-of-list). This re-centers until the match's viewport position stops moving
 * (row measured, expansion + build finished), then stops — landing on the
 * correct spot on the FIRST click. A ~2s wall-clock backstop guarantees a
 * genuinely unreachable match still terminates rather than spinning.
 *
 * The loop bails immediately on any user scroll intent — wheel, touch,
 * scrollbar drag, or a scrolling keypress (see attachUserScrollIntent) — so it
 * never fights them. Returns a `cancel()` so callers (e.g. a useEffect that
 * re-runs
 * on every active-occurrence change) can abort the previous loop before
 * starting a new one and on unmount — otherwise rapid navigation accumulates
 * concurrent loops, each with its own window listeners, some against detached
 * DOM.
 */
/**
 * Keys that scroll the page. A `keydown` abort must filter on these: the search
 * box is focused while matches are navigated, so aborting on *any* keystroke
 * would cancel convergence on every character the user types.
 */
const SCROLLING_KEYS = new Set([
  'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
  'PageUp', 'PageDown', 'Home', 'End', ' ', 'Spacebar',
])

// Direction of one scrolling key, for callers that need the input's own
// direction. Comparisons against `KeyboardEvent.key` protocol values, never
// rendered; horizontal arrows scroll neither way, and Space pages down.
function scrollKeyDirection(key: string): 'up' | 'down' | undefined {
  if (key === 'ArrowUp' || key === 'PageUp' || key === 'Home') return 'up'
  if (key === 'ArrowDown' || key === 'PageDown' || key === 'End') return 'down'
  if (key === ' ' || key === 'Spacebar') return 'down'
  return undefined
}

/**
 * Attach a one-shot "the user is trying to scroll" listener set and return a
 * detach function.
 *
 * `wheel` + `touchmove` alone miss three real input paths that all move the
 * scroller: dragging the scrollbar (pointer events, no wheel), keyboard
 * scrolling (arrows / PageUp / Home / space), and assistive technology driving
 * either of those. Missing them meant a user who dragged the scrollbar during
 * convergence was silently recentered for up to CONVERGE_MAX_MS.
 *
 * Shared so every scroll-abort site (this module's convergence poll and
 * ChatPage's navigation settle) reacts to the same input set, rather than
 * duplicating the logic per site where the gap can reappear in two places at
 * once.
 */
export function attachUserScrollIntent(
  target: EventTarget | undefined,
  onUser: (dir?: 'up' | 'down') => void,
): () => void {
  if (!target) return () => {}
  const onKey = (e: Event) => {
    const key = (e as KeyboardEvent).key
    // A bare modifier press is not scroll intent; an unknown key is not either.
    if (typeof key !== 'string' || !SCROLLING_KEYS.has(key)) return
    onUser(scrollKeyDirection(key))
  }
  const onWheel = (e: Event) => {
    // The wheel delta is the input's own direction, available BEFORE any scroll
    // event: negative deltaY scrolls up. A zero/absent delta stays directionless.
    const dy = (e as WheelEvent).deltaY
    onUser(dy < 0 ? 'up' : dy > 0 ? 'down' : undefined)
  }
  // Track the previous touch Y within ONE gesture: a finger moving DOWN the
  // screen scrolls the content UP.
  //
  // The baseline is taken at `touchstart` and dropped at `touchend`, because a
  // baseline that OUTLIVES its gesture reports the opposite direction. Carried
  // across, the first move of every later gesture is compared against wherever
  // the previous finger was lifted: lift at y=500, touch down at y=200 and drag
  // to 260, and a scroll into older history reports 'down'. On a phone that is
  // the one event that matters -- it is the input the clamp-release gate looks
  // for at the start of a scroll-up -- and the gesture only self-corrects on its
  // SECOND move. Anchoring on `touchstart` also gives the first move of the
  // first gesture a direction, which a NaN seed could not.
  let lastTouchY = Number.NaN
  // Baseline only: `pointerdown` already reports the directionless "a finger
  // landed" input for this same touch, so stamping again here would say nothing
  // new. Gated on the touch COUNT so a landing finger never moves the baseline
  // mid-gesture: a second finger landing would otherwise rebase finger 0's path
  // onto finger 1's starting point and invert one move. A finger LIFTING is the
  // mirror case and is handled in `onTouchEnd`, which rebases onto the touch
  // still on the glass rather than dropping the baseline or keeping the lifted
  // finger's.
  const onTouchStart = (e: Event) => {
    const touches = (e as TouchEvent).touches
    if (touches && touches.length !== 1) return
    const y = touches?.[0]?.clientY
    lastTouchY = typeof y === 'number' ? y : Number.NaN
  }
  const onTouchEnd = (e: Event) => {
    const touches = (e as TouchEvent).touches
    // A finger leaving a multi-touch gesture does not end the gesture, but it
    // can change WHICH finger `touches[0]` names: lift the first of two and the
    // remaining one takes that slot, so holding the lifted finger's Y would
    // measure the survivor's next move against a finger no longer on the glass
    // and invert it. Rebase onto whoever holds the slot the move handler reads;
    // only an empty list drops the baseline.
    if (touches && touches.length > 0) {
      const y = touches[0]?.clientY
      lastTouchY = typeof y === 'number' ? y : Number.NaN
      return
    }
    lastTouchY = Number.NaN
  }
  const onTouch = (e: Event) => {
    const y = (e as TouchEvent).touches?.[0]?.clientY
    if (typeof y !== 'number') {
      onUser()
      return
    }
    const prev = lastTouchY
    lastTouchY = y
    onUser(Number.isNaN(prev) || y === prev ? undefined : y > prev ? 'up' : 'down')
  }
  // A scrollbar grab carries no direction until it actually scrolls.
  const onPointer = () => onUser()
  const passive = { passive: true } as const
  target.addEventListener('wheel', onWheel, passive)
  target.addEventListener('touchstart', onTouchStart, passive)
  target.addEventListener('touchmove', onTouch, passive)
  target.addEventListener('touchend', onTouchEnd, passive)
  target.addEventListener('touchcancel', onTouchEnd, passive)
  // pointerdown fires when the scrollbar thumb is grabbed, before any scroll
  // event arrives, so the abort lands ahead of the first drag movement.
  target.addEventListener('pointerdown', onPointer, passive)
  target.addEventListener('keydown', onKey, passive)
  return () => {
    target.removeEventListener('wheel', onWheel)
    target.removeEventListener('touchstart', onTouchStart)
    target.removeEventListener('touchmove', onTouch)
    target.removeEventListener('touchend', onTouchEnd)
    target.removeEventListener('touchcancel', onTouchEnd)
    target.removeEventListener('pointerdown', onPointer)
    target.removeEventListener('keydown', onKey)
  }
}

export function scrollCurrentMatchIntoView(
  root?: Element | null,
  opts: { maxMs?: number; now?: () => number; raf?: (cb: () => void) => number } = {},
): () => void {
  const { maxMs = CONVERGE_MAX_MS, now = defaultNow, raf = defaultRaf } = opts
  const target: EventTarget | undefined =
    typeof window !== 'undefined' ? window : undefined
  let match: Range | null = null
  // Hoisted so `cleanup` (referenced by pollRowSettled's onEnd) can name it.
  function onUser() { stop() }
  let detachUser: () => void = () => {}
  const cleanup = () => { detachUser() }
  const stop = pollRowSettled({
    // Reading the match's position IS the convergence signal; scrolling is the
    // step. The match is a Range painted by domHighlight, not an element: the
    // transcript never wraps matched text in a node of its own (see
    // `applySearchHighlights`), so there is no `<mark>` to query. While the
    // range is absent (off-window row not mounted, or its text node replaced
    // by a re-render and not yet re-walked), measure returns null and the poll
    // idles until the backstop.
    measure: () => {
      const r = getCurrentSearchRange()
      match = r && (!root || root.contains(r.startContainer)) ? r : null
      if (!match) return null
      return typeof match.getBoundingClientRect === 'function'
        ? match.getBoundingClientRect().top
        : 0
    },
    step: () => { if (match) scrollRangeToCenter(match) },
    raf,
    now,
    maxMs,
    onEnd: cleanup,
  })
  detachUser = attachUserScrollIntent(target, onUser)
  return () => { stop(); cleanup() }
}

/**
 * Scroll so `range` sits at the vertical centre of every scrollable ancestor
 * and of the viewport — what `Element.scrollIntoView({ block: 'center' })` does
 * for an element, which `Range` does not offer. Each scroller is adjusted by
 * the range's offset from that scroller's own centre, and the adjustment the
 * scroller actually accepted (a clamped `scrollTop` moves less than asked) is
 * carried outward so the enclosing scrollers centre the range's final position.
 *
 * Scrollers are detected by computed `overflow-y` plus real overflow, so a
 * paragraph taller than the scroller still centres on the match itself rather
 * than on the paragraph.
 */
export function scrollRangeToCenter(range: Range): void {
  if (typeof getComputedStyle !== 'function') return
  const rect = range.getBoundingClientRect()
  let mid = rect.top + rect.height / 2
  const start = range.startContainer
  let el: HTMLElement | null =
    start.nodeType === Node.ELEMENT_NODE ? (start as HTMLElement) : start.parentElement
  for (; el; el = el.parentElement) {
    if (el.scrollHeight <= el.clientHeight) continue
    const overflowY = getComputedStyle(el).overflowY
    if (overflowY !== 'auto' && overflowY !== 'scroll') continue
    const box = el.getBoundingClientRect()
    const before = el.scrollTop
    el.scrollTop = before + (mid - (box.top + box.height / 2))
    mid -= el.scrollTop - before
  }
  if (typeof window !== 'undefined' && typeof window.scrollBy === 'function') {
    const delta = mid - window.innerHeight / 2
    if (Math.abs(delta) >= 1) window.scrollBy(0, delta)
  }
}
