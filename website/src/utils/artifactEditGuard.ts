/**
 * Which artifacts currently have an unsaved edit buffer open.
 *
 * The WebSocket transport must not refresh an artifact's cached content while a
 * human is mid-edit. Refetching swaps the editor's *baseline* (`artifact.content`)
 * while the buffer (`editedContent`) keeps the user's older text, so the next Save
 * writes the buffer over whatever arrived in between — silently losing the agent's
 * update. Skipping the refresh keeps the editor coherent; the page picks the new
 * content up on save or cancel, when the buffer is no longer at risk.
 *
 * This is a module-level registry rather than Redux or context because the reader
 * is `useWebSocket` — a global transport hook with no relationship to the artifact
 * page's local component state, and which must make the decision synchronously
 * inside the frame handler.
 *
 * It is intentionally NOT conflict resolution. It only prevents one side from
 * clobbering the other while both are live; a real merge/"content changed
 * underneath you" flow is separate, larger work.
 */
const editing = new Set<string>()

/** Mark/unmark `slug` as having an unsaved edit buffer. */
export function setArtifactEditing(slug: string, isEditing: boolean): void {
  if (!slug) return
  if (isEditing) editing.add(slug)
  else editing.delete(slug)
}

/** True while `slug` has an unsaved edit buffer open in this window. */
export function isArtifactEditing(slug: string): boolean {
  return editing.has(slug)
}

/** Test seam — no production caller. */
export function __resetArtifactEditing(): void {
  editing.clear()
}
