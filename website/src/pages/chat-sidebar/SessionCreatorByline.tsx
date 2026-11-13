/** The session's creator, inline on the row's agent/meta line after the agent
 *  name (AUTOSDE session-row-fixed-height): plain muted text that yields first
 *  when the line is tight, never a new stacked line.
 *
 *  It reads "You" for the verified viewer, a person's name, the trigger behind
 *  unattended work ("Scheduled: <job>"), or Unattributed. It renders nothing for
 *  a slot the gateway sent no attribution for, and nothing on a personal Crew
 *  (`SharedCrewBylineContext.hidden`), where every session is the operator's.
 *
 *  After it, under the same rule, the initials of the people who have written in
 *  the session (`participants`, #35145). */
import { createContext, useContext } from 'react'
import type { PublicPrincipal, SessionAttribution } from '../../api/sharedCrew'
import { ParticipantChips } from '../chat/shared-crew/ParticipantChips'
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

export function SessionCreatorByline({ attribution, participants }: { attribution?: SessionAttribution | null; participants?: readonly PublicPrincipal[] | null }) {
  const { hidden } = useContext(SharedCrewBylineContext)
  if (hidden) return null
  return (
    <>
      {attribution && <CreatorName attribution={attribution} />}
      <ParticipantChips participants={participants} testId="session-participants" />
    </>
  )
}

function CreatorName({ attribution }: { attribution: SessionAttribution }) {
  const { viewerId } = useContext(SharedCrewBylineContext)
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
