/**
 * The DM thread column. `useMembersPageThread` is the open crewmate's thread:
 * the idempotent thread-open POST with its per-member sequencing, the
 * reconnect re-confirm, the first greeting, Fresh start, and the confirmed
 * slot the side panel may bind. `useMembersPageThreadView` is what the thread
 * shows and records while it is on screen: the identity pill's activity line,
 * the floating header's measured insets and phone top-bar slot, the greeting
 * card, and marking the slot read while the window is visible.
 * `MembersPageThreadColumn` renders the column: its notices, the team view,
 * the empty hero, the header and the `ChatPane`.
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
import { useCallback, useEffect, useMemo, useRef, useState, type MutableRefObject, type Dispatch, type SetStateAction, type RefObject } from 'react'
import { type NavigateFunction, type Location, type SetURLSearchParams } from 'react-router-dom'
import { RotateCw } from 'lucide-react'
import { useReducedMotion } from 'framer-motion'
import { usePreviewFlag } from '../../hooks/usePreviewFlag'
import { PREVIEW_CREW } from '../../utils/previewFlags'
import { Btn } from '../../components/ui'
import { type CreatedCrewmate, type CrewmateDraft } from './NewCrewmateDialog'
import { useTranslation } from 'react-i18next'
import { api, type MemberRosterRow } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { crewDisplayName } from '../../components/AgentSelector'
import { hasOtherMates, isAssistantMember } from '../../lib/assistantMember'
import { useFirstGreeting } from './useFirstGreeting'
import { MEMBERS_ROSTER_QUERY_KEY, memberThreadQueryKey, type MemberThreadOutcome } from '../../api/membersQuery'
import { useMutation, type QueryClient } from '@tanstack/react-query'
import { timeAgo } from '../../utils/timeAgo'
import { findReport, reportForError, type ErrorReport } from '../../utils/errorReport'
import { useAppDispatch, useAppSelector } from '../../store'
import { selectSlotMessages, selectSlotStreamState, selectSlotTurnFromWake } from '../../store/chatSlice'
import { useLanguage } from '../../i18n/LanguageProvider'
import { markSlotRead } from '../../store/dashboardSlice'
import { selectSlotLastTs } from '../../store/slotLastTs'
import { emitSlotRead, flushSlotRead } from '../../lib/slotReadRelay'
import { setViewedThreadSlot, clearViewedThreadSlot } from '../../lib/viewedThread'
import { setMountedMemberThreadSlot, clearMountedMemberThreadSlot } from '../../lib/mountedMemberThread'
import { PILL_ACTIVITY_KEY, type PillActivityKind } from './pillActivity'
import { useSlotActivity } from './useSlotActivity'
import { AWAITING_REPLY_ACTIVITY } from '../../components/chat/useCrewmateAwaitingReply'
import ChatPane from '../../components/ChatPane'
import MateWelcomeCard from './MateWelcomeCard'
import MateEntrance from './MateEntrance'
import CrewmateImportDialog from './CrewmateImportDialog'
import { useMateGreeting } from './mateGreeting'
import CrewComposer from './CrewComposer'
import ErrorBoundary from '../../components/ErrorBoundary'
import ErrorNotice from '../../components/ErrorNotice'
import { useConfirm } from '../../components/ConfirmDialog'
import { useConnected } from '../../hooks/useConnected'
import { hasNoCrewmates } from './rosterFilter'
import { useShellSlot } from '../../hooks/useShellSlot'
import { useMeasuredHeight } from '../../hooks/useMeasuredHeight'
import TeamView from './TeamView'
import { type MembersPageProfileFlight, type MembersPageProfileState } from './MembersPageProfile'
import { type MembersPageRosterView } from './MembersPageRoster'
import { CrewmateEmptyHero } from './MembersPageRosterRows'
import { type MembersPageSidePanelState } from './MembersPageSidePanel'
import { MembersPageThreadHeader } from './MembersPageThreadHeader'
import { createDraftFromHref, type GoneNotice, type PostCreateFailure, type ProfileCardOpen, type TFn } from './membersPageModel'
import { type MembersPageCrewEditing } from './useMembersPageCrewEditor'
import { type MembersPageDrawers } from './useMembersPageDrawers'
import { type MembersPageLayout } from './useMembersPageLayout'
import { type MembersPageNavigation } from './useMembersPageNavigation'
import { type MembersPageRosterRead, type MembersPageRosterSignals } from './useMembersPageRoster'
import { type MembersPageTeamsRead, type MembersPageTeamView } from './useMembersPageTeams'
import { slotRowByKey } from '../../store/slotIndex'

/** The notice for a refused Fresh start, by the route's refusal code: queued
 *  work says to wait, a turn that would not stop says to stop it first. */
function freshStartFailureKey(err: unknown): string {
  let code = ''
  if (err instanceof ApiError) {
    try { code = String((JSON.parse(err.body) as { code?: unknown }).code ?? '') } catch { /* not JSON */ }
  }
  if (code === 'slot_queue_pending') return 'pages.membersPage.fresh_start_failed_queued'
  if (code === 'turn_in_flight') return 'pages.membersPage.fresh_start_failed_busy'
  return 'pages.membersPage.fresh_start_failed'
}
/** The identity pill's second line, per activity kind (`pillActivity.ts`),
 *  for the kinds the page owns the copy of. `tool` and `thinking` are absent
 *  on purpose: their text comes from the shared status seam
 *  (`toolStatusLabel`), the same string the sessions sidebar paints for that
 *  moment. `writing` has its own word — the seam's "Streaming" is transport
 *  jargon, and this line says what the crewmate is doing. `idle` has two
 *  spellings — with the time since the thread last moved when one is known,
 *  bare when it is not. File-scope and indexed in place so the key checker
 *  resolves every entry. */
/** How often the "next wake in …" countdown in the drawer re-reads the clock.
 *  Coarser than the popover's per-second tick on purpose: the drawer line is
 *  an at-a-glance status, and a per-second re-render of the whole drawer for
 *  a readout that already drops seconds above a minute buys nothing. */
const PATROL_TICK_MS = 15_000

interface MembersPageThreadStateOptions {
  profileCard: MembersPageProfileState
  t: TFn
  queryClient: QueryClient
  pendingOpens: MutableRefObject<Set<string>>
  setFollowUp: Dispatch<SetStateAction<CreatedCrewmate | null>>
  followUpRef: MutableRefObject<CreatedCrewmate | null>
  threadOutcome: MemberThreadOutcome | undefined
  active: MemberRosterRow | undefined
}

/** The open crewmate's thread: open POST, greeting, Fresh start, reconnect and confirmed slot. */
export function useMembersPageThread({
  profileCard, t, queryClient, pendingOpens, setFollowUp, followUpRef, threadOutcome, active,
}: MembersPageThreadStateOptions) {
  const { requestCloseProfile } = profileCard
  const activeSlot = active ? threadOutcome?.slot_key ?? '' : ''
  // Fresh start: the server stops any turn and drops the conversation; the
  // slot key and its transcript stay. Each slot's reset time folds the rows
  // before it in the pane. The server folds `reset_at` into the member's own
  // event log (member/reset -> the roster projection), which is what makes the
  // fold survive a reload -- `activeRoster.reset_at` below reads it back off
  // the SAME roster projection block the row already carries (no second copy
  // of the field). This map is only the IN-SESSION override for the moment
  // between a successful clear and the next projection read catching up to it.
  const [freshStarts, setFreshStarts] = useState<Record<string, string>>({})
  const [freshStartError, setFreshStartError] = useState<{ slot: string; message: string; report?: ErrorReport } | null>(null)
  const freshStart = useMutation({
    mutationFn: ({ slug }: { slug: string; slot: string }) => api.memberFreshStart(slug),
    onMutate: () => setFreshStartError(null),
    // Fold only once the conversation is gone. A queued clear (the session was
    // still busy) says so in the transcript line the clear path writes.
    //
    // `reset_persisted: false` is a cleared conversation whose fold BOUNDARY
    // never reached the member's event log, so the fold holds for this session
    // and is gone after a reload. That is a backend failure the user has to
    // see, and the notice below is where this page shows one; the clear itself
    // is not retried, since the conversation is already gone and only the
    // record is missing.
    onSuccess: (res, { slot }) => {
      if (res.outcome !== 'cleared') return
      setFreshStarts((prev) => ({ ...prev, [slot]: res.reset_at }))
      if (!res.reset_persisted) setFreshStartError({ slot, message: t('pages.membersPage.fresh_start_unsaved_fold') })
    },
    onError: (err: unknown, { slot }) => setFreshStartError({
      slot,
      message: t(freshStartFailureKey(err)),
      report: findReport(err instanceof Error ? err.message : undefined),
    }),
  })
  // The page's themed confirm, as the Schedules guard uses: one ask, naming
  // what stops and what stays.
  const { confirm: confirmFreshStart, confirmDialog: freshStartConfirmDialog } = useConfirm()
  // Fresh start lives in the Profile card. One ask; the card closes on a yes,
  // so the fold in the chat is what the user sees next.
  const freshStartMutate = freshStart.mutate
  const askFreshStart = useCallback(() => {
    if (!active || !activeSlot) return
    const slug = active.slug
    const slot = activeSlot
    void confirmFreshStart({
      title: t('pages.membersPage.fresh_start_confirm_title'),
      body: t('pages.membersPage.fresh_start_confirm_body'),
      confirmLabel: t('pages.membersPage.fresh_start_confirm_action'),
    }).then((ok) => {
      if (!ok) return
      requestCloseProfile()
      freshStartMutate({ slug, slot })
    })
  }, [active, activeSlot, confirmFreshStart, t, requestCloseProfile, freshStartMutate])
  // The Crewmates preview. Mate is created in the background whatever it says,
  // but nothing about Mate is put in front of the user -- no landing, no
  // welcome -- until the preview is on and the page is opened.
  const crewPreview = usePreviewFlag(PREVIEW_CREW)
  // Mate's first-visit landing happens at most once per page mount, so a phone
  // Back to the roster is never bounced into Mate's chat again.
  const mateLandedRef = useRef(false)
  // Mate, and a crewmate just created here, speak first: once the confirmed
  // thread is open, ask the gateway for the first greeting. The server greets
  // only an empty, never-greeted thread that still owes one (Mate's welcome,
  // or the goal question of a crewmate created with `first_greeting`), so this
  // is a no-op on every later open. Asked only for Mate (with the preview on)
  // and dashboard-created crewmates: no other row can be owed one.
  // The first Mate's entrance (#23207): in the one tab whose greeting request
  // the server answers `started`, Mate's face enters centre stage with a line,
  // flies into the header pill's face slot, and only then does the greeting
  // turn stream in. Keyed on that answer alone, so a reload, a second tab or a
  // failed turn (`already_greeted`, `not_empty`, an error) never replays it:
  // the server's own once-only claim is the entrance's once-only too. Mate
  // only: a mate created on the New mate card already showed the person the
  // face and name they picked, so there is no one new to meet there.
  const reduceMotion = useReducedMotion()
  const [entranceState, setEntrance] = useState<MateEntranceState | null>(null)
  const startEntrance = useCallback((slot: string) => setEntrance({ slot, phase: 'stage' }), [])
  const landEntrance = useCallback(
    () => setEntrance((e) => (e && e.phase === 'stage' ? { ...e, phase: 'landed' } : e)),
    [],
  )
  const endEntrance = useCallback(() => setEntrance(null), [])
  useFirstGreeting(
    active?.slug,
    activeSlot,
    !!active && ((crewPreview && isAssistantMember(active)) || active.dashboard_created === true),
    isAssistantMember(active) ? startEntrance : undefined,
  )
  // An entrance belongs to the thread it was answered for. Leaving that thread
  // mid-entrance ends it (the stage unmounts with the column) and nothing
  // brings it back: `started` is answered once per thread.
  const entrance = entranceState && entranceState.slot === activeSlot ? entranceState : null
  useEffect(() => {
    if (entranceState && entranceState.slot !== activeSlot) setEntrance(null)
  }, [entranceState, activeSlot])
  // While the face is on stage the header pill's slot and the roster row hold
  // their face unpainted, so there is one face on screen. Reduced motion has
  // no face on stage, so both keep theirs from the start.
  const entranceFaceHidden = !!entrance && entrance.phase === 'stage' && !reduceMotion
  // Two distinct verdicts with two different sentences: a collision is a
  // fact about the roster (the slug's thread belongs to another crew), a
  // failed POST is a transport error. Both render through ErrorNotice so
  // the structured report and the agent hand-off survive.
  const activeCollision = active ? threadOutcome?.collision ?? '' : ''
  const activeThreadFailed = !!active && !!threadOutcome?.failed
  // The thread open. A mutation, not a query: the endpoint is a write (the
  // idempotent creator/repairer of member slots), so it is issued on EVERY
  // open — never served from cache — and its answer is what the cache holds.
  // Outcomes are keyed by the member the POST was FOR, so a late answer for a
  // member the user has already left lands in that member's entry, not the
  // open one's. The roster row is patched with a freshly confirmed key so the
  // row's live readings (presence dot, unread, patrol badge) resolve to the
  // same slot the thread mounted on, without a roster refetch that would
  // re-sort the list under the cursor.
  const setThreadOutcome = useCallback(
    (name: string, update: (prev: MemberThreadOutcome | undefined) => MemberThreadOutcome) =>
      queryClient.setQueryData<MemberThreadOutcome>(memberThreadQueryKey(name), update),
    [queryClient],
  )
  // Per-member sequence of thread POSTs. Within ONE member only the LATEST
  // request may write its verdict back: an older answer arriving after a newer
  // one (a re-click on a slow link) is dropped whole, because letting a stale
  // success overwrite the refusal the newest POST just recorded would re-bind
  // the side panel to a key the endpoint has since refused — aiming Side chat
  // / Artifacts / Terminal at a foreign session. Dropping it loses nothing:
  // every open re-POSTs. Across members the rule above stands untouched (a
  // late answer for another member is that member's newest, so it lands).
  const threadReqSeq = useRef<Record<string, number>>({})
  // The "+" hold (`followUp`) is released by an open's outcome ONLY when the
  // hold belongs to the crewmate that open was for: re-clicking crewmate A
  // while crewmate B's follow-up is still in flight lands A's answer here, and
  // clearing unconditionally would drop B's hold mid-follow-up. A's own hold
  // (if any) is the one this open may release.
  const releaseFollowUp = useCallback((name: string) => {
    if (followUpRef.current?.name === name) setFollowUp(null)
  }, [followUpRef, setFollowUp])
  const openThread = useMutation({
    mutationFn: (m: MemberRosterRow) => api.memberThread(m.slug),
    onMutate: (m) => {
      // A previous verdict for this member is retired while the re-POST is
      // out: a confirmed key keeps rendering (the cached thread stays up), a
      // collision or failure line comes down until the new answer is in.
      setThreadOutcome(m.name, (prev) => ({ slot_key: prev?.slot_key ?? '' }))
      // The sequence number rides the mutation context to onSuccess/onError.
      const seq = (threadReqSeq.current[m.name] ?? 0) + 1
      threadReqSeq.current[m.name] = seq
      return seq
    },
    onSuccess: (r, m, seq) => {
      if (seq !== threadReqSeq.current[m.name]) return
      if (r.member !== m.name) {
        // The slug's thread belongs to another crew (lossy-slug collision,
        // first-bound-wins). Mounting it would be a silent misroute — the
        // defining failure for a page whose premise is identity.
        setThreadOutcome(m.name, () => ({ slot_key: '', collision: r.member }))
        if (pendingOpens.current.delete(m.name)) releaseFollowUp(m.name)
        return
      }
      setThreadOutcome(m.name, () => ({ slot_key: r.slot_key }))
      // The crewmate created moments ago has its chat: the follow-up is over.
      if (pendingOpens.current.delete(m.name)) releaseFollowUp(m.name)
      if (m.slot_key !== r.slot_key) {
        queryClient.setQueryData<MemberRosterRow[]>(MEMBERS_ROSTER_QUERY_KEY, (rows) =>
          rows?.map((row) => (row.name === m.name ? { ...row, slot_key: r.slot_key, bound: true } : row)),
        )
      }
    },
    onError: (error, m, seq) => {
      if (seq !== threadReqSeq.current[m.name]) return
      // The hold comes down: the failure notice under this line is the
      // user's surface now. The greeting the server owes this crewmate is
      // still owed, so its next successful open (the re-click repair gesture,
      // the reconnect re-POST) asks for it then.
      if (pendingOpens.current.delete(m.name)) releaseFollowUp(m.name)
      setThreadOutcome(m.name, (prev) => ({
        slot_key: prev?.slot_key ?? '',
        failed: true,
        errorReport: findReport(error instanceof Error ? error.message : undefined),
      }))
    },
  })
  const { mutate: postThread } = openThread
  // The cached key is trusted only for as long as the gateway is known not to
  // have restarted. A dropped-then-restored socket is the one client-visible
  // sign that it may have (a restart drops an unmessaged member slot while
  // its binding survives), so on a RECONNECT the open member's thread is
  // re-confirmed by the same idempotent POST every open makes — the mounted
  // pane stays up meanwhile, exactly as during any other repair. The
  // websocket hook forgets the entries nobody is looking at at the same
  // moment. Only a true->false->true sequence seen by THIS mounted page
  // counts: the first connect after a reload is not a reconnect, and the
  // roster-driven open already confirms the thread then.
  const connected = useConnected()
  const hadConnectionRef = useRef(false)
  const activeRowRef = useRef<MemberRosterRow | undefined>(undefined)
  activeRowRef.current = active
  useEffect(() => {
    if (!connected) return
    if (hadConnectionRef.current && activeRowRef.current) postThread(activeRowRef.current)
    hadConnectionRef.current = true
  }, [connected, postThread])

  // The member whose thread POST is IN FLIGHT — the mutation's own pending
  // reading, which follows the LATEST call: a fast re-click (two POSTs out)
  // stays pending until the second answers, so the first one's completion
  // cannot re-bind the panel while the second is still unanswered. While it
  // is in flight the cached key is only a render hint for the thread column:
  // the side panel must not bind to it, because the POST may come back
  // refusing that very key (renamed / deleted member, a stale binding another
  // session now occupies) and a panel action dispatched in the window — an
  // artifact involvement write, a Side chat turn — cannot be recalled by the
  // unbind that follows.
  const pendingThreadFor = openThread.isPending ? openThread.variables?.name ?? '' : ''
  // The slot the SIDE PANEL may bind: the cached key only once the CURRENT
  // open's POST has confirmed it. Empty for the whole in-flight window, so no
  // slot-bound view is offered and no document action can record against a
  // key the endpoint is about to refuse — and empty again after a REFUSAL: a
  // 409 means the canonical key is occupied by a session that is not this
  // member's (or the endpoint could not repair it), so a cached key kept
  // through it would leave every slot-bound panel view (Side chat, Artifacts,
  // Files…) aimed at a foreign session. The panel falls back to its slot-free
  // Dashboard tab; Profile is separate. The thread keeps rendering the cached key under its
  // own failure notice (its pre-existing contract, see activeThreadFailed).
  const confirmedSlot =
    active && (pendingThreadFor === active.name || activeThreadFailed) ? '' : activeSlot

  return {
    activeSlot, freshStarts, freshStartError, setFreshStartError, freshStart, freshStartConfirmDialog,
    askFreshStart, crewPreview, mateLandedRef, activeCollision, activeThreadFailed, postThread, connected,
    confirmedSlot, entrance, entranceFaceHidden, landEntrance, endEntrance,
  }
}

/** The first Mate's entrance in progress (#23207): `stage` while the face is
 *  centre stage or in flight, `landed` while the slot's own face pops. */
export interface MateEntranceState {
  slot: string
  phase: 'stage' | 'landed'
}

export type MembersPageThreadState = ReturnType<typeof useMembersPageThread>

interface MembersPageThreadViewOptions {
  layout: MembersPageLayout
  rosterView: MembersPageRosterView
  thread: MembersPageThreadState
  t: TFn
  createOpen: boolean
  active: MemberRosterRow | undefined
  /** The Profile card is up as a FLOATING card over the thread column (its
   *  docked placement sits beside the chat and covers nothing). */
  profileFloating: boolean
  /** The phone roster drawer is mounted over the thread column (opening, open
   *  or closing); it covers the chat for all three. */
  rosterDrawerMounted: boolean
}

/** The header's live readings, the greeting card, and read marking while the thread is on screen. */
export function useMembersPageThreadView({
  layout, rosterView, thread, t, createOpen, active, profileFloating, rosterDrawerMounted,
}: MembersPageThreadViewOptions) {
  const { isMobile, rosterBar } = layout
  const { isRunning, activeView } = rosterView
  const { activeSlot, confirmedSlot } = thread
  // Mounting a member thread IS reading it, but nothing on this page moves
  // `chat.activeSlot` (that transition belongs to the Sessions page's
  // switchSlot, the only other markSlotRead caller). Two things follow:
  //
  // 1. The websocket unread-marker must learn about the open thread another
  //    way, or it flags every message that lands in it. It reads
  //    `viewedThread` beside `chat.activeSlot`; the visible-view effect below
  //    registers the mounted thread there. Before this, each arrival was
  //    flagged and drained a render later, and both writes relayed to the
  //    parent dashboard's crew tab -- a badge that lit and vanished on every
  //    message.
  // 2. A flag that was set while the thread was NOT on screen (closed, or
  //    this window hidden) still has to be drained when it opens or is
  //    revealed. Without this the rail badge is permanent -- no code path
  //    clears a live member slot's unread until the slot itself is deleted.
  const dispatch = useAppDispatch()
  const activeSlotUnread = useAppSelector(
    (s) => !!activeSlot && s.dashboard.unreadSlots.includes(activeSlot),
  )
  const activeSlotLastTs = useAppSelector(
    (s) => (activeSlot ? selectSlotLastTs(s, activeSlot) : undefined),
  )
  // The identity pill's second line — what the crewmate is doing now, read
  // through the ONE seam the chat's live status line reads too
  // (`useSlotActivity`): the slot's live status record, the SAME one the
  // sessions sidebar and the command palette render through `toolStatusLabel`,
  // so the pill names a moment the way the sidebar row does and honours the
  // `simplifiedToolNames` preference. The key falls back to the roster's
  // slot_key the same way the avatar does, so a thread whose confirmed slot
  // has not resolved yet still reads live.
  const pillSlotKey = activeSlot || active?.slot_key || ''
  // The DM header floats over the top of the thread (#18325): the identity
  // pill is a Glass chip, so the conversation scrolls under it the way it
  // scrolls under the composer dock. Two measurements, never constants: the
  // header's own height is what the content under it pays, and the block of
  // notices that can sit between header and pane reports
  // its VISIBLE height (its own content, never the padding above it) so the
  // pane knows whether IT is the thing under the header (nothing between: the
  // pane's rows pad below the header) or something else is (a card between:
  // the card pads, the pane starts below the card).
  const [threadHeaderRef, threadHeaderH] = useMeasuredHeight<HTMLElement>()
  // Bumped by the unread column's "+N" pill; the switcher opens its list on
  // each change (a counter, so two presses in a row both open).
  const [switcherOpenRequest, setSwitcherOpenRequest] = useState(0)
  /** Phone: the shell's single top bar slot this page's thread header moves
   *  into while a crewmate is open, as the chat page's header does. `null` on
   *  a wide window, a bare roster, or any host that renders no such slot. */
  // Only while the page reports `open`: before that the shell may still swap
  // the single bar out (a read that ends on the bare roster), and a slot
  // looked up then would be a detached node the header portals into, out of
  // sight. Asking again once `open` is reported finds the shell's current one.
  const topbarSlot = useShellSlot('mobile-topbar-slot', isMobile && rosterBar === 'open')
  const headerInBar = !!topbarSlot
  /** The transcript's top inset: the floating header's height, or nothing
   *  once the header sits in the shell bar above the pane. */
  const headerInset = headerInBar ? 0 : threadHeaderH
  const [betweenHeaderRef, betweenHeaderH] = useMeasuredHeight<HTMLDivElement>()
  const pillStreamState = useAppSelector((s) => (pillSlotKey ? selectSlotStreamState(s, pillSlotKey) : 'idle'))
  const pillLiveSlot = useAppSelector((s) => (pillSlotKey ? slotRowByKey(s.dashboard.slots, pillSlotKey) : undefined))
  const uiLang = useLanguage().resolved
  const pillTurnAct = useSlotActivity(pillSlotKey, {
    running: !!active && !!isRunning(active),
    delegatedOnly: !!pillLiveSlot?.subagents_running && !pillLiveSlot?.running,
    stopping: !!pillLiveSlot?.stopping,
  })
  // Preparation outside a turn reads as working, never as the last turn's status.
  const pillAct = pillTurnAct.kind === 'idle' && pillLiveSlot?.preparing ? AWAITING_REPLY_ACTIVITY : pillTurnAct
  // The resting line's age ("Idle · 6m ago") is on screen for as long as the
  // thread rests, so it must move on its own: re-read the clock on the
  // drawer's coarse tick while the pill is resting, and not at all while it is
  // busy (the busy line carries no age). A separate clock from the Schedules card, because it runs under a different
  // condition.
  const pillResting = !!active && !isRunning(active) && pillStreamState === 'idle'
  // The greeting the open chat starts on (cold welcome or warm resume), read
  // once per open of a confirmed thread. Mid-turn means the crewmate's OWN
  // turn: workers it runs do not count, since a goal in flight is the very case
  // the resume speaks to. Nor does a background wake (a monitor loop's cycle,
  // a worker's completion): a conductor is woken every few minutes, so
  // counting those turns took the card down within a minute and skipped every
  // open that met one.
  // Who opened a running turn is read off the transcript, so a busy slot's
  // open waits for it: the thread confirms before the transcript lands.
  const pillLoopTurn = useAppSelector((s) => (pillSlotKey ? selectSlotTurnFromWake(s, pillSlotKey) : false))
  const pillTranscriptKnown = useAppSelector((s) => (pillSlotKey ? selectSlotMessages(s, pillSlotKey).length > 0 : false))
  const greetingIdle = pillLoopTurn || (pillStreamState === 'idle' && !pillLiveSlot?.running)
  const { greeting: mateGreeting, failure: mateGreetingFailure, dismiss: dismissMateGreeting } = useMateGreeting(
    greetingIdle || pillTranscriptKnown ? confirmedSlot : '',
    greetingIdle,
    activeView ? { slug: activeView.slug, member: activeView.name, lastActiveTs: activeView.last_active_ts ?? 0 } : null,
  )
  const pillLastActive = (activeView ?? active)?.last_active_ts
  const [pillIdleAge, setPillIdleAge] = useState('')
  useEffect(() => {
    if (!pillResting || !pillLastActive) { setPillIdleAge(''); return }
    const read = () => setPillIdleAge(timeAgo(pillLastActive))
    read()
    const timer = setInterval(read, PATROL_TICK_MS)
    return () => clearInterval(timer)
    // `uiLang` is a deliberate extra dep: the age is a formatted string in the
    // UI language, so a language switch must re-read it at once rather than on
    // the next tick.
  }, [pillResting, pillLastActive, uiLang])
  const pillActivity = useMemo(() => {
    const label = pillAct.text !== undefined
      ? pillAct.text
      : pillAct.kind === 'idle' && pillIdleAge
        ? t('pages.membersPage.pill_idle_since', { when: pillIdleAge })
        : t(PILL_ACTIVITY_KEY[pillAct.kind as Exclude<PillActivityKind, 'tool' | 'thinking'>])
    return { kind: pillAct.kind, label }
  }, [pillAct, t, pillIdleAge])
  // Reactive document visibility AND focus, so the read effect below re-runs
  // when the user returns to a hidden tab or focuses the window — a plain
  // document.hidden read would leave the effect settled and the reveal
  // unnoticed, and Page Visibility alone calls occluded/unfocused windows
  // "visible", which would let a parked window mark threads read.
  const [pageVisible, setPageVisible] = useState(() => !document.hidden && document.hasFocus())
  useEffect(() => {
    const onVis = () => setPageVisible(!document.hidden && document.hasFocus())
    document.addEventListener('visibilitychange', onVis)
    window.addEventListener('focus', onVis)
    window.addEventListener('blur', onVis)
    return () => {
      document.removeEventListener('visibilitychange', onVis)
      window.removeEventListener('focus', onVis)
      window.removeEventListener('blur', onVis)
    }
  }, [])
  // The New crewmate card hides the chat while it
  // stays mounted: hidden is not viewed, so it neither reads nor suppresses.
  const threadShown = pageVisible && !createOpen
  // What the PANE may count as seen: the thread is shown AND nothing floats
  // over the chat. An overlay side panel (a phone sheet, a desktop overlay
  // under its scrim), a floating Profile card and the phone roster drawer all
  // cover the transcript, while the docked placements sit beside it, so a reply
  // landing behind any of them stays unseen. Narrower than `threadShown` on
  // purpose: the roster's read marking above keeps its own meaning of a viewed
  // thread.
  const chatUncovered = threadShown && !(layout.panelVisible && !layout.beside) && !profileFloating && !rosterDrawerMounted
  useEffect(() => {
    // Viewing the thread is the read — but only a VISIBLE view is a view. A
    // hidden member tab neither clears its own badge nor relays one; on
    // reveal this effect re-runs via pageVisible and does both. The relay is
    // NOT gated on this window's own badge: this window may have read the
    // thread earlier (locally clean) while another window's badge is still
    // lit, and a visible view here retires that too. Throttled per slot by
    // the relay module; watermarked at the slot's newest known message ts.
    if (activeSlot && threadShown) {
      if (activeSlotUnread) dispatch(markSlotRead(activeSlot))
      emitSlotRead(activeSlot, activeSlotLastTs)
    }
  }, [activeSlot, activeSlotUnread, threadShown, activeSlotLastTs, dispatch])
  // Tell the unread-marker which thread is on screen, for exactly as long as
  // it is: registered while the thread is mounted AND this window is visible
  // and focused, retired on switch, hide, blur and unmount. A hidden window's
  // open thread therefore badges like any other slot, and the read effect
  // above drains it on reveal -- same visibility bar for both directions.
  useEffect(() => {
    if (!activeSlot || !threadShown) return
    setViewedThreadSlot(activeSlot)
    return () => {
      // Like switchSlot, flush before retiring the view so its trailing read
      // timer cannot outlive it and clear a later, unseen message's badge.
      flushSlotRead(activeSlot)
      clearViewedThreadSlot(activeSlot)
    }
  }, [activeSlot, threadShown])

  // Presence for return summaries is visible-mount based, not focus based.
  // The unread registry above keeps its stricter focused-window rule.
  const mountedThreadShown = !createOpen
  useEffect(() => {
    if (!activeSlot || !mountedThreadShown) return
    setMountedMemberThreadSlot(activeSlot)
    return () => clearMountedMemberThreadSlot(activeSlot)
  }, [activeSlot, mountedThreadShown])

  return {
    threadHeaderRef, setSwitcherOpenRequest, switcherOpenRequest, topbarSlot, headerInBar, headerInset,
    betweenHeaderH, betweenHeaderRef, dismissMateGreeting, mateGreeting, mateGreetingFailure, pillActivity,
    // The same "a person can see the thread" the read marking above uses; the
    // pane's unread landing marks a message read under the same condition.
    threadShown,
    chatUncovered,
  }
}

export type MembersPageThreadView = ReturnType<typeof useMembersPageThreadView>

interface MembersPageThreadColumnProps {
  rosterRead: MembersPageRosterRead
  crewEditing: MembersPageCrewEditing
  teamsRead: MembersPageTeamsRead
  layout: MembersPageLayout
  profileCard: MembersPageProfileState
  drawers: MembersPageDrawers
  rosterView: MembersPageRosterView
  thread: MembersPageThreadState
  sidePanel: MembersPageSidePanelState
  threadView: MembersPageThreadView
  rosterSignals: MembersPageRosterSignals
  profileFlight: MembersPageProfileFlight
  navigation: MembersPageNavigation
  teamView: MembersPageTeamView
  navigate: NavigateFunction
  location: Location
  activeName: string
  setSearchParams: SetURLSearchParams
  editingCrew: string
  profile: ProfileCardOpen | null
  gone: GoneNotice | null
  openCreate: (expanded: boolean, draft?: CrewmateDraft, leaveGranted?: boolean) => void
  openAdvanced: () => void
  openQuick: () => void
  threadColumnRef: RefObject<HTMLElement>
  postCreateError: PostCreateFailure | null
  setPostCreateError: Dispatch<SetStateAction<PostCreateFailure | null>>
  postCreateDismissable: boolean
  createHeld: string | null
  threadOutcome: MemberThreadOutcome | undefined
  slotsLoaded: boolean
  active: MemberRosterRow | undefined
  leftCreateFailure: string | null
  setLeftCreateFailure: Dispatch<SetStateAction<string | null>>
  retryPostCreate: () => void
}

/** The page's main column: the thread, the team view, or an empty state. */
export function MembersPageThreadColumn({
  rosterRead, crewEditing, teamsRead, layout, profileCard, drawers, rosterView, thread, sidePanel, threadView,
  rosterSignals, profileFlight, navigation, teamView, navigate, location, activeName, setSearchParams,
  editingCrew, profile, gone, openCreate, openAdvanced, openQuick, threadColumnRef, postCreateError,
  setPostCreateError, postCreateDismissable, createHeld, threadOutcome, slotsLoaded, active, leftCreateFailure,
  setLeftCreateFailure, retryPostCreate,
}: MembersPageThreadColumnProps) {
  const { t } = useTranslation()
  const { rosterQuery, loaded, loadError, members } = rosterRead
  const { crewAgentsQuery, closeCrewEditor } = crewEditing
  const { teamsNoticeShown, dismissTeamsNotice, activeTeam, setTeamDialog } = teamsRead
  const { rosterPinned, landingPending } = layout
  const { openRosterDrawer, rosterDrawerAvailable } = drawers
  const { activeRoster, crewmateIdentity, setStarError, starError } = rosterView
  const {
    activeSlot, freshStarts, freshStartError, setFreshStartError, activeCollision, activeThreadFailed,
    connected, entrance, landEntrance, endEntrance,
  } = thread
  const { pillFaceRef } = profileCard
  const { reduceMotion } = profileFlight
  // What Mate says on stage: "your first Mate" only while it IS the only mate
  // on the roster, the same split the server's welcome kickoff makes
  // (`FIRST_WELCOME` vs `FIRST_WELCOME_WITH_CREW`); the reserved `default`
  // agent on every roster does not count.
  const entranceText = active && entrance
    ? hasOtherMates(members)
      ? t('pages.membersPage.entrance_hello', { name: crewDisplayName(active) })
      : t('pages.membersPage.entrance_first_mate')
    : ''
  // The pane is veiled while the face is on stage, so the greeting's first row
  // cannot stream in under the entrance; it fades back as the face lands.
  const entranceOnStage = !!entrance && entrance.phase === 'stage'
  const {
    openMemberSideChat, openCrewWorkLog, sessionRoster, retryThreadsFlag, threadsFlagFailed,
    threadsFlagRetrying, threadsOn, threadsQuery, threadHooks, actionError, setActionError, actionInfo,
    openFileGuarded, openSessionGuarded,
  } = sidePanel
  const {
    headerInset, betweenHeaderH, betweenHeaderRef, dismissMateGreeting, mateGreeting, mateGreetingFailure,
  } = threadView
  const { patrol } = rosterSignals
  const { openMember, openArtifactInThread } = navigation
  const { closeTeamView, teamMembers } = teamView
  // Import from file on the empty roster's hero (the switcher is not shown yet).
  const [importing, setImporting] = useState(false)
  // The post-create notice (a failed roster re-read) with its retry, rendered
  // ONCE, in the chat column: below md a roster notice brings that column on
  // screen (see the two `className`s: this column's `<section>` below, and
  // `MembersPageRosterColumn`'s `<aside>` in MembersPageRoster.tsx).
  const postCreateNotice = postCreateError && (
    /* No hand-off: a chat may already be mounted under this line with
       a draft in its composer (ChatPane keeps it in local state), and
       the hand-off navigates away, unmounting it. The retry repeats the
       one step that failed; the create itself already succeeded. */
    <div
      // With no chat open the notice is the whole column's content: centred
      // like the hero it replaced, so the empty pane reads as intended, not
      // broken.
      className={!active
        ? 'flex flex-1 flex-col items-center justify-center gap-3 px-6 py-10 text-center animate-rise'
        : 'flex flex-wrap items-center gap-2 px-4 py-2'}
      data-testid="member-post-create-error"
    >
      {/* The retry sits right after the text, not at the far edge of a
          stretched notice: the two read as one sentence, and its label
          names the one step it repeats ("Refresh your crewmates") so it
          cannot read as "create Radar again". The dismiss follows
          `postCreateDismissable`: over an empty cached roster there is
          none — closing it would put "No crewmates yet" under a crewmate
          that exists, and the retry is the only honest way off it; over a
          roster with other rows it can be closed, so the existing chats
          stay reachable below md. */}
      <ErrorNotice
        message={t('pages.membersPage.create_roster_failed', { name: postCreateError.created.name })}
        variant="inline"
        onDismiss={postCreateDismissable ? () => setPostCreateError(null) : undefined}
        className="min-w-0"
      />
      <Btn onClick={retryPostCreate} className="shrink-0" data-testid="member-post-create-retry">
        {t('pages.membersPage.create_retry_roster')}
      </Btn>
    </div>
  )
  return (
    <section
            // Below md the column shows only while a chat or a team view is open —
            // except while a ROSTER post-create notice is up: a failed re-read
            // opens no chat, and hiding the column would hide the one place the
            // failure and its retry are said.
            className={`${activeName || activeTeam || postCreateError ? 'flex' : 'hidden md:flex'} flex-1 min-w-0 flex-col min-h-0 relative`}
            ref={threadColumnRef}
            // The page's main column: the thread (or the team view, or an empty
            // state), and never the side panel, which is a sibling of this section.
            // Named so a case can ask what the THREAD says without matching the
            // panel's own copy — the two surfaces share several sentences, "Opening
            // the conversation…" among them, and each says it about itself.
            data-testid="member-main-column"
            onClickCapture={(event) => {
              if (!isAssistantMember(active) || event.button !== 0 || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return
              const anchor = event.target instanceof Element ? event.target.closest('a[href]') : null
              if (!anchor) return
              const draft = createDraftFromHref(anchor.getAttribute('href'), window.location.href)
              if (!draft) return
              event.preventDefault()
              event.stopPropagation()
              openCreate(false, draft)
            }}
          >
            {postCreateNotice}
            {leftCreateFailure && (
              /* No hand-off: the chat the user chose is mounted under this line
                 with its own draft, and a hand-off would navigate away from it. */
              <div className="flex flex-wrap items-center gap-2 px-4 py-2" data-testid="member-left-create-failed">
                <ErrorNotice
                  message={leftCreateFailure}
                  variant="inline"
                  onDismiss={() => setLeftCreateFailure(null)}
                  className="min-w-0"
                />
              </div>
            )}
            {/* The hero yields to a post-create notice: after the FIRST create a
                failed re-read leaves the cached roster at [] while the crewmate
                exists, and "No crewmates yet" under "Radar was created" would be
                two contradicting statements in one column. It yields to an open
                team view too: a team can exist before its first crewmate does,
                and the pane is the team's then. */}
            {!active && !activeTeam && !postCreateError && loaded && !loadError && hasNoCrewmates(members) && (
              <CrewmateEmptyHero onCreate={openQuick} onAdvanced={openAdvanced} held={createHeld} onImport={() => setImporting(true)} />
            )}
            {importing && <CrewmateImportDialog open onOpenChange={setImporting} />}
            {/* A failed roster read at md and up: the roster column shows its own
                notice, but this column would otherwise be blank — no chat can
                open (the URL sync waits on the roster) and the hero is gated on a
                successful read. Say the same failure here, so a wide window is
                never half empty. Hidden below md, where this column is not shown
                and the roster's notice is the screen. An open team view carries
                its own copy of this notice (below), so it steps aside then. */}
            {!active && !activeTeam && !postCreateError && loadError && (
              <div className="hidden md:flex flex-1 flex-col items-center justify-center gap-3 px-6 py-10 text-center" data-testid="member-column-load-error">
                <ErrorNotice
                  message={t('pages.membersPage.roster_load_failed')}
                  variant="inline"
                  askAgent
                  // Named, not "the agent": on a page of crewmates a bare "agent"
                  // reads as a third party, and the hand-off goes unused.
                  askAgentLabel={t('pages.membersPage.roster_load_failed_ask')}
                />
                {/* Same shape as the post-create roster notice: the plain retry
                    under the sentence, the hand-off link beside it as the second
                    option, never the only one. */}
                <Btn onClick={() => void rosterQuery.refetch()} data-testid="member-column-load-retry">
                  {t('pages.membersPage.roster_load_retry')}
                </Btn>
              </div>
            )}
            {/* The team view takes the pane a chat would: the manager's desk for
                the open team. Keyed by team so switching teams remounts its reads. */}
            {/* The desktop landing folds the roster before its crewmate opens
                (`landingPending`), so the pane says what it is waiting on rather
                than standing empty: the same line an open with no pane yet shows. */}
            {!active && landingPending && (
              <div className="flex-1 flex items-center justify-center text-xs text-muted" data-testid="member-landing-pending">
                {t('pages.membersPage.opening_thread')}
              </div>
            )}
            {!active && activeTeam && (
              <>
                {/* The roster column -- and the notice it carries -- is hidden below
                    md while a team is open, so a failed read is said HERE too: the
                    team on screen may be stale. The pane's copy steps out of the way
                    where the roster's own copy is on screen: the roster stands beside
                    the pane at md and up in a team view, so the pane's notice hides
                    there (`md:hidden`), exactly the column's own hidden range. */}
                {loadError && (
                  <div className="px-4 pt-3 md:hidden">
                    <ErrorNotice
                      message={t('pages.membersPage.roster_load_failed')}
                      variant="inline"
                      askAgent
                      testId="team-view-roster-error"
                    />
                  </div>
                )}
                {teamsNoticeShown && (
                  <div className="px-4 pt-3 md:hidden">
                    <ErrorNotice
                      message={t('pages.membersPage.teams_load_failed')}
                      variant="inline"
                      askAgent
                      onDismiss={dismissTeamsNotice}
                      testId="team-view-teams-error"
                    />
                  </div>
                )}
                <TeamView
                  key={activeTeam.id}
                  team={activeTeam}
                  members={teamMembers}
                  rosterFailed={loadError}
                  onOpenMember={openMember}
                  onEdit={() => setTeamDialog({ team: activeTeam })}
                  onBack={rosterDrawerAvailable ? openRosterDrawer : closeTeamView}
                />
              </>
            )}
            {active && (
              <>
                {/* The header floats over the thread's top edge (#18325), the way
                    ChatTitleOverlay's title row overlays its transcript: absolute, no
                    background of its own, so the conversation scrolls under the
                    Glass identity pill and the pill reads as a chip on the
                    conversation rather than a bar above it. The pane under it pays
                    for the band with its scroller's top padding (`topInset`,
                    measured from this box by `threadHeaderRef`), so at scroll top
                    nothing hides under the pill. `pointer-events-none` on the row
                    and on its three cells, restored on the controls only, so a
                    wheel or touch beside the pill reaches the transcript (the
                    strip beside an iOS toolbar does the same). z-20: above the
                    pane's own chrome (its dock and pinned prompt, z-10 at most),
                    below the floating Profile card (z-30), whose scrim covers the
                    header. No rule under it: a hairline here read as a second
                    frame inside the pane (issue #9425).
                    Three columns, the outer two equal, so the identity pill in the
                    middle is centred on the pane whether or not the back button
                    (narrow) or the panel opener (docked, panel hidden) is present:
                    a flex row with `flex-1` around the pill would shift it by the
                    width of whichever side control is missing. */}
                <MembersPageThreadHeader
                  layout={layout} profileCard={profileCard} drawers={drawers} rosterView={rosterView}
                  thread={thread} threadView={threadView} rosterSignals={rosterSignals}
                  profileFlight={profileFlight} navigation={navigation} navigate={navigate} location={location}
                  setSearchParams={setSearchParams} profile={profile} openQuick={openQuick}
                  createHeld={createHeld} active={active}
                />
                {/* Everything that can sit between the header and the pane —
                    notices, the greeting cards — stays in flow and is padded
                    below the floating header by the header's measured height;
                    these carry controls, and a control under the glass is an
                    ambiguous tap. The padding goes on the OUTER box and the
                    measurement on the INNER one, so the padding never counts
                    toward the height that decides whether anything is shown:
                    with nothing visible the inner box is 0px tall (a child that
                    is in the DOM but `display: none` — the roster-error block
                    under `md:hidden` with the roster pinned — still measures 0,
                    where `:empty` would have called it content), the outer box
                    pads nothing, and the pane takes the inset. */}
                <div style={{ paddingTop: betweenHeaderH > 0 ? headerInset : 0 }}>
                <div ref={betweenHeaderRef} className="flex flex-col">
                {/* The roster is folded into the switcher while a DM is open, so
                    every roster-only failure also needs a visible copy above the
                    thread. When the roster is pinned back on desktop its own copy
                    is visible and this wrapper steps aside; below md the roster is
                    hidden regardless of the pin. No hand-off: the Profile card may
                    hold an unsaved schedule draft, and Ask the agent navigates away. */}
                {(patrol.failed || teamsNoticeShown || starError) && (
                  <div
                    className={`${rosterPinned ? 'md:hidden' : ''} flex flex-col gap-2 px-4 pb-2`}
                    data-testid="member-main-roster-errors"
                  >
                    {patrol.failed && (
                      <ErrorNotice
                        message={t('pages.membersPage.patrol_error_roster')}
                        variant="inline"
                        askAgent={false}
                        testId="member-main-patrol-error"
                      />
                    )}
                    {teamsNoticeShown && (
                      <ErrorNotice
                        message={t('pages.membersPage.teams_load_failed')}
                        variant="inline"
                        askAgent={false}
                        onDismiss={dismissTeamsNotice}
                        testId="member-main-teams-error"
                      />
                    )}
                    {starError && (
                      <ErrorNotice
                        message={starError.message}
                        report={starError.report}
                        title={t('pages.membersPage.star_failed_title')}
                        onDismiss={() => setStarError(null)}
                        askAgent={false}
                        actionPlacement="below"
                        testId="member-main-star-error"
                      />
                    )}
                  </div>
                )}
                {/* A failed document read from the panel's Files / Artifacts tabs.
                    Reported here, above the thread, rather than inside the tab
                    that failed to open — there is no such tab. Dismissable. No
                    hand-off: the ChatPane below holds the DM composer draft as
                    unsaved local state (its own notices say the same), and the
                    agent hand-off navigates away, which would unmount it. */}
                {/* No hand-off: the ChatPane below holds the DM composer draft. */}
                {freshStartError && freshStartError.slot === activeSlot && (
                  <ErrorNotice
                    message={freshStartError.message}
                    report={freshStartError.report}
                    onDismiss={() => setFreshStartError(null)}
                    askAgent={false}
                    variant="inline"
                    className="mx-4 mb-2"
                    testId="member-fresh-start-error"
                  />
                )}
                {actionError && (
                  <ErrorNotice
                    message={actionError}
                    onDismiss={() => setActionError('')}
                    testId="member-panel-action-error"
                  />
                )}
                {actionInfo && (
                  <p
                    className="text-[13px] text-muted px-4 py-1.5"
                    data-testid="member-panel-action-info"
                    role="status"
                    aria-live="polite"
                  >
                    {actionInfo}
                  </p>
                )}
                {/* The editor's roster read (crewAgentsQuery, enabled only once the
                    identity pill sets editingCrew) failed: without this the pill would
                    be a silent dead click — editingAgent stays undefined, the hook's
                    `open` never flips, and CrewEditorDialog returns null forever with no
                    report (F1). No hand-off: this notice renders ONLY when the roster
                    read failed, so editingAgent is undefined and dirtyPanes is always
                    empty here — a `dirtyPanes.size === 0` guard is therefore inert and
                    would make the hand-off unconditionally on, silently discarding the
                    ChatPane DM composer draft and the panel's Schedules create draft via
                    the raw `/chat` navigate that skips NavigationLeaveGuard. Like the two
                    neighbouring notices, this one does not hand off. */}
                {editingCrew && crewAgentsQuery.isError && (
                  <ErrorNotice
                    title={t('pages.membersPage.roster_load_failed')}
                    message={crewAgentsQuery.error instanceof Error ? crewAgentsQuery.error.message : String(crewAgentsQuery.error)}
                    askAgent={false}
                    onDismiss={closeCrewEditor}
                    testId="member-crew-roster-load-error"
                  />
                )}
                {gone && gone.shown === active.name && (
                  /* Decision-critical (the user is about to type into a thread they
                     did not ask for), so it wears the warn tone at body size, not
                     the drawer's muted timestamp style. A status, not an alert:
                     the fallback did open something. */
                  <div className="px-4 py-2 text-[13px] text-warn" role="status" data-testid="member-gone-notice">
                    {t('pages.membersPage.member_gone', { name: gone.name, shown: gone.shown })}
                  </div>
                )}
                {activeCollision && (
                  /* Nothing is mounted under a collision (the slot is cleared), so
                     there is no draft to lose: the hand-off is safe. */
                  <div className="px-4 py-2">
                    <ErrorNotice
                      message={t('pages.membersPage.slug_collision', {
                        name: activeCollision,
                        // The crewmate whose chat shows this notice is the one to
                        // recreate, so it is named the way the header pill names it.
                        self: active ? crewDisplayName(active) : '',
                      })}
                      variant="inline"
                      askAgent
                      testId="member-thread-collision"
                    />
                  </div>
                )}
                {threadsFlagFailed && (
                  /* The config read behind the reply-threads flag failed. Said
                     here, not rendered as "threads are off": with nothing cached
                     the Reply control is withheld (the routes would refuse), with
                     a cached value the last reading stands; either way the user
                     can ask for the read again in place. No hand-off: the DM
                     composer below holds an unsaved draft. */
                  <div className="px-4 py-2 flex items-start gap-2" data-testid="member-threads-flag-error-row">
                    <ErrorNotice
                      message={t('pages.chat.thread.err_flag_failed')}
                      variant="inline"
                      className="flex-1 min-w-0"
                      testId="member-threads-flag-error"
                    />
                    <Btn
                      disabled={threadsFlagRetrying}
                      onClick={retryThreadsFlag}
                      className="shrink-0"
                      data-testid="member-threads-flag-retry"
                    >
                      <RotateCw className="lucide-inline" aria-hidden />
                      {t('pages.chat.thread.retry')}
                    </Btn>
                  </div>
                )}
                {threadsOn && threadsQuery.isError && (
                  /* The per-message reply counts failed to load: the chat itself is
                     fine and stays mounted below, so this says only what is
                     missing (the footers) and offers the read again in place. No
                     hand-off, for the reason the notices around it give: the DM
                     composer below holds an unsaved draft. */
                  <div className="px-4 py-2 flex items-start gap-2" data-testid="member-threads-error-row">
                    <ErrorNotice
                      message={t('pages.chat.thread.err_summary_failed')}
                      variant="inline"
                      className="flex-1 min-w-0"
                      testId="member-threads-error"
                    />
                    <Btn
                      disabled={threadsQuery.isFetching}
                      onClick={() => { void threadsQuery.refetch() }}
                      className="shrink-0"
                      data-testid="member-threads-retry"
                    >
                      <RotateCw className="lucide-inline" aria-hidden />
                      {t('pages.chat.thread.retry')}
                    </Btn>
                  </div>
                )}
                {activeThreadFailed && (
                  /* No hand-off while a cached thread is mounted under this line:
                     its DM composer still holds whatever the user typed (ChatPane
                     keeps that draft in local state), and the hand-off navigates
                     away, unmounting it. With nothing mounted (a cold open that
                     failed) there is no draft to lose, so the hand-off is on —
                     otherwise the column is a dead end. */
                  <div className="px-4 py-2">
                    {/* Two sentences for two situations: with no cached thread
                        the column is empty and the open failed outright; with one
                        still mounted below, "could not open" would contradict the
                        conversation the user is looking at — it is the REPAIR
                        that failed, and the copy says so. */}
                    <ErrorNotice
                      message={t(
                        activeSlot
                          ? 'pages.membersPage.thread_repair_failed'
                          : 'pages.membersPage.thread_open_failed',
                      )}
                      report={threadOutcome?.errorReport}
                      variant="inline"
                      askAgent={!activeSlot}
                      testId="member-thread-error"
                    />
                    {threadOutcome?.errorReport && (
                      <details className="mt-1.5 text-[12px] text-muted" data-testid="member-thread-error-details">
                        <summary className="w-fit cursor-pointer rounded-sm focus-ring">{t('pages.membersPage.details')}</summary>
                        {/* No hand-off here: the notice above owns the sole action
                            and disables it while the cached conversation holds a draft. */}
                        <ErrorNotice
                          message={threadOutcome.errorReport.message}
                          report={threadOutcome.errorReport}
                          variant="inline"
                          askAgent={false}
                          className="mt-1"
                          messageClassName="whitespace-pre-wrap"
                        />
                      </details>
                    )}
                  </div>
                )}
                {mateGreetingFailure && crewmateIdentity && (
                  /* The status read failed (not "no ledger", which is no greeting).
                     No hand-off, for the reason the notices above give: the DM
                     composer below may hold an unsaved draft. */
                  <div className="px-4 pt-3">
                    <ErrorNotice
                      message={t('pages.membersPage.welcome_failed', { name: crewmateIdentity.label || crewmateIdentity.name })}
                      report={reportForError(mateGreetingFailure.error)}
                      variant="inline"
                      askAgent={false}
                      onDismiss={dismissMateGreeting}
                      testId="member-resume-error"
                    />
                  </div>
                )}
                </div>
                </div>
                {entrance && crewmateIdentity && (
                  <MateEntrance
                    key={entrance.slot}
                    crewmate={crewmateIdentity}
                    slotKey={activeSlot}
                    text={entranceText}
                    stageRef={threadColumnRef}
                    targetRef={pillFaceRef}
                    reduceMotion={!!reduceMotion}
                    onLanded={landEntrance}
                    onDone={endEntrance}
                  />
                )}
                {activeSlot ? (
                  <div
                    // Veiled at once (a cut, so no "Working" row shows under the
                    // face for a frame); unveiled with a fade as the face lands.
                    className={entranceOnStage ? 'flex-1 min-h-0' : 'flex-1 min-h-0 transition-opacity duration-300 motion-reduce:transition-none'}
                    style={entranceOnStage ? { opacity: 0 } : undefined}
                    data-testid="member-thread-pane"
                    data-entrance-veiled={entranceOnStage ? '' : undefined}
                  >
                    <ErrorBoundary>
                      {/* Same reading measure as the main chat transcript — the
                          pane resolves the user's Content width setting itself
                          (transcript and composer both). The DM column is the
                          page's widest region, and an uncapped line length is
                          unreadable on wide screens.

                          The crewmate identity opts this pane into a normal Send
                          at every runtime state; the gateway owns prioritization. */}
                      <ChatPane
                        slotKey={activeSlot}
                        agentLocked
                        composerInput={CrewComposer}
                        frameless
                        followContentWidth
                        // The DM is this page's only composer, so it owns the
                        // "Start Dictation with a Key" binding (#24967) — but
                        // only while nothing covers it: the key listens on the
                        // whole document, so under the New crewmate form or an
                        // overlay it would dictate into (and maybe send to)
                        // the hidden chat.
                        pushToTalk={threadView.chatUncovered}
                        // The failure notice above owns the verdict on this thread
                        // while a repair has failed; the pane's own "Session
                        // ready" would contradict it one line down.
                        // The crewmate's greeting already speaks for the empty chat.
                        hideEmptyHint={activeThreadFailed || !!mateGreeting || !!entrance}
                        foldBefore={freshStarts[activeSlot] ?? activeRoster?.reset_at}
                        // Under the floating header only when nothing sits between
                        // them (see the block above); a card between already paid.
                        topInset={betweenHeaderH > 0 ? 0 : headerInset}
                        crewmate={crewmateIdentity}
                        crewmateShown={threadView.chatUncovered}
                        crewmateGreeting={mateGreeting && crewmateIdentity
                          ? <MateWelcomeCard recap={mateGreeting.recap} crewmate={crewmateIdentity} onDismiss={dismissMateGreeting} />
                          : null}
                        onOpenCrewWorkLog={openCrewWorkLog}
                        openSideChat={openMemberSideChat}
                        threads={threadHooks}
                        onFileOpen={openFileGuarded}
                        onArtifactOpen={openArtifactInThread}
                        onSessionOpen={openSessionGuarded}
                        sessions={connected && slotsLoaded ? sessionRoster : undefined}
                        activeSession={activeSlot}
                      />
                    </ErrorBoundary>
                  </div>
                ) : (
                  !activeCollision && !activeThreadFailed && (
                    <div className="flex-1 flex items-center justify-center text-xs text-muted">
                      {t('pages.membersPage.opening_thread')}
                    </div>
                  )
                )}
              </>
            )}
          </section>
  )
}
