/**
 * The chat SidePanel docked to the Crewmates page. `useMembersPageSidePanel`
 * binds it to the open crewmate's confirmed slot: the tab strip, the views it
 * withholds, the document and terminal actions, reply threads, the dashboard
 * preview link and mount continuity. `MembersPageSidePanel` renders it as a
 * docked column or an overlay, with an open reply thread over its tabs.
 *
 * The hook also declares two pieces of Profile-card state, at their place in
 * the page's hook order: the Sessions tab's driving list (its committed order
 * and fold) and the Schedules draft's retention flags.
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
import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from 'react'
import { type NavigateFunction, type Location, type SetURLSearchParams } from 'react-router-dom'
import { LayoutDashboard } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { type MemberRosterRow } from '../../api/client'
import { crewDisplayName } from '../../components/AgentSelector'
import { useQuery, type QueryClient } from '@tanstack/react-query'
import type { ThreadHooks } from '../../app-sdk/messageRenderers'
import { threadsApi, threadsQueryKey } from '../../api/threads'
import ThreadPanel from './ThreadPanel'
import { useCrewmateThreadsFlag } from '../../hooks/useCrewmateThreadsFlag'
import CrewDashboardTab from './CrewDashboardTab'
import { isDashboardPreview, withoutDashboardPreview } from './dashboardPreview'
import { claimDashboardOnLoad } from './dashboardOnLoad'
import { suggestPaneDraft } from '../../utils/chatPaneDrafts'
import { revealComposer } from '../chat/composerFocus'
import { sessionTitleRoster } from '../../utils/sessionRoster'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { AnimatePresence, motion } from 'framer-motion'
import { isSidePanelHidden, shouldMountSidePanel, sidePanelDockMotion } from '../chat/sidePanelMount'
import SidePanel, { SIDE_PANEL_MIN_W, type SidePanelLeadingTab, type SidePanelWithholdable } from '../chat/SidePanel'
import { useRailWidth } from '../../hooks/useRailWidth'
import { useAnyLiveAppTab, usePanelTabs, type ViewKind } from '../../hooks/usePanelTabs'
import { usePanelTabDescriptors } from '../../hooks/panelTabRegistry'
import { usePanelDocumentActions } from '../../hooks/usePanelDocumentActions'
import { useMemberDocumentComments } from '../../hooks/useMemberDocumentComments'
import { useRunInTerminalBridge, useRunInTerminalRefs } from '../../hooks/useRunInTerminalBridge'
import { useSlotRunsHere } from '../../components/sessionEnvironmentHooks'
import { lastActivityEpoch } from '../chat/sessionOrder'
import { type ChatSlot } from '../../types'
import { type MembersPageProfileFlight, type MembersPageProfileState } from './MembersPageProfile'
import { type MembersPageRosterView } from './MembersPageRoster'
import { type MembersPageThreadState } from './MembersPageThread'
import { CREW_DASHBOARD_TAB_ID, CREW_PANEL_TAB_IDS, MEMBERS_UNCONFIRMED_WITHHELD_VIEWS, MEMBERS_WITHHELD_VIEWS, PANEL_GAPS_W, type ProfileCardOpen } from './membersPageModel'
import { type MembersPageDrawers } from './useMembersPageDrawers'
import { type MembersPageLayout } from './useMembersPageLayout'
import { type MembersPageNavigation } from './useMembersPageNavigation'

/** The chat side panel's right-dock mount preset — module-pure, so one
 *  constant serves every render. */
const dockMotion = sidePanelDockMotion('right')

interface MembersPageSidePanelStateOptions {
  layout: MembersPageLayout
  profileCard: MembersPageProfileState
  drawers: MembersPageDrawers
  rosterView: MembersPageRosterView
  thread: MembersPageThreadState
  navigate: NavigateFunction
  location: Location
  queryClient: QueryClient
  activeName: string
  searchParams: URLSearchParams
  setSearchParams: SetURLSearchParams
  profile: ProfileCardOpen | null
  setProfile: Dispatch<SetStateAction<ProfileCardOpen | null>>
  liveSlots: ChatSlot[]
  slotsLoaded: boolean
  active: MemberRosterRow | undefined
  activeMemberName: string
}

/** The side panel's binding to the confirmed slot, and the Profile tab state declared beside it. */
export function useMembersPageSidePanel({
  layout, profileCard, drawers, rosterView, thread, navigate, location, queryClient, activeName, searchParams,
  setSearchParams, profile, setProfile, liveSlots, slotsLoaded, active, activeMemberName,
}: MembersPageSidePanelStateOptions) {
  const { isMobile, winW, beside, setOverlayOpen, setDockedOpen, panelVisible } = layout
  const { openProfile, schedGuardRef, schedAtStakeRef } = profileCard
  const { sidePanelPhase } = drawers
  const { crewmateLabel } = rosterView
  const { activeSlot, confirmedSlot, connected } = thread
  // Crewmate-panel IA: the right column rests at 60% of the content row (the
  // window minus the live nav rail), clamped so the thread keeps its minimum.
  // The same number sizes the docked profile card, so card and panel swap in
  // place; a width the user dragged (persisted by SidePanel) wins over it.
  const railW = useRailWidth()
  const panelDefaultW = Math.max(SIDE_PANEL_MIN_W, Math.round((winW - railW) * 0.6))
  // The profile card: a hover card a third of the row wide (clamped to its
  // column by the style in `MembersPageProfileCard`, MembersPageProfile.tsx), never the side
  // panel's width.
  const profileCardW = Math.max(360, Math.round((winW - railW) * 0.34))
  // Sessions this member is driving: every live slot whose `created_by` is the
  // member's DM slot key. A member dispatches its real work into worker
  // sessions it opens via session_create and steers via session_send, and the
  // backend fences a member caller to the slots it created — so "created by"
  // IS "driven by", and the durable birth attribution is the whole source of
  // truth (no transcript scraping for the `[sent by session …]` prefix). Rides
  // the already-subscribed WS `slots` frames, which is also what gives each row
  // its live status — the same running / needs-approval / needs-input signals
  // the sidebar dot reads. Newest activity first; a closed worker leaves the
  // live slots and therefore this list, which is the honest reading of
  // "driving right now".
  const activeMemberKey = activeSlot || active?.slot_key || ''
  // The ORDER is committed per DRIVEN SET, not per frame. `liveSlots` refreshes
  // on every WS slots frame and `lastActivityEpoch` advances whenever a worker
  // does anything, so sorting per render moves rows under the cursor mid-click —
  // and each row is a jump into a session, so a shifted row navigates into the
  // WRONG one.
  // Row CONTENT still updates from every frame (status dot, title, timestamp);
  // only the positions hold, until the driven set changes (a worker opens or
  // closes) or the member does, either of which re-sorts from scratch by
  // recency. Same rule as the roster order (`orderedMembers`, in
  // MembersPageRoster.tsx), and the same reason.
  const committedDrivingRef = useRef<{ member: string; keys: string[] }>({ member: '', keys: [] })
  // The side panel's tab strip, bucketed by the member's slot key exactly as
  // the chat page buckets by chat slot: switching members swaps the whole strip
  // and switching back restores it. Keyed on the POST-CONFIRMED `activeSlot`
  // ONLY — never the roster's derived key. That key is a stale binding until
  // the thread endpoint confirms it (see the `slots` comment in MembersPage.tsx): an
  // ordinary slot can occupy the canonical key after a restart, the POST then
  // answers 409 and `activeSlot` stays empty, and a panel bound to the derived
  // key would aim Side chat / Terminal / Summary at that unrelated session.
  // While no confirmed slot exists the strip lives in the shared no-slot bucket
  // and every slot-bound view is withheld (`hiddenViews` below); the Crew
  // summary needs no slot and stays.
  const panelTabDescriptors = usePanelTabDescriptors()
  const tabsCtl = usePanelTabs(activeSlot || null, panelTabDescriptors, { leadingIds: CREW_PANEL_TAB_IDS })
  // The member slot's project directory (the WS slots frame carries it) roots
  // the Files tab and is the cwd a Terminal tab spawns in. Only a record from
  // the CURRENT snapshot counts: a reconnect drops `slotsLoaded` but keeps the
  // pre-disconnect `slots` until the fresh frame lands, and the thread POST
  // can confirm inside that window — binding to the stale record would root
  // Files / Terminal in whatever project the key had BEFORE the restart, and a
  // save or command dispatched then would land in the wrong workspace. The
  // record must also be the member's own (`mode === 'member'`): a 200 confirm
  // names the member slot, never an ordinary session that happens to hold
  // the canonical key (that case answers 409 and never confirms).
  const activeLiveSlot = useMemo(
    () => (confirmedSlot && slotsLoaded
      ? liveSlots.find((s) => s.key === confirmedSlot && s.mode === 'member')
      : undefined),
    [liveSlots, slotsLoaded, confirmedSlot],
  )
  // The DM slot's RESOLVED project directory. Not `active.workspace`: that is
  // the workspace NAME the crew record carries (a key into the configured
  // workspaces, "default" for most), and the backend resolves it to a directory
  // when it opens the member slot (`default_project_dir`), which is what the
  // slot frame's `project` holds. Handing the name to Files / Terminal rooted
  // both in a folder literally called "default".
  const projectDir = activeLiveSlot?.project || undefined
  // Terminal needs the slot RECORD, not just the confirmed key: the thread
  // POST answers before the WS `slots` frame that carries the slot's project,
  // and a shell spawned in that window would take `cwd: undefined` — the
  // backend's HOME fallback — with no re-rooting once the frame lands. Gate on
  // the record being present, not on `project` being set: a member with no
  // project legitimately opens its shell in the fallback cwd, exactly as a
  // project-less chat does.
  const slotRecordPresent = !!activeLiveSlot
  // Views this host withdraws from the strip and the + menu. Always: the views
  // fed by ChatPage-owned transcript indexes (pull-request / issue / link
  // extraction, the pins query) — this page has none of those, and an empty
  // Changes chip on the monitoring page would assert "nothing changed" while a
  // member is editing. `summary` is withheld too: Profile Sessions and the generated
  // Dashboard already own the member-level views. So is the pinned Artifacts tab:
  // this page's standing tabs are Dashboard + Files only, while every + menu view
  // stays offered (see MEMBERS_WITHHELD_VIEWS). Until the thread is confirmed, EVERY slot-bound view is
  // withheld as well, per the binding rule above — and so is Terminal: while
  // unconfirmed the strip sits in the shared no-slot bucket, so a PTY opened
  // then would be orphaned (live shell, unreachable tab) the moment the
  // confirmation re-keys the strip to the member's slot; app-contributed tabs
  // (`'app'`) likewise, since the re-key would remount their `AppHost` and
  // discard the app's own unsaved state. Terminal stays withheld a moment
  // longer — until the WS slots frame carries the confirmed slot, so its cwd
  // is known (see slotRecordPresent).
  const hiddenViews = useMemo<ReadonlySet<SidePanelWithholdable>>(
    () =>
      new Set<SidePanelWithholdable>(
        !confirmedSlot
          ? MEMBERS_UNCONFIRMED_WITHHELD_VIEWS
          : !slotRecordPresent
            ? [...MEMBERS_WITHHELD_VIEWS, 'terminal']
            : MEMBERS_WITHHELD_VIEWS,
      ),
    [confirmedSlot, slotRecordPresent],
  )
  // The selection toolbar's "Ask about this" on the thread: the Side Chat for
  // this member lives in the panel's Side tab (the chat page's home for it),
  // so opening it means focusing that tab — and revealing the panel if it is
  // hidden, in either placement, since a tab behind a hidden panel is not "on
  // screen". Only the endpoint-confirmed slot may host it (the pane is keyed on
  // that same slot, so the two agree); `false` tells the selection seam the Ask
  // did NOT happen, so it never seeds a quote into a Side Chat that never
  // opened. Switching the side-panel tab leaves the Profile card mounted, so a
  // schedule draft there is not an exit and needs no discard guard.
  const openMemberSideChat = useCallback((slot: string): boolean => {
    if (!confirmedSlot || slot !== confirmedSlot) return false
    tabsCtl.openView('side')
    if (beside) setDockedOpen(true)
    else setOverlayOpen(true)
    return true
  }, [confirmedSlot, tabsCtl, beside, setDockedOpen, setOverlayOpen])
  // Legacy-named ChatPane seam: the quiet-chat link opens Profile on Sessions,
  // where the sessions this crewmate is driving now live. Changing the card tab
  // remounts its pushed page, so a schedule draft takes the same guard as every
  // other Profile exit.
  const openCrewWorkLog = useCallback(() => {
    const open = () => openProfile('sessions')
    if (!schedAtStakeRef.current()) { open(); return }
    void schedGuardRef.current().then((ok) => { if (ok) open() })
  }, [openProfile, schedAtStakeRef, schedGuardRef])
  // The card's Workspace tile opens the panel's Files view. A docked Profile
  // column occupies the space the revealed panel needs, so this accepted exit
  // closes that card exactly like togglePanel's reveal path. A floating Profile
  // stays mounted beside the panel — and so is never asked: the draft question
  // belongs only to an exit that unmounts the card, and a "keep my draft" here
  // would otherwise block Files for a draft that was never at risk.
  const openCrewView = useCallback((view: ViewKind) => {
    const unmountsCard = profile?.placement === 'column'
    const focus = () => {
      if (unmountsCard) setProfile(null)
      tabsCtl.openView(view)
      if (beside) setDockedOpen(true)
      else setOverlayOpen(true)
    }
    if (!unmountsCard || !schedAtStakeRef.current()) { focus(); return }
    void schedGuardRef.current().then((ok) => { if (ok) focus() })
  }, [tabsCtl, beside, setDockedOpen, profile, schedAtStakeRef, schedGuardRef, setOverlayOpen, setProfile])
  // Session routing inside the DM transcript. A crewmate's prose names sessions
  // constantly -- "picked this up in `chat-2235-…`", a `/chat?sid=…` link to the
  // worker it dispatched -- and until now every one of those was inert here
  // while the same text on the chat page resolved.
  //
  // The roster is the WS `slots` frame this page already subscribes to, narrowed
  // by the shared builder to the slots the DESTINATION can render: the handler
  // navigates to the unified chat view, so a chip to anything that view drops
  // would clear itself on arrival. Withheld -- not emptied -- until a real
  // snapshot has arrived and the socket is up: absent means "this surface does
  // not know which sessions exist", which is the honest answer then, and it
  // leaves the link plain rather than live-looking and dead.
  // Rebuilt on a language switch too: the roster's "Untitled session" label is localized (#23059).
  const languageGeneration = useLanguageGeneration()
  // eslint-disable-next-line react-hooks/exhaustive-deps -- languageGeneration is the trigger, not an input
  const sessionRoster = useMemo(() => sessionTitleRoster(liveSlots), [liveSlots, languageGeneration])
  // A foreign slot is NOT hosted in this page's own pane -- it belongs to the
  // chat page, with its sidebar, its history paging and its composer. Same
  // primitive the Driving-sessions rows use.
  const openSessionOnChatPage = useCallback(
    (key: string) => { navigate(`/chat?sid=${encodeURIComponent(key)}`) },
    [navigate],
  )
  // Reply threads (screen 07). The footer data per message is one small read
  // beside the transcript; the open thread takes over the side panel while it
  // is on screen, and closing it hands the panel's tabs back. Keyed on the
  // CONFIRMED slot only, like every other slot-bound view here.
  // Behind `dashboard.crewmate_threads` (off by default): off, no footer read
  // is made, no Reply in thread control is offered and no panel is mounted --
  // the routes answer 404 then, so a control drawn anyway would only reach a
  // refusal. Flipping the flag off closes an open thread. A config read that
  // FAILED is not "off": the flag keeps its last known value and the failure
  // is said in MembersPageThread.tsx (ErrorNotice + Retry), so threads that are on do not vanish
  // as if the switch had been flipped.
  const {
    on: threadsOn,
    failed: threadsFlagFailed,
    retrying: threadsFlagRetrying,
    retry: retryThreadsFlag,
  } = useCrewmateThreadsFlag()
  const [openThreadMid, setOpenThreadMid] = useState<string | null>(null)
  useEffect(() => { setOpenThreadMid(null) }, [confirmedSlot, threadsOn])
  const threadsQuery = useQuery({
    queryKey: threadsQueryKey(confirmedSlot || ''),
    queryFn: () => threadsApi.summary(confirmedSlot),
    enabled: threadsOn && !!confirmedSlot,
    staleTime: 30_000,
  })
  const threadSummaries = threadsQuery.data?.threads
  const openReplyThread = useCallback((mid: string) => {
    setOpenThreadMid(mid)
    if (beside) setDockedOpen(true)
    else setOverlayOpen(true)
  }, [beside, setDockedOpen, setOverlayOpen])
  const closeReplyThread = useCallback(() => setOpenThreadMid(null), [])
  const threadHooks = useMemo<ThreadHooks | undefined>(
    () => (threadsOn && confirmedSlot
      ? {
          summaryOf: (mid: string) => threadSummaries?.[mid],
          onOpen: openReplyThread,
          crewmateName: activeName,
        }
      : undefined),
    [threadsOn, confirmedSlot, threadSummaries, openReplyThread, activeName],
  )
  // Whether each leading tab's body is on screen — the gate for its data reads.
  // Read from what the panel SHOWS (`onActiveTabChange`), not from the stored
  // focus: a stored focus on a withheld view falls back to the first leading tab
  // in the strip without moving the store, and that tab must load when it is the
  // one on screen.
  const [shownTabId, setShownTabId] = useState<string | null>(null)
  // Whether the Schedules tab is holding unsaved work. Declared here rather than beside
  // its own guards because the mount decision below reads it: see `keepMountedForDraft`.
  const [schedAtStake, setSchedAtStake] = useState(false)
  // Has the user just accepted a discard? Then retention stands down for that one exit.
  // A ref, not state: the exit that sets it re-renders the page by itself (it hides the
  // panel), and the flag must already be true at that render.
  const schedReleased = useRef(false)
  // Whether the Schedules section is mounted, readable from the release effect in
  // MembersPageProfile.tsx without making it depend on a value computed later in
  // this render (`schedulesMounted`, in MembersPage.tsx).
  const schedulesMountedRef = useRef(false)
  const activeTabId = shownTabId ?? tabsCtl.activeId
  const dashboardVisible = panelVisible && activeTabId === CREW_DASHBOARD_TAB_ID
  const [dashboardVisitedFor, setDashboardVisitedFor] = useState<string | null>(null)
  useEffect(() => {
    // Once the Dashboard has been on screen for this crewmate its body stays
    // mounted across tab switches in the strip (Dashboard <-> Files), so the
    // report keeps its scroll and its minted document instead of re-minting.
    if (dashboardVisible) setDashboardVisitedFor(activeMemberKey)
  }, [dashboardVisible, activeMemberKey])
  const closeOverlay = useCallback(() => setOverlayOpen(false), [setOverlayOpen])
  // Mount continuity — the chat page's rule, verbatim: a live Browser tab (its
  // WebContentsView) or a body-owning app tab (any slot's) cannot survive a
  // remount, so while one exists a closed overlay is kept mounted and hidden
  // rather than unmounted. There is no find pane on this page.
  const hasLiveAppTab = useAnyLiveAppTab()
  const hasBrowserTab = tabsCtl.tabs.some((tab) => tab.kind === 'browser')
  const hasTaskDashboard = tabsCtl.tabs.some(tab => tab.kind === 'command-center')
  const mountInput = { activityOpen: panelVisible, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: false }
  const panelMounted = shouldMountSidePanel(mountInput)
  const panelHidden = isSidePanelHidden(mountInput)
  // Phone: the slide owns the overlay's presence. It stays mounted and shown
  // while a drag is revealing it or a close is still carrying it out, which
  // the open flag alone cannot say.
  const sidePanelOnScreen = isMobile ? panelMounted || sidePanelPhase !== 'closed' : panelMounted
  const sidePanelHiddenNow = isMobile ? panelHidden && sidePanelPhase === 'closed' : panelHidden
  // File / artifact / save for the panel's Files, Artifacts and document tabs —
  // the chat page's own implementation, not a copy. A failed read is reported
  // above the thread; an open reveals whichever placement is live, since the
  // tab it focused is otherwise behind a hidden panel. Each placement arms its
  // OWN flag: arming `overlayOpen` from the docked column would leave the
  // overlay already open over the thread the next time the window narrows.
  const activeSlotRef = useRef<string | null>(confirmedSlot || null)
  activeSlotRef.current = confirmedSlot || null
  const besideRef = useRef(beside)
  besideRef.current = beside
  const [actionError, setActionError] = useState('')
  // A failed document read or comment send is reported above the thread. In
  // overlay mode the open panel covers exactly that spot — the click that
  // failed happened inside it — so the overlay closes as the notice appears;
  // otherwise the failure is silent to the person who caused it.
  const showActionError = useCallback((message: string) => {
    setActionError(message)
    if (!besideRef.current) setOverlayOpen(false)
  }, [setOverlayOpen])
  const revealPanelAfterOpen = useCallback(() => {
    if (besideRef.current) setDockedOpen(true)
    else setOverlayOpen(true)
  }, [setDockedOpen, setOverlayOpen])
  const { openFile, openWorkingTreeDiff, openArtifact, saveFile } = usePanelDocumentActions({
    tabsCtl,
    slotRef: activeSlotRef,
    queryClient,
    showActionError,
    onOpened: revealPanelAfterOpen,
  })
  // Inline comments on a document in the side panel: the hook owns the send and
  // the "N comments sent" line; this owner supplies the confirmed DM slot, the
  // panel placement and the failure notice.
  const clearActionError = useCallback(() => setActionError(''), [])
  const { info: actionInfo, submitComments } = useMemberDocumentComments({
    connected, slotRef: activeSlotRef, besideRef, crewmateLabel, showActionError, clearActionError,
  })
  // "Run in terminal" from a crewmate chat's code blocks. Without a host here
  // the request had no receiver and the button timed out. The shell starts in
  // the member slot's project — the cwd this page's own Terminal tab spawns in
  // — and only once the slot record carrying it has arrived (slotRecordPresent).
  const runInTerminalCwdRef = useRef<string | undefined>(projectDir)
  runInTerminalCwdRef.current = projectDir
  const runInTerminalReadyRef = useRef(slotRecordPresent)
  runInTerminalReadyRef.current = slotRecordPresent
  const runInTerminalRefs = useRunInTerminalRefs()
  // The member chat's placement, as on the chat page: no dock shell here for a
  // chat that runs in a Session Environment workspace.
  const runsHere = useSlotRunsHere(confirmedSlot)
  const runsHereRef = useRef(runsHere)
  runsHereRef.current = runsHere
  const runInTerminalSlotRef = useRef(confirmedSlot)
  runInTerminalSlotRef.current = confirmedSlot
  const runInTerminalScope = useRunInTerminalBridge({
    queryClient,
    showActionError,
    cwdRef: runInTerminalCwdRef,
    readyRef: runInTerminalReadyRef,
    runsHereRef,
    slotRef: runInTerminalSlotRef,
    ...runInTerminalRefs,
  })
  // Transcript file links focus a side-panel tab, but the schedule draft now
  // lives in the separate Profile card. Opening a file leaves that card mounted,
  // so this compatibility wrapper deliberately does not invoke the draft guard.
  const openFileGuarded = useCallback((...args: Parameters<typeof openFile>) => {
    void openFile(...args)
  }, [openFile])
  // Same shape for an artifact a crewmate names in its reply: the panel's own
  // Artifacts opener (a document tab against the DM slot), fire-and-forget
  // like the file one. Without it the `/artifacts/<slug>` link opened the
  // standalone view in a new browser tab, out of the DM (#18320).
  const openArtifactGuarded = useCallback((slug: string) => {
    void openArtifact(slug)
  }, [openArtifact])
  // Opening one of the crewmate's sessions leaves `/members` for `/chat` outright, so it
  // destroys the Schedules form as surely as the identity pill does. It is a raw
  // `navigate`, which the leave channel never sees -- only callers that ask reach it --
  // so it asks here, like every other exit.
  const openSessionGuarded = useCallback((key: string) => {
    if (!schedAtStakeRef.current()) { openSessionOnChatPage(key); return }
    void schedGuardRef.current().then((ok) => { if (ok) openSessionOnChatPage(key) })
  }, [openSessionOnChatPage, schedAtStakeRef, schedGuardRef])
  const drivingSessions = useMemo(() => {
    if (!activeMemberKey) return []
    const mine = liveSlots.filter((s) => !!s.created_by && s.created_by === activeMemberKey)
    const byKey = new Map(mine.map((s) => [s.key, s]))
    const prev = committedDrivingRef.current
    const sameSet =
      prev.member === activeMemberKey &&
      prev.keys.length === byKey.size &&
      prev.keys.every((k) => byKey.has(k))
    const keys = sameSet
      ? prev.keys
      : [...mine].sort((a, b) => lastActivityEpoch(b) - lastActivityEpoch(a)).map((s) => s.key)
    committedDrivingRef.current = { member: activeMemberKey, keys }
    return keys.map((k) => byKey.get(k)).filter((s): s is (typeof mine)[number] => !!s)
  }, [liveSlots, activeMemberKey])
  // A PREVIEW LINK -- `?dashboard=preview`, the link `dashboard_preview` hands the
  // crewmate to show the person -- opens the side panel on the Dashboard tab, where
  // the staged page is drawn under its preview label. Once per crewmate and strip
  // bucket: the strip re-buckets when the thread's slot confirms, and that bucket's
  // stored focus may be another tab; after that, closing the panel or picking
  // another tab is the person's choice and is not undone while the link is still in
  // the URL. Switching crewmate writes a fresh `?member=`, which drops the request.
  const dashboardPreviewLink = isDashboardPreview(searchParams)
  const focusPanelTab = tabsCtl.setActive
  const previewOpenedFor = useRef('')
  useEffect(() => {
    if (!dashboardPreviewLink) {
      previewOpenedFor.current = ''
      return
    }
    if (!activeMemberName) return
    const opened = JSON.stringify([activeMemberName, activeSlot])
    if (previewOpenedFor.current === opened) return
    previewOpenedFor.current = opened
    if (beside) setDockedOpen(true)
    else setOverlayOpen(true)
    focusPanelTab(CREW_DASHBOARD_TAB_ID)
  }, [dashboardPreviewLink, activeMemberName, activeSlot, beside, setDockedOpen, focusPanelTab, setOverlayOpen])
  // The staged page is gone (kept, applied by the agent, discarded, expired): drop
  // the preview request so the tab shows the live page. Functional, so it edits the
  // URL as it is now; the tab stays open because the link effect only opens it.
  const leaveDashboardPreview = useCallback(() => {
    setSearchParams(withoutDashboardPreview, { replace: true, state: location.state })
  }, [setSearchParams, location.state])
  // A REFRESH opens the Dashboard tab -- the crewmate's page, or the suggestions
  // page when none is adopted -- rather than whatever tab the persisted strip had
  // focused. Once per crewmate per page load, claimed once its slot confirms
  // (`claimDashboardOnLoad`), so a tab the person picks afterwards, in another
  // thread, or on an in-app return, is theirs.
  useEffect(() => {
    if (!activeMemberName) return
    if (!claimDashboardOnLoad(activeMemberName, activeSlot || '')) return
    focusPanelTab(CREW_DASHBOARD_TAB_ID)
  }, [activeMemberName, activeSlot, focusPanelTab])
  // The overlay scrim hides only the side panel. Profile is a sibling and stays
  // mounted, so closing the overlay is not a schedule-draft exit.
  const requestCloseOverlay = useCallback(() => {
    closeOverlay()
  }, [closeOverlay])

  return {
    panelDefaultW, profileCardW, activeMemberKey, tabsCtl, projectDir, hiddenViews, openMemberSideChat,
    openCrewWorkLog, openCrewView, sessionRoster, retryThreadsFlag, threadsFlagFailed, threadsFlagRetrying,
    threadsOn, openThreadMid, threadsQuery, closeReplyThread, threadHooks, setShownTabId, schedAtStake,
    setSchedAtStake, schedReleased, schedulesMountedRef, dashboardVisitedFor, closeOverlay, sidePanelOnScreen,
    sidePanelHiddenNow, actionError, setActionError, actionInfo, showActionError, revealPanelAfterOpen, openArtifact,
    openFile, openWorkingTreeDiff, saveFile, submitComments, runInTerminalScope, openFileGuarded, openArtifactGuarded,
    openSessionGuarded, drivingSessions,
    dashboardPreviewLink, focusPanelTab, leaveDashboardPreview, requestCloseOverlay,
  }
}

export type MembersPageSidePanelState = ReturnType<typeof useMembersPageSidePanel>

interface MembersPageSidePanelProps {
  layout: MembersPageLayout
  profileCard: MembersPageProfileState
  drawers: MembersPageDrawers
  rosterView: MembersPageRosterView
  thread: MembersPageThreadState
  sidePanel: MembersPageSidePanelState
  profileFlight: MembersPageProfileFlight
  navigation: MembersPageNavigation
  activeName: string
  active: MemberRosterRow
  activeSlug: string
  activeMemberName: string
}

/** The side panel, docked beside the thread or as an overlay, with the open reply thread over its tabs. */
export function MembersPageSidePanel({
  layout, profileCard, drawers, rosterView, thread, sidePanel, profileFlight, navigation, activeName, active,
  activeSlug, activeMemberName,
}: MembersPageSidePanelProps) {
  const { t } = useTranslation()
  const { roster, isMobile, rosterShown, beside, panelFillWidth } = layout
  const { openProfile, schedGuardRef, schedAtStakeRef, closeDocked } = profileCard
  const { sidePanelX, sidePanelSlideRef } = drawers
  const { activeView, crewmateLabel } = rosterView
  const { activeSlot, activeThreadFailed, confirmedSlot, connected } = thread
  const {
    panelDefaultW, activeMemberKey, tabsCtl, projectDir, hiddenViews, threadsOn, openThreadMid,
    closeReplyThread, setShownTabId, dashboardVisitedFor, closeOverlay, sidePanelOnScreen, sidePanelHiddenNow,
    openFile, openWorkingTreeDiff, saveFile, submitComments, dashboardPreviewLink, leaveDashboardPreview,
    requestCloseOverlay,
  } = sidePanel
  const { reduceMotion } = profileFlight
  const { openArtifactInPanel } = navigation
  // One Dashboard: the existing crew publication is one task view.
  // Preserve its renderer and exact member identity, while the host
  // owns live task summaries, questions and approval controls.
  // The Dashboard tab is the crewmate's own dynamic dashboard -- the page the
  // read resolves for it, its adopted copy or the default template, with every
  // number folded from its crew log. The tab is a standing entry.
  //
  // No card, Contained bar or Expand around it and no Command Center above
  // it: each read as one more container stacked over the one page that
  // matters.
  const dashboardBody = (
    <div className="h-full min-h-0 flex flex-col" data-testid="member-dashboard" aria-label={t('pages.membersPage.dashboard_tab')}>
      {/* Said here only when the MAIN COLUMN is not already saying it:
          that column renders the same sentence while it has no pane
          (`activeSlot` unset), and two live regions saying it at once
          on every cold open is one too many. */}
      {!confirmedSlot && !activeThreadFailed && activeSlot
        ? <p role="status" className="px-4 pt-3 text-sm text-muted">{t('pages.membersPage.opening_thread')}</p>
        : null}
      {activeSlug && activeMemberName && (
        // Keyed per crewmate so the tab remounts on a switch instead of
        // opening the next crewmate on the page held for this one.
        <CrewDashboardTab
          key={JSON.stringify([activeSlug, activeMemberName])}
          slug={activeSlug}
          member={activeMemberName}
          displayName={crewDisplayName(activeView ?? active)}
          avatar={(activeView ?? active)?.avatar}
          // A needs-you option or a suggested prompt lands in this crewmate's
          // chat box; the person sends it. The composer is revealed (focused on
          // desktop) so the result of the click is where the reader looks.
          onAct={activeSlot ? (text: string) => { suggestPaneDraft(activeSlot, text); revealComposer() } : undefined}
          // Badge click opens the Profile tab directly so the person can unlock.
          // Guard against an unsaved schedule form in the already-open card:
          // openProfile() increments the nonce which re-mounts the panel.
          onOpenProfileTab={() => {
            if (!schedAtStakeRef.current()) { openProfile('profile'); return }
            void schedGuardRef.current().then((ok) => { if (ok) openProfile('profile') })
          }}
          preview={dashboardPreviewLink}
          onLeavePreview={leaveDashboardPreview}
        />
      )}
    </div>
  )
  // The side panel's host tabs: the dynamic Dashboard only. The Workspace
  // file browser is the one chat view left unwithheld (see
  // MEMBERS_WITHHELD_VIEWS); together they are the whole strip.
  const leadingTabs: SidePanelLeadingTab[] = [
    {
      id: CREW_DASHBOARD_TAB_ID,
      title: t('pages.membersPage.dashboard_tab'),
      icon: <LayoutDashboard className="lucide-inline" aria-hidden="true" />,
      keepMounted: dashboardVisitedFor === activeMemberKey,
      render: () => dashboardBody,
    },
  ]
  // Everything both placements share. Two different keys do two
  // different jobs here. `slot` is the IDENTITY of the panel's bodies —
  // the key a Browser tab's native WebContentsView, an app frame and
  // every document body are keyed by — so it is the same key the strip
  // is bucketed on (`activeSlot`, the key the LAST successful open
  // confirmed) and it holds steady through a re-POST: the transient
  // withdrawal while a thread is being revalidated (a routine WS
  // reconnect re-POSTs) must hide the slot-bound views, not re-key them
  // — a Browser body re-keyed to '' and back would `close()` its live
  // WebContentsView and lose history and form state. WHICH views may be
  // offered is `hiddenViews`' job, gated on `confirmedSlot`: until the
  // CURRENT open's POST confirms the key (and after a refusal) every
  // slot-bound view is withheld, so nothing can be dispatched against a
  // key the endpoint may refuse; the document actions above bind to
  // `confirmedSlot` for the same reason. No bottom dock: this page has
  // no bottom grid row for it to move into.
  const panelProps = {
    tabsCtl,
    slot: activeSlot,
    // The member is the chat's identity while its slot is still being
    // confirmed, so a resize started before the POST answers lands on
    // the confirmed key, and one that spans a member switch does not.
    slotOwner: active?.name,
    // Sizes are SAVED only under the confirmed key: `activeSlot` holds
    // its last good key through a refusal, and that key now belongs to
    // another session, whose remembered size a drag here must not take.
    persistSlot: confirmedSlot,
    hiddenViews,
    onActiveTabChange: setShownTabId,
    projectDir,
    onFileOpen: openFile,
    onOpenWorkingTreeDiff: openWorkingTreeDiff,
    onArtifactOpen: openArtifactInPanel,
    onFileSave: saveFile,
    // Document tabs take inline comments here as on the chat page; their
    // Submit lands in this crewmate's chat (`submitComments`) and is
    // disabled while the gateway is offline.
    onSubmitComments: submitComments,
    connected,
    leadingTabs,
    slotTitle: crewDisplayName(activeView ?? active),
    canDockBottom: false,
    defaultWidth: panelDefaultW,
  }
  // ONE SidePanel instance for both placements. Docked and overlay differ
  // only in the wrapper (an in-flow column vs a fixed sheet below the
  // 42px app topbar) and in the motion axis (the chat page's width
  // reveal vs a slide from the right edge), so they share one keyed
  // element and the panel is never remounted by a placement flip — a
  // live Browser tab's WebContentsView, like an app tab's frame, does
  // not survive a remount. For the same reason a CLOSED overlay stays
  // MOUNTED and hidden while such a tab exists (`shouldMountSidePanel`
  // / `isSidePanelHidden`, the chat page's exact rule); with no live
  // tab it unmounts on close, which preserves the exit motion. Both
  // axes are named in every target — see sidePanelDockMotion for why an
  // axis left out of `animate` freezes at its last value.
  // Two nested motion elements in BOTH placements so the SidePanel
  // instance is the same React subtree whichever way it is shown. Docked:
  // the outer is the chat page's width reveal and the inner is inert.
  // Overlay: the outer is a full-bleed SCRIM below the 42px app topbar
  // (fades in; a click on it closes the overlay — the whole chat column
  // is dimmed rather than left peeking out as a sliver beside the panel,
  // which read as a rendering fault) and the inner slides the panel in
  // from the right edge. Both axes are named in every target — see
  // sidePanelDockMotion for why an axis left out of `animate` freezes.
  const outerMotion = beside
    ? dockMotion
    : isMobile
      // Phone: the inner slide is the whole motion and the panel fills
      // the window, so the outer neither fades nor dims.
      ? { initial: false as const, animate: { opacity: 1 }, exit: { opacity: 1 } }
      : {
        initial: { opacity: 0, width: 'auto', height: '100%' },
        animate: { opacity: 1, width: 'auto', height: '100%' },
        exit: { opacity: 0, width: 'auto', height: '100%' },
      }
  const innerMotion = beside
    ? { initial: { x: 0 }, animate: { x: 0 }, exit: { x: 0 } }
    : { initial: { x: '100%' }, animate: { x: 0 }, exit: { x: '100%' } }
  return (
    <AnimatePresence initial={false}>
                  {sidePanelOnScreen && (
                    <motion.div
                      key="member-side-panel"
                      id="member-side-panel"
                      initial={outerMotion.initial}
                      animate={outerMotion.animate}
                      exit={outerMotion.exit}
                      transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
                      className={beside
                        ? 'h-full overflow-visible flex justify-end shrink-0'
                        /* Both placements are dismissable, and the overlay carries a
                           second dismiss on top of the strip's close: the scrim,
                           the drawer convention. On a phone the panel is
                           handed the window width (`fillWidth`) so it fills the
                           scrim; on a tablet-width window the panel keeps its own
                           (resizable, persisted) width against the dimmed chat. */
                        : isMobile
                          ? 'fixed top-safe-offset-[42px] bottom-safe left-safe right-safe z-40 flex justify-end'
                          : 'fixed top-safe-offset-[42px] bottom-safe left-safe right-safe z-40 flex justify-end bg-bg/60 backdrop-blur-xs'}
                      style={sidePanelHiddenNow ? { display: 'none' } : undefined}
                      // The scrim dismisses only the side panel. The Profile card is a
                      // sibling and stays mounted, including any schedule draft it holds.
                      onClick={beside ? undefined : (e) => { if (e.target === e.currentTarget) requestCloseOverlay() }}
                      data-testid="member-side-panel"
                      data-placement={beside ? 'docked' : 'overlay'}
                    >
                      <motion.div
                        // Phone: the offset is the finger's and the settle's
                        // (`sidePanelX`), so no enter/exit of its own competes.
                        {...(isMobile
                          ? { ref: sidePanelSlideRef, style: { x: sidePanelX } }
                          : { initial: innerMotion.initial, animate: innerMotion.animate, exit: innerMotion.exit })}
                        transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
                        className={beside ? 'h-full flex justify-end relative' : 'h-full flex justify-end max-w-full relative'}
                        data-testid="member-side-panel-slide"
                      >
                        {/* The open reply thread covers the panel's tabs while it is on
                            screen and slides away on close, so the tabs the user had are
                            where they left them. `mb-2` + `rounded-l-xl` match the
                            panel's own frame (SidePanel's root) so the thread reads as
                            the panel showing something else, not a second panel. */}
                        <AnimatePresence initial={false}>
                          {threadsOn && openThreadMid && confirmedSlot && (
                            <motion.div
                              key={`thread-${openThreadMid}`}
                              initial={reduceMotion ? { opacity: 1 } : { x: 24, opacity: 0 }}
                              animate={{ x: 0, opacity: 1 }}
                              exit={reduceMotion ? { opacity: 0 } : { x: 24, opacity: 0 }}
                              transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
                              className={`absolute inset-0 z-20 overflow-hidden ${beside ? 'mb-2 rounded-l-xl border-l border-t border-b border-border' : ''}`}
                              style={beside ? { inset: 0, bottom: 8 } : undefined}
                            >
                              <ThreadPanel
                                slot={confirmedSlot}
                                mid={openThreadMid}
                                crewmateName={activeName}
                                crewmateLabel={crewmateLabel}
                                onClose={closeReplyThread}
                              />
                            </motion.div>
                          )}
                        </AnimatePresence>
                        <SidePanel
                          {...panelProps}
                          panelHidden={sidePanelHiddenNow}
                          /* Both placements are dismissable, so both hand the panel
                             an onClose: the strip renders its close control and
                             Escape inside a view answers. Docked it hides the
                             column and the thread takes the full width; as an
                             overlay the scrim is a second dismiss, the drawer
                             convention. `extraReserveW` keeps the live roster width
                             plus the page's gaps clear on top of the shell reserve,
                             so a drag can never fold the thread to nothing (the
                             contract the old drawer's reserveWidth carried).
                             Overlay: the panel covers the thread, so nothing to
                             reserve. */
                          onClose={beside ? closeDocked : closeOverlay}
                          extraReserveW={beside && rosterShown ? roster.width + PANEL_GAPS_W : 0}
                          /* Phone only (see panelFillWidth): the overlay fills the
                             window. Off the phone this is undefined and the panel
                             sizes itself. */
                          fillWidth={panelFillWidth}
                        />
                      </motion.div>
                    </motion.div>
                  )}
                </AnimatePresence>
  )
}
