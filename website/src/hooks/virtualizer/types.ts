// Public types for the chat virtualizer hook.

export interface UseVirtualChatOptions<T> {
  /** Full list of items in display order. */
  items: T[]
  /** Stable key extractor used as the height-cache key. */
  getKey: (item: T, index: number) => string
  /**
   * Identity that SURVIVES a display-key reshuffle, for scroll-anchor
   * resolution only. Display keys (getKey) can be renamed wholesale by a
   * prepend landing — dedupe suffixes shift and a turn's lead key changes when
   * an older page regroups into it — and an anchor held by such a key dies
   * exactly when it is needed, dropping the compensation (measured: content
   * sliding −721px in one frame under a still reader). Return something
   * derived from the row's TAIL message (a prepend regroups a turn's HEAD,
   * never its tail). Optional: defaults to getKey, which preserves today's
   * behavior for callers whose keys are already stable.
   */
  getStableId?: (item: T, index: number) => string
  /**
   * SECOND stable id for the same row, from its LEAD message. Persisted
   * alongside `getStableId`'s value so a saved reading position resolves when
   * EITHER end of the row survived: appends rename the tail, an older page
   * landing renames the lead. Optional -- without it a restore matches on the
   * tail id alone, which is the previous behaviour.
   */
  getAltId?: (item: T, index: number) => string
  /**
   * Display index at (or below) which the older-history prefetch fires — the
   * caller's own notion of "close enough to the top". ChatPage passes the
   * index of the SECOND USER MESSAGE from the top of the loaded transcript,
   * per the stated contract "start loading while I am still two of my own
   * messages away". Fired on the downward CROSSING of this index, so a
   * landing (which shifts every index) re-arms it without looping. Defaults
   * to a small display-row lead.
   *
   * With `followEdge: 'top'` history is at the END, so this is the index the
   * window's END must travel DOWN past (row `prefetchStartIndex` becoming
   * mounted); the caller passes the second user message counted from the END.
   * The default lead is then counted from the end too.
   */
  prefetchStartIndex?: number
  /** Height to use when no measurement is cached. Default: 80. */
  estimatedHeight?: number
  /**
   * Identity for the HEIGHT CACHE only (defaults to sessionId). Callers whose
   * row heights depend on layout width append a width bucket, so heights
   * measured at one width are never served at another (desktop cache on a
   * phone = a correction jump on every mount). Scroll restore and prepend
   * detection stay on the pure sessionId.
   */
  heightScopeKey?: string
  /** Whether live layout matches the height scope. Checked at every measurement
   * write (observer, ref seed, farm); a changed callback remeasures mounted rows. */
  canMeasure?: () => boolean
  /** Items to mount above and below the visible viewport. Default: 5. */
  overscan?: number
  /** Session ID — partitions the persisted height cache. */
  sessionId: string
  /**
   * Whether to pin the scroll position to the bottom on item appends.
   * Default: true. The user scrolling away from the bottom always disables
   * pinning regardless of this option (see Property 7).
   */
  followOutput?: boolean
  /**
   * Where the list opens when there is no saved scroll anchor to restore.
   * `'bottom'` (default) is the chat contract: slot entry pins to the tail
   * and the initial mount window is the LAST items. `'top'` is the
   * list/gallery contract: open at the head with the FIRST items mounted.
   *
   * `'top'` matters beyond the landing position: opening at the tail places
   * every not-yet-measured row ABOVE the viewport, so each measurement that
   * lands must compensate scrollTop, and with many estimate-to-real
   * corrections in flight the repeated compensation writes read as flicker.
   * At the head the unmeasured rows are all BELOW the viewport — a
   * measurement only grows the bottom spacer, which is invisible.
   */
  initialPlacement?: 'top' | 'bottom'
  /**
   * Which end of the list is the LIVE edge: where new output appears, where
   * follow keeps the reader, and opposite the end history pages load at.
   *
   * `'bottom'` (default) is the chat contract. `'top'` is the newest-first
   * transcript, whose newest rows are at index 0:
   *   - implies `initialPlacement: 'top'` (the head window, scrollTop 0);
   *   - `followOutput` keeps its meaning, with the live edge at scrollTop 0. A
   *     reader there follows: rows inserted at the head stay in view. A reader
   *     who scrolled down into history is held still while rows arrive above.
   *   - `isAtBottom` reads "at the live edge" (scrollTop within
   *     `bottomThreshold` of 0) and `scrollToBottom` jumps to the live edge
   *     (head window, scrollTop 0). The names are kept so callers need no
   *     branch.
   *   - history is at the END: `onTopReached` fires from the BOTTOM sentinel,
   *     and `prefetchStartIndex` is crossed as the window's end travels DOWN
   *     past it.
   */
  followEdge?: 'bottom' | 'top'
  /**
   * Sync a row's FIRST measurement into the offset math immediately instead
   * of through the debounced height-sync. Default: false (the chat contract —
   * first-mount seeds ride the debounce, which keeps the upward-scroll anchor
   * compensation's commit ordering exactly as it is).
   *
   * Turn this on for gallery/list content whose real heights vary widely
   * around `estimatedHeight` (mixed HTML/GIF/image cards). Scrolling down
   * mounts a new row every few dozen ms and each seed RESETS the debounce
   * timer, so the offset tree starves — frozen at estimates for the whole
   * gesture — and every row the window front hands from real DOM to the
   * before-spacer shrinks the content above the viewport by (real − estimate),
   * felt as a per-card bounce. A first measurement happens once per row, so
   * syncing it eagerly cannot be the oscillation the debounce protects
   * against; subsequent re-measures of the same row stay debounced.
   */
  eagerFirstMeasure?: boolean
  /**
   * Threshold in pixels from the bottom below which `isAtBottom` becomes
   * true. Default: 100. The same threshold gates the follow-output
   * auto-pin so callers can tune sensitivity.
   */
  bottomThreshold?: number
  /**
   * Optional predicate: items for which this returns `true` are mounted
   * regardless of the viewport window — they never become placeholders.
   * Useful for items with expensive children (widget iframes, images
   * with custom decoders) that lose state on unmount and recreate it
   * slowly on remount, causing visible flicker.
   *
   * Use sparingly: each sticky item permanently consumes the memory and
   * resources of its mounted form for the session lifetime.
   */
  isSticky?: (item: T, index: number) => boolean
  /**
   * Optional external scroll container ref. When provided, the hook
   * manages the same DOM element instead of creating a new one. Useful
   * when integrating with existing scroll-management code that owns the
   * scroller. Accepts both `RefObject<HTMLDivElement>` (non-null) and
   * `RefObject<HTMLDivElement | null>` (nullable) styles of useRef result.
   */
  externalScrollerRef?: React.RefObject<HTMLDivElement | null> | React.RefObject<HTMLDivElement>
  /**
   * Index of the item currently receiving live content growth (e.g. the
   * streaming assistant message), if any. When set, ResizeObserver-driven
   * height changes for THIS index bypass the debounced height→offset sync
   * (see HEIGHT_SYNC_DEBOUNCE_MS) and apply immediately instead.
   *
   * Rationale: while a message streams, its element's height changes on
   * nearly every animation frame. Debouncing (as every other row still does)
   * means the offset memos (`totalHeight`/`offsetAfter`, which back the
   * scroll content's total size and the bottom spacer) sit frozen at a stale
   * value for as long as growth keeps arriving, then jump by the ENTIRE
   * accumulated backlog in one commit the instant growth pauses long enough
   * for the debounce to fire. For a user scrolled away from the bottom
   * reading history, that spacer sits directly below their viewport, and the
   * large discrete jump reads as a visible flash — unrelated to (and never
   * fixed by) the separate text-reveal-edge flash fixes in MarkdownRenderer.
   * Passing the live streaming index here makes that row's growth track the
   * viewport every tick instead. Every OTHER row keeps the debounced path
   * (still needed to avoid a render storm from an oscillating auto-height
   * widget), so this is scoped narrowly to the one row that is guaranteed to
   * keep resizing for the duration of the turn.
   */
  streamingIndex?: number
  /**
   * Is a turn producing output right now?
   *
   * Gates the AUTOMATIC bottom pin only; explicit pins (slot entry, the
   * jump-to-bottom pill, sending) are unaffected. Following means "keep me at
   * the end of a live turn", so with nothing running a reader who sits above the
   * bottom is not following, and pinning them there is a yank with no cause —
   * reported from a phone as the transcript springing back after a scroll up of
   * about a hundred pixels with nothing streaming. Broader than a streaming row
   * on purpose: a turn spends much of its life in tool calls, with no streaming
   * row named, and follow has to keep working there.
   *
   * Omitted = assume a run is live, which is the behaviour of every caller that
   * has no such signal to give.
   */
  runActive?: boolean
  /**
   * Called when the HISTORY-edge sentinel comes into view, alongside that
   * edge's window expansion: the top sentinel by default, the BOTTOM sentinel
   * with `followEdge: 'top'`. Lets the caller fetch history that lies behind
   * the loaded slice; the virtualizer itself only ever widens the window over
   * `items`.
   */
  onTopReached?: () => void
}

export interface VirtualItem<T> {
  /** Original item data — render this when `mounted` is true. */
  data: T
  /** Index in the source array. */
  index: number
  /** Stable key (from getKey) used for React reconciliation and cache. */
  key: string
  /** True when the item should render as a full React component. */
  mounted: boolean
  /** Measured (or estimated) height — placeholder size when !mounted. */
  height: number
}

export interface ScrollToIndexOptions {
  align?: 'start' | 'center' | 'end'
  behavior?: ScrollBehavior
}

/** Inclusive-start, exclusive-end item range retained while native selection
 * handles cross a virtualized transcript. */
export interface RetainedVirtualRange {
  start: number
  end: number
}

export interface UseVirtualChatReturn<T> {
  /** True when row `index` has a real (render-based) measurement cached. */
  farmIsMeasured: (index: number) => boolean
  /** True when the row is currently mounted in the live window (the
   *  ResizeObserver owns its height; the farm must skip it). */
  farmRowMounted: (index: number) => boolean
  /**
   * Background write-back from the off-screen measure farm. Identity-checked:
   * the write is dropped (returns false) when the key no longer matches the
   * item at `index` (a landing shifted indices mid-measure).
   */
  farmRecord: (index: number, key: string, px: number) => boolean
  /** Attach to the scroll container (`overflow-y: auto`). */
  scrollerRef: React.RefObject<HTMLDivElement | null>
  /** Attach to the inner content wrapper (sized to totalHeight). */
  contentRef: React.RefObject<HTMLDivElement>
  /** Top sentinel — attach for upward expansion detection. */
  topSentinelRef: React.RefObject<HTMLDivElement>
  /** Bottom sentinel — attach for downward expansion detection. */
  bottomSentinelRef: React.RefObject<HTMLDivElement>
  /** Trailing chrome — attach to the wrapper around content rendered BELOW the
   *  rows inside the scroller (a working footer, a survey card). The resize
   *  observer watches it so growth there is followed like tail growth; it is
   *  not a row and never enters the height cache. */
  trailingRef: React.RefObject<HTMLDivElement>
  /** Leading chrome — attach to the wrapper around content rendered ABOVE the
   *  rows in the top-down layout (the working footer, the preparing caption, a
   *  survey card). The resize observer watches it so a change there above a
   *  released reader is held by the measured residual; it is not a row and
   *  never enters the height cache. Leave it unattached in the bottom layout. */
  leadingRef: React.RefObject<HTMLDivElement>
  /** Move the scroller to `top` (clamped at 0) on the host's behalf, through
   *  the follow write chokepoint: the write is instant and recorded as the
   *  virtualizer's own, so its scroll event neither arms nor releases follow.
   *  For a host write that holds the reader's content still (the top-down
   *  dock padding shift, the side-panel slide hold), never for navigation. */
  holdScrollTop: (el: HTMLDivElement, top: number) => void
  /** Items to render — both mounted React components and placeholder rows. */
  virtualItems: VirtualItem<T>[]
  /** Pixel offset of the first virtual item (top spacer height). */
  offsetBefore: number
  /** Pixel height of all items after the window (bottom spacer height). */
  offsetAfter: number
  /** Total content height (top spacer + window + bottom spacer). */
  totalHeight: number
  /** Whether the scroller is within bottomThreshold of the live edge: the
   *  bottom by default, scrollTop 0 with `followEdge: 'top'`. */
  isAtBottom: boolean
  /** Live follow ("stick to bottom") state: true while the transcript is
   * auto-following output. Stable identity; reads the live value at call
   * time, so effect gates see flips that happened after the last render.
   * Distinct from `isAtBottom`: the user can be inside the at-bottom band
   * with follow released (they scrolled up a little to read). */
  getFollow: () => boolean
  /** Scroll the scroller so item `index` is visible. */
  scrollToIndex: (index: number, opts?: ScrollToIndexOptions) => void
  /** Scroll to the live edge (latest message): the bottom by default, the
   *  head window at scrollTop 0 with `followEdge: 'top'`. */
  scrollToBottom: (behavior?: ScrollBehavior) => void
  /** Ensure `index` is mounted (in the window) without scrolling — lets a
   * caller's DOM-based scroll target an off-window item. Returns `true` when
   * the target is FAR (off the current window by more than the near-jump
   * slack). By default a far target REPLACES the window, leaving an unmounted
   * gap, so callers teleport instead of gliding through blank space; with
   * `unionOnly` a far target mounts nothing and the caller steers toward it
   * itself, letting the window follow each write (see `estimateRowTop`). */
  mountIndex: (index: number, opts?: { unionOnly?: boolean }) => boolean
  /** Scroller-coordinate top of row `index` from the height index, mounted or
   * not. Unmeasured rows above it contribute estimates, so re-read it each
   * frame while steering toward an unmounted row. `null` with no scroller or
   * no items. */
  estimateRowTop: (index: number) => number | null
  /** Keep every item in `range` mounted until the caller clears it. This is
   * for a native text selection whose endpoints would otherwise be unmounted
   * while the user scrolls a selection handle. */
  retainRange: (range: RetainedVirtualRange | null) => void
  /** Ref callback used per-item to register ResizeObserver measurement. */
  measureRef: (index: number) => (el: HTMLElement | null) => void
  /** True while an anchored entry is still waiting for its row to hydrate, so a
   *  caller can cover the transcript instead of letting the reader watch it
   *  assemble and then jump. Always false for an entry with no saved anchor. */
  restoreGate: boolean
}
