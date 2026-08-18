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
export function shouldPaginateOlder({ loadingOlder, slotHasMore }: PaginateOlderInput): boolean {
  return !loadingOlder && slotHasMore
}

export interface ForkEligibilityInput {
  isStreaming: boolean
  isInject: boolean
  slotHasMore: boolean
  /** Does the paging cursor describe the slot currently on screen? */
  cursorIsForActiveSlot: boolean
}

/**
 * Whether forking is safe at this row.
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
