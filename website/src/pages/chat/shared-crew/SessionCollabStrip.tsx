/** Reports this tab's presence for the open session and says, once, when a
 *  shared-session read fails. It draws nothing otherwise: who is here shows in
 *  the composer's context row (`SessionPresence`), who is typing in the locked
 *  composer (`TypingLockBanner`). */
import { useEffect } from 'react'
import { capabilityUsable } from '../../../api/sharedCrewUi'
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
} from '../../../hooks/sharedCrew/useSharedCrew'
import { usePresenceReporter } from '../../../hooks/sharedCrew/usePresenceReporter'
import { i18nT } from '../../../i18n/t'

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
  // Read here so its failure is said once; `useResolvedElsewhere` and `useRaisedBy` use the cache.
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

  const failed = [caps, viewer, mode, people, presence, decisions, sendState, typingLease].find(query => query.isError)
  if (!sessionKey || !failed) return null
  return <ErrorNotice message={failed.error instanceof Error ? failed.error.message : i18nT('sharedCrew.session_state_failed')} variant="inline" askAgent testId="session-collab-error" />
}
