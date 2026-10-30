/**
 * A side panel sliding under a trackpad drag (chat page and Mates page). While
 * one slides, the chat column changes width on every frame, so per-frame work
 * that width would cause is held until it rests (`slideHeld` / `afterSlide`).
 *
 * 1. The transcript's published pane width (`--chat-pane-width`,
 *    TranscriptScrollShell). It is an INHERITED custom property on the
 *    scroller, so every change restyles every element under it: on a long
 *    thread that was a 6,000-element style recalc per frame, the main cause of
 *    the dropped frames. While held, a publisher only notes its new width;
 *    when the last slide ends, each publishes its final width once. The prose
 *    still reflows with the column; only a table the reader expanded past the
 *    reading column keeps its width until the slide ends. A table's column
 *    handles (useMarkdownTableColumns) are re-measured once at the end too.
 * 2. Hover under a still pointer. The text reflows under the pointer on every
 *    frame, so the hovered message changed on every frame and mounted its
 *    action bar (and its menus' queries) again. `data-panel-sliding` on the
 *    root turns pointer events off over the transcript (index.css) for the
 *    slide; the wheel run still reaches the page's swipe listener above it.
 * 3. The reading position. `watchPanelSlide` tells the transcript a slide
 *    started and ended, so it can keep the line the reader was looking at in
 *    place while the text reflows (TranscriptScrollShell's `useSlideAnchor`).
 */

type Publisher = () => void

let slides = 0
const pending = new Set<Publisher>()
const watchers = new Set<(sliding: boolean, retired: boolean) => void>()
let retiring = false

/** Be told when the first slide starts (true) and when the last one ends
 *  (false, after the held work has run). `retired` says the slide was
 *  dropped rather than landed (a session switch, the layout changing): the
 *  page under it may be another one now, so nothing is held past it.
 *  Returns the unsubscribe. */
export function watchPanelSlide(fn: (sliding: boolean, retired: boolean) => void): () => void {
  watchers.add(fn)
  return () => { watchers.delete(fn) }
}

/** Whether a panel is sliding now. */
export function slideHeld(): boolean {
  return slides > 0
}

/** Run `fn` once when the slide ends (once however often it is asked). */
export function afterSlide(fn: Publisher): void {
  pending.add(fn)
}

/** Forget `fn`, for a caller that has gone. */
export function forgetAfterSlide(fn: Publisher): void {
  pending.delete(fn)
}

/** The slide in flight is being dropped, not landed: call before ending it. */
export function retirePanelSlide(): void {
  if (slides > 0) retiring = true
}

/** A panel starts sliding; returns its end, which is safe to call twice. */
export function beginPanelSlide(): () => void {
  slides += 1
  document.documentElement.setAttribute('data-panel-sliding', '')
  if (slides === 1) for (const fn of [...watchers]) fn(true, false)
  let ended = false
  return () => {
    if (ended) return
    ended = true
    slides -= 1
    if (slides > 0) return
    document.documentElement.removeAttribute('data-panel-sliding')
    const due = [...pending]
    pending.clear()
    for (const fn of due) fn()
    const retired = retiring
    retiring = false
    for (const fn of [...watchers]) fn(false, retired)
  }
}

const settling = new WeakSet<Element>()
const settledWaiters = new WeakMap<Element, Set<() => void>>()

/** A transcript holds its place through a slide (its slide anchor, bottom
 *  pin included), from the slide's start until it lets go. */
export function markSlideSettling(scroller: Element, on: boolean): void {
  if (on) { settling.add(scroller); return }
  if (!settling.delete(scroller)) return
  const due = settledWaiters.get(scroller)
  settledWaiters.delete(scroller)
  if (due) for (const fn of due) fn()
}

/** Whether `scroller`'s geometry is still moving under a slide: the panel
 *  slides, or the transcript is still settling its place after it. Work that
 *  reads where rows sit (the pinned prompt) waits, or it reads one frame of
 *  in-between layout and acts on it. */
export function slideSettling(scroller: Element): boolean {
  return slides > 0 || settling.has(scroller)
}

/** Run `fn` once `scroller` has settled (at once when it already has). */
export function afterSlideSettles(scroller: Element, fn: () => void): void {
  if (settling.has(scroller)) {
    let set = settledWaiters.get(scroller)
    if (!set) settledWaiters.set(scroller, (set = new Set()))
    set.add(fn)
  } else if (slides > 0) {
    afterSlide(fn)
  } else {
    fn()
  }
}

const heldScrollers = new WeakSet<Element>()
const releasers = new WeakMap<Element, () => void>()

/** A transcript holds its reading position itself across a slide (its slide
 *  anchor), or stops holding it. While held, the virtualizer's own reprice
 *  compensation stands down for that scroller: two mechanisms pulling one
 *  scroll offset toward two different anchors made the text jitter. */
export function holdReadingPosition(scroller: Element, held: boolean, release?: () => void): void {
  if (held) {
    heldScrollers.add(scroller)
    if (release) releasers.set(scroller, release)
  } else {
    heldScrollers.delete(scroller)
    releasers.delete(scroller)
  }
}

/** Register how to let go of a hold that does not stand the virtualizer down
 *  (a transcript pinned to its bottom through a slide); null clears it. */
export function onReleaseReadingPosition(scroller: Element, release: (() => void) | null): void {
  if (release) releasers.set(scroller, release)
  else releasers.delete(scroller)
}

/** Explicit navigation (jump to the bottom or to a message, a pinned-prompt
 *  jump, a restored reading position) takes the scroll position: a slide anchor holding this scroller lets
 *  go first, or its next correction would put the reader back. */
export function releaseReadingPosition(scroller: Element | null | undefined): void {
  if (scroller) releasers.get(scroller)?.()
}

/** Whether a slide anchor holds this scroller's reading position now. */
export function readingPositionHeld(scroller: Element): boolean {
  return heldScrollers.has(scroller)
}
