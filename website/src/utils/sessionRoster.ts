import { isChatPageSurface } from './channelOrigin'

/**
 * The roster a session chip may be offered against.
 *
 * `SessionActions.sessions` decides whether a `/chat?sid=…` link or a bare slot
 * key renders as a live affordance at all, so what goes in it is a claim that
 * clicking will land somewhere: the destination is the unified chat view, and a
 * slot that view cannot render (`isChatPageSurface`) would chip to a session the
 * switch immediately clears. ABSENT is not the same as empty — see
 * `markdown/contexts.ts` — so a host that does not know which sessions exist
 * must withhold the map rather than pass one built from an unloaded snapshot.
 *
 * Shared because two pages now build it. `ChatPage` has always derived it from
 * its own `filteredSlots`; the Members page needs the identical answer for a
 * crewmate DM, whose chips navigate to that same page. Two independent copies of
 * "which slots may chip" is how the affordance and the destination drift apart.
 *
 * The value is the display title (falling back to the key), used for the chip's
 * tooltip only.
 */
export type RosterSlot = { key: string; title?: string; surface?: string; mode?: string }

export function sessionTitleRoster(slots: readonly RosterSlot[]): Map<string, string> {
  return new Map(
    slots
      .filter(s => isChatPageSurface(s.surface ?? s.mode))
      .map(s => [s.key, s.title || s.key] as const),
  )
}
