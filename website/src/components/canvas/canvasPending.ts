/**
 * The canvas on screen, as the artifact page's own controls see it.
 *
 * A canvas saves itself, so the page's `dirty` flag (an edit buffer that
 * differs from the stored content) never covers it. The page's exits and its
 * Snapshot control ask here instead: `flushCanvas` sends whatever is waiting
 * and says whether the stored content now matches what the canvas shows, and
 * `discardCanvas` drops edits the person chose to lose. One canvas per slug is
 * mounted at a time; an artifact with no canvas mounted answers "nothing
 * waiting".
 */

export type CanvasPending = {
  /** Save what is waiting now. Resolves `true` once nothing is left unsaved. */
  flush: () => Promise<boolean>
  /** Forget this page's unsaved edits and show the stored content. */
  discard: () => void
  /** The document the canvas shows now, saved or not; `null` on a past version. */
  document: () => string | null
}

const mounted = new Map<string, CanvasPending>()

/** Register the mounted canvas for `slug`; the returned function unregisters it. */
export function registerCanvasPending(slug: string, pending: CanvasPending): () => void {
  mounted.set(slug, pending)
  return () => {
    if (mounted.get(slug) === pending) mounted.delete(slug)
  }
}

/** Save the canvas's waiting edits. `true` when nothing unsaved remains, or no canvas is mounted. */
export function flushCanvas(slug: string): Promise<boolean> {
  return mounted.get(slug)?.flush() ?? Promise.resolve(true)
}

/** Drop the canvas's unsaved edits, if one is mounted. */
export function discardCanvas(slug: string): void {
  mounted.get(slug)?.discard()
}

/**
 * What the mounted canvas for `slug` shows right now, for an export that must
 * match the screen (a download), or `null` when none is mounted or a past
 * version is open. Synchronous, so it can run inside the click that asked.
 */
export function canvasDocument(slug: string): string | null {
  return mounted.get(slug)?.document() ?? null
}
