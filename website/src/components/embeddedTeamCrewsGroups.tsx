/**
 * The Remote Crews group an SSH Crew's own switcher shows, from the rows its
 * window relays (lib/paneRelayCrews.ts). Lazy, as the top bar's group is, so the
 * relay rows' code stays out of the app core.
 */
import { lazy, Suspense } from 'react'
import type { PaneRelayCrew } from '../lib/paneRelayCrews'

const EmbeddedTeamCrewsGroup = lazy(() => import('./TeamCrewsGroup').then(m => ({ default: m.EmbeddedTeamCrewsGroup })))

/** The switcher's extra group for these rows, or none when there are none (an older host, the relay viewer off). */
export function embeddedTeamCrewsGroups(crews: PaneRelayCrew[] | undefined) {
  if (!crews?.length) return undefined
  return (close: () => void, headed?: boolean) => (
    <Suspense fallback={null}><EmbeddedTeamCrewsGroup crews={crews} close={close} headed={headed} /></Suspense>
  )
}
