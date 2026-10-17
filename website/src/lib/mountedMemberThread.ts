/**
 * The confirmed member thread mounted on the Crewmates route. The focus sender
 * reads it only while the document is visible. Unlike viewedThread, window focus
 * is intentionally irrelevant: this value is authenticated presence for the
 * return edge, not unread state.
 */
let mountedMemberThread: string | null = null
const listeners = new Set<() => void>()

export function getMountedMemberThreadSlot(): string | null {
  return mountedMemberThread
}

export function setMountedMemberThreadSlot(slot: string): void {
  if (!slot || mountedMemberThread === slot) return
  mountedMemberThread = slot
  for (const listener of listeners) listener()
}

export function clearMountedMemberThreadSlot(slot?: string): void {
  if (slot && mountedMemberThread !== slot) return
  if (mountedMemberThread === null) return
  mountedMemberThread = null
  for (const listener of listeners) listener()
}

export function subscribeMountedMemberThread(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}
