import { type RefObject, useCallback, useEffect, useRef } from 'react'

import { attachUserScrollIntent, glideOnceStep, pollRowSettled } from '../../utils/searchScroll'
import { leadMessage, rowOfMessage as rowOfMessageIn } from '../../chat-core/transcript/rowKeys'
import { useStableMessageKey } from '../../chat-core/transcript/useStableMessageKey'
import type { DisplayItem } from './types'
import type { ChatMessage } from '../../types'

export type NavToDisplayIndexOptions = { behavior?: ScrollBehavior; align?: ScrollLogicalPosition; offset?: number }

interface UseNavToDisplayIndexOptions {
  /** The transcript scroll container. Rows inside it carry `data-display-index`. */
  scrollerRef: RefObject<HTMLDivElement | null>
  /** The rows on screen, in the order of their `data-display-index`. */
  displayItemsRef: RefObject<readonly DisplayItem[]>
  /** The virtualizer's `mountIndex`: mounts a row (replacing the window for a
   *  far row) and reports whether the jump was far. */
  mountIndex: (index: number, opts?: { unionOnly?: boolean }) => boolean
  /** Scrolls to a mounted row; false while the row is not in the DOM yet. */
  scrollToDisplayIndex: (index: number, opts?: NavToDisplayIndexOptions) => boolean
}

/**
 * The jump to a (possibly unmounted) row of a virtualized transcript: the
 * Sessions chat's search, pins and links, and a Bot chat's Find in chat. It
 * mounts the row first through the virtualizer, then scrolls once the row is
 * in the DOM and keeps it in place until its height settles.
 *
 * A jump aims at a MESSAGE (`navToMessage`) and finds the row that draws it
 * again on every frame (`rowOfMessage`). A display index is good for the rows
 * of one render only: a page of older history that lands while the poll is
 * still running renumbers every row, and the index then names an older
 * message. `navToDisplayIndex` is the jump for a caller that holds a row of
 * the rows on screen (the turn minimap): it aims at that row's first message.
 *
 * `navScrollRafRef` / `navPollCancelRef` are returned so another jump on the
 * same scroller (the pinned-prompt glide) can cancel and own the same handles.
 */
export function useNavToDisplayIndex({ scrollerRef, displayItemsRef, mountIndex, scrollToDisplayIndex }: UseNavToDisplayIndexOptions) {
  // The row identity rule the transcript keys its rows by.
  const msgKey = useStableMessageKey()
  /** The display index of the row on screen that draws `m`, or -1. */
  const rowOf = useCallback(
    (m: ChatMessage) => rowOfMessageIn(displayItemsRef.current ?? [], m, msgKey),
    [displayItemsRef, msgKey],
  )
  // Navigate to a (possibly off-window) message: mount its row first via the
  // virtualizer so the DOM-based scroll can find it, then scroll next frame.
  // Tracks the in-flight row-mount poll (below) so a newer navigation cancels
  // the previous one. Without this, an earlier far-jump loop whose target
  // finally mounts would scroll to that stale destination, yanking away from
  // the newer target (rapid stepping / click-then-click). cancelAnimationFrame(0)
  // is a no-op, so 0 is a safe initial value.
  const navScrollRafRef = useRef(0)
  // Cancel handle for the in-flight settle poll, so a newer navigation or an
  // unmount terminates it rather than letting it run to the wall-clock backstop.
  const navPollCancelRef = useRef<(() => void) | null>(null)
  /** Scroll to the row that draws `target`. False, and nothing moves, when no
   *  row on screen draws it. */
  const navToMessage = useCallback((
    target: ChatMessage,
    opts?: NavToDisplayIndexOptions,
  ): boolean => {
    const first = rowOf(target)
    if (first < 0) return false
    cancelAnimationFrame(navScrollRafRef.current)
    // Signal WidgetFrames that a jump is starting so the span of widgets
    // mountIndex is about to union doesn't all build their iframes in one
    // frame (see PROGRAMMATIC_BUILD_DELAY_MS in WidgetFrame).
    window.dispatchEvent(new Event('mc-chat-scroll-jump'))
    const jumpedFar = mountIndex(first)
    // A FAR jump replaces the window, so the rows between the old viewport and
    // the target are NOT mounted — a smooth glide would scrub the scroller
    // through blank spacer (the "occasional flicker" on the ↑/jump pills when
    // the target is past a long turn). Teleport instantly instead: the target
    // block is already mounted so it shows immediately, and overflow-anchor
    // keeps it stable as its rows measure. NEAR jumps keep their smooth glide
    // (mountIndex unioned the whole path, so there's nothing blank to scrub).
    const behavior: ScrollBehavior = jumpedFar ? 'auto' : (opts?.behavior ?? 'smooth')
    // mountIndex queues a React state update (the virtualizer's window range).
    // A FAR jump REPLACES the window, so the target row is NOT painted into the
    // DOM within a single frame — one rAF then a DOM query misses it. Poll for
    // the row and scroll once it mounts, then keep re-scrolling (re-reading the
    // live offset each frame) until the row's measured height SETTLES — a far
    // row must mount + measure, and a widget target keeps growing for ~450ms as
    // its iframe builds (PROGRAMMATIC_BUILD_DELAY_MS). A fixed frame-count
    // ceiling (~0.5s) gives up before the widget settles, so the jump silently
    // no-ops and only works on a second click once cached. Condition-based
    // instead: retry until the target reports a stable (non-estimated) height,
    // with a ~2s wall-clock backstop so a genuinely unreachable target still
    // terminates instead of spinning. While the row is missing we do NOTHING —
    // we never teleport to top (the "far jump jumps to top, second click works"
    // bug). navScrollRafRef holds the in-flight frame so a newer navigation
    // cancels this loop (rapid stepping / click-then-click).
    // The row is found again each frame, and the step scrolls to the row the
    // same frame's measure found.
    let row = first
    const rowEl = (): HTMLElement | null => {
      row = rowOf(target)
      if (row < 0) return null
      return (scrollerRef.current?.querySelector(`[data-display-index="${row}"]`) as HTMLElement | null) ?? null
    }
    navPollCancelRef.current?.()
    // The poll re-scrolls every frame for up to CONVERGE_MAX_MS (~2s). If the
    // user tries to scroll during that window, continuing to step would drag
    // the viewport back to the target and fight their input — so user scroll
    // ABORTS the convergence, exactly as scrollCurrentMatchIntoView does. (A
    // fixed frame-count ceiling short enough (~0.5s) masks this; the
    // longer, condition-based window makes it reachable.) The shared
    // attachUserScrollIntent covers scrollbar drag and keyboard scrolling too,
    // not just wheel/touch.
    const scrollEl = scrollerRef.current
    const onUserScroll = () => { navPollCancelRef.current?.() }
    const detachUserScroll = attachUserScrollIntent(scrollEl ?? undefined, onUserScroll)
    navPollCancelRef.current = pollRowSettled({
      measure: () => {
        const el = rowEl()
        // The row is gone again before the first step scrolled to it: a landing
        // that commits in the same frame recomputes the window from the scroll
        // position the jump has not moved yet, and unmounts a far row. Mount it
        // again; once the step has scrolled, the window follows the scroll.
        if (!el && row >= 0) mountIndex(row)
        return el ? el.getBoundingClientRect().height : null
      },
      // Only the FIRST step may glide — see glideOnceStep. Re-issuing a smooth
      // scroll cancels and restarts the animation, so stepping every frame
      // through the quiet window would leave a NEAR jump stuttering until the
      // poll ends (the same restart trap removed from the streaming pin).
      step: glideOnceStep(
        (b) => { scrollToDisplayIndex(row, { ...opts, behavior: b }) },
        behavior,
      ),
      raf: (cb) => (navScrollRafRef.current = requestAnimationFrame(cb)),
      now: () =>
        typeof performance !== 'undefined' && typeof performance.now === 'function'
          ? performance.now()
          : Date.now(),
      onEnd: () => { detachUserScroll(); navPollCancelRef.current = null },
    })
    return true
  }, [scrollToDisplayIndex, scrollerRef, mountIndex, rowOf])
  /** Scroll to display row `idx` of the rows on screen: the jump aims at the
   *  first message that row draws. */
  const navToDisplayIndex = useCallback((idx: number, opts?: NavToDisplayIndexOptions) => {
    const item = displayItemsRef.current?.[idx]
    const lead = item ? leadMessage(item) : null
    if (lead) navToMessage(lead, opts)
  }, [displayItemsRef, navToMessage])
  // Ends the in-flight navigation's settle poll, for a scroll that takes over
  // from it: a fold that opened under a jump target scrolls to the row itself,
  // and the poll re-centering the whole turn would undo that.
  const cancelNavScroll = useCallback(() => {
    cancelAnimationFrame(navScrollRafRef.current)
    navPollCancelRef.current?.()
  }, [])

  // Stop any in-flight settle poll on unmount. Without this the loop keeps
  // ticking rAFs against a null scroller until the ~2s backstop (harmless but
  // pointless work after the page is gone).
  useEffect(() => () => {
    navPollCancelRef.current?.()
    navPollCancelRef.current = null
    cancelAnimationFrame(navScrollRafRef.current)
  }, [])

  return { navToMessage, navToDisplayIndex, rowOfMessage: rowOf, cancelNavScroll, navScrollRafRef, navPollCancelRef }
}
