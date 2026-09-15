/**
 * Where an async-captured/uploaded file should land.
 *
 * Screen capture (getDisplayMedia), cropping, and upload are all async, and the
 * user may switch sessions in between. The file must land in the slot that
 * INITIATED the action, not whatever slot happens to be active when it resolves
 * — otherwise a screenshot started in session A attaches to session B after a
 * mid-capture switch. This pure helper encodes that routing so it's regression-
 * tested independently of the async plumbing.
 */
export type FileLanding =
  | { target: 'pending' }               // initiating slot still on screen → live composer
  | { target: 'draft'; slot: string }   // switched away → initiating slot's persisted draft
  | { target: 'drop' }                  // no initiating slot → nowhere

export function fileLandingSlot(
  requestSlot: string | null | undefined,
  activeSlot: string | null | undefined,
): FileLanding {
  if (!requestSlot) return { target: 'drop' }
  return requestSlot === activeSlot ? { target: 'pending' } : { target: 'draft', slot: requestSlot }
}
