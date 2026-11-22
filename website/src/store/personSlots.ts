import { crewModeEnabled, crewModeView, liveMateThreads } from '../lib/crewMode'
import type { ChatSlot } from '../types'
import { selectSlotsWithLiveLastTs } from './slotLastTs'

/**
 * The person's own session list: every live slot except a Crew Mode root's
 * workers, which their root lists in its side panel, a live mate thread's
 * sessions its Bot does not show here, which its Sessions tab lists, and, with
 * `slack.show_in_session_list` off, conversations that started in Slack other
 * than the open one (`crewModeView`, `slackRowHidden`). The one
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
let personSlotsActive: string | null = null
let personSlots: ChatSlot[] = []
export function selectPersonSlots(state: Parameters<typeof selectSlotsWithLiveLastTs>[0]): ChatSlot[] {
  // Memoized on the live-row array itself, not on the state object, so a reader
  // re-renders only when the rows change and a stale state can never answer.
  const slots = selectSlotsWithLiveLastTs(state)
  // And on whether this build offers conductor mode, which decides whether crew members are left out.
  const crewMode = crewModeEnabled()
  // And on the open session, which keeps its row when the Slack list setting
  // leaves its conversation out (`slackRowHidden`).
  const active = state.chat?.activeSlot ?? null
  if (slots !== personSlotsOf || crewMode !== personSlotsCrewMode || active !== personSlotsActive) {
    personSlotsOf = slots
    personSlotsCrewMode = crewMode
    personSlotsActive = active
    personSlots = crewModeView(slots, false, undefined, active)
  }
  return personSlots
}

/** The folders that any live session of the full slot store is filed in. It
 *  counts the sessions the person's list leaves out (a live mate thread's, a
 *  crew worker's), because the server counts them when it refuses to delete a
 *  folder that is not empty. Memoized on the raw rows. */
let holdingOf: unknown = null
let holding: ReadonlySet<string> = new Set()
export function selectFoldersHoldingSessions(state: { dashboard: { slots: ChatSlot[] } }): ReadonlySet<string> {
  const slots = state.dashboard.slots
  if (slots !== holdingOf) {
    holdingOf = slots
    holding = new Set(slots.flatMap(s => (s.folder_id ? [s.folder_id] : [])))
  }
  return holding
}

/** The live mate threads of the full slot store, whose sessions leave the
 *  person's lists (`liveMateThreads`), for a list
 *  that `crewModeView` filters after the chat page left the mate threads out.
 *  Memoized on the raw rows, so a reader re-renders only when they change. */
let mateThreadsOf: unknown = null
let mateThreads: ReadonlySet<string> = new Set()
export function selectMateThreadsHidingSessions(state: { dashboard: { slots: ChatSlot[] } }): ReadonlySet<string> {
  const slots = state.dashboard.slots
  if (slots !== mateThreadsOf) {
    mateThreadsOf = slots
    mateThreads = liveMateThreads(slots)
  }
  return mateThreads
}
