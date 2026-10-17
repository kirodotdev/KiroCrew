/**
 * One-shot handoff: "the library just created this blank document for you".
 *
 * The artifacts library creates an empty document and navigates to its detail
 * page, which then opens the editor and — if you walk away without naming it or
 * writing anything — cleans it up again.
 *
 * That hand-off deliberately lives in module memory rather than router state,
 * and is consumed exactly once. Router state survives a page RELOAD, which would
 * mean re-arming the cleanup on a document you have since come back to and
 * worked on. Module memory does not survive a reload, so a returning visit can
 * never be treated as a fresh blank — the only page instance that can clean up a
 * document is the one that watched it get created.
 */
let pending: { slug: string; createdName: string } | null = null

/**
 * Called by the library immediately before navigating to the new document.
 *
 * `createdName` is the placeholder the library actually stored. It travels with
 * the hand-off rather than being re-derived on departure, because the untitled
 * placeholder is LOCALISED: re-translating it at leave time means a language
 * change in between makes the stored name look like a deliberate rename, and the
 * document is kept with the user's unsaved draft silently dropped.
 */
export function markJustCreatedBlank(slug: string, createdName: string): void {
  pending = { slug, createdName }
}

/** Claim the hand-off for `slug`, returning the name it was created with. */
export function consumeJustCreatedBlank(slug: string): string | null {
  if (slug && pending && pending.slug === slug) {
    const { createdName } = pending
    pending = null
    return createdName
  }
  return null
}

/** Test-only reset. */
export function __resetJustCreatedBlank(): void {
  pending = null
}
