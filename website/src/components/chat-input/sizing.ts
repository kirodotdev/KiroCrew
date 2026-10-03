import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { markComposerResize } from '../../utils/composerResize'
import { safeSetItem } from '../../utils/safeStorage'
import { usePointerDrag } from '../../hooks/usePointerDrag'
import { useIsTouchDevice } from '../../hooks/useIsTouchDevice'
import { useMeasuredHeight } from '../../hooks/useMeasuredHeight'

/* The composer's height: the content-driven autosize (measured on an
   off-screen twin), the persisted drag-to-resize preference, and the strips
   stacked above the textarea that the drag floor and the transient
   adjustment both count. */
const INPUT_MIN_H = 44
const INPUT_DEFAULT_MAX_H = 140
const INPUT_PREFILL_MAX_H = 320
export const INPUT_DRAG_MIN_H = 93
const INPUT_DRAG_MAX_RATIO = 0.5
const INPUT_HEIGHT_LS_KEY = 'mc-input-height'

/** Usable viewport height. Native window zoom already reports zoomed CSS
 *  pixels through innerHeight, so no compensation var is needed. */
function effectiveVh(): number {
  return window.innerHeight
}

/** Off-screen twin used to measure the composer's content height.
 *
 *  Measuring must NOT touch the live textarea's box. The live element is a flex
 *  item, so setting its height (even for one synchronous read) changes what the
 *  transcript scroller above it is allotted — the scroller reclaims the height
 *  one-for-one, measured on the real dashboard: composer 44 -> 140px moved the
 *  scroller's clientHeight 561 -> 465px. A momentarily TALLER scroller has a
 *  smaller maximum scrollTop, so the engine clamps any reader parked closer to
 *  the bottom than the textarea is tall, and the reader lands at the end with no
 *  application write anywhere. `overflow:hidden` does not prevent this: overflow
 *  governs scrollbars, not a flex item's contribution to its parent.
 *
 *  Engine asymmetry is why this reads as an iOS-only defect: Blink defers scroll
 *  offset clamping to the rendering lifecycle, so a transient that is undone
 *  inside the same task never clamps, while WebKit clamps during layout. A
 *  Chromium reproduction of the keystroke case therefore shows nothing at all. */
/** Far enough off-screen that no scrollable ancestor can reach the twin. */
const TWIN_OFFSCREEN_PX = '-99999px'

let measureTwin: HTMLTextAreaElement | null = null

/** Content height of `el`'s value, measured without mutating `el`. */
function measuredContentHeight(el: HTMLTextAreaElement): number {
  if (typeof document === 'undefined') return INPUT_MIN_H
  if (!measureTwin) {
    measureTwin = document.createElement('textarea')
    measureTwin.setAttribute('aria-hidden', 'true')
    measureTwin.tabIndex = -1
    measureTwin.readOnly = true
    document.body.appendChild(measureTwin)
  }
  const twin = measureTwin
  const cs = window.getComputedStyle(el)
  // `position:fixed` keeps the twin out of every flow, so no ancestor of the live
  // composer — and therefore not the transcript scroller — can see it at all. It
  // also escapes a transformed ancestor, which a `position:absolute` twin would not.
  // Set per property rather than through one `cssText` declaration string: that
  // form reads as user-facing copy to the i18n gate, and this one matches the
  // property-by-property copying below.
  twin.style.position = 'fixed'
  twin.style.top = TWIN_OFFSCREEN_PX
  twin.style.left = TWIN_OFFSCREEN_PX
  twin.style.visibility = 'hidden'
  twin.style.pointerEvents = 'none'
  twin.style.height = '0'
  twin.style.overflow = 'hidden'
  twin.style.resize = 'none'
  twin.style.border = '0'
  // Everything that can move where the text wraps or how tall a line is. Width and
  // the horizontal box must match or the twin wraps at a different column and
  // reports a height the live element would never have. The live textarea is
  // `border-none`, which is why clearing the border above is safe: under
  // `box-sizing:border-box` a themed border would otherwise give the twin a WIDER
  // content box than the element it stands in for. `scrollbarGutter` is copied
  // for the same reason: the live textarea reserves its 6px scrollbar gutter
  // permanently (`scrollbar-gutter:stable`), which the copied border-box `width`
  // does not show. A twin without the gutter wraps 6px wider, measures a draft
  // whose break falls in that band one line short, and the textarea then scrolls
  // below the cap instead of growing.
  const COPIED = [
    'width', 'boxSizing', 'scrollbarGutter',
    'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
    'font', 'fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'fontStretch',
    'fontFeatureSettings', 'fontVariationSettings', 'fontKerning',
    'lineHeight', 'letterSpacing', 'wordSpacing', 'textIndent', 'textTransform',
    'whiteSpace', 'wordBreak', 'overflowWrap', 'hyphens', 'tabSize',
    'direction', 'writingMode', 'unicodeBidi',
  ] as const
  const style = twin.style as unknown as Record<string, string>
  const computed = cs as unknown as Record<string, string>
  for (const prop of COPIED) {
    const v = computed[prop]
    // Firefox returns '' for the `font` shorthand; the longhands below it cover the
    // same ground, so skip rather than clobber a good value with an empty one.
    if (v) style[prop] = v
  }
  // An empty composer still renders its PLACEHOLDER in the content box, and that
  // counts toward scrollHeight — several of these placeholders are long translated
  // strings that wrap to two lines at phone width, so measuring the empty value
  // alone would clip the box to one line. The text is measured as the twin's VALUE
  // rather than as its `placeholder` attribute: the two lay out through the same
  // path at the same width, and an off-screen node carrying a real placeholder
  // attribute would answer accessibility and test queries meant for the live one.
  twin.value = el.value || el.placeholder || ''
  // A placeholder the stylesheet holds to one line must be MEASURED on one line;
  // the copied `whiteSpace` above is the element's, which still wraps.
  if (!el.value && el.placeholder && getComputedStyle(el, '::placeholder').whiteSpace === 'nowrap') {
    twin.style.whiteSpace = 'nowrap'
  }
  return twin.scrollHeight
}

/** The inputs that produced each textarea's current auto-sized height, and the
 *  height they produced. Both call sites below run for every keystroke — the
 *  input handler, then the auto-size effect once the new `value` commits — so
 *  without this the second call repeats a measurement whose every input is
 *  unchanged. A WeakMap rather than an expando keeps the entry's lifetime tied
 *  to the element's.
 *
 *  Only the MEASUREMENT is elided, never the write: `next !== prev` below still
 *  runs on a memo hit, so a height this function did not write is still
 *  corrected. That is what makes the drag handle's double-click reset work
 *  without a measurement — it clears the inline height while the value stays
 *  put, so the cached height is both still correct and no longer applied. */
const lastMeasured = new WeakMap<HTMLTextAreaElement, { inputs: string; height: string }>()

/** Auto-size textarea to fit content (only when not manually sized).
 *
 *  The measurement happens on an off-screen twin (see `measuredContentHeight`),
 *  so this function's only write to the live element is its FINAL height.
 *
 *  `parked` is a hard precondition, not an optimisation. Voice hold mode and the
 *  dictation panel both keep the textarea mounted inside an `sr-only` box (value,
 *  caret and IME state have to survive the swap), and `sr-only` is a 1px clip — a
 *  textarea one pixel wide reports a `scrollHeight` of the better part of a
 *  viewport, which this function would then clamp to `cap` and WRITE BACK as an
 *  inline height. That height outlives the parking (nothing re-measures until
 *  `value` changes again), so a single voice round-trip left the composer stuck
 *  at the 140px ceiling with an empty box, on a surface whose only way to shrink
 *  it — the drag handle's double-click — does not exist under a finger. */
function applyHeight(
  el: HTMLTextAreaElement,
  manualHeight: number | null,
  prefillHint?: boolean,
  parked?: boolean,
  caretFollow?: boolean,
) {
  if (parked) {
    // Clipped out of layout — there is nothing valid to measure. Drop the memo
    // too: unparking re-runs the effect at an UNCHANGED value, so a cached
    // height would be re-applied without measuring, and font metrics may have
    // changed across the round-trip. One measurement per unpark is not a cost
    // worth caching against.
    lastMeasured.delete(el)
    return
  }
  if (manualHeight !== null) return // manual height — wrapper controls size
  const cap = prefillHint ? INPUT_PREFILL_MAX_H : INPUT_DEFAULT_MAX_H
  const prev = el.style.height
  // Everything the twin measures against: its width and box come from the live
  // element, and an EMPTY value is measured as the placeholder (see
  // `measuredContentHeight`), so a placeholder swap changes the height too.
  // `value` last — it is user text and may itself contain the delimiter, so no
  // content can forge a boundary against the fields in front of it.
  const inputs = [el.clientWidth, cap, el.placeholder, el.value].join('\u0000')
  const memo = lastMeasured.get(el)
  let next: string
  if (memo !== undefined && memo.inputs === inputs) {
    next = memo.height
  } else {
    next = Math.max(INPUT_MIN_H, Math.min(measuredContentHeight(el), cap)) + 'px'
    lastMeasured.set(el, { inputs, height: next })
  }
  if (next !== prev) {
    el.style.height = next
    // Attribute the transcript's resulting viewport change to the composer, so the
    // transcript can hold still instead of chasing it (see composerResize.ts).
    markComposerResize()
  }
  // When typing at the end of overflowing content, snap to the bottom so the caret
  // stays visible. `caretFollow` is false for exactly one caller: the value
  // effect re-measuring a value the PARENT set -- a hand-off prefill, a slot's
  // draft restore. Snapping there yanked the view to the LAST line of a seeded
  // prompt (an error hand-off landed showing only the closing fence of its
  // report, with the sentence that says what broke scrolled out of sight), and
  // the caret was not at risk: it only moves when the user edits, and a real
  // edit comes through the `input` event, which follows it. A re-measure at an
  // UNCHANGED value -- the cap change when the prefill hint expires, unparking,
  // a width change -- is a viewport change under a caret the user placed, so it
  // still follows.
  const caretAtEnd = el.selectionStart === el.value.length && el.selectionEnd === el.value.length
  if (caretFollow && document.activeElement === el && el.scrollHeight > el.clientHeight && caretAtEnd) {
    el.scrollTop = el.scrollHeight
  }
}

/** Drag-to-resize: the persisted preference, the pointer drag on the resize
 *  handle and its double-click reset, and the wrapper height they drive. */
export function useManualHeight({ wrapperRef, pendingFilesCount, pendingSessionsCount }: {
  wrapperRef: React.RefObject<HTMLDivElement>
  pendingFilesCount: number
  pendingSessionsCount: number
}) {
  /** The persisted drag-to-resize preference. Read `manualHeight` below instead —
   *  this is the raw stored value and is not what the composer renders at. */
  const [manualHeightPref, setManualHeight] = useState<number | null>(() => {
    const saved = localStorage.getItem(INPUT_HEIGHT_LS_KEY)
    const n = saved ? parseInt(saved, 10) : NaN
    return !isNaN(n) && n >= INPUT_MIN_H ? n : null
  })
  /**
   * Drag-to-resize is pointer-only, so on a touch device the composer always
   * auto-sizes and the persisted preference is ignored outright.
   *
   * Nobody drags a phone's message box, and the affordance is not merely unused
   * there — it is a trap. The handle is a 6px strip with `touch-action:none` and a
   * zero-px drag threshold sitting directly above the input, so a thumb that lands
   * short pins the height on the spot; and the only way back out is a
   * double-click, which no finger can produce. One stray tap and the box was that
   * size for good, across reloads.
   *
   * Derived rather than baked into the state's seed so a pointer-class change
   * mid-session (a tablet gaining a trackpad) is honoured in both directions:
   * the preference is never destroyed, only disregarded while there is no pointer
   * to have set it. Every consumer — the wrapper's height, the textarea's
   * `flex-1`, the manual-resize floor, `applyHeight`'s bail — reads this and so
   * follows automatically.
   */
  const isTouch = useIsTouchDevice()
  const manualHeight = isTouch ? null : manualHeightPref

  // Drag-to-resize refs — resize wrapper div via direct DOM writes, commit on mouseup.
  // Resizing the wrapper (not the textarea) avoids layout thrashing: the textarea
  // fills the wrapper with height:100% so the browser only reflows the wrapper's
  // subtree, not the entire flex column + Virtuoso list above.
  const dragging = useRef(false)
  const dragStartY = useRef(0)
  const dragStartH = useRef(0)
  /** The drag floor: the resize minimum plus every strip stacked above the
   *  textarea. `useStripHeights` rewrites it each render from the measured
   *  strips; the drag handlers read it at event time. */
  const dragMinHRef = useRef(INPUT_DRAG_MIN_H)

  // Teardown keyed to the HANDLE's lifecycle, not the component's: the handle
  // leaves the tree on its own mid-drag (the pointer type flipping coarse swaps
  // it for the plain spacer; the approval ghost swap unmounts the strip) and
  // pointer capture dies with the element — the terminal lostpointercapture
  // then fires on a DETACHED node, which React's root listener never sees, so
  // onEnd never arrives. React invokes callback refs with null on unmount
  // (component unmount included), making this the one teardown path that
  // covers every exit. onEnd keeps the normal path; whichever runs first wins,
  // the `dragging` flag makes the loser a no-op.
  const releaseDragSuppression = useCallback(() => {
    if (!dragging.current) return
    dragging.current = false
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
    if (wrapperRef.current) wrapperRef.current.style.contain = ''
  }, [wrapperRef])
  const resizeHandleLifecycleRef = useCallback((node: HTMLDivElement | null) => {
    if (node === null) releaseDragSuppression()
  }, [releaseDragSuppression])

  const inputResize = usePointerDrag({
    threshold: 0,
    onStart: (e) => {
      if (!wrapperRef.current) return
      const h = wrapperRef.current.offsetHeight
      dragging.current = true
      dragStartY.current = e.clientY
      dragStartH.current = h
      // Use current natural height as floor so drag never snaps up
      dragMinHRef.current = Math.min(dragMinHRef.current, h)
      // Lock in current height so auto-resize stops interfering
      setManualHeight(h)
      document.body.style.cursor = 'row-resize'
      document.body.style.userSelect = 'none'
      // Isolate reflow to this subtree during drag
      wrapperRef.current.style.contain = 'strict'
    },
    onMove: ({ y }) => {
      if (!dragging.current || !wrapperRef.current) return
      // Account for CSS zoom/scale on #root
      const scale = parseInt(localStorage.getItem('mc-zoom') || '100', 10) / 100
      const maxH = effectiveVh() * INPUT_DRAG_MAX_RATIO
      const delta = (dragStartY.current - y) / scale
      const h = Math.min(maxH, Math.max(dragMinHRef.current, dragStartH.current + delta))
      // Direct DOM write on wrapper — no React state, no textarea auto-size
      wrapperRef.current.style.height = h + 'px'
    },
    onEnd: () => {
      if (!dragging.current) return
      // Restore the page-wide suppression BEFORE anything that can bail: the
      // wrapper ref going null must never strand body.cursor/userSelect.
      releaseDragSuppression()
      const el = wrapperRef.current
      if (!el) return // suppression released; nothing to measure or commit
      // Commit final height to React state
      const finalH = el.offsetHeight
      setManualHeight(finalH)
      safeSetItem(INPUT_HEIGHT_LS_KEY, String(Math.round(finalH)))
    },
  })
  // (Component-unmount teardown is covered by resizeHandleLifecycleRef above:
  // React fires callback refs with null on unmount at every level, so a
  // separate unmount-only effect guard would be a dead duplicate here.)

  const resetHeight = useCallback(() => {
    setManualHeight(null)
    localStorage.removeItem(INPUT_HEIGHT_LS_KEY)
    if (wrapperRef.current) { wrapperRef.current.style.height = ''; wrapperRef.current.style.maxHeight = '' }
  }, [wrapperRef])

  // Sync persisted manual height to DOM (same path as drag writes)
  useEffect(() => {
    if (!wrapperRef.current) return
    if (manualHeight !== null) {
      wrapperRef.current.style.height = Math.max(manualHeight, INPUT_MIN_H) + 'px'
      wrapperRef.current.style.maxHeight = `${INPUT_DRAG_MAX_RATIO * 100}vh`
    } else {
      wrapperRef.current.style.height = ''
      wrapperRef.current.style.maxHeight = ''
    }
  }, [manualHeight, pendingFilesCount, pendingSessionsCount, wrapperRef])

  return { manualHeight, setManualHeight, isTouch, dragging, dragMinHRef, inputResize, resizeHandleLifecycleRef, resetHeight }
}

/** The strips stacked above the textarea (staged files and folders, session
 *  refs): their measured heights, the drag floor they raise, and the manual
 *  height's transient adjustment when one appears or leaves. */
export function useStripHeights({ pendingFilesCount, pendingDirsCount, hasSessionRefs, setManualHeight, dragMinHRef }: {
  pendingFilesCount: number
  pendingDirsCount: number
  hasSessionRefs: boolean
  setManualHeight: (update: (h: number | null) => number | null) => void
  dragMinHRef: React.MutableRefObject<number>
}) {
  const [fileStripRef, fileStripH] = useMeasuredHeight<HTMLDivElement>()
  const [sessionStripRef, sessionStripH] = useMeasuredHeight<HTMLDivElement>()
  /** Combined height of every strip currently stacked above the textarea,
   *  MEASURED rather than predicted from the strips' Tailwind classes. The
   *  manual-resize floor and the transient height adjustment below both work off
   *  this total, so adding a strip can never leave one of them counting only
   *  attachments.
   *
   *  Each strip reports 0 while unmounted, so the sum needs no per-strip
   *  booleans: an absent strip reserves nothing by construction. That also
   *  retires the `hasResizedFile` special case — a chip carrying a resize pill
   *  is simply taller when measured, instead of needing a second predicted
   *  height, which is how the third constant came to exist in the first place.
   */
  const stripH = fileStripH + sessionStripH
  /** Whether `stripH` describes what is actually on screen right now.
   *
   *  A measured height arrives one commit AFTER the strip mounts: the ref
   *  callback cannot read a box that has not been laid out yet. Without this
   *  gate the settling 0 -> 81 reads as "a strip appeared" and the transient
   *  adjustment below inflates a persisted manual height by the strip's height
   *  on every mount that already had something staged. Waiting for a mounted
   *  strip to report a non-zero box makes the first value a BASELINE rather
   *  than a change. */
  const stripsMounted = pendingFilesCount > 0 || pendingDirsCount > 0 || hasSessionRefs
  const stripHSettled = stripsMounted ? stripH > 0 : stripH === 0
  const prevStripH = useRef<number | null>(null)
  const dragMinH = INPUT_DRAG_MIN_H + stripH
  dragMinHRef.current = dragMinH
  // Adjust height transiently when a strip appears/disappears (not persisted —
  // staged files and session refs are both session-scoped). Diffing the TOTAL
  // rather than a per-strip boolean keeps the arithmetic correct when both
  // strips change in the same commit (e.g. send clears files and refs at once).
  useLayoutEffect(() => {
    if (!stripHSettled) return
    const prev = prevStripH.current
    prevStripH.current = stripH
    // `null` is the first settled reading: there is no previous state to have
    // moved from, so it establishes the baseline instead of adjusting.
    if (prev === null || prev === stripH) return
    setManualHeight(h => h !== null ? Math.max(INPUT_DRAG_MIN_H, h + (stripH - prev)) : h)
  }, [stripH, stripHSettled, setManualHeight])

  return { fileStripRef, sessionStripRef, stripH }
}

/** Content-driven height for the textarea: every re-measure, and the paste
 *  mirror's scroll sync that follows one. Takes `textareaParked` from the voice
 *  hooks, which is why the composer calls this after them. */
export function useTextareaAutosize({ inputRef, mirrorRef, value, prefillHint, manualHeight, dragging, textareaParked, activePlaceholder }: {
  inputRef: React.RefObject<HTMLTextAreaElement>
  mirrorRef: React.RefObject<HTMLDivElement>
  value: string
  prefillHint?: boolean
  manualHeight: number | null
  dragging: React.MutableRefObject<boolean>
  textareaParked: boolean
  activePlaceholder: string
}) {
  /** Mirrors `textareaParked` for the handlers that read it at event time (the
   *  input handler, the width observer). Assigned during render, so it is
   *  already current by the time any effect or event handler reads it. */
  const parkedRef = useRef(false)
  parkedRef.current = textareaParked

  const handleInput = useCallback((e: React.FormEvent<HTMLTextAreaElement>) => {
    // This IS the user's edit, so the caret is followed.
    if (!dragging.current) applyHeight(e.target as HTMLTextAreaElement, manualHeight, prefillHint, parkedRef.current, true)
  }, [manualHeight, prefillHint, dragging])

  // Auto-resize textarea to fit content.
  const lastMeasuredValueRef = useRef(value)
  useEffect(() => {
    // A changed value here was set by the parent (the user's own edits already
    // followed the caret in handleInput); an unchanged one means the cap, the
    // parking or the manual height moved under text the user placed the caret
    // in. See `applyHeight` for why only the former must not follow the caret.
    const valueChanged = lastMeasuredValueRef.current !== value
    lastMeasuredValueRef.current = value
    if (inputRef.current && !dragging.current) applyHeight(inputRef.current, manualHeight, prefillHint, textareaParked, !valueChanged)
  }, [value, prefillHint, manualHeight, textareaParked, inputRef, dragging])

  // A pre-filled prompt is read from its first line. When the seed REPLACES what
  // the box held, a box that was scrolled for the previous text keeps that
  // offset across the value swap, so the new prompt's first line can start above
  // the fold: reset once, when the hint arrives with the seed. When the seed was
  // APPENDED to a draft the user was writing (the widget send path), the new
  // text is the tail and the offset they had is the right one, so leave it. The
  // caret stays at the end either way, so typing still appends. The DOM value is
  // read rather than the prop so the effect keys on the hint alone.
  const valueBeforeHintRef = useRef(value)
  useEffect(() => {
    const el = inputRef.current
    if (!prefillHint || !el) return
    // The append path joins on a trimmed draft, so compare against that form.
    const prev = valueBeforeHintRef.current.trimEnd()
    const appended = prev.trim().length > 0 && el.value.startsWith(prev)
    if (!appended) el.scrollTop = 0
  }, [prefillHint, inputRef])
  useEffect(() => { valueBeforeHintRef.current = value }, [value])

  // Re-measure when the textarea's WIDTH changes at an unchanged value: a window
  // resize, a sibling column folding, the side panel docking. The wrapped
  // placeholder or text needs a different height at the new column, and the
  // effect above cannot know — none of its deps moved. Without this the box kept
  // the height it had at the old width and clipped the placeholder's second
  // line mid-glyph on the Members DM thread (issue #9979, finding 4).
  // Width ONLY: the observer also fires for the height `applyHeight` itself
  // writes, and re-running on that would measure for nothing (the memo makes it
  // a no-op, but the guard makes the intent legible). `dragging` and `parked`
  // are the same preconditions the two call sites above honour.
  useEffect(() => {
    const el = inputRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    let lastWidth = el.clientWidth
    const ro = new ResizeObserver(() => {
      const width = el.clientWidth
      if (width === lastWidth) return
      lastWidth = width
      if (!dragging.current) applyHeight(el, manualHeight, prefillHint, parkedRef.current, true)
    })
    ro.observe(el)
    return () => ro.disconnect()
    // `textareaParked` re-arms the observer on the way back from the sr-only box,
    // where the 1px width must not be the baseline the next change is judged from.
  }, [manualHeight, prefillHint, textareaParked, inputRef, dragging])

  // Keep the paste-highlight mirror's scroll aligned with the textarea after
  // value/height changes (applyHeight mutates scrollTop programmatically, which
  // doesn't fire the textarea's onScroll). rAF lets layout settle first.
  useEffect(() => {
    const id = requestAnimationFrame(() => {
      if (mirrorRef.current && inputRef.current) mirrorRef.current.scrollTop = inputRef.current.scrollTop
    })
    return () => cancelAnimationFrame(id)
  }, [value, prefillHint, manualHeight, textareaParked, mirrorRef, inputRef])
  // Re-measure when the PLACEHOLDER swaps at an unchanged value: an empty composer
  // measures its placeholder, and the value effect's deps cannot see it. The caret
  // is NOT followed here — a placeholder only shows over an empty box, so there is
  // no line of the user's to keep in view, and this effect also runs on a
  // parent-driven value change, where snapping is what the seeded-prompt rule forbids.
  useEffect(() => {
    const el = inputRef.current
    if (el && !dragging.current) applyHeight(el, manualHeight, prefillHint, parkedRef.current, false)
  }, [activePlaceholder, manualHeight, prefillHint, inputRef, dragging])

  return { handleInput }
}
