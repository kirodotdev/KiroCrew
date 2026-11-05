/** The session's creator, inline on the row's agent/meta line after the agent
 *  name (AUTOSDE session-row-fixed-height): plain muted text that yields first
 *  when the line is tight, never a new stacked line.
 *
 *  It reads "You" for the verified viewer, a person's name, the trigger behind
 *  unattended work ("Scheduled: <job>"), or Unattributed. It renders nothing for
 *  a slot the gateway sent no attribution for, and nothing on a personal Crew
 *  (`SharedCrewBylineContext.hidden`), where every session is the operator's. */
import { createContext, useContext } from 'react'
import type { SessionAttribution } from '../../api/sharedCrew'
import { creatorDisplay, creatorTitle } from '../../api/sharedCrewMode'
import { i18nT } from '../../i18n/t'

export interface SharedCrewBylineView {
  /** True on a personal Crew, or until the Crew's mode is known. */
  hidden: boolean
  /** The verified viewer's principal id, for the "You" label; `''` when unknown. */
  viewerId: string
}

/** Provided by the session sidebar from the Crew's mode (`useSharedCrewListView`). */
export const SharedCrewBylineContext = createContext<SharedCrewBylineView>({ hidden: false, viewerId: '' })

export function creatorLabel(attribution: SessionAttribution, viewerId?: string): string {
  return creatorDisplay(attribution, viewerId)
}

export function SessionCreatorByline({ attribution }: { attribution?: SessionAttribution | null }) {
  const { hidden, viewerId } = useContext(SharedCrewBylineContext)
  if (!attribution || hidden) return null
  const label = creatorDisplay(attribution, viewerId)
  const spoken = attribution.trigger && attribution.creator.kind !== 'human'
    ? i18nT('sharedCrew.started_by', { name: label })
    : i18nT('sharedCrew.created_by', { name: label })
  return (
    <span
      data-testid="session-creator-byline"
      className="min-w-0 max-w-[40%] truncate font-normal text-muted"
      title={creatorTitle(attribution, viewerId)}
    >
      <span aria-hidden>{'\u00A0·\u00A0'}</span>
      <span className="sr-only">{spoken}</span>
      <span aria-hidden>{label}</span>
    </span>
  )
}
