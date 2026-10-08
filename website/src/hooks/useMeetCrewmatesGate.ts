import { useCallback, useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { type MemberRosterRow } from '../api/client'
import { membersRosterQuery } from '../api/membersQuery'
import { CREWMATES_PAGE_ENTERED_EVENT, START_MEET_CREWMATES_EVENT } from '../components/MeetCrewmatesFlow'
import { pendingMate } from '../lib/assistantMember'
import { PREVIEW_CREW } from '../utils/previewFlags'
import { usePreviewFlag } from './usePreviewFlag'
import { useTheme } from './useTheme'

/** No crewmate beyond the always-present `default` row. The first crewmate
 *  (key `mate`) IS a crewmate, so a roster holding it is not empty: the page
 *  opens it instead. */
export function hasNoCrewmates(rows: readonly Pick<MemberRosterRow, 'name'>[] | undefined): boolean {
  return Array.isArray(rows) && rows.every(r => r.name === 'default')
}

/** The first-run host owns automatic entry; the page owns explicit creation.
 * Completion and dismissal both persist the workspace's seen flag. */
export function useMeetCrewmatesGate({ automatic = true, explicit = true } = {}) {
  const { onboarded, importOnboarded, privacyAcked, themeBootReady,
    crewmatesOnboarded, crewmatesFlowSeen, markCrewmatesOnboarded } = useTheme()
  const crewPreview = usePreviewFlag(PREVIEW_CREW)
  const firstRunDone = themeBootReady && onboarded && importOnboarded && privacyAcked
  const tourEndDue = automatic && crewPreview && firstRunDone && !crewmatesOnboarded
  const pageEntryDue = automatic && firstRunDone && !crewmatesFlowSeen
  const [open, setOpen] = useState(false)
  const [persistFailed, setPersistFailed] = useState(false)
  const closedThisSessionRef = useRef(false)
  const [pageEntered, setPageEntered] = useState(false)
  useEffect(() => {
    if (!explicit) return
    const start = () => setOpen(true)
    window.addEventListener(START_MEET_CREWMATES_EVENT, start)
    return () => window.removeEventListener(START_MEET_CREWMATES_EVENT, start)
  }, [explicit])
  useEffect(() => {
    const entered = () => setPageEntered(true)
    window.addEventListener(CREWMATES_PAGE_ENTERED_EVENT, entered)
    return () => window.removeEventListener(CREWMATES_PAGE_ENTERED_EVENT, entered)
  }, [])
  // Mate's first-visit welcome and this flow never both fire: while the
  // Crewmates preview is on and Mate exists with no chat history, neither
  // trigger opens the flow, and one held that way stays held for the session
  // so it never lands over the user's first exchange with Mate. Mate absent
  // or already chatted with, or a roster that cannot be read: unchanged.
  const triggerDue = tourEndDue || (pageEntered && pageEntryDue)
  const roster = useQuery({ ...membersRosterQuery, enabled: crewPreview && triggerDue })
  const rosterSettled = !crewPreview || roster.data !== undefined || roster.isError
  const matePending = crewPreview && roster.data !== undefined && pendingMate(roster.data) !== undefined
  const heldForMateRef = useRef(false)
  useEffect(() => {
    if (!triggerDue || !rosterSettled || closedThisSessionRef.current || heldForMateRef.current) return
    if (matePending) {
      heldForMateRef.current = true
      return
    }
    setOpen(true)
  }, [triggerDue, rosterSettled, matePending])
  const persist = useCallback(async () => {
    try {
      await markCrewmatesOnboarded()
      setPersistFailed(false)
    } catch {
      setPersistFailed(true)
    }
  }, [markCrewmatesOnboarded])
  const onCreated = useCallback(() => { void persist() }, [persist])
  const onDone = useCallback((_outcome: 'completed' | 'dismissed') => {
    closedThisSessionRef.current = true
    setOpen(false)
    void persist()
  }, [persist])
  return { open, onDone, onCreated, persistFailed }
}
