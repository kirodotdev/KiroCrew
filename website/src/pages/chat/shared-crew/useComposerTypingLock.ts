/** Whether this tab's composer is locked because someone else is typing in the
 *  open shared session, and who. `null` means unlocked: no lease, the lease is
 *  this person's own (any of their tabs), the Crew does not report presence, or
 *  the viewer is not known. A single operator's Crew never locks. */
import { useEffect, useMemo } from 'react'
import { leaseLocks, useRefusedLease } from '../../../api/typingLease'
import { capabilityUsable, principalInitials, principalName } from '../../../api/sharedCrewUi'
import { useSessionTypingLease, useSharedCrewCapabilities, useSharedCrewViewer } from '../../../hooks/sharedCrew/useSharedCrew'
import { i18nT } from '../../../i18n/t'

export interface ComposerTypingLock {
  holderId: string
  holderName: string
  holderInitials: string
}

export function useComposerTypingLock(sessionKey: string | null | undefined): ComposerTypingLock | null {
  // SessionCollabStrip reports capability, viewer and lease failures once for
  // this composer; cached values keep the typing lock during a failed refresh.
  const { capabilities } = useSharedCrewCapabilities()
  const presenceOn = capabilityUsable(capabilities.presence)
  const viewer = useSharedCrewViewer(presenceOn)
  const lease = useSessionTypingLease(sessionKey, presenceOn)
  const refused = useRefusedLease(sessionKey)
  const { refetch } = lease
  // A refusal may arrive after a release frame. Re-read server truth rather
  // than using response arrival time to order lease state.
  useEffect(() => {
    if (presenceOn && sessionKey && refused) void refetch()
  }, [presenceOn, sessionKey, refused, refetch])
  const current = lease.data
  const viewerData = viewer.data
  return useMemo(() => {
    if (!presenceOn || !current || !leaseLocks(current, viewerData) || !current.holder) return null
    const holder = current.holder
    return {
      holderId: holder.id,
      holderName: principalName(holder) || i18nT('sharedCrew.unattributed'),
      holderInitials: principalInitials(holder),
    }
  }, [presenceOn, current, viewerData])
}
