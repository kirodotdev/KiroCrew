/** Whether this tab's composer is locked because someone else is typing in the
 *  open shared session, and who. `null` means unlocked: no lease, the lease is
 *  this person's own (any of their tabs), the Crew does not report presence, or
 *  the viewer is not known. A single operator's Crew never locks.
 *
 *  With the Shared Crew preview on (`sharedCrewPreviewOn`), a locked tab may take
 *  the lease over. The tab then re-reads the lease rather than caching the
 *  takeover answer, because a newer lease frame may arrive before that answer
 *  and must not be overwritten. A tab the lease was taken from shows who took it; its draft
 *  is never touched. */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../../api/client'
import { leaseLocks, useRefusedLease } from '../../../api/typingLease'
import { capabilityUsable, principalInitials, principalName, sharedCrewPreviewOn } from '../../../api/sharedCrewUi'
import { sharedCrewKeys } from '../../../hooks/sharedCrew/sharedCrewSocket'
import { useSessionTypingLease, useSharedCrewCapabilities, useSharedCrewViewer } from '../../../hooks/sharedCrew/useSharedCrew'
import { i18nT } from '../../../i18n/t'

export interface ComposerTypingLock {
  holderId: string
  holderName: string
  holderInitials: string
  /** The holder took the lease over from this viewer. */
  tookOver: boolean
  /** Take the lease over; present only with the Shared Crew preview on. */
  onTakeOver?: () => void
  takeOverPending?: boolean
  /** Why the last takeover failed, or ''. */
  takeOverError?: string
}

export function useComposerTypingLock(sessionKey: string | null | undefined): ComposerTypingLock | null {
  // SessionCollabStrip reports capability, viewer and lease failures once for
  // this composer; cached values keep the typing lock during a failed refresh.
  const { capabilities } = useSharedCrewCapabilities()
  const presenceOn = capabilityUsable(capabilities.presence)
  const previewOn = sharedCrewPreviewOn(capabilities)
  const viewer = useSharedCrewViewer(presenceOn)
  const lease = useSessionTypingLease(sessionKey, presenceOn)
  const refused = useRefusedLease(sessionKey)
  const queryClient = useQueryClient()
  const { refetch } = lease
  // A refusal may arrive after a release frame. Re-read server truth rather
  // than using response arrival time to order lease state.
  useEffect(() => {
    if (presenceOn && sessionKey && refused) void refetch()
  }, [presenceOn, sessionKey, refused, refetch])
  const current = lease.data
  const viewerData = viewer.data
  const holderId = current?.holder?.id ?? ''

  const pending = useRef(false)
  const [takeOverPending, setTakeOverPending] = useState(false)
  const [takeOverError, setTakeOverError] = useState('')
  // A failure belongs to the lease it was pressed against.
  useEffect(() => { setTakeOverError('') }, [sessionKey, holderId])
  const takeOver = useCallback(async () => {
    if (!sessionKey || pending.current) return
    pending.current = true
    setTakeOverPending(true)
    setTakeOverError('')
    try {
      await api.sharedCrewTakeOverTypingLease(sessionKey)
      await queryClient.invalidateQueries({ queryKey: sharedCrewKeys.typingLease(sessionKey), exact: true })
    } catch (e) {
      setTakeOverError(e instanceof Error && e.message ? e.message : i18nT('sharedCrew.take_over_failed'))
    } finally {
      pending.current = false
      setTakeOverPending(false)
    }
  }, [sessionKey, queryClient])

  return useMemo(() => {
    if (!presenceOn || !current || !leaseLocks(current, viewerData) || !current.holder) return null
    const holder = current.holder
    return {
      holderId: holder.id,
      holderName: principalName(holder) || i18nT('sharedCrew.unattributed'),
      holderInitials: principalInitials(holder),
      tookOver: !!viewerData?.id && !!current.taken_over_from_ids?.includes(viewerData.id),
      ...(previewOn ? { onTakeOver: () => { void takeOver() }, takeOverPending, takeOverError } : {}),
    }
  }, [presenceOn, previewOn, current, viewerData, takeOver, takeOverPending, takeOverError])
}
