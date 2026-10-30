import { crewModeEnabled, crewModeView } from '../lib/crewMode'
import type { ChatSlot } from '../types'
import { selectSlotsWithLiveLastTs } from './slotLastTs'

/**
 * The person's own session list: every live slot except a Crew Mode root's
 * workers, which their root lists in its side panel (`crewModeView`). The one
 * rule every place that lists sessions for the person reads: the chat sidebar
 * (through its `slots` prop, filtered by the same function), the Sessions page,
 * the command palette's Recents and an artifact's Send to session. The tab strip
 * and a worker's own open view are not lists of the person's sessions and read
 * the raw rows. Rows carry their live `last_ts` (`selectSlotsWithLiveLastTs`),
 * so a reader that orders by message recency keeps moving mid-turn.
 * Re-exported from `dashboardSlice`, the path callers import.
 */
let personSlotsOf: ChatSlot[] | null = null
let personSlotsCrewMode = false
let personSlots: ChatSlot[] = []
export function selectPersonSlots(state: Parameters<typeof selectSlotsWithLiveLastTs>[0]): ChatSlot[] {
  // Memoized on the live-row array itself, not on the state object, so a reader
  // re-renders only when the rows change and a stale state can never answer.
  const slots = selectSlotsWithLiveLastTs(state)
  // And on the Crew Mode preview, which decides whether crew members are left out.
  const crewMode = crewModeEnabled()
  if (slots !== personSlotsOf || crewMode !== personSlotsCrewMode) {
    personSlotsOf = slots
    personSlotsCrewMode = crewMode
    personSlots = crewModeView(slots)
  }
  return personSlots
}
