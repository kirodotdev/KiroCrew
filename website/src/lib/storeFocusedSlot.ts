/** The slot the store says this window shows: a crewmate thread mounted on the
 *  Crewmates page, else the active chat.
 *
 *  The Crewmates page shows a crewmate's thread without moving
 *  `chat.activeSlot` (that field belongs to the Sessions page's switchSlot), so
 *  a reader of `chat.activeSlot` alone misses the thread on screen. The focus
 *  announcement (`hooks/websocket/attention.ts`) and speech
 *  (`hooks/websocket/voicePlayback.ts`) both read this one resolver. */
import { store } from '../store'
import { getMountedMemberThreadSlot } from './mountedMemberThread'

export function storeFocusedSlot(): string | null {
  return getMountedMemberThreadSlot() || store.getState().chat.activeSlot
}
