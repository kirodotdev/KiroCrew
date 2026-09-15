/** Attribution for a scroller viewport change: did the COMPOSER cause it?
 *
 *  The transcript scroller and the composer are siblings in one column, so the
 *  composer's height is exactly what the scroller does not get. Both of the
 *  viewport changes the transcript sees are therefore ambiguous by geometry alone
 *  and only separable by CAUSE:
 *
 *  - composer grows under the reader's own typing  -> scroller SHRINKS
 *  - a banner / attachment strip / queue band mounts -> scroller SHRINKS
 *
 *  The second must re-pin a bottom-flush follower, because a shrink raises the
 *  maximum scrollTop and no engine pushes a reader down — leaving them stranded
 *  above the bottom ("switching sessions doesn't land at the bottom"). The first
 *  must NOT: chasing it walks the transcript up by a line every few characters,
 *  which is the composer bounce reported from a real phone.
 *
 *  A timestamp rather than a subscription because the two ends are a leaf input
 *  component and a hook mounted by a different subtree, and the fact is genuinely
 *  page-global: at any instant either the composer just resized or it did not. */

/** How long a composer resize keeps explaining a viewport change.
 *
 *  A ResizeObserver callback runs before the next paint, so the honest gap is one
 *  frame; the window is wider to cover a coalesced batch and a slow frame under a
 *  streaming turn. Widening it further would start swallowing a genuine chrome
 *  shrink that happens to land just after a keystroke. */
export const COMPOSER_RESIZE_ATTRIBUTION_MS = 120

let lastComposerResizeAt = 0

/** Called by the composer's autosizer whenever it commits a NEW height. */
export function markComposerResize(now: number = Date.now()): void {
  lastComposerResizeAt = now
}

/** Did the composer resize recently enough to explain a viewport change now? */
export function composerExplainsViewportChange(now: number = Date.now()): boolean {
  return lastComposerResizeAt > 0 && now - lastComposerResizeAt <= COMPOSER_RESIZE_ATTRIBUTION_MS
}

/** Test seam — the module-level stamp outlives a single test otherwise. */
export function __resetComposerResizeMark(): void {
  lastComposerResizeAt = 0
}
