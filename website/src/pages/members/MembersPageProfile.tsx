/**
 * The crewmate's Profile card. `useMembersPageProfile` opens and closes it and
 * folds it when the side panel is revealed; `useMembersPageSchedules` holds its
 * Schedules draft and the discard questions that protect it;
 * `useMembersPageProfileFlight` flies the face between the identity pill and
 * the card and keeps the card's relative times moving.
 * `MembersPageProfileCard` renders the card as a docked column or a floating
 * card over the thread.
 *
 * CAPTURED: Settings > Developer > Feature Previews shows a screenshot of the
 * Crewmates page in its "See what it looks like" dialog. A visible change here
 * makes that picture stale — re-shoot with `scripts/capture-feature-previews.mjs`.
 *
 * The `MembersPage` facade calls each hook here at the point its code sits in
 * the page's hook order, which `MembersPage.owners.test.tsx` pins: React runs
 * a component's effects in registration order, so moving one is a behaviour
 * change even when every test of the moved code still passes.
 */
import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useRef, useState, type Dispatch, type SetStateAction, type RefObject } from 'react'
import { type NavigateFunction } from 'react-router-dom'
import { ChevronRight, Circle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api, type MemberRosterRow } from '../../api/client'
import { crewDisplayName } from '../../components/AgentSelector'
import { useQuery } from '@tanstack/react-query'
import { timeAgo } from '../../utils/timeAgo'
import CrewProfilePanel, { type ProfileTab } from './CrewProfilePanel'
import CrewGoalsPane, { useMemberGoals } from './CrewGoals'
import { sendTurn } from '../../chat-core/transport/sendTurn'
import { createPortal } from 'react-dom'
import { useConfirm } from '../../components/ConfirmDialog'
import { useGuardedLeave } from '../../components/NavigationLeaveGuard'
import CrewNotesTab from './CrewNotesTab'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import CrewWakeSection from '../../components/CrewWakeSection'
import { crewWakeQueryKey, wakesCrew } from '../../components/crew/wakesCrew'
import { tabStatus, type TabStatus } from '../../lib/sessionTabs'
import { lastActivityEpoch } from '../chat/sessionOrder'
import { type MembersPageRosterView } from './MembersPageRoster'
import { type MembersPageSidePanelState } from './MembersPageSidePanel'
import { type MembersPageThreadState } from './MembersPageThread'
import { DRIVING_VISIBLE, memberMemoryDisplay, PROFILE_MEMORY_KEYS, type ProfileCardOpen, type TFn } from './membersPageModel'
import { type MembersPageCrewEditing } from './useMembersPageCrewEditor'
import { type MembersPageLayout } from './useMembersPageLayout'
import { type MembersPageRosterSignals } from './useMembersPageRoster'
import { type AutomationRecord } from '../../monitoring/automation'
import { readSessionAutomationSnapshot } from '../../monitoring/sessionAutomationSnapshot'

// Lazy for the same reason as `CrewEditorDialog` in MembersPage.tsx: its fields come from the crew manager page module.
const CrewProfileSettings = lazy(() => import('./CrewProfileSettings'))
/** Punctuation, not prose: joins an activity label to its project name, and a
 *  driving row's title to its status word in the hover title. */
const PROJECT_SEPARATOR = ' \u00b7 '
/** The crewmate's face in flight between the identity pill and the docked
 *  Profile card's head: one face moves, two never cross-fade. It used to be a
 *  framer `layoutId` shared by the two faces, but the shared element travels
 *  INSIDE whichever surface owns it: the card's face is clipped by the card's
 *  rounded `overflow-hidden` shell, its scrolling body and the width-revealing
 *  aside until it is already inside them, and the pill's face is painted under
 *  the thread and the closing card, both later siblings of the header (#18236).
 *  So the flight is its own copy, portaled to `document.body` above every
 *  surface, while both real faces hold their place unpainted. `from` is the
 *  departing face measured before it went; `to` is read live each frame because
 *  the landing face moves while the card's column reveals or folds. */
export interface FaceFlight {
  key: number
  seed: string
  avatar: unknown
  slotKey: string
  running: boolean
  automation: AutomationRecord | null
  from: DOMRect
  to: () => DOMRect | null
}
/** Framer's own `defaultLayoutTransition` (motion-dom, create-projection-node:
 *  0.45s, cubic-bezier 0.4/0/0.1/1) — the clock the shared `layoutId` flight
 *  ran on, kept to the number so the copy moves exactly as that one did and
 *  the only visible change is that it stays on top. */
export const FACE_FLIGHT_SECS = 0.45
export const FACE_FLIGHT_EASE: [number, number, number, number] = [0.4, 0, 0.1, 1]
/** How each shared tab status renders on a driving row. The ORDER lives in
 *  `tabStatus` (lib/sessionTabs.ts) — this only maps its verdict to a dot
 *  class, an i18n label, and whether the label is spoken aloud in the row.
 *  `unread` cannot occur here (no unread set is passed) and reads as idle. */
const DRIVING_STATUS: Record<TabStatus, { cls: string; text: string; label: string; spoken: boolean }> = {
  permission: { cls: 'fill-warn text-warn', text: 'text-warn', label: 'pages.chatSidebar.needs_approval', spoken: true },
  question: { cls: 'fill-info text-info', text: 'text-info', label: 'pages.chatSidebar.needs_your_answer', spoken: true },
  running: { cls: 'fill-ok text-ok', text: 'text-ok', label: 'pages.membersPage.drawer_working', spoken: false },
  unread: { cls: 'fill-muted text-muted', text: 'text-muted', label: 'pages.membersPage.driving_idle', spoken: false },
  idle: { cls: 'fill-muted text-muted', text: 'text-muted', label: 'pages.membersPage.driving_idle', spoken: false },
}

interface MembersPageProfileStateOptions {
  layout: MembersPageLayout
  activeName: string
  profile: ProfileCardOpen | null
  setProfile: Dispatch<SetStateAction<ProfileCardOpen | null>>
}

/** The Profile card's open, close and placement controls, and the panel toggle that folds it. */
export function useMembersPageProfile({ layout, activeName, profile, setProfile }: MembersPageProfileStateOptions) {
  const { isMobile, beside, overlayOpen, setOverlayOpen, dockedOpen, setDockedOpen, panelVisible } = layout
  // The two real faces the flight copy departs from and lands on (#18236), and
  // the pill face's box taken HERE, on open: by the time the dock commits the
  // pill has already given way to its placeholder and there is nothing left to
  // measure. Undock measures the card face in the layout effect instead — the
  // closing column stays mounted through its exit.
  const pillFaceRef = useRef<HTMLSpanElement>(null)
  const cardFaceRef = useRef<HTMLSpanElement>(null)
  const flightFromRef = useRef<DOMRect | null>(null)
  // The card opens on Sessions — what the crewmate is doing — unless a caller
  // names another tab (today only the quiet-chat link, which also asks for Sessions).
  const openProfile = useCallback((tab: ProfileTab = 'sessions') => {
    flightFromRef.current = pillFaceRef.current?.getBoundingClientRect() ?? null
    setProfile((current) => ({
      tab,
      placement: isMobile || panelVisible ? 'floating' : 'column',
      openNonce: (current?.openNonce ?? 0) + 1,
    }))
  }, [isMobile, panelVisible, setProfile])
  // The Schedules tab's unsaved-draft question, reachable from the exits declared
  // ABOVE where the tab itself is built. There is exactly one guard for exits that
  // unmount Profile; a ref lets those early callbacks reach the late definition
  // (`mayLeaveSchedules`) without hoisting the whole tab up here. Answers true while
  // no draft is at stake, so this is inert on every other tab.
  const schedGuardRef = useRef<() => Promise<boolean>>(async () => true)
  // Whether an unmounting exit needs to ask, answerable SYNCHRONOUSLY. Those exits
  // check this first and keep their old synchronous path when nothing is at stake:
  // routing every crewmate switch and panel reveal through a promise would make the
  // whole page's navigation async to protect a form that is usually not open.
  const schedAtStakeRef = useRef<() => boolean>(() => false)
  // Closing the card asks the Schedules draft question like every other exit. Hoisted
  // above the header (`MembersPageThreadHeader`, in MembersPageThreadHeader.tsx) because the
  // identity pill is one of those exits: with Profile
  // floating, the pill reads `aria-expanded` and a press on it CLOSES the card rather
  // than re-opening it. Re-opening set `profile.tab` back to the default, and the tab
  // is part of the card's React key, so a card the reader had moved to another tab
  // (Schedules, say) was remounted by that press and a New schedule draft inside it
  // destroyed — with no question asked. Pointer users reach the pill rarely (the
  // floating card's scrim covers the header), keyboard users reach it every time.
  const requestCloseProfile = useCallback(() => {
    if (!schedAtStakeRef.current()) { setProfile(null); return }
    void schedGuardRef.current().then((ok) => { if (ok) setProfile(null) })
  }, [setProfile])
  // A local panel reveal goes through `togglePanel`, which can ask before it drops the
  // Profile column. A cross-tab persisted-state update cannot be declined: keep a dirty
  // Profile mounted rather than discarding its pushed schedule form. With no draft, the
  // remote reveal keeps the ordinary fold-to-pill behavior.
  const panelWasVisibleRef = useRef(panelVisible)
  useEffect(() => {
    const was = panelWasVisibleRef.current
    panelWasVisibleRef.current = panelVisible
    if (!was && panelVisible && profile?.placement === 'column') {
      if (schedAtStakeRef.current()) return
      setProfile(null)
    }
  }, [panelVisible, profile, setProfile])
  // The card is ABOUT the open crewmate, so it goes when the crewmate does: a
  // switch to another crewmate, a team open, the phone's Back or a bare
  // `/members` all change `activeName`, and a card kept across that change popped
  // open by itself over the NEXT crewmate — on the old tab, since the card is
  // keyed on name + tab + accepted-open nonce (`profilePanel`). No guard here: every one of those exits
  // already asked the Schedules draft question BEFORE changing the crewmate, so
  // by the time the name moves there is nothing left to ask about. Keyed on the
  // name, not the row object: a re-POST of the same thread is not a switch.
  useEffect(() => { setProfile(null) }, [activeName, setProfile])
  // Placement is decided when the card opens. A window that crosses below `md`
  // while the card holds its in-flow column would keep a fixed-width aside on a
  // phone-width viewport, so the card is re-placed as the floating one — kept,
  // not dropped. The two placements are different subtrees, so the move remounts
  // the card body; with a typed Schedules draft at stake the column is kept
  // instead, the same call the cross-tab panel reveal above makes: a misplaced
  // column beats a draft discarded with no question asked.
  useEffect(() => {
    if (!isMobile) return
    setProfile((p) => (p && p.placement === 'column' && !schedAtStakeRef.current() ? { ...p, placement: 'floating' } : p))
  }, [isMobile, setProfile])
  const closeDocked = useCallback(() => setDockedOpen(false), [setDockedOpen])
  // One gesture drives whichever placement is live, so the header button and
  // the dashboard's side-panel chord share it. The chord reaches this page the
  // way it reaches the chat page: App dispatches `toggle-activity-panel` on the
  // window and the page that owns a panel listens, which keeps one binding
  // across both surfaces instead of each inventing its own.
  //
  // Both the panel and its opener live behind an open member, so the gesture
  // is gated on one too: the roster with no member open draws no panel, and a
  // toggle there would move the persisted choice with nothing on screen
  // changing, so the next member opened would come up hidden for no reason the
  // user can see. `openMemberRef` is read rather than closed over so the
  // listener binds once instead of re-binding per selection.
  const openMemberRef = useRef(false)
  const togglePanel = useCallback(() => {
    if (!openMemberRef.current) return
    // Hiding the panel leaves the Profile card mounted, so it cannot discard
    // the schedule draft now hosted there. Revealing still guards the Profile
    // fold: that path closes the card and does unmount its pushed form.
    const hiding = beside ? dockedOpen : overlayOpen
    const flip = () => {
      if (beside) setDockedOpen((v) => !v)
      else setOverlayOpen((v) => !v)
    }
    const revealAndFoldProfile = () => {
      flip()
      setProfile(null)
    }
    if (!hiding && profile) {
      if (!schedAtStakeRef.current()) { revealAndFoldProfile(); return }
      void schedGuardRef.current().then((ok) => { if (ok) revealAndFoldProfile() })
      return
    }
    flip()
  }, [beside, dockedOpen, overlayOpen, setDockedOpen, profile, setOverlayOpen, setProfile])
  useEffect(() => {
    const onToggle = () => togglePanel()
    window.addEventListener('toggle-activity-panel', onToggle)
    return () => window.removeEventListener('toggle-activity-panel', onToggle)
  }, [togglePanel])

  return {
    pillFaceRef, cardFaceRef, flightFromRef, openProfile, schedGuardRef, schedAtStakeRef, requestCloseProfile,
    closeDocked, openMemberRef, togglePanel,
  }
}

export type MembersPageProfileState = ReturnType<typeof useMembersPageProfile>

interface MembersPageSchedulesOptions {
  crewEditing: MembersPageCrewEditing
  layout: MembersPageLayout
  rosterView: MembersPageRosterView
  sidePanel: MembersPageSidePanelState
  t: TFn
  profile: ProfileCardOpen | null
  activeSlug: string
  activeMemberName: string
}

/** The Profile card's Schedules draft, its count and its discard questions. */
export function useMembersPageSchedules({
  crewEditing, layout, rosterView, sidePanel, t, profile, activeSlug, activeMemberName,
}: MembersPageSchedulesOptions) {
  const { releaseSchedRef } = crewEditing
  const { panelVisible } = layout
  const { activeView } = rosterView
  const { setSchedAtStake, schedReleased, schedulesMountedRef } = sidePanel
  // What a schedule created from the Schedules tab must carry in its `agent` field --
  // which is the provider template only for a crewmate whose identity the server will
  // KEEP. `wakesCrew` matches a job on `member_id` when there is one, and otherwise
  // compares `agent` against the crewmate's DISPLAY NAME. A crewmate with no persisted
  // identity has its `member_id` cleared as the job is created
  // (`cron_service/identity.py`), so writing the template into `agent` there left the new
  // schedule matching neither field and invisible on the very tab that made it. Submit
  // whatever the matcher will actually read.
  const schedAgentField = activeView && memberMemoryDisplay(activeView) === 'private'
    ? activeView.kiro_agent
    : activeMemberName
  // Schedules count behind the Schedules tab's chip. Read whenever the STRIP is
  // on screen, not when the tab is — the whole job of the badge is to answer
  // "does anything wake this mate" without opening it, and a read gated on the
  // tab being open could only ever report a count the user is already looking at.
  // Same key and same queryFn as the pane inside (`crewWakeQueryKey` +
  // `api.crons()`), so opening the tab spends no second request and the chip can
  // never disagree with the list it summarizes.
  // The first goal, for the profile card's About when the crewmate has no
  // description: a crewmate made from a Create crewmate card starts with a goal
  // and no description. Same query key as the Goals tab, so no second request.
  const profileGoalsQuery = useMemberGoals(activeSlug, activeMemberName, !!profile)
  const schedulesCountQuery = useQuery({
    queryKey: crewWakeQueryKey(activeMemberName),
    queryFn: () => api.crons(),
    enabled: (panelVisible || !!profile) && !!activeMemberName,
  })
  // This crewmate's own schedules. The `false` is `wakesCrew`'s `isDefaultCrew`
  // argument, not a claim about this crewmate: it withholds the unowned-job fallback,
  // so a job with no `member_id` and no bound agent is never claimed here even when
  // this crewmate IS the default crew — that one lives on `/schedule`, the
  // cross-crewmate view, which is also why this page never reads which crew is the
  // default. Everything `wakesCrew` attributes earlier (durable `member_id`, a bound
  // `agent`, a multi-entry `agent_sequence`) still counts, same as in the editor.
  const schedulesForActive = (schedulesCountQuery.data?.jobs || [])
    .filter((j) => wakesCrew(j, activeMemberName, false, activeSlug))
  // Unsaved work inside the Schedules tab. Refs, not state: only the guard below reads
  // them and a re-render per keystroke would remount nothing but cost the panel a pass.
  // (`schedAtStake`, the state mirror, is declared in MembersPageSidePanel.tsx with the
  // mount decision that also reads it.)
  const schedDraftDirty = useRef(false)
  const schedSaving = useRef(false)
  // The refs are what the synchronous guards read; the state mirror is what the published
  // navigation stake and the `beforeunload` listener need, since both live in effects.
  const setSchedDraftDirty = useCallback((d: boolean) => {
    schedDraftDirty.current = d
    setSchedAtStake(d || schedSaving.current)
  }, [setSchedAtStake])
  const setSchedSaving = useCallback((s: boolean) => {
    schedSaving.current = s
    setSchedAtStake(s || schedDraftDirty.current)
  }, [setSchedAtStake])
  const { confirm: confirmSched, confirmDialog: schedConfirmDialog } = useConfirm()
  /** The section's own collapse toggle, which it cannot guard itself. */
  const requestCancelSchedDraft = useCallback((proceed: () => void) => {
    void (async () => {
      const ok = await confirmSched({
        title: t('pages.kiroCrewAgentsPage.discard_new_schedule'),
        body: schedSaving.current ? t('pages.kiroCrewAgentsPage.discard_anyway_note') : undefined,
        confirmLabel: t('pages.kiroCrewAgentsPage.discard_schedule_confirm'),
      })
      if (ok) proceed()
    })()
  }, [confirmSched, t])
  /** The user accepted a discard, so RETENTION stands down for the exit that asked --
   *  and only retention. The draft's own flags are left alone, so it stays guarded until
   *  the form actually unmounts and reports itself clean.
   *
   *  Retention has to stand down before the exit runs, not after: `keepMountedForDraft`
   *  is what would hold the panel mounted, and waiting for the unmount to clear the flag
   *  that is preventing that unmount keeps the panel hidden and mounted for good.
   *  Clearing the draft flags instead was the first version of this, and it disarmed the
   *  page: with two dirty surfaces on the leave channel, accepting the schedule's
   *  question and then vetoing the other one left a visible, unguarded draft to be lost
   *  on some later exit. A release is a statement about one exit; the draft is still a
   *  draft until it is gone. */
  const releaseSchedRetention = useCallback(() => { schedReleased.current = true }, [schedReleased])
  // Expose it to `onCrewDeleted` (declared in useMembersPageCrewEditor.ts): a
  // confirmed crew delete stands down the schedule retention so the panel unmount
  // is an acknowledged discard.
  releaseSchedRef.current = releaseSchedRetention
  // The release is good for the exit that asked and no further. If the form is still
  // mounted on the next render, that exit did not happen -- a guard after this one
  // vetoed it -- so retention arms again for whatever comes next. Runs after every
  // render on purpose: it is answering "did the exit land", which only the next render
  // knows.
  useEffect(() => {
    if (schedReleased.current && schedulesMountedRef.current) schedReleased.current = false
  })
  /** Asked by the strip before it switches away from Schedules; see `onBeforeLeave`. */
  const mayLeaveSchedules = useCallback(async () => {
    // A create request in flight is never discardable: unmounting the form does not
    // cancel the POST, so the "discarded" schedule would persist. The section disables
    // its own cancel for exactly this window.
    if (schedSaving.current) return false
    if (!schedDraftDirty.current) return true
    const ok = await confirmSched({
      title: t('pages.kiroCrewAgentsPage.discard_new_schedule'),
      confirmLabel: t('pages.kiroCrewAgentsPage.discard_schedule_confirm'),
    })
    if (ok) releaseSchedRetention()
    return ok
  }, [confirmSched, t, releaseSchedRetention])

  return {
    schedAgentField, profileGoalsQuery, schedulesCountQuery, schedulesForActive, schedDraftDirty, schedSaving,
    setSchedDraftDirty, setSchedSaving, confirmSched, schedConfirmDialog, requestCancelSchedDraft,
    releaseSchedRetention, mayLeaveSchedules,
  }
}

export type MembersPageSchedules = ReturnType<typeof useMembersPageSchedules>

interface MembersPageProfileFlightOptions {
  profileCard: MembersPageProfileState
  rosterView: MembersPageRosterView
  rosterSignals: MembersPageRosterSignals
  thread: MembersPageThreadState
  activeName: string
  profile: ProfileCardOpen | null
  active: MemberRosterRow | undefined
}

/** The Profile card's clock and the face flight between pill and card. */
export function useMembersPageProfileFlight({
  profileCard, rosterView, rosterSignals, thread, activeName, profile, active,
}: MembersPageProfileFlightOptions) {
  const { pillFaceRef, cardFaceRef, flightFromRef } = profileCard
  const { isRunning, activeView } = rosterView
  const { automationOf } = rosterSignals
  const { activeSlot } = thread
  // Profile reuses the active chat's existing authoritative snapshot query.
  // A websocket record wins while live; the focused read supplies retained
  // stopped state and full owner-gated structured details.
  const activeAutomation = active ? automationOf(active) : null
  const profileSlot = activeSlot || active?.slot_key || ''
  const profileMonitorQuery = useQuery({
    queryKey: ['session-automation', profileSlot],
    enabled: !!profile && !!profileSlot,
    queryFn: () => readSessionAutomationSnapshot(profileSlot),
    staleTime: 0,
  })
  const profileMonitorReadState = !profileSlot
    ? 'ready' as const
    : profileMonitorQuery.isError
      ? 'error' as const
      : profileMonitorQuery.isSuccess || activeAutomation
        ? 'ready' as const
        : 'loading' as const
  const profileAutomation = profileMonitorReadState === 'error'
    ? null
    : activeAutomation ?? profileMonitorQuery.data ?? null
  // Relative times in the Profile Schedules tab move while the card is open.
  const [nowTs, setNowTs] = useState(() => Date.now() / 1000)
  useEffect(() => {
    if (!profile) return
    setNowTs(Date.now() / 1000)
    const timer = setInterval(() => setNowTs(Date.now() / 1000), 60_000)
    return () => clearInterval(timer)
  }, [profile])
  // The roster badge's mount/unmount tween honours the OS motion preference:
  // the state change still happens, it just cuts instead of fading.
  const reduceMotion = useReducedMotion()
  // Whether Profile holds its in-flow column right now.
  const profileDocked = !!activeView && !!profile && profile.placement === 'column'
  // Whether the card is up at all — the state whose two flips the face flies
  // on, whichever placement the card takes. Floating, the pill stays beside the
  // card with its face slot held empty: the face is IN the card.
  const profileOpen = !!activeView && !!profile
  const [faceFlight, setFaceFlight] = useState<FaceFlight | null>(null)
  const flightKeyRef = useRef(0)
  const wasOpenRef = useRef(profileOpen)
  useLayoutEffect(() => {
    const was = wasOpenRef.current
    wasOpenRef.current = profileOpen
    // A re-placement (column <-> floating) keeps the card open: no flip, no flight.
    if (was === profileOpen) return
    const from = profileOpen ? flightFromRef.current : cardFaceRef.current?.getBoundingClientRect() ?? null
    flightFromRef.current = null
    // No box to depart from (reduced motion, a never-laid-out face, a dock that
    // came from somewhere other than the pill) means a plain swap, not a flight.
    if (reduceMotion || !active || !from || from.width === 0) return
    // The folding card is AnimatePresence's exiting child, re-rendered with the
    // props it left with — `faceHidden` can no longer reach it — so its face is
    // unpainted here, on the element, for the rest of its exit.
    if (!profileOpen && cardFaceRef.current) cardFaceRef.current.style.visibility = 'hidden'
    const target = profileOpen ? cardFaceRef : pillFaceRef
    setFaceFlight({
      key: ++flightKeyRef.current,
      seed: active.name,
      avatar: active.avatar,
      slotKey: activeSlot || active.slot_key || '',
      running: !!isRunning(active),
      automation: automationOf(active),
      from,
      to: () => target.current?.getBoundingClientRect() ?? null,
    })
  }, [profileOpen, reduceMotion, active, activeSlot, isRunning, automationOf, cardFaceRef, flightFromRef, pillFaceRef])
  const endFaceFlight = useCallback(() => setFaceFlight(null), [])
  // A flight belongs to the crewmate it was measured on. Leaving that crewmate
  // mid-flight (Back, a bare /members, a team open) unmounts the copy before its
  // `onComplete`, so the state is cleared here or the next crewmate's open would
  // replay the old face from the old box and keep its own pill face hidden.
  useEffect(() => { setFaceFlight(null) }, [activeName])

  return {
    nowTs, reduceMotion, profileDocked, faceFlight, endFaceFlight, profileAutomation, profileMonitorReadState,
  }
}

export type MembersPageProfileFlight = ReturnType<typeof useMembersPageProfileFlight>

interface MembersPageProfileCardProps {
  crewEditing: MembersPageCrewEditing
  profileCard: MembersPageProfileState
  rosterView: MembersPageRosterView
  thread: MembersPageThreadState
  sidePanel: MembersPageSidePanelState
  schedules: MembersPageSchedules
  profileFlight: MembersPageProfileFlight
  navigate: NavigateFunction
  leave: ReturnType<typeof useGuardedLeave>
  setEditingCrew: Dispatch<SetStateAction<string>>
  profile: ProfileCardOpen | null
  threadColumnRef: RefObject<HTMLElement>
  slotsLoaded: boolean
  active: MemberRosterRow
  activeSlug: string
  activeMemberName: string
  onGoalsDraftChange: (dirty: boolean, busy: boolean) => void
}

/** The Profile card, as a docked column or a floating card over the thread column. */
export function MembersPageProfileCard({
  crewEditing, profileCard, rosterView, thread, sidePanel, schedules, profileFlight, navigate, leave,
  setEditingCrew, profile, threadColumnRef, slotsLoaded, active, activeSlug, activeMemberName,
  onGoalsDraftChange,
}: MembersPageProfileCardProps) {
  const { t } = useTranslation()
  const { profileRegistryQuery } = crewEditing
  const { cardFaceRef, schedGuardRef, schedAtStakeRef, requestCloseProfile } = profileCard
  const { isRunning, activeView } = rosterView
  const { activeSlot, freshStart, askFreshStart, confirmedSlot } = thread
  const {
    profileCardW, activeMemberKey, openCrewView, openSessionGuarded, drivingSessions, setDrivingExpandedFor,
    drivingExpanded, visibleDriving,
  } = sidePanel
  const {
    schedAgentField, profileGoalsQuery, schedulesCountQuery, schedulesForActive, setSchedDraftDirty,
    setSchedSaving, requestCancelSchedDraft,
  } = schedules
  const { nowTs, reduceMotion, profileDocked, faceFlight, profileAutomation, profileMonitorReadState } = profileFlight
  // Notes — the crewmate's own standing notes, read-only (no editor:
  // see CrewNotesTab). Its briefing read is gated on the tab being on
  // screen, like the work log's reads.
  const notesBody = activeSlug && activeMemberName ? (
    <CrewNotesTab
      slug={activeSlug}
      member={activeMemberName}
      displayName={crewDisplayName(active)}
      header={null}
      visible
    />
  ) : null
  // Schedules — what wakes THIS crewmate without anyone asking, and nothing
  // else. The body is the crew editor's own pane (`CrewWakeSection`), scoped
  // with `ownedOnly`. Not a copy: one schedules editor exists in the product
  // and this is a second mount of it, so the create form, the
  // member-immutability rule and the Global Memory V1 attribution behave
  // identically on both surfaces. A schedule belonging to no crewmate is not
  // this crewmate's business and stays on `/schedule`.
  const schedulesBody = activeMemberName ? (
    <div className="px-3 py-3" data-testid="member-schedules" aria-label={t('pages.membersPage.schedules_tab')}>
      {/* Keyed per crewmate so a member switch REMOUNTS the section. Without
          it the open create form survives the switch and keeps whatever was
          typed, while its `memberId` becomes the new crewmate -- submitting
          one crewmate's draft as another's schedule. */}
      <CrewWakeSection
        key={activeMemberName}
        crew={activeMemberName}
        // The crew's IMMUTABLE id, which is what a private schedule's
        // `member_id` holds. Not the display name: they differ for any
        // crewmate whose name is not already its own slug, and passing the
        // name made every such crewmate read as having no schedules and hid
        // a job the moment it was created here. See `wakesCrew`.
        memberId={activeSlug}
        // The job's `agent` field, which has to be passed: `JobForm` falls back
        // to `''`, so omitting it persisted a created schedule with no agent at
        // all and `/schedule` labelled this crewmate's own job as the default
        // crew's. The prop is named for the editor's case, where this IS always
        // the provider template; here it is the template only for a crewmate
        // whose identity persists. See `schedAgentField`.
        agentTemplate={schedAgentField}
        // This crewmate's own schedules only: `ownedOnly` drops `wakesCrew`'s
        // unowned-job fallback and nothing else, so a job attributed by
        // `member_id`, by `agent`, or by an `agent_sequence` still lists here
        // exactly as it does in the editor. See `WakeScope`.
        ownedOnly
        initialAdding
        // The pushed page's bar already says "New schedule" and holds the
        // one Back control, which runs the draft guard below. The
        // section's own heading and its Cancel X would be a second title
        // and a second exit on the same short page.
        chromeless
        // The panel column is ~460px on a wide screen, where every `sm:` /
        // `md:` promotion in the section fires and truncates a job name to
        // about ten characters. The host knows its own width; the section
        // cannot ask about it with viewport breakpoints.
        dense
        heading={t('pages.membersPage.schedules_heading')}
        blurb={t('pages.membersPage.schedules_blurb')}
        emptyLine={t('pages.membersPage.schedules_empty')}
        // The create form's pinned-crew hint names the crew EDITOR; this
        // page is the crewmate's Profile, so it says so in its own noun.
        pinnedHint={t('pages.membersPage.schedules_pinned_hint')}
        onDraftChange={setSchedDraftDirty}
        onSavingChange={setSchedSaving}
        onRequestCancel={requestCancelSchedDraft}
      />
    </div>
  ) : null
  // The card's Sessions tab: the sessions this crewmate is driving — the
  // worker sessions it opened and steers — and nothing else. Rows use live slots, the sidebar's status vocabulary, and a jump into
  // the session, in the card's row idiom.
  const sessionsBody = (
    <div className="flex flex-col gap-3" data-testid="crew-profile-sessions">
      {drivingSessions.length === 0 && !slotsLoaded ? (
        <div className="space-y-2" aria-hidden>
          <div className="h-12 rounded-2xl bg-bg-hover animate-pulse" />
          <div className="h-12 rounded-2xl bg-bg-hover animate-pulse" />
        </div>
      ) : drivingSessions.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-border-strong bg-bg px-4 py-8 text-center text-[13px] text-muted" data-testid="crew-profile-sessions-empty">
          {t('pages.membersPage.driving_none')}
        </div>
      ) : (
        <ul className="list-none m-0 p-0 rounded-2xl border border-border bg-bg overflow-hidden" data-testid="crew-profile-driving">
          {visibleDriving.map((s) => {
            const kind = tabStatus(s, [], s.key)
            const status = DRIVING_STATUS[kind]
            const label = t(status.label)
            const activityTs = lastActivityEpoch(s)
            const title = s.title || s.key
            return (
              <li key={s.key} className="border-t border-border first:border-t-0">
                <button
                  type="button"
                  onClick={() => openSessionGuarded(s.key)}
                  className="w-full flex items-center gap-3 px-3.5 py-3 text-left hover:bg-bg-hover transition-colors cursor-pointer"
                  title={title + PROJECT_SEPARATOR + label}
                  data-testid="crew-profile-driving-row"
                  data-status={kind}
                >
                  <Circle size={9} className={`shrink-0 ${status.cls}`} aria-hidden />
                  <span className="flex-1 min-w-0 leading-tight">
                    <span className="block text-[13px] font-semibold truncate">{title}</span>
                    <span className="block text-[11.5px] text-muted truncate">
                      {label}{activityTs > 0 ? `${PROJECT_SEPARATOR}${timeAgo(activityTs)}` : ''}
                    </span>
                  </span>
                  <ChevronRight size={15} className="text-muted shrink-0" aria-hidden="true" />
                </button>
              </li>
            )
          })}
        </ul>
      )}
      {drivingSessions.length > DRIVING_VISIBLE && (
        <button
          type="button"
          onClick={() => setDrivingExpandedFor(drivingExpanded ? '' : activeMemberKey)}
          className="self-start text-[12.5px] font-semibold text-accent cursor-pointer"
          aria-expanded={drivingExpanded}
        >
          {drivingExpanded
            ? t('pages.membersPage.driving_show_less')
            : t('pages.membersPage.driving_show_all', { count: drivingSessions.length })}
        </button>
      )}
    </div>
  )
  const requestProfileBack = (proceed: () => void) => {
    if (!schedAtStakeRef.current()) { proceed(); return }
    void schedGuardRef.current().then((ok) => { if (ok) proceed() })
  }
  // The profile card (crewmate-panel IA): the pill's surface. Notes,
  // Schedules and the work log are ITS tabs/pages now, so the three
  // bodies above mount here instead of on the side panel's strip.
  // The Goals tab's "Track something in chat": the user's own message in
  // this crewmate's thread, through the ordinary send path, so it shows in
  // the transcript like anything they typed. A card floating over the
  // thread closes so the reply is in view; a docked column stays.
  const trackInChat = async (message: string) => {
    if (!confirmedSlot) return 'refused' as const
    return (await sendTurn({ slot: confirmedSlot, message })).status
  }
  const onTracked = () => {
    // CrewGoals clears its delivered draft and busy flag before this exit.
    if (!profileDocked) requestCloseProfile()
  }
  const profilePanel = activeView && profile ? (
    <CrewProfilePanel
      key={`${active.name}:${profile.tab}:${profile.openNonce}`}
      member={activeView}
      running={!!isRunning(active)}
      slotKey={activeSlot || active.slot_key}
      automation={profileAutomation ?? null}
      monitorReadState={profileMonitorReadState}
      description={profileRegistryQuery.data?.agents.find((a) => a.name === active.name)?.description ?? ''}
      descriptionError={profileRegistryQuery.isError}
      goal={profileGoalsQuery.data?.goals[0]?.text ?? ''}
      goalError={profileGoalsQuery.isError}
      memoryLabel={t(PROFILE_MEMORY_KEYS[memberMemoryDisplay(activeView)])}
      // Only a crewmate's OWN (V2) store has a page to manage; the shared
      // global store is not this crewmate's, so that tile stays a readout.
      onOpenMemory={memberMemoryDisplay(activeView) === 'private' && activeView.memory_store
        ? () => { const destination = `/settings/overview?view=memory&store=${encodeURIComponent(activeView.memory_store ?? '')}`; leave(() => navigate(destination), destination) }
        : undefined}
      initialTab={profile.tab}
      schedules={schedulesForActive}
      schedulesLoading={schedulesCountQuery.isPending}
      schedulesError={schedulesCountQuery.isError}
      nowTs={nowTs}
      onOpenSchedule={() => { const destination = '/schedule'; leave(() => navigate(destination), destination) }}
      onOpenScheduleJob={(id) => { const destination = `/schedule?job=${encodeURIComponent(id)}`; leave(() => navigate(destination), destination) }}
      newScheduleBody={schedulesBody}
      sessionsBody={sessionsBody}
      notesBody={notesBody}
      goalsBody={activeSlug && activeMemberName ? (
        <CrewGoalsPane
          slug={activeSlug}
          member={activeMemberName}
          name={crewDisplayName(activeView ?? active)}
          onTrackInChat={confirmedSlot ? trackInChat : undefined}
          onTracked={onTracked}
          onDraftStateChange={onGoalsDraftChange}
          onRequestLeave={requestProfileBack}
        />
      ) : null}
      faceRef={cardFaceRef}
      faceHidden={!!faceFlight}
      settingsBody={<Suspense fallback={null}><CrewProfileSettings member={activeView} slotKey={confirmedSlot || null} waiting={!confirmedSlot} /></Suspense>}
      onClose={requestCloseProfile}
      onRequestBack={requestProfileBack}
      onEdit={() => setEditingCrew(active.name)}
      onOpenFiles={() => openCrewView('files')}
      onFreshStart={activeSlot && !freshStart.isPending ? askFreshStart : undefined}
    />
  ) : null
  // The card is a hover card either way — rounded on every corner, a third
  // of the row wide, the side panel's height. WHERE it sits depends on the
  // side panel and viewport: with the panel hidden on a non-phone viewport,
  // the card takes its own column and the thread gives up the width (the
  // pill is hidden then, the card's head names the crewmate); with the panel
  // open or on a phone, the card floats centred over the DM column (its
  // containing block, which is exactly the chat width the side panel leaves),
  // and a click outside it closes it. No scrim: the thread stays readable
  // beside it.
  // The column reveals on the chat page's width axis (so the thread
  // narrows instead of jumping), while the face slides over from the pill.
  // Keyed on the placement: a column RE-PLACED as the floating card (the
  // window crossed below md) remounts the presence and drops the aside at
  // once, with no width exit — the floating card is already up, and a
  // phone-width viewport must never hold the in-flow column, not even for
  // the 0.26s the exit would take. A close keeps the key and plays it.
  const dockedSurface = (
    <AnimatePresence initial={false} key={profile?.placement === 'floating' ? 'crew-profile-re-placed' : 'crew-profile-column'}>
      {profileDocked && (
        <motion.aside
          key="crew-profile-docked"
          initial={{ width: 0, opacity: 0 }}
          animate={{ width: profileCardW + 8, opacity: 1 }}
          exit={{ width: 0, opacity: 0 }}
          transition={{ duration: 0.26, ease: [0.2, 0.8, 0.2, 1] }}
          className="h-full shrink-0 flex flex-col min-h-0 pb-2 pr-2 overflow-hidden"
          data-testid="crew-profile-docked"
        >
          <div className="flex-1 min-h-0 rounded-2xl border border-border bg-bg-elevated shadow-lg overflow-hidden flex flex-col" style={{ width: profileCardW }} data-testid="crew-profile-card">{profilePanel}</div>
        </motion.aside>
      )}
    </AnimatePresence>
  )
  // The floating card fades in and out on the face flight's clock while the
  // face itself flies from the pill into the card head (and back): the one
  // moving thing is the face, the card is the surface it lands on. Mounted
  // through AnimatePresence so the close plays too; a plain portal unmounted
  // it in one frame.
  const floatingSurface = threadColumnRef.current ? createPortal(
    <AnimatePresence initial={false}>
      {profilePanel && !profileDocked && (
        <motion.div
          key="crew-profile-floating"
          className="absolute inset-0 z-30 flex justify-center px-2 py-2"
          role="presentation"
          onClick={(e) => { if (e.target === e.currentTarget) requestCloseProfile() }}
          data-testid="crew-profile-modal"
        >
          <motion.div
            className="h-full rounded-2xl border border-border-strong bg-bg-elevated shadow-[var(--shadow-float,var(--shadow-lg)),inset_0_1px_0_var(--card-hl)] overflow-hidden flex flex-col"
            style={{ width: `min(${profileCardW}px, calc(100% - 16px))` }}
            initial={reduceMotion ? false : { opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={reduceMotion ? undefined : { opacity: 0 }}
            transition={{ duration: FACE_FLIGHT_SECS, ease: FACE_FLIGHT_EASE }}
            data-testid="crew-profile-card"
          >{profilePanel}</motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    threadColumnRef.current,
  ) : null
  const profileSurface = profileDocked ? dockedSurface : floatingSurface
  return (
    <>
      {/* The docked column's AnimatePresence keeps ONE slot in both states,
                      so folding it runs its exit (a column that swapped against a
                      fragment here was unmounted outright, no width tween) and the
                      leaving card's face stays measurable for the flight. */}
                  {dockedSurface}
                  {profileDocked ? null : profileSurface}
    </>
  )
}
