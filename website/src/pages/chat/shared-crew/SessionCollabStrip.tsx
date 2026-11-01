/** The open session's collaboration state, above the composer: who else is
 *  here and this session's shared decisions. Accepted work is not listed here:
 *  a send the host holds for capacity shows on its own message bubble
 *  (`QueuedSendLabel`). Each part reads only when the Crew reports the
 *  capability behind it, and the whole strip renders nothing when there is
 *  nothing to say, so a single-operator Crew's chat is unchanged. It also
 *  reports this tab's presence for the session. */
import { useEffect } from 'react'
import { capabilityUsable, isUnknownPrincipal } from '../../../api/sharedCrewUi'
import ErrorNotice from '../../../components/ErrorNotice'
import {
  useSessionDecisions,
  useSessionPresence,
  useSessionSendState,
  useSessionTypingLease,
  useSharedCrewCapabilities,
  useSharedCrewMode,
  useSharedCrewPeople,
  useSharedCrewViewer,
  useTeamAttribution,
} from '../../../hooks/sharedCrew/useSharedCrew'
import { usePresenceReporter } from '../../../hooks/sharedCrew/usePresenceReporter'
import { i18nT } from '../../../i18n/t'
import { DecisionAttributionList } from './DecisionAttributionList'
import { othersPresent, PresenceChips } from './PresenceChips'

/** A composer `input` listener target: the strip tells the reporter about
 *  keystrokes without ever reading what was typed. */
export interface TypingSignal {
  subscribe(listener: () => void): () => void
  /** Fires when the composer's draft is sent, ending this tab's typing. */
  subscribeSent?(listener: (sessionKey: string | null) => void): () => void
}

export function SessionCollabStrip({ sessionKey, typing }: { sessionKey: string | null | undefined; typing?: TypingSignal }) {
  const caps = useSharedCrewCapabilities()
  const { capabilities } = caps
  const presenceOn = capabilityUsable(capabilities.presence)
  const attributionOn = capabilityUsable(capabilities.attribution)
  const viewer = useSharedCrewViewer(presenceOn || attributionOn)
  // Own MessageByline's shared reads here, once per chat even with the sidebar closed.
  const mode = useSharedCrewMode(attributionOn)
  const people = useSharedCrewPeople(attributionOn)
  const presence = useSessionPresence(sessionKey, presenceOn)
  const decisions = useSessionDecisions(sessionKey, attributionOn)
  // The revision this tab's next send is based on (first submit wins). Read for
  // every open session: the host settles every send against it, whichever
  // capabilities the Crew reports, and app or automation sends count too.
  const sendState = useSessionSendState(sessionKey)
  // The same cached read `useComposerTypingLock` locks the composer from; the
  // strip is where its failure shows.
  const typingLease = useSessionTypingLease(sessionKey, presenceOn)
  const { noteTyping, noteSent } = usePresenceReporter(sessionKey, presenceOn)
  useEffect(() => typing?.subscribe(noteTyping), [typing, noteTyping])
  useEffect(() => typing?.subscribeSent?.(noteSent), [typing, noteSent])

  const entries = presence.data?.entries ?? []
  // Only other people draw a chip: the viewer's own entry alone must not open
  // an empty band above the composer (it would shift the chat under the pointer).
  // Until the viewer read settles, the viewer's own entry cannot be told apart
  // from another person's, so no chip shows yet: a chip that flashes for the
  // viewer and then goes would resize the band and move the chat under the pointer.
  const viewerKnown = !(presenceOn || attributionOn) || !viewer.isPending
  const shown = viewerKnown && othersPresent(entries, viewer.data).length > 0
  // Who approved what is for a Crew with other people in it; alone, every
  // line would only name the owner's own token.
  const team = useTeamAttribution()
  const decisionItems = team ? decisions.data?.items ?? [] : []
  const viewerId = viewer.data && !isUnknownPrincipal(viewer.data) ? viewer.data.id : ''
  const failed = [caps, viewer, mode, people, presence, decisions, sendState, typingLease].find(query => query.isError)
  if (!sessionKey || (!failed && !shown && decisionItems.length === 0)) return null
  return (
    <>
      {failed && <ErrorNotice message={failed.error instanceof Error ? failed.error.message : i18nT('sharedCrew.session_state_failed')} variant="inline" askAgent testId="session-collab-error" />}
      {(shown || decisionItems.length > 0) && (
        <section
          className="w-full px-3 py-1 flex flex-col gap-1 min-w-0"
          aria-label={i18nT('sharedCrew.collab_region')}
          data-testid="session-collab-strip"
        >
          {shown && <PresenceChips entries={entries} viewer={viewer.data} />}
          <DecisionAttributionList items={decisionItems} viewerId={viewerId} />
        </section>
      )}
    </>
  )
}
