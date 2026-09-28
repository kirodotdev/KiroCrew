import { useCallback, useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api, type MemberRosterRow } from '../api/client'
import { membersRosterQuery } from '../api/membersQuery'
import { START_MEET_CREWMATES_EVENT } from '../components/MeetCrewmatesFlow'
import { PREVIEW_CREW } from '../utils/previewFlags'
import { usePreviewFlag } from './usePreviewFlag'
import { useTheme } from './useTheme'

/**
 * A row Kiro Crew itself wrote (never a user's own custom agent). The server
 * stamps every `/api/agents/installed` row with `kirocrew_owned`
 * (`agent_discovery.AgentInfo`, from `OWNED_KIRO_AGENT_FILES`), so a new
 * built-in template needs no edit here; a row without the flag is not a
 * built-in.
 */
export function isBuiltinAgent(a: { name: string; kirocrew_owned?: boolean }): boolean {
  return a.kirocrew_owned === true
}

/** No crewmate beyond the always-present `default` row (the main assistant). */
export function hasNoCrewmates(rows: readonly Pick<MemberRosterRow, 'name'>[] | undefined): boolean {
  return Array.isArray(rows) && rows.every(r => r.name === 'default')
}

/** No installed agent beyond the ones Kiro Crew itself wrote. */
export function hasNoCustomAgents(installed: readonly { name: string; kirocrew_owned?: boolean }[] | undefined): boolean {
  return Array.isArray(installed) && installed.every(isBuiltinAgent)
}

/**
 * Decides when the "Meet CrewMates" first-run flow is on screen.
 *
 * Fires ONCE per workspace, automatically, after the other first-run chapters
 * are done, only while the Crew Members preview (`PREVIEW_CREW`, Settings ->
 * Developer -> Feature Previews) is on — the same switch that shows the
 * Crewmates page and its rail item, so the flow launches with them and never
 * introduces a page the user cannot reach — and only for a user with no
 * crewmates AND no custom agents (an existing user with custom agents is
 * never shown it: their earlier-sync crewmates are the launch migration's job,
 * see rfc-crewmates-launch.md "Existing installs"; and a gate whose reads fail
 * fails safe by not firing). Completion or
 * dismissal is persisted as `dashboard.crewmates_onboarded` through the
 * theme-config endpoint, the same server-backed record the other chapters
 * use, so a second machine does not replay it.
 *
 * The Crewmates page re-opens it on demand through the
 * `mc-start-meet-crewmates` window event (no eligibility check: the page is
 * itself behind the preview, and the user asked).
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
  /** One of the two eligibility reads (roster, installed agents) failed while
   *  the auto-fire was still possible, so the flow could not decide whether
   *  to show itself. App renders this as an ErrorNotice; the Crew Members
   *  page entry stays available regardless. */
  eligibilityError: boolean
  dismissEligibilityError: () => void
} {
  const {
    onboarded,
    importOnboarded,
    privacyAcked,
    themeBootReady,
    crewmatesOnboarded,
    markCrewmatesOnboarded,
  } = useTheme()
  const crewPreview = usePreviewFlag(PREVIEW_CREW)
  const firstRunDone = themeBootReady && onboarded && importOnboarded && privacyAcked
  const eligible = crewPreview && firstRunDone && !crewmatesOnboarded

  // Both reads are needed only while the auto-fire is still possible; once the
  // flag is set they never run again for this workspace.
  const roster = useQuery({ ...membersRosterQuery, enabled: eligible })
  const installed = useQuery<{ name: string; kirocrew_owned?: boolean }[]>({
    queryKey: ['agents-installed'],
    queryFn: () => api.agentsInstalled(),
    enabled: eligible,
  })

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
  const autoOpen = eligible && hasNoCrewmates(roster.data) && hasNoCustomAgents(installed.data)
  useEffect(() => {
    if (autoOpen && !closedThisSessionRef.current) setOpen(true)
  }, [autoOpen])

  // A failed eligibility read is shown, not swallowed: without this the flow
  // would simply never appear and the user would not know why. Dismissal is
  // per failure -- a later refetch that fails again shows it again.
  const readFailed = eligible && (roster.isError || installed.isError)
  const [eligibilityDismissed, setEligibilityDismissed] = useState(false)
  useEffect(() => {
    if (!readFailed) setEligibilityDismissed(false)
  }, [readFailed])
  const eligibilityError = readFailed && !eligibilityDismissed
  const dismissEligibilityError = useCallback(() => setEligibilityDismissed(true), [])

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

  return { open, onDone, onCreated, persistFailed, eligibilityError, dismissEligibilityError }
}
