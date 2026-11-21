/**
 * The Remote Crews group in an SSH Crew's own switcher. With an SSH Crew in
 * front, the switcher on screen is the one that Crew's page draws
 * (EmbeddedInstanceTabBar), fed only by the `mc-host-model` this window posts
 * to it. So this window adds its relay rows to that model, and the pane sends a
 * pick back as `mc-switch-instance` with a `relay:<endpointId>` id, which opens
 * only a Crew this window lists or has open.
 *
 * Only what a row shows crosses: the endpoint ID, the name, the type, the
 * relay's state words and when it was last seen, never the Crew's URL, a token
 * or the account. The pane is a Crew this person chose to connect to over SSH,
 * which this window already sends its SSH Crew list the same way.
 */
import type { RelayCrewEntry } from '../api/client'
import type { AppDispatch } from '../store'
import { relayPaneOpened, type RelayTab } from '../store/instancesSlice'

/** The chat page's query that opens a relay Crew from the top bar (TeamCrewsGroup's
 *  `useOpenFromNote`), as a "You've been added" note's Open does (`relay_viewer/crew_notices.py`). */
export const OPEN_RELAY_CREW_PARAM = 'relay_crew'

/** The id prefix a relay row carries in a switcher. */
export const PANE_RELAY_PREFIX = 'relay:'

/** One relay row as the host model carries it. */
export type PaneRelayCrew = Omit<RelayCrewEntry, 'crewUrl' | 'unseen'> & {
  /** Its pane is open in this window. */
  open: boolean
  /** Its pane is the one in front. */
  current: boolean
}

/** The rows this window sends a pane: every listed Crew, in the list's order. */
export function paneRelayCrews(crews: readonly RelayCrewEntry[], tabs: readonly RelayTab[], active: string | null): PaneRelayCrew[] {
  return crews.map(c => ({
    endpointId: c.endpointId,
    name: c.name,
    crewType: c.crewType,
    presence: c.presence,
    relayConnectionStatus: c.relayConnectionStatus,
    lastSeenAt: c.lastSeenAt ?? null,
    open: tabs.some(t => t.id === c.endpointId),
    current: active === c.endpointId,
  }))
}

const str = (v: unknown) => (typeof v === 'string' ? v : '')

/** Narrow the untrusted `relayCrews` field of a host model, element by element.
 *  An older host sends none, which reads as no rows. */
export function parsePaneRelayCrews(raw: unknown): PaneRelayCrew[] {
  if (!Array.isArray(raw)) return []
  return raw
    .filter((r): r is Record<string, unknown> => !!r && typeof r === 'object')
    .map(r => ({
      endpointId: str(r.endpointId),
      name: str(r.name),
      crewType: str(r.crewType),
      presence: str(r.presence),
      relayConnectionStatus: str(r.relayConnectionStatus),
      lastSeenAt: typeof r.lastSeenAt === 'number' && Number.isFinite(r.lastSeenAt) ? r.lastSeenAt : null,
      open: r.open === true,
      current: r.current === true,
    }))
    .filter(r => r.endpointId)
}

/** A pane's pick of `relay:<endpointId>`: an open tab comes forward, a listed Crew
 *  opens through `open`, and any other ID is ignored. Answers whether it was a relay pick. */
export function pickRelayFromPane(
  id: unknown,
  crews: readonly RelayCrewEntry[],
  tabs: readonly RelayTab[],
  dispatch: AppDispatch,
  open: (endpointId: string) => void,
): boolean {
  if (typeof id !== 'string' || !id.startsWith(PANE_RELAY_PREFIX)) return false
  const endpointId = id.slice(PANE_RELAY_PREFIX.length)
  const tab = tabs.find(t => t.id === endpointId)
  if (tab) {
    dispatch(relayPaneOpened(tab))
    return true
  }
  const crew = crews.find(c => c.endpointId === endpointId)
  if (crew) open(crew.endpointId)
  return true
}
