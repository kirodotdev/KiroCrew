import { useCallback, useEffect, useRef, useState } from 'react'
import { type MemberRosterRow } from '../api/client'
import { CREWMATES_PAGE_ENTERED_EVENT, START_MEET_CREWMATES_EVENT } from '../components/MeetCrewmatesFlow'
import { PREVIEW_CREW } from '../utils/previewFlags'
import { usePreviewFlag } from './usePreviewFlag'
import { useTheme } from './useTheme'

/** No crewmate beyond the always-present `default` row (the main assistant). */
export function hasNoCrewmates(rows: readonly Pick<MemberRosterRow, 'name'>[] | undefined): boolean {
  return Array.isArray(rows) && rows.every(r => r.name === 'default')
}

/**
 * Decides when the "Meet CrewMates" first-run flow is on screen.
 *
 * The ONLY condition on showing it is whether this workspace has seen it:
 * completion or dismissal is persisted as `dashboard.crewmates_onboarded`
 * through the theme-config endpoint, the same server-backed record the other
 * chapters use, so a second machine does not replay it. Existing crewmates or
 * custom agents do not suppress it.
 *
 * It opens, once, at the first of:
 * - the end of the first-run tour for a new user, while the Crew Members
 *   preview (`PREVIEW_CREW`, Settings -> Developer -> Feature Previews) is on
 *   -- the same switch that shows the Crewmates page, so the flow never
 *   introduces a page the user cannot reach;
 * - the first visit to the Crewmates page (`mc-crewmates-page-entered`), for
 *   everyone else, e.g. a workspace that finished first run before the
 *   chapter shipped.
 *
 * The Crewmates page also re-opens it on demand through the
 * `mc-start-meet-crewmates` window event (no check: the user asked).
 */
export function useMeetCrewmatesGate(): {
  open: boolean
  onDone: (outcome: 'completed' | 'dismissed') => void
  onCreated: () => void
  /** The last attempt to persist `crewmates_onboarded` was refused. The flow
   *  renders this as an ErrorNotice: on the current step when the create-time
   *  write failed, on the next entry when the write at exit failed (an exit
   *  never waits for the server). */
  persistFailed: boolean
} {
  const {
    onboarded,
    importOnboarded,
    privacyAcked,
    themeBootReady,
    crewmatesOnboarded,
    crewmatesFlowSeen,
    markCrewmatesOnboarded,
  } = useTheme()
  const crewPreview = usePreviewFlag(PREVIEW_CREW)
  const firstRunDone = themeBootReady && onboarded && importOnboarded && privacyAcked
  // Tour-end timing: `crewmatesOnboarded` is false only for a workspace whose
  // first run this browser saw end (the `mc-crewmates-pending` mark) and that
  // has not seen the flow, so an existing user is not interrupted mid-task on
  // their next load -- they get it on the Crewmates page instead.
  const tourEndDue = crewPreview && firstRunDone && !crewmatesOnboarded

  // `open` is STICKY once it fires: the flow persists "done" the moment the
  // crewmate exists (onCreated), which flips `eligible` off, and the ready step
  // must still be on screen after that. Only onDone closes it.
  const [open, setOpen] = useState(false)
  useEffect(() => {
    const start = () => setOpen(true)
    window.addEventListener(START_MEET_CREWMATES_EVENT, start)
    return () => window.removeEventListener(START_MEET_CREWMATES_EVENT, start)
  }, [])
  // Once the flow has closed in this session it does not auto-fire again here,
  // even while `eligible` stays true because the "done" write was refused: the
  // user has answered it once this session, and the notice already said it may
  // appear once more (after a reload, where the refused write means it is
  // genuinely still due). The Crew Members entry still opens it on demand.
  const closedThisSessionRef = useRef(false)
  useEffect(() => {
    if (tourEndDue && !closedThisSessionRef.current) setOpen(true)
  }, [tourEndDue])

  // First visit to the Crewmates page. The entry is remembered, not judged on
  // arrival: on a reload straight into /members the page can announce itself
  // before the theme boot says whether the flow was already seen.
  const pageEntryDue = firstRunDone && !crewmatesFlowSeen
  const [pageEntered, setPageEntered] = useState(false)
  useEffect(() => {
    const entered = () => setPageEntered(true)
    window.addEventListener(CREWMATES_PAGE_ENTERED_EVENT, entered)
    return () => window.removeEventListener(CREWMATES_PAGE_ENTERED_EVENT, entered)
  }, [])
  useEffect(() => {
    if (pageEntered && pageEntryDue && !closedThisSessionRef.current) setOpen(true)
  }, [pageEntered, pageEntryDue])

  // A refused write is shown, not swallowed, and nothing local is marked done
  // on a refusal (`markCrewmatesOnboarded` applies its local state only after
  // the server accepted), so a reload re-offers the chapter -- which is what
  // the notice says -- rather than this browser silently counting a completion
  // the server never recorded. WHERE it is shown depends on which write failed:
  // the create-time write (onCreated) fails while the ready step is still up,
  // so the notice lands on the current step; the write at exit (onDone) is
  // never awaited -- the exit closes the chapter at once, because several
  // exits navigate (`/members`, `/schedule`) and a `fixed inset-0` chapter
  // gated on a round trip would cover the page the user just asked for -- so a
  // refusal there is carried in `persistFailed` and shown on the next entry.
  const [persistFailed, setPersistFailed] = useState(false)
  const persist = useCallback(async (): Promise<boolean> => {
    try {
      await markCrewmatesOnboarded()
      setPersistFailed(false)
      return true
    } catch {
      setPersistFailed(true)
      return false
    }
  }, [markCrewmatesOnboarded])

  const onCreated = useCallback(() => {
    // Awaited by nobody: the ready step is still on screen, and a refusal
    // renders there through `persistFailed`.
    void persist()
  }, [persist])

  const onDone = useCallback(() => {
    // Close first, persist after. The exit is the user's, so it happens now;
    // the write's outcome only decides what the NEXT entry shows.
    closedThisSessionRef.current = true
    setOpen(false)
    void persist()
  }, [persist])

  return { open, onDone, onCreated, persistFailed }
}
