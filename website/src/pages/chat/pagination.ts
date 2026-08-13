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
