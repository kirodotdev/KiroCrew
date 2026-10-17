/**
 * Remembered scroll position for the workspace file tree, keyed by project
 * directory and tree mode, for the page session.
 *
 * The pinned Files tab mounts only while active, so leaving it (opening a
 * file focuses that file's tab, or the user clicks another tab) destroys the
 * tree and its scroll element. `./treeExpansionMemory` brings the expanded
 * folders back on the next mount, but a fresh scroll element starts at the
 * top, so the user still has to find their place again. This module keeps
 * the path of the row at the TOP of the viewport while the tree is mounted
 * and scrolls back to it on the next mount.
 *
 * A row path rather than a pixel offset: the tree can change between visits
 * (a folder above was expanded, a file was added), and a pixel offset would
 * then land on a different row. A path survives that, and the restore goes
 * through the library's public `scrollToPath`. The sub-row remainder is
 * dropped, so the restored view can differ by less than one row.
 *
 * Session memory only, deliberately not `localStorage`: the case is a tab
 * round trip within one page, and a jump into the middle of the tree after a
 * reload would be surprising rather than helpful.
 */

import { useCallback, useEffect, useLayoutEffect, useRef } from 'react'

/** Cap on remembered (project, mode) entries; the least recently written is
 *  evicted past it, the same bound the expansion memory uses. */
const MAX_REMEMBERED = 20

/** How long after mount a restore that could only reach an ancestor keeps
 *  waiting for the exact row. Rows under a folder past the listing's row cap
 *  arrive with that folder's own listing, after the first projection; a
 *  listing that has not landed by then is not coming back for this visit. */
const EXACT_ROW_WAIT_MS = 15_000

/** Input events that mean the user has taken the tree over. They end the
 *  wait for the exact row: from then on the user decides where it is. */
const USER_INPUTS = ['wheel', 'touchstart', 'pointerdown', 'keydown'] as const

/** Frames to wait for the tree's shadow root to appear before giving up on
 *  capture for this mount: `<FileTree>` creates it only after its own commit. */
const ATTACH_ATTEMPTS = 10

const anchors = new Map<string, string>()

const keyOf = (projectDir: string, mode: string) => `${projectDir}\u0000${mode}`

function remember(key: string, path: string | null): void {
  anchors.delete(key)
  if (path === null) return
  anchors.set(key, path)
  for (const k of anchors.keys()) {
    if (anchors.size <= MAX_REMEMBERED) break
    anchors.delete(k)
  }
}

/** Test-only: drop the session map so suites stay isolated. */
export function __resetTreeScrollAnchorsForTests(): void {
  anchors.clear()
}

/** Test-only: the remembered anchor, without going through a mount. */
export function __recallTreeScrollAnchorForTests(projectDir: string, mode: string): string | null {
  return anchors.get(keyOf(projectDir, mode)) ?? null
}

/** The slice of the `@pierre/trees` model this hook reads. */
export interface ScrollAnchorModel {
  getFileTreeContainer(): HTMLElement | undefined
  getItemHeight(): number
  getVisibleCount(): number
  getVisibleRows(start: number, end: number): ReadonlyArray<{ path: string }>
  scrollToPath(path: string, options?: { focus?: boolean; offset?: 'top' | 'center' | 'nearest' }): void
}

const stripDirSlash = (path: string) => (path.endsWith('/') ? path.slice(0, -1) : path)

/** Visible rows by path without the directory slash, to the path the model
 *  takes back. O(rows); called once per restore attempt, not per scroll. */
function visibleRowPaths(model: ScrollAnchorModel): Map<string, string> {
  const visible = new Map<string, string>()
  const count = model.getVisibleCount()
  if (count === 0) return visible
  for (const row of model.getVisibleRows(0, count - 1)) visible.set(stripDirSlash(row.path), row.path)
  return visible
}

/**
 * Remember the tree's top visible row while it is mounted and scroll back to
 * it on the next mount.
 *
 * Call it AFTER the host's path-reset, search and selection layout effects:
 * React runs layout effects in declaration order, and the restore must see
 * the projection those effects produce (restored expansion included), or the
 * anchor row is not visible yet and there is nothing to scroll to.
 *
 * Every scroll is recorded once the restore has been decided, whoever made
 * it: the library's own scrolls (clamping when rows shrink, bringing focus
 * into view) leave the tree where the user sees it, which is exactly what the
 * next visit should show. Two windows are not recorded: before the restore is
 * decided, when the scroller still sits at the top of a fresh mount, and while
 * waiting for the exact row after an ancestor fallback, when the scroller
 * shows the stand-in rather than the user's place.
 *
 * Inert while a search query is active: a filter changes which rows exist, so
 * neither its top row nor a restore into it reflects the unfiltered tree the
 * user browses.
 */
export function useTreeScrollAnchor({ model, enabled, paused = false, ready, projectDir, mode, pathsKey, searchQuery }: {
  model: ScrollAnchorModel
  /** Opt-in from the host. False leaves the tree's scroll behavior untouched. */
  enabled: boolean
  /** The tree is mounted but not shown (the host covers it). Holds the
   *  restore until it is shown again: a hidden scroller has no height, so a
   *  restore into it would land at the top. */
  paused?: boolean
  /** The model holds the first payload. */
  ready: boolean
  projectDir: string
  mode: string
  /** Changes whenever the model was reset with new paths. */
  pathsKey: string
  searchQuery?: string | null
}): void {
  const key = keyOf(projectDir, mode)
  const searchRef = useRef(searchQuery)
  searchRef.current = searchQuery

  // The restore is decided once per mount, on the first projection it can
  // run against. Until then nothing is recorded.
  const decidedRef = useRef(false)
  // After an ancestor fallback: the exact row still being waited for, and
  // until when. Nothing is recorded while it is set.
  const pendingRef = useRef<{ anchor: string; until: number } | null>(null)
  // A restore target to send again once the view has measured its viewport,
  // cleared by user input. The view applies a request against its initial
  // viewport estimate until it has measured, and a later measurement only
  // clamps: a target in the last screenful of a panel shorter than the
  // estimate would stay that many rows too high. `measuredRef` turns true on
  // the host's first measured size; requests after it need no resend.
  const resendRef = useRef<string | null>(null)
  const measuredRef = useRef(false)
  const scrollTo = useCallback((target: string) => {
    model.scrollToPath(target, { focus: false, offset: 'top' })
    resendRef.current = measuredRef.current ? null : target
  }, [model])

  useLayoutEffect(() => {
    if (!enabled || paused || !ready || searchQuery) return
    if (model.getVisibleCount() === 0) return
    const pending = pendingRef.current
    if (decidedRef.current && !pending) return
    if (pending && Date.now() > pending.until) {
      pendingRef.current = null
      return
    }
    const anchor = pending?.anchor ?? anchors.get(key) ?? null
    decidedRef.current = true
    if (!anchor) return
    // `scrollToPath` returns silently for a row that is not visible, so pick
    // the target first: the anchor itself, else the nearest ancestor still
    // on screen (the anchor was deleted, a folder above it collapsed, or its
    // folder's own listing has not landed yet).
    const visible = visibleRowPaths(model)
    const exact = visible.get(anchor)
    if (exact) {
      pendingRef.current = null
      scrollTo(exact)
      return
    }
    // Keep waiting for the exact row across later payloads. Scrolling to the
    // ancestor happens once, on the first attempt only.
    if (!pending) pendingRef.current = { anchor, until: Date.now() + EXACT_ROW_WAIT_MS }
    if (pending) return
    let candidate = anchor.lastIndexOf('/') > 0 ? anchor.slice(0, anchor.lastIndexOf('/')) : null
    while (candidate) {
      const target = visible.get(candidate)
      if (target) {
        scrollTo(target)
        return
      }
      const slash = candidate.lastIndexOf('/')
      candidate = slash > 0 ? candidate.slice(0, slash) : null
    }
  }, [enabled, paused, ready, pathsKey, searchQuery, model, key, scrollTo])

  // Capture. The listeners go capture-phase on the tree's shadow root, since
  // `scroll` is not composed and never reaches a light-DOM listener; the
  // tree's scroller is the only scroll container in that root. Waits for
  // `ready`: the host renders a skeleton, not `<FileTree>`, until then.
  useEffect(() => {
    if (!enabled || !ready) return
    let root: ShadowRoot | null = null
    let frame = 0
    let resendFrame = 0
    let attempts = 0
    let observer: ResizeObserver | null = null
    const onInput = () => {
      pendingRef.current = null
      resendRef.current = null
    }
    const onScroll = (event: Event) => {
      if (!decidedRef.current || pendingRef.current) return
      if (searchRef.current) return
      const scroller = event.target
      if (!(scroller instanceof HTMLElement)) return
      // A hidden tree (the rail's name-search pane covers it) measures zero.
      if (scroller.clientHeight === 0) return
      const count = model.getVisibleCount()
      if (count === 0) return
      if (scroller.scrollTop <= 0) {
        remember(key, null)
        return
      }
      const height = model.getItemHeight()
      if (!(height > 0)) return
      const index = Math.min(count - 1, Math.floor(scroller.scrollTop / height))
      const row = model.getVisibleRows(index, index)[0]
      if (row) remember(key, stripDirSlash(row.path))
    }
    const attach = () => {
      const shadow = model.getFileTreeContainer()?.shadowRoot ?? null
      if (!shadow) {
        if (++attempts < ATTACH_ATTEMPTS) frame = requestAnimationFrame(attach)
        return
      }
      root = shadow
      // The view measures its scroller in a ResizeObserver delivery; the host
      // it fills is measured in the same delivery. The resend waits one frame
      // so the view has re-rendered with that measurement before it reads
      // the request.
      const host = shadow.host
      if (typeof ResizeObserver !== 'undefined') {
        observer = new ResizeObserver(entries => {
          const height = entries[0]?.contentRect.height ?? 0
          if (!(height > 0) || measuredRef.current) return
          measuredRef.current = true
          observer?.disconnect()
          if (!resendRef.current) return
          resendFrame = requestAnimationFrame(() => {
            const again = resendRef.current
            resendRef.current = null
            if (again) model.scrollToPath(again, { focus: false, offset: 'top' })
          })
        })
        observer.observe(host)
      }
      for (const type of USER_INPUTS) root.addEventListener(type, onInput, { capture: true, passive: true })
      root.addEventListener('scroll', onScroll, { capture: true, passive: true })
    }
    attach()
    return () => {
      cancelAnimationFrame(frame)
      cancelAnimationFrame(resendFrame)
      observer?.disconnect()
      if (!root) return
      for (const type of USER_INPUTS) root.removeEventListener(type, onInput, { capture: true })
      root.removeEventListener('scroll', onScroll, { capture: true })
    }
  }, [enabled, ready, model, key])
}
