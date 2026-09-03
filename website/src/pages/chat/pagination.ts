// Pure predicate for the "load older history" trigger, shared by the page and its
// tests so a test cannot keep passing against logic the real gate no longer uses.

export interface PaginateOlderInput {
  /** A fetch for older messages is already in flight. */
  loadingOlder: boolean
  /** The server reported unloaded history behind the currently loaded slice. */
  slotHasMore: boolean
}

/**
 * Whether a top-of-transcript signal should fetch the next page of older messages.
 *
 * Scroll provenance is deliberately not checked: an unrequested page is not
 * observable, because the prepend is compensated and the reading position holds.
 *
 * Repeated fetches are prevented structurally rather than by this gate:
 *
 *  - the observer reports intersection *transitions* against a sentinel outside
 *    the virtualised window, and is not re-created per commit, so one that merely
 *    stays visible produces no further callback;
 *  - a prepend pushes that sentinel far above the viewport;
 *  - `loadingOlder` closes this gate mid-fetch, and the thunk's own `condition`
 *    refuses a second dispatch besides.
 */
export function shouldPaginateOlder({ loadingOlder, slotHasMore }: PaginateOlderInput): boolean {  return !loadingOlder && slotHasMore
}

export interface ForkEligibilityInput {
  isStreaming: boolean
  isInject: boolean
  slotHasMore: boolean
  /** Does the paging cursor describe the slot currently on screen? */
  cursorIsForActiveSlot: boolean
}

/**
 * Whether an index-only fork is safe at this row.
 *
 * Rows carrying a stable server `meta.mid` bypass this positional fallback: the
 * fork endpoint resolves their cutoff against full chained history. This predicate
 * remains the fail-closed path for legacy rows that have no server identity.
 *
 * The fork index is an index into FULL history, while the rendered index counts
 * only the loaded window, so the two agree only when the window starts at the
 * beginning of history. `!slotHasMore` says the server reported nothing older.
 *
 * `cursorIsForActiveSlot` is the load-bearing half. A slot switch installs a
 * CACHED window synchronously and nulls the cursor key, but leaves `slotHasMore`
 * describing the slot being left. Switching from a short chat to a cached long
 * one therefore pairs a tail-only window with a stale `slotHasMore === false`,
 * and forking there would silently cut at the wrong message. Requiring the
 * cursor to describe the active slot closes that window.
 */
export function canForkAtWindow({ isStreaming, isInject, slotHasMore, cursorIsForActiveSlot }: ForkEligibilityInput): boolean {
  if (isStreaming || isInject) return false
  return !slotHasMore && cursorIsForActiveSlot
}

export interface SearchScopeInput {
  /** The server reported unloaded history behind the currently loaded slice. */
  slotHasMore: boolean
  /** Does the paging cursor describe the slot currently on screen? */
  cursorIsForActiveSlot: boolean
}

/**
 * Whether search results cover only part of the conversation, so the count has to
 * say so rather than read as complete.
 *
 * `slotHasMore` is the direct signal, but it is trustworthy only when the cursor
 * describes the slot on screen: a cached switch installs the incoming window
 * synchronously and nulls the cursor key while leaving `slotHasMore` describing the
 * slot being LEFT, so a tail-only window can carry a stale `false` and search would
 * announce "N results" as though it had seen everything.
 *
 * The same trust question `canForkAtWindow` answers, at the opposite polarity:
 * forking must REFUSE where a count must merely QUALIFY itself.
 */
export function searchScopeIsLimited({ slotHasMore, cursorIsForActiveSlot }: SearchScopeInput): boolean {
  return !cursorIsForActiveSlot || slotHasMore
}

/** Pages the top-of-transcript walk may issue on ONE expression of intent. */
export const OLDER_WALK_MAX_PAGES_PER_INPUT = 4

/**
 * Whether the top-of-transcript walk may issue another page.
 *
 * The walk exists so "load to the beginning" is not one page per manual climb:
 * a landing's own compensation moves scrollTop thousands of px, which would
 * otherwise throw the reader off the near-top gate after every page. So the
 * walk stays alive while the newest landing postdates the newest input
 * (`walking`).
 *
 * That latch SELF-PERPETUATES: each page it issues produces a landing, which
 * re-establishes the very condition keeping it alive. One wheel event was
 * therefore enough to walk an entire multi-megabyte archive with no further
 * input — a permanent "load previous" spinner and a session that pages while
 * the user is trying to switch away from it. `sawRealInput` bounds who can
 * start a walk; only a page BUDGET bounds how far it goes. The budget is per
 * expression of intent: fresh input refills it, so a reader who keeps climbing
 * still gets a continuous walk.
 */
export function shouldContinueOlderWalk(input: {
  sawRealInput: boolean
  nearTop: boolean
  walking: boolean
  pagesSinceInput: number
  maxPages?: number
}): boolean {
  const { sawRealInput, nearTop, walking, pagesSinceInput } = input
  const maxPages = input.maxPages ?? OLDER_WALK_MAX_PAGES_PER_INPUT
  if (!sawRealInput) return false
  if (pagesSinceInput >= maxPages) return false
  return nearTop || walking
}

/**
 * Is the "load earlier messages" control inside the transcript's viewport?
 *
 * FIRST-PRINCIPLES ADMISSION RULE for every AUTOMATIC older-history fetch. The
 * reader may only be paged backwards when the control that offers it is on their
 * screen. Manual activation (the click itself) is not subject to this.
 *
 * Why this replaces a pile of per-trigger guards: every automatic trigger in the
 * transcript keys off a proxy for "the reader wants history" — the mounted
 * window's start index, the scroller's height against the viewport's, a quiet
 * timer, a one-way input latch — and every one of those proxies has a second
 * cause that is not the reader. Measured on a real phone, in this order: a page
 * landing's own compensation write refreshing the quiet timer; a geometry
 * transient during a per-keystroke re-render; and the soft keyboard closing,
 * which grows the viewport by its whole height so the window extends upward and
 * walks `start` down across the lead with nobody travelling anywhere.
 *
 * The control's visibility has no second cause. It is at the head of the
 * transcript and it unmounts with the virtualized window, so it can only be on
 * screen when the reader has actually arrived at the top — and it subsumes the
 * special case the old geometry test needed, because a transcript too short to
 * scroll shows the control without anyone scrolling at all.
 *
 * Rects are passed in rather than read here so the rule itself is pure.
 */
export function earlierAffordanceInView(
  bar: { top: number; bottom: number } | null,
  viewport: { top: number; bottom: number },
  leadPx: number = EARLIER_ADMISSION_LEAD_PX,
): boolean {
  if (!bar) return false
  // Any overlap counts: a partially-revealed control is a control the reader can
  // see and press, and demanding full containment would refuse a bar clipped by
  // one pixel of the fade band.
  //
  // The lead extends the acceptance region UPWARD only, so the page lands before
  // the reader arrives rather than under their eyes (see the constant).
  return bar.bottom > viewport.top - leadPx && bar.top < viewport.bottom
}

/** How far ABOVE the viewport the control still counts as reachable.
 *
 *  Without a lead, the rule authorizes a fetch only once the reader is at the very
 *  top edge — and then the prepend happens IN VIEW. That is a real regression even
 *  with perfect compensation: the reader watches the rows they were reading get
 *  re-laid-out ("it slides up a little now; it used to stay put and the old
 *  messages just appeared"). The trigger this rule replaced fired at a lead of
 *  OLDER_PREFETCH_START_ROWS rows for exactly this reason, so the landing happened
 *  off-screen.
 *
 *  It does NOT weaken the rule the lead is attached to: the control still has to be
 *  within about one screen of the reader, which a reader at the live end of a long
 *  transcript never is, and a geometry transient, a viewport growth or a re-render
 *  cannot manufacture. Same idea as VIEWPORT_PRELOAD_MARGIN_PX for row staging. */
export const EARLIER_ADMISSION_LEAD_PX = 600

/** Selector for the control the rule above measures. */
export const EARLIER_BAR_SELECTOR = '[data-testid="load-earlier-messages"]'
