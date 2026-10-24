/**
 * Crew Members — one durable, pinned DM thread per crew member.
 *
 * CAPTURED: Settings > Developer > Feature Previews shows a screenshot of this
 * page in its "See what it looks like" dialog. A visible change here makes that
 * picture stale — re-shoot with `scripts/capture-feature-previews.mjs`.
 *
 * While no thread is open the page shows the full roster. Once a thread opens,
 * the roster folds into `CrewmateSwitcher` in the header and the conversation
 * receives that width. The centred identity pill opens `CrewProfilePanel`: an
 * in-flow 34% column while SidePanel is closed, or a rounded hover card centred
 * over the remaining thread width while SidePanel is open or on a phone. The
 * shared avatar moves between pill and card.
 *
 * The chat SidePanel has two standing entries here: the crewmate's generated
 * Dashboard and the workspace-scoped Files browser. Side Chat remains dynamic.
 * Profile owns Notes, Schedules, driven Sessions and browser-local Goals.
 * Configuration still writes through CrewEditorModal.
 *
 * Identity is the exact CREW NAME, never the slug: slugification is lossy
 * (`Oncall` and `oncall` share a slug and therefore one thread directory),
 * so rows are keyed and selected by name, and a thread-open response whose
 * `member` is a DIFFERENT name is surfaced as a collision instead of being
 * silently mounted (first-bound-wins is the backend contract).
 *
 * The pin is a server-side property of member slots (born only through
 * POST /api/members/{slug}/thread). It is an invariant of every member
 * thread, so the UI does not announce it — there is no unpinned state to
 * contrast against.
 *
 * Which crewmate is open rides the URL (`?member=<name>`). A visit that names
 * no one lands like a messages app: on Mate (key `mate`, see
 * lib/assistantMember) while no message has been exchanged with it, so a first
 * visit lands on its welcome; else on the conversation last opened in this
 * browser if it is still on the roster; else on the conversation holding the
 * newest message (greatest `last_active_ts`, whoever sent it). The built-in
 * `default` crewmate is a row like any other here. Only a roster with no
 * crewmate beyond `default` opens nothing; it shows the New crewmate hero
 * instead. The phone lands the same way: there the roster is a drawer over the
 * open chat (with the shell's navigation rail beside it, the chat page's
 * sessions drawer), revealed by a rightward drag or the header's drawer toggle,
 * while a leftward drag reveals the side panel. Mate's first-visit landing (see
 * `resolveMateLanding`) runs ahead of all of this, at every width.
 *
 * Creating a crewmate happens in place of the chat, on the New crewmate card,
 * with the chat and side panel hidden but still MOUNTED. See `openCreate`.
 *
 * The page is composed from owners beside this file, called in the order the
 * page registers its hooks (pinned by `MembersPage.owners.test.tsx`):
 * `useMembersPageRoster` (roster data), `useMembersPageCrewEditor`,
 * `useMembersPageTeams`, `useMembersPageLayout`, `MembersPageProfile`,
 * `useMembersPageDrawers`, `MembersPageRoster` (roster column and its state),
 * `MembersPageThread` (thread column and its state), `MembersPageSidePanel`,
 * `useMembersPageNavigation`, with the pure rules in `membersPageModel`.
 * Owners never import this file; it re-exports the page's original named
 * exports from `membersPageModel`. It keeps
 * the New crewmate card, the guide hand-offs, the page's exit guards and the
 * crewmate's face flight overlay.
 */
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate, useNavigationType, useSearchParams } from 'react-router-dom'
import CrewEditorChunkLoading from '../../components/crew/CrewEditorChunkLoading'
import NewCrewmateDialog, { type CreatedCrewmate, type CrewmateDraft } from './NewCrewmateDialog'
import { useTranslation } from 'react-i18next'
import { type MemberRosterRow } from '../../api/client'
import { MEMBERS_ROSTER_QUERY_KEY, memberThreadQueryKey, type MemberThreadOutcome } from '../../api/membersQuery'
import { skipToken, useQuery, useQueryClient } from '@tanstack/react-query'
import { useAppSelector } from '../../store'
import CrewMonitoredAvatar from '../../components/crew/CrewMonitoredAvatar'
import { PROFILE_FACE_PX } from './CrewProfilePanel'
import { OverlayPortal } from '../../components/overlayRoot'
import { useGuideOpenedSurface } from '../../guide/useGuideOpenedSurface'
import { useGuideRevealScope } from '../../guide/GuideRevealScope'
import { useGuidePredicate, useGuideSelection } from '../../guide/guidePredicates'
import { useGuardedLeave, useMayLeaveForNavigation, usePublishNavigationStake, useRegisterNavigationLeaveGuard } from '../../components/NavigationLeaveGuard'
import { hasNoCrewmates } from './rosterFilter'
import { AnimatePresence, animate, motion } from 'framer-motion'
import OverlayDrawer from '../../components/OverlayDrawer'
import { isTrapEntry } from '../../lib/routeHistoryPosition'
import { RunInTerminalScope } from '../../components/runInTerminalScope'
import TeamDialog from './TeamDialog'
import { TEAM_PARAM } from './teamGroups'
import { SidePanelDockHost } from '../../components/SidePanelGlyph'
import { FACE_FLIGHT_EASE, FACE_FLIGHT_SECS, MembersPageProfileCard, useMembersPageProfile, useMembersPageProfileFlight, useMembersPageSchedules, type FaceFlight } from './MembersPageProfile'
import { MembersPageRosterColumn, useMembersPageRosterView } from './MembersPageRoster'
import { MembersPageSidePanel, useMembersPageSidePanel } from './MembersPageSidePanel'
import { MembersPageThreadColumn, useMembersPageThread, useMembersPageThreadView } from './MembersPageThread'
import { MEMBER_PARAM, ROSTER_DRAWER_UNCOVERED_PX, type GoneNotice, type PostCreateFailure, type ProfileCardOpen } from './membersPageModel'
import { useMembersPageCrewEditor } from './useMembersPageCrewEditor'
import { useMembersPageDrawers, useMembersPageDrawerSwipes } from './useMembersPageDrawers'
import { useMembersPageLayout } from './useMembersPageLayout'
import { useMembersPageNavigation } from './useMembersPageNavigation'
import { useMembersPageRoster, useMembersPageRosterSignals } from './useMembersPageRoster'
import { useMembersPageTeams, useMembersPageTeamView } from './useMembersPageTeams'

export {
  resolveDefaultMember, resolveMateLanding, memberMemoryDisplay, CREW_DASHBOARD_TAB_ID, CREW_PANEL_TAB_IDS,
  MEMBERS_UNFED_VIEWS, MEMBERS_WITHHELD_VIEWS, MEMBERS_UNCONFIRMED_WITHHELD_VIEWS, panelSitsBeside,
  panelChrome, createDraftFromHref,
} from './membersPageModel'

// Lazy so the editor's code (this dialog + the field components it re-exports)
// splits out of the main App chunk — it is only needed once the identity pill
// opens the modal, and keeping it eager pushed App over its bundle budget. The
// hook stays eager: it is called every render to drive the pill's open state.
const CrewEditorModal = lazy(() => import('../../components/crew/CrewEditorModal'))

/** Creating a crewmate happens in a modal over this page: the header
 *  "+" New crewmate, the empty-state hero and the switcher's New crewmate open
 *  `NewCrewmateDialog` as a modal with its Advanced settings folded; the
 *  hero's Advanced link opens the same card with Advanced settings already
 *  unfolded. A drafted proposal (a crewmate's proposal link carrying a name and
 *  goal, a guide) opens the same card with that name and goal filled in. All
 *  perform the same `POST /api/agents` write as the crew manager's create form
 *  (one write path, several front doors); this page's card also asks for the
 *  crewmate's first greeting.
 *  Editing an existing crewmate also happens IN this page now (CREW-18688): the
 *  Profile card's edit controls open `CrewEditorModal` as a modal, driven by the
 *  shared `useCrewEditor` state machine — the SAME editor the crew manager
 *  mounts, so there is still one write path, reached from two surfaces. */

function CrewFaceFlight({ flight, onDone }: { flight: FaceFlight; onDone: () => void }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    // The copy is drawn at the card face's side and scaled, so its pill-sized
    // end is a scale-down of a sharp raster, not a blurry scale-up. The scale
    // would shrink the face's corner radius and hairline border with it, while
    // the real faces at either end draw both at their fixed CSS size — the
    // corner visibly snapped on landing — so the two are counter-scaled to
    // hold their on-screen size for the whole flight.
    const img = el.querySelector('img')
    const rest = img ? getComputedStyle(img) : null
    const restRadius = rest ? parseFloat(rest.borderRadius) || 0 : 0
    const restBorder = rest ? parseFloat(rest.borderWidth) || 0 : 0
    const place = (left: number, top: number, side: number) => {
      const k = side / PROFILE_FACE_PX
      el.style.transform = `translate(${left}px, ${top}px) scale(${k})`
      if (img) {
        img.style.borderRadius = `${restRadius / k}px`
        img.style.borderWidth = `${restBorder / k}px`
      }
    }
    const { from } = flight
    place(from.left, from.top, from.width)
    const ctrl = animate(0, 1, {
      duration: FACE_FLIGHT_SECS,
      ease: FACE_FLIGHT_EASE,
      onUpdate: (p) => {
        const to = flight.to() ?? from
        place(from.left + (to.left - from.left) * p, from.top + (to.top - from.top) * p, from.width + (to.width - from.width) * p)
      },
      onComplete: onDone,
    })
    return () => ctrl.stop()
  }, [flight, onDone])
  return <OverlayPortal>
    <div
      ref={ref}
      className="fixed left-0 top-0 z-[60] pointer-events-none origin-top-left will-change-transform"
      aria-hidden="true"
      data-testid="crew-face-flight"
    >
      <CrewMonitoredAvatar
        seed={flight.seed}
        avatar={flight.avatar}
        slotKey={flight.slotKey}
        running={flight.running}
        automation={flight.automation}
        size={PROFILE_FACE_PX}
        working="full"
      />
    </div>
  </OverlayPortal>
}

export default function MembersPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const leave = useGuardedLeave()
  const location = useLocation()
  const queryClient = useQueryClient()
  const rosterRead = useMembersPageRoster()
  const { rosterQuery, existingNames, loaded, loadError, members } = rosterRead
  // Identity is the exact crew name (unique in the registry); the slug is not.
  const [activeName, setActiveName] = useState<string>('')
  // The member the LAST open asked for, written synchronously by `activate`.
  // useMembersPageNavigation's URL sync effect guards on this, not on `activeName`: the roster
  // is a React Query read, so a store update (the thread endpoint confirming a
  // key patches the row) can re-render this component on React's sync lane
  // BEFORE the default-lane `setActiveName` from the open has committed —
  // and an effect re-run in that window would see the old name, open the same
  // member twice and, for a stand-in open, overwrite the remembered member.
  const activeNameRef = useRef('')
  // Callback ref on the OPEN row only: React calls it as a row becomes the
  // open one (the prop flips from undefined to this), so no effect has to
  // re-find the element. `nearest` scrolls only when the row is actually
  // out of view — a click on a visible row must not shift the list. Guarded:
  // happy-dom has no scrollIntoView.
  const scrollActiveRowIntoView = useCallback((el: HTMLButtonElement | null) => {
    el?.scrollIntoView?.({ block: 'nearest' })
  }, [])
  // The URL is the one source of WHICH member is open; activeName follows it
  // (useMembersPageNavigation's sync effect). Clicks write the URL, never activeName directly, so
  // the phone's back gesture, a reload and a shallow link go through the same
  // path as a click.
  const [searchParams, setSearchParams] = useSearchParams()
  const urlMember = searchParams.get(MEMBER_PARAM) ?? ''

  // CREW-18688: editing a bot opens the crew editor as a MODAL on this page,
  // instead of navigating to /capabilities?tab=crews. The editor's whole
  // state machine lives in useCrewEditor; CrewEditorModal renders it. The
  // identity pill in the thread header sets `editingCrew`, which drives the hook.
  const [editingCrew, setEditingCrew] = useState('')
  // The profile card (crewmate-panel IA): a floating hover card centred over the
  // thread, which tab it opened on. Every explicit open gets a nonce so asking
  // for the same initial tab again still resets a card the user navigated within.
  // The header pill hides while the card is up and the side panel is not (the
  // card's head names the crewmate then).
  const [profile, setProfile] = useState<ProfileCardOpen | null>(null)

  const active = useMemo(
    () => members.find((m) => m.name === activeName),
    [members, activeName],
  )

  const activeSlug = active?.slug ?? ''
  const activeMemberName = active?.name ?? ''
  const crewEditing = useMembersPageCrewEditor({
    rosterRead, leave, queryClient, activeName, setSearchParams, urlMember, editingCrew, setEditingCrew,
  })
  const { crewEditor, crewModalAtStake, crewModalAtStakeRef, crewModalBusy, crewModalBusyRef } = crewEditing
  const teamsRead = useMembersPageTeams({ searchParams })
  const { teams, urlTeam, setTeamDialog, teamDialog } = teamsRead
  // Set when a URL NAMED a member that is gone: the user asked for someone
  // specific, so the outcome is said out loud — above the fallback thread
  // (`shown` = who opened instead), or above the roster when nothing could
  // open in their place (`shown` is ''). The remembered-member fallback never sets it —
  // there the user named nobody. Cleared once a different member opens.
  const [gone, setGone] = useState<GoneNotice | null>(null)
  // The New crewmate card, shown in place of the chat.
  const [createOpen, setCreateOpen] = useState(false)
  // Whether the card opens with Advanced settings unfolded: folded for New
  // crewmate, unfolded for the empty-state hero's Advanced link.
  // A proposal carrying a goal unfolds it too (the card's own rule), so the
  // goal is in view.
  const [createExpanded, setCreateExpanded] = useState(false)
  // The proposal an opening starts from (a create link's name and goal), or
  // none. A NEW object per opening: the card re-fills from it by identity.
  const [createDraft, setCreateDraft] = useState<CrewmateDraft>()
  const mayLeave = useMayLeaveForNavigation()
  // The card's own report of whether the user changed what it opened with,
  // and whether a create is in flight.
  const createEditedRef = useRef(false)
  const createBusyRef = useRef(false)
  const onCreateDraftState = useCallback(({ edited, busy }: { edited: boolean; busy: boolean }) => {
    createEditedRef.current = edited
    createBusyRef.current = busy
  }, [])
  const createOpenRef = useRef(false)
  createOpenRef.current = createOpen
  // Set when the user chose another chat or team while a create was in
  // flight: that create's success must not replace the chat they picked.
  const leftCreateRef = useRef(false)
  /** Every door to the New crewmate card: the "+" menu, the hero and the
   *  switcher (no draft), and a create link or guide (a proposed name and
   *  goal). A card already open is left only if its guard agrees, which asks
   *  only when the user changed it (a guide that brought us here already
   *  asked: `leaveGranted`); refused, it stays as it is. The chat behind the card
   *  stays mounted, so its unsent draft is there again on the way back. */
  const openCreate = useCallback((expanded: boolean, draft?: CrewmateDraft, leaveGranted = false) => {
    // A create in flight keeps its card as sent, open or left: a second
    // opening would blank a form still locked on that create, and its
    // settle would then close or error the card the user just opened.
    if (createBusyRef.current) return
    if (createOpenRef.current && !leaveGranted && !mayLeave()) return
    createEditedRef.current = false
    if (!createBusyRef.current) leftCreateRef.current = false
    setLeftCreateFailure(null)
    // Always a new object, even with no proposal: the card resets by the
    // draft's identity, and a card already open must drop what was typed.
    setCreateDraft(draft ? { ...draft } : { name: '', goal: '' })
    setCreateExpanded(expanded)
    setCreateOpen(true)
  }, [mayLeave])
  const draftRequest = searchParams.get('create') === '1'
  // Set below, once the card's close exists: records the guide that opened it.
  const markGuideOpenedRef = useRef<() => void>(() => {})
  useEffect(() => {
    if (!draftRequest) return
    const granted = (location.state as { leaveGranted?: boolean } | null)?.leaveGranted === true
    markGuideOpenedRef.current()
    openCreate(false, { name: searchParams.get('name') ?? '', goal: searchParams.get('goal') ?? '' }, granted)
    const next = new URLSearchParams(searchParams)
    next.delete('create'); next.delete('name'); next.delete('goal')
    // A create link names no member, so the chat it was followed from stays
    // the one behind the card (and the one its close returns to), rather than
    // whatever the page's own landing would pick for a bare `/members`.
    if (!next.get(MEMBER_PARAM) && !next.get(TEAM_PARAM) && activeNameRef.current) {
      next.set(MEMBER_PARAM, activeNameRef.current)
    }
    setSearchParams(next, { replace: true })
  }, [draftRequest, searchParams, setSearchParams, openCreate, location.state])
  // A guide that opened the card and was then called off steps it aside so the
  // chat (with the guide's result line) shows again -- only while the user has
  // not touched the draft. An edited draft is theirs and stays open.
  markGuideOpenedRef.current = useGuideOpenedSurface('crewmate.create', {
    open: createOpen,
    isPristine: () => !createEditedRef.current && !createBusyRef.current,
    close: () => setCreateOpen(false),
  })
  // Choosing a chat or a team while the card is up means "show me that": the
  // card is left only if its own guard agrees (a create in flight asks too).
  // A card whose create just succeeded has already been stood down by
  // `handleCreated`, so opening the new crewmate passes straight through.
  const leaveCreate = useCallback((): boolean => {
    if (createOpenRef.current) {
      if (!mayLeave()) return false
      if (createBusyRef.current) leftCreateRef.current = true
      setCreateOpen(false)
    }
    return true
  }, [mayLeave])
  // The rail's Crewmates row (or a palette jump to `/members`) while the card
  // is up means "show me the page", like choosing a chat does. The navigation
  // lands on this same page, which stays mounted with `createOpen` still true,
  // so without this the card stayed exactly where it was and the click read as
  // dead -- even after the user answered Leave. Read off the location LANDING:
  // a PUSH onto a bare `/members` while the card is open. Three marks tell that
  // click from every other way this location changes -- this page's own URL
  // writes (the remembered chat, a gone name cleared, a create link's cleanup)
  // are replaces, the phone's drawer and team pushes name a member or a team,
  // and the Back guard's own duplicate entry carries its marker.
  //
  // The URL is where the user landed, never proof that anyone asked. A caller
  // that asked the guards just now says so with `leaveGranted` in the entry's
  // state (the rail's `NavItem`, the palette, the guide's `enter`): then Leave
  // was the answer and the card closes without a second question. Any other
  // push here -- the rail row clicked while the address is already a bare
  // `/members`, which asks nothing; a raw `navigate('/members')` -- goes through
  // `leaveCreate`, which asks the card's own guard when there is text to lose
  // and leaves the card standing when the user says Stay. Typed text is never
  // dropped unasked. A create in flight is left, not blanked, and its settle
  // must not replace the chat (`leftCreateRef`), exactly as `leaveCreate` does.
  const navigationType = useNavigationType()
  const landingKeyRef = useRef(location.key)
  useEffect(() => {
    if (location.key === landingKeyRef.current) return
    landingKeyRef.current = location.key
    if (navigationType !== 'PUSH' || isTrapEntry(location.state)) return
    if (location.pathname !== '/members' || location.search !== '' || !createOpen) return
    const granted = (location.state as { leaveGranted?: boolean } | null)?.leaveGranted === true
    if (!granted) { leaveCreate(); return }
    if (createBusyRef.current) leftCreateRef.current = true
    setCreateOpen(false)
  }, [location.key, location.pathname, location.search, location.state, navigationType, createOpen, leaveCreate])
  const openAdvanced = useCallback(() => openCreate(true), [openCreate])
  const openQuick = useCallback(() => openCreate(false), [openCreate])
  /** The DM column, the containing block of the modal profile card. */
  const threadColumnRef = useRef<HTMLElement>(null)
  // Crewmates just created here whose first chat open has not answered yet,
  // keyed by NAME: two creates can race inside one thread round trip. An
  // entry lives until its own thread answers (confirmed, collision or failed
  // open) and is what lets that answer release the create hold. The first
  // greeting itself is not the page's: the server owes it to the crewmate
  // (`first_greeting` on the create) and the greeting request every member
  // thread open makes (`useFirstGreeting`) starts it, hidden.
  const pendingOpens = useRef(new Set<string>())
  // The post-create follow-up (`openCreated` → roster re-read → thread open)
  // spans several awaits, and nothing above cancels them when the user leaves
  // the page mid-way: a route change unmounts this component, but the roster
  // refetch still resolves and `openThread`'s `onSuccess` still runs.
  // Continuing would `setSearchParams` the page they navigated to back to
  // `/members?member=<new name>`. Every post-await step checks this ref and,
  // when the page is gone, drops the follow-up.
  const pageMounted = useRef(true)
  useEffect(() => {
    pageMounted.current = true
    return () => {
      pageMounted.current = false
    }
  }, [])
  // A step AFTER a successful create that failed: the roster re-read. The
  // record is what the retry needs and the notice above the chat column says
  // so; the create itself is never in doubt here (the server has the
  // crewmate), so the dialog does not reopen.
  const [postCreateError, setPostCreateError] = useState<PostCreateFailure | null>(null)
  // Mirror for the thread mutation's callbacks, which must read the CURRENT
  // record, not the one of the render that armed them.
  const postCreateErrorRef = useRef(postCreateError)
  postCreateErrorRef.current = postCreateError
  // Whether the post-create notice may be closed without its retry: only
  // when the CACHED roster is non-empty — below md the
  // notice column replaces the roster, so an undismissable notice over a
  // roster that has other crewmates would lock every existing chat behind a
  // server that keeps failing (Opus, round 36). Closing it leaves the old
  // list, missing the new name until the next read; that is the honest
  // trade against a page with no way back. Over an EMPTY cached roster it
  // stays undismissable: "No crewmates yet" under "Radar was created" would
  // be two contradicting statements in one column. "Empty" is the hero's own
  // predicate (`hasNoCrewmates`), so the two can never disagree about what
  // counts as empty.
  const postCreateDismissable =
    postCreateError !== null && !hasNoCrewmates(members)
  // The crewmate whose follow-up (roster re-read, chat open) is still in
  // flight. One at a time, by design: `postCreateError` holds ONE record, so a
  // second create started inside the first one's window could overwrite the
  // first one's retry. While this is set the header "+" is held (the hero is
  // already gone once a crewmate exists), so the window cannot be entered; it
  // clears at every terminal point of the follow-up, a failed chat open
  // included.
  const [followUp, setFollowUp] = useState<CreatedCrewmate | null>(null)
  // Mirror for the thread mutation's callbacks (same reason as
  // `postCreateErrorRef`): an open's answer must read the follow-up that is
  // in flight NOW, not the one of the render that armed the callback.
  const followUpRef = useRef(followUp)
  followUpRef.current = followUp
  // The one reason every create door (header "+", both heroes) is held, or
  // `null` when creating is open. The failed-step record wins the wording:
  // it is the one with a retry the user can act on.
  const createHeld: string | null = postCreateError
    ? !postCreateDismissable
      // A roster notice over an EMPTY cached roster has no dismiss (see its
      // `onDismiss`), so its hold names only the retry; "retry or dismiss"
      // is every dismissable notice's.
      ? t('pages.membersPage.add_member_pending_roster', { name: postCreateError.created.name })
      : t('pages.membersPage.add_member_pending', { name: postCreateError.created.name })
    : followUp
      ? t('pages.membersPage.add_member_settling', { name: followUp.name })
      : loadError
        // A failed arrival read leaves the dialog's exact-name check with
        // nothing to check against (`existingNames` is empty), and the
        // post-failure reconcile in the dialog is premised on the roster
        // having been read. The door is held until a read lands; the
        // notice's Try again is the way back.
        ? t('pages.membersPage.roster_load_failed')
        : null
  // The member the fallback is about to open in place of a gone one a link
  // named. Set right before the fallback's URL write, read (and cleared) by
  // the open that write triggers, so that open can skip the memory write. A
  // ref, not state: it is a note between two runs of one effect, and must
  // not re-arm it.
  const goneStandInRef = useRef('')
  // The open member's thread, as the thread endpoint last answered it. The
  // roster's `bound`/`slot_key` are never trusted as mountable: dm.json
  // outlives the live slot (a restart drops an unmessaged slot while the
  // binding survives), and mounting an unconfirmed key would let the first
  // message auto-create an ordinary UNPINNED slot on the member key.
  // POST /api/members/{slug}/thread is idempotent and is the only creator/
  // repairer of member slots — so every open goes through it (the mutation
  // `openThread`, in MembersPageThread.tsx), and its answer is cached per member NAME
  // (memberThreadQueryKey) so a return to a member mounts the cached thread at once while the
  // re-POST repairs in the background. Keying by the member the answer was
  // requested FOR makes a late completion of a previously selected member
  // harmless. `skipToken`: this entry is written by the mutation, never
  // fetched — the read only subscribes to it.
  const threadQuery = useQuery<MemberThreadOutcome>({
    queryKey: memberThreadQueryKey(activeName),
    queryFn: skipToken,
  })
  const threadOutcome = threadQuery.data
  // The slot a row's live readings (presence, unread, patrol) resolve to: the
  // thread endpoint's confirmed key for the open member, the roster binding
  // for everyone else. The roster row is patched with the confirmed key the
  // moment an open confirms one (see openThread), so a member opened earlier
  // in this visit keeps resolving after the selection moves on.
  const slotKeyOf = useCallback(
    (m: MemberRosterRow) =>
      (m.name === activeName ? threadOutcome?.slot_key : '') || m.slot_key,
    [activeName, threadOutcome],
  )
  const layout = useMembersPageLayout({ rosterRead, teamsRead, activeName })
  const { isMobile, winW, rosterShown } = layout
  const profileCard = useMembersPageProfile({ layout, activeName, profile, setProfile })
  const { schedGuardRef, schedAtStakeRef, openMemberRef } = profileCard
  const drawers = useMembersPageDrawers({
    teamsRead, layout, navigate, location, activeName, createOpen, navigationType, active,
  })
  const {
    rosterDrawerOpen, rosterDrawerMounted, rosterDrawerX, rosterDrawerPanelRef, rosterDrawerScrimRef,
    setRosterDrawerHost, mobileNavRail, vv, keyboardInset, rosterDrawerScrim, closeRosterDrawer,
    rosterDrawerAvailable, sidePanelSwipeAvailable, placeRoster,
  } = drawers
  // Live presence rides the already-subscribed WS `slots` frames — the roster
  // endpoint only fills the cold-start gap (its `running` is a snapshot).
  const liveSlots = useAppSelector((s) => s.dashboard.slots)
  // Whether a real slots snapshot has arrived. Before it, an empty `slots` is
  // ambiguous (the store itself refuses to treat a pre-first-frame empty frame
  // as authoritative), so the driving-sessions block must not assert "not
  // driving" on a cold open or a WS reconnect — it shows a skeleton instead,
  // the same three-state discipline the Recent-activity section keeps.
  const slotsLoaded = useAppSelector((s) => s.dashboard.slotsLoaded)
  // What the panel gesture reads: the panel and its opener are both drawn only
  // while a member is open, so the toggle is inert otherwise.
  useEffect(() => { openMemberRef.current = !!active }, [active, openMemberRef])
  // Guide facts: the roster picker's selection (whether a crewmate is open and
  // whether any exists, never which), and whether the roster is shown, per
  // layout: folded into the switcher on a wide screen (`members.roster`),
  // behind the open chat on a phone, in the roster drawer (`members.roster-phone`).
  useGuideSelection('crewmate_selected', { selected: !!active, available: loaded && !hasNoCrewmates(members), name: active?.name })
  // The header "Add…" menu is drawn once the roster has a crewmate.
  useGuidePredicate('has_crewmates', loaded ? !hasNoCrewmates(members) : null)
  useGuideRevealScope(isMobile ? undefined : 'members.roster', rosterShown)

  // The phone drawers bind on the page root: a rightward drag reveals the
  // roster drawer, a leftward one the side panel. Told apart by DIRECTION, and
  // each is off while the other's panel is OPEN, because one's closing drag is
  // the other's opening drag. The roster drawer exists only while something is
  // open over the roster; the side panel only while a crewmate is.
  const membersRootRef = useRef<HTMLDivElement | null>(null)
  // Shown exactly where `placeRoster` draws it: on the page while no drawer
  // exists, in the drawer only while it is open. A team keeps the drawer too,
  // so `!activeName` alone would claim a roster that is not on screen.
  useGuideRevealScope(isMobile ? 'members.roster-phone' : undefined, !rosterDrawerAvailable || rosterDrawerOpen)
  const drawerSwipes = useMembersPageDrawerSwipes({
    teamsRead, layout, drawers, activeName, createOpen, membersRootRef,
  })
  const { rosterDrawerDragging } = drawerSwipes
  // The profile card (opened from the header pill) holds the Permissions row a
  // "crewmate tools" guide points at (`members.permissions`).
  useGuideRevealScope('members.profile', !!profile)
  const rosterView = useMembersPageRosterView({
    rosterRead, teamsRead, t, queryClient, activeName, slotKeyOf, liveSlots, active,
  })
  const { orderedMembers } = rosterView
  const thread = useMembersPageThread({
    profileCard, t, queryClient, pendingOpens, setFollowUp, followUpRef, threadOutcome, active,
  })
  const { freshStartConfirmDialog } = thread
  const sidePanel = useMembersPageSidePanel({
    layout, profileCard, drawers, rosterView, thread, navigate, location, queryClient, activeName, searchParams,
    setSearchParams, profile, setProfile, liveSlots, slotsLoaded, active, activeMemberName,
  })
  const { schedAtStake, schedulesMountedRef, runInTerminalScope } = sidePanel
  const schedules = useMembersPageSchedules({
    crewEditing, layout, rosterView, sidePanel, t, profile, activeSlug, activeMemberName,
  })
  const {
    schedDraftDirty, schedSaving, confirmSched, schedConfirmDialog, releaseSchedRetention, mayLeaveSchedules,
  } = schedules
  // ONE guard for exits that unmount Profile. The ref is what lets callbacks declared
  // in the owners (the header's panel reveal in MembersPageProfile.tsx, a roster
  // crewmate switch in useMembersPageNavigation.ts) reach it.
  //
  // The gate is the section being MOUNTED, not visible. The dirty flag is only
  // meaningful while the form exists, and a stale true would confirm-prompt over a tab
  // with no form -- but draft-destroying exits must stay armed while a hidden Profile
  // is being retained: hidden is exactly when the draft has nowhere else to live.
  const schedulesMounted = !!profile
  schedulesMountedRef.current = schedulesMounted
  const goalsDraft = useRef({ dirty: false, busy: false })
  const [goalsAtStake, setGoalsAtStake] = useState(false)
  const onGoalsDraftChange = useCallback((dirty: boolean, busy: boolean) => {
    goalsDraft.current = { dirty, busy }
    setGoalsAtStake(dirty || busy)
  }, [])
  // The Export view's lessons text: typed, unsaved, and held only in that view, so
  // every exit that unmounts Profile asks about it the way it asks about Goals.
  const exportDraft = useRef(false)
  const [exportAtStake, setExportAtStake] = useState(false)
  const onExportDraftChange = useCallback((dirty: boolean) => {
    exportDraft.current = dirty
    setExportAtStake(dirty)
  }, [])
  const mayLeaveExport = useCallback(async () => {
    if (!exportDraft.current) return true
    return confirmSched({
      title: t('pages.membersPage.export_discard_lessons'),
      body: t('pages.membersPage.export_discard_lessons_body'),
      confirmLabel: t('pages.membersPage.export_discard_lessons_confirm'),
    })
  }, [confirmSched, t])
  const mayLeaveGoals = useCallback(async () => {
    if (goalsDraft.current.busy) return false
    if (!goalsDraft.current.dirty) return true
    return confirmSched({
      title: t('components.markdownPanel.discard_unsaved_changes'),
      confirmLabel: t('components.markdownPanel.discard_changes_button'),
    })
  }, [confirmSched, t])
  schedGuardRef.current = useCallback(async () => {
    // An in-flight editor write must not be abandoned: refuse the leave outright
    // until it settles (the request could still land and persist a "discarded"
    // change), matching requestClose's mid-commit refusal.
    if (crewModalBusyRef.current) return false
    // The modal's unsaved panes are a second draft this page can lose on an
    // exit it does not own. The async guard backs the in-app exits (sidebar
    // clicks and session opens); confirm before discarding modal edits, the
    // same question the Schedules draft asks.
    if (crewModalAtStakeRef.current && !window.confirm(t('pages.membersPage.edit_leave_draft'))) return false
    if (!schedulesMounted) return true
    if (schedSaving.current || goalsDraft.current.busy) return false
    return await mayLeaveGoals() && await mayLeaveExport() && await mayLeaveSchedules()
  }, [schedulesMounted, mayLeaveGoals, mayLeaveExport, mayLeaveSchedules, t, crewModalAtStakeRef, crewModalBusyRef, schedSaving])
  schedAtStakeRef.current = useCallback(
    () => schedulesMounted && (schedSaving.current || schedDraftDirty.current || goalsDraft.current.dirty || goalsDraft.current.busy || exportDraft.current),
    [schedulesMounted, schedDraftDirty, schedSaving],
  )
  // Leaving the ROUTE is the last exit, and the registry that owns it is synchronous, so
  // it cannot use the app's confirm dialog. `window.confirm`, exactly as the New crewmate
  // dialog's own guard does on this page for the same reason. A create in flight refuses
  // outright here too: the POST would land with the page gone.
  useRegisterNavigationLeaveGuard(() => {
    // Refuse a route leave while an editor write is in flight (see the async
    // guard): the request could still persist a change the user discarded.
    if (crewModalBusyRef.current) return false
    // Leaving the route also abandons an open editor modal's unsaved panes.
    // Same synchronous `window.confirm` the Schedules draft uses here, since
    // the registry cannot await the app's confirm dialog.
    if (crewModalAtStakeRef.current && !window.confirm(t('pages.membersPage.edit_leave_draft'))) return false
    if (!schedulesMounted) return true
    if (schedSaving.current || goalsDraft.current.busy) return false
    if (goalsDraft.current.dirty && !window.confirm(t('components.markdownPanel.discard_unsaved_changes'))) return false
    if (exportDraft.current && !window.confirm(`${t('pages.membersPage.export_discard_lessons')}\n${t('pages.membersPage.export_discard_lessons_body')}`)) return false
    if (!schedDraftDirty.current) return true
    const ok = window.confirm(t('pages.membersPage.schedules_leave_draft'))
    if (ok) releaseSchedRetention()
    return ok
  })
  // Registering a guard is not enough on its own: `NavigationBackGuard` arms off the
  // published STAKE, not off the guard, so without this the browser's own Back button
  // discarded the draft silently while every wired in-app exit asked. The New crewmate
  // dialog on this same page already publishes, which is what made the gap uneven rather
  // than merely absent.
  const anyDraftAtStake = schedAtStake || goalsAtStake || exportAtStake || crewModalAtStake || crewModalBusy
  usePublishNavigationStake(anyDraftAtStake)
  // A reload or a tab close is not a route change, so the guard above never sees it; the
  // browser's own prompt is the only thing that can. Registered while a Schedules draft,
  // the editor modal's unsaved panes, or an in-flight editor write is at stake, since an always-on `beforeunload` nags on every ordinary close.
  useEffect(() => {
    if (!anyDraftAtStake) return
    const onUnload = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = '' }
    window.addEventListener('beforeunload', onUnload)
    return () => window.removeEventListener('beforeunload', onUnload)
  }, [anyDraftAtStake])
  const threadView = useMembersPageThreadView({
    layout, rosterView, thread, t, createOpen, active,
    profileFloating: !!profile && profile.placement !== 'column',
    rosterDrawerMounted,
  })
  const rosterSignals = useMembersPageRosterSignals({ rosterRead, rosterView, slotKeyOf })
  const profileFlight = useMembersPageProfileFlight({ profileCard, rosterView, rosterSignals, thread, activeName, profile, active })
  const { faceFlight, endFaceFlight } = profileFlight
  // A create the user walked away from that then FAILED: the card that says so
  // is hidden, so the page says it instead, in the card's own words. Cleared by
  // the next opening of the card, which shows its own state again.
  const [leftCreateFailure, setLeftCreateFailure] = useState<string | null>(null)
  const navigation = useMembersPageNavigation({
    rosterRead, teamsRead, layout, profileCard, drawers, rosterView, thread, sidePanel, location, queryClient,
    activeName, setActiveName, activeNameRef, searchParams, setSearchParams, urlMember, setGone, draftRequest,
    leaveCreate, followUp, goneStandInRef, activeMemberName,
  })
  const { openMember } = navigation

  // A crewmate was just created in this page's dialog: close it and open its
  // chat; the hold rides that open (openThread's outcome releases it). The
  // roster is re-read BEFORE the URL names the new crewmate, so the URL sync
  // effect finds it — a name not yet on the roster would read
  // as "gone" and open someone else in its place. A FAILED re-read must not
  // take that path either: react-query keeps the stale roster as `res.data`
  // on error, so the name would be missing for the wrong reason. It is
  // reported instead, with a retry that repeats exactly this step.
  const openCreated = useCallback(async (created: CreatedCrewmate) => {
    setPostCreateError(null)
    setFollowUp(created)
    // A re-read that did not actually land is a failed re-read. The star
    // mutation's `cancelQueries` (MembersPageRoster.tsx) can cut this refetch short, and a
    // cancelled query REVERTS to its previous successful state: `refetch()`
    // then resolves without `isError`, carrying the pre-create roster, and
    // the new name would read as absent and someone else's chat opened. Only
    // a response newer than what we had counts as fresh.
    const before = queryClient.getQueryState(MEMBERS_ROSTER_QUERY_KEY)?.dataUpdatedAt ?? 0
    const res = await rosterQuery.refetch()
    // The user left while the roster was re-reading: nothing below may run.
    // The URL write would drag the page they chose back here.
    if (!pageMounted.current) return
    const fresh = !res.isError && res.dataUpdatedAt > before
    if (!fresh) {
      setFollowUp(null)
      setPostCreateError({ kind: 'roster', created })
      return
    }
    const hit = res.data?.find((r) => r.name === created.name)
    if (hit) {
      // The hold now rides the thread open: released by openThread's
      // onSuccess or onError.
      pendingOpens.current.add(created.name)
      if (await openMember(hit)) return
      pendingOpens.current.delete(created.name)
      // The Schedules guard refused: the user kept a dirty draft, so no thread
      // opens and nothing is left to release the hold — the create button would
      // stay disabled for the rest of the page's life. Drop the hold here; the
      // crewmate IS on the roster now, and its first open asks for the
      // greeting the server owes it.
      setFollowUp(null)
      return
    }
    // The re-read landed without the name (the server accepted a name the
    // roster's read does not list). The URL write below lets the URL sync
    // effect say "gone"; the hold is dropped rather than kept for a row that
    // is not coming.
    setFollowUp(null)
    setSearchParams({ [MEMBER_PARAM]: created.name }, { replace: true })
  }, [rosterQuery, openMember, setSearchParams, queryClient])

  // The roster re-read for a create the user already left: the chat they
  // chose stays open. A re-read that did not land (an error, or a cancelled
  // refetch that reverted to the pre-create roster, see `openCreated`) is
  // reported through the same notice, whose retry repeats only this read.
  const listCreated = useCallback(async (created: CreatedCrewmate) => {
    const before = queryClient.getQueryState(MEMBERS_ROSTER_QUERY_KEY)?.dataUpdatedAt ?? 0
    const res = await rosterQuery.refetch()
    if (!pageMounted.current) return
    if (res.isError || res.dataUpdatedAt <= before) setPostCreateError({ kind: 'roster', created, stay: true })
  }, [rosterQuery, queryClient])

  const handleCreated = useCallback((created: CreatedCrewmate) => {
    // The form's work is done: the open that follows must not ask to leave it.
    createOpenRef.current = false
    setCreateOpen(false)
    if (leftCreateRef.current) {
      // The user already chose another chat: list the new crewmate, stay put.
      leftCreateRef.current = false
      void listCreated(created)
      return
    }
    void openCreated(created)
  }, [openCreated, listCreated])
  const handleCreateFailed = useCallback((message: string) => {
    if (!leftCreateRef.current) return
    leftCreateRef.current = false
    setLeftCreateFailure(message)
  }, [])

  const retryPostCreate = useCallback(() => {
    const failed = postCreateError
    if (!failed) return
    setPostCreateError(null)
    void (failed.stay ? listCreated(failed.created) : openCreated(failed.created))
  }, [postCreateError, openCreated, listCreated])
  const teamView = useMembersPageTeamView({
    rosterRead, teamsRead, layout, profileCard, rosterView, rosterSignals, navigate, location, setSearchParams,
    urlMember, leaveCreate, slotKeyOf,
  })
  const { openTeam } = teamView

  return (
    // No bottom inset on the root: the card columns carry their own pb-2 and
    // the side panel brings the chat SidePanel's mb-2, so all three end 8px
    // above the window edge without stacking two insets. No right padding
    // either — the panel docks FLUSH to the window's right edge, exactly as it
    // does in the chat page's actbar column; the card columns' pr-2 lives on
    // the inner wrapper below.
    // This page has no bottom row for the side panel, so every panel glyph
    // in its transcripts draws the right-dock pane.
    <SidePanelDockHost value={false}>
    <RunInTerminalScope.Provider value={runInTerminalScope}>
    <div
      className="flex h-full min-h-0"
      data-testid="members-page"
      ref={membersRootRef}
      // Claims both sides from the shell's nav-drawer swipe exactly while this
      // page binds its own drawers, so one drag never arms two of them. With
      // nothing open over the roster the page binds nothing, and the shell's
      // gesture is left working.
      data-owns-swipe={rosterDrawerAvailable || sidePanelSwipeAvailable ? 'left right' : undefined}
    >
      {isMobile && (
        <>
          <AnimatePresence>
            {rosterDrawerMounted && (
              <motion.div
                key="roster-drawer-backdrop"
                data-testid="roster-drawer-backdrop"
                role="presentation"
                className="fixed inset-0 z-[46] bg-black/50 backdrop-blur-xs"
                ref={rosterDrawerScrimRef}
                style={{ opacity: rosterDrawerScrim, marginTop: vv.offsetTop, marginBottom: keyboardInset }}
                // A drag's release lands here as a click: it must not run a
                // second close over the settle.
                onClick={() => { if (!rosterDrawerDragging) closeRosterDrawer() }}
              />
            )}
          </AnimatePresence>
          <OverlayDrawer
            open={rosterDrawerMounted}
            width={Math.max(0, winW - ROSTER_DRAWER_UNCOVERED_PX)}
            slideX={rosterDrawerX}
            slideRef={rosterDrawerPanelRef}
            slideStyle={{ marginTop: vv.offsetTop, marginBottom: keyboardInset }}
            // The full safe-area height, over the shell's bar, as the chat
            // page's drawer does: the rail is sized for that height, and its
            // brand mark then sits where the bar's logo was.
            className="fixed top-safe bottom-safe left-safe z-50 bg-bg-elevated !py-0 rounded-r-xl shadow-lg"
          >
            <div className="flex h-full min-h-0 w-full" data-testid="roster-drawer">
              {mobileNavRail?.({ onActivate: () => closeRosterDrawer() })}
              {/* The roster aside is portaled in here (`placeRoster`), with the
                  card chrome reset the way the sessions drawer resets its pane. */}
              <div
                ref={setRosterDrawerHost}
                className="flex-1 min-w-0 h-full min-h-0 flex flex-col overflow-hidden bg-bg-elevated [&>*]:!rounded-none [&>*]:!border-0 [&>*]:!m-0 [&>*]:!shadow-none"
              />
            </div>
          </OverlayDrawer>
        </>
      )}
      {/* Card columns (roster + thread) keep the page's original insets. */}
      <div className="flex flex-1 min-w-0 gap-2 pr-2 pb-2">
      {/* Member list. Below md the page is single-pane: the roster IS the
          page until a member is picked, then the thread takes over and the
          header's back button returns here. Two fixed rails (264+300px)
          otherwise crush the flex-1 thread to zero at narrow widths.
          The card, header line, list body and rows are the Sessions sidebar's
          own recipes (components/listShell) so the two conversation lists read
          as one surface — including the kiro-light shell hook that steps the
          card back from the white canvas. */}
      {placeRoster(<MembersPageRosterColumn
        rosterRead={rosterRead} teamsRead={teamsRead} layout={layout} drawers={drawers}
        rosterView={rosterView} rosterSignals={rosterSignals} profileFlight={profileFlight}
        navigation={navigation} teamView={teamView} activeName={activeName}
        scrollActiveRowIntoView={scrollActiveRowIntoView} gone={gone} createOpen={createOpen}
        openAdvanced={openAdvanced} openQuick={openQuick} postCreateError={postCreateError}
        createHeld={createHeld} slotKeyOf={slotKeyOf}
        faceOnStageFor={thread.entranceFaceHidden && active ? active.name : ''}
      />)}

      {/* DM thread */}
      <MembersPageThreadColumn
        rosterRead={rosterRead} crewEditing={crewEditing} teamsRead={teamsRead} layout={layout}
        profileCard={profileCard} drawers={drawers} rosterView={rosterView} thread={thread}
        sidePanel={sidePanel} threadView={threadView} rosterSignals={rosterSignals}
        profileFlight={profileFlight} navigation={navigation} teamView={teamView} navigate={navigate}
        location={location} activeName={activeName} setSearchParams={setSearchParams}
        editingCrew={editingCrew} profile={profile} gone={gone} openCreate={openCreate}
        openAdvanced={openAdvanced} openQuick={openQuick} threadColumnRef={threadColumnRef}
        postCreateError={postCreateError} setPostCreateError={setPostCreateError}
        postCreateDismissable={postCreateDismissable} createHeld={createHeld} threadOutcome={threadOutcome}
        slotsLoaded={slotsLoaded} active={active} leftCreateFailure={leftCreateFailure}
        setLeftCreateFailure={setLeftCreateFailure} retryPostCreate={retryPostCreate}
      />
      {/* New crewmate: a bounded modal (its own 90vh-capped scrim + glass
          blur) over the chat it was opened from, which stays mounted and
          marked not-viewed behind it (see threadShown). */}
      <NewCrewmateDialog
        open={createOpen}
        startExpanded={createExpanded}
        initialDraft={createDraft}
        guided
        onDraftStateChange={onCreateDraftState}
        onFailed={handleCreateFailed}
        firstGreeting
        onClose={() => setCreateOpen(false)}
        onCreated={handleCreated}
        existingNames={existingNames}
      />
      </div>

      {/* Side panel — the chat page's tabbed SidePanel, docked to this page.
          Read-only observation lives in its permanent first tab (Crew
          summary); writes live in the crew manager. The + menu is the chat
          panel's own (Files / Artifacts / Terminal / Browser / Side chat …),
          all against the member's DM slot, and the strip is bucketed per
          member so it follows the roster selection. Wide windows dock it as a
          column the strip's own close control hides, with the header opener
          taking the gesture back while it is hidden; narrow ones make it an
          overlay the header button opens, with the same close control, on the
          chat page's dock motion. The create modal is an overlay, so the page
          behind it (chat AND this panel) stays in place rather than reflowing. */}
      <div className="contents">
      {active && (<>
        {faceFlight && <CrewFaceFlight key={faceFlight.key} flight={faceFlight} onDone={endFaceFlight} />}
        <MembersPageProfileCard
          crewEditing={crewEditing} profileCard={profileCard} rosterView={rosterView} thread={thread}
          sidePanel={sidePanel} schedules={schedules} profileFlight={profileFlight} navigate={navigate}
          leave={leave} setEditingCrew={setEditingCrew} profile={profile} threadColumnRef={threadColumnRef}
          slotsLoaded={slotsLoaded} active={active} activeSlug={activeSlug}
          activeMemberName={activeMemberName} onGoalsDraftChange={onGoalsDraftChange}
          onExportDraftChange={onExportDraftChange}
        />
        <MembersPageSidePanel
          layout={layout} profileCard={profileCard} drawers={drawers} rosterView={rosterView} thread={thread}
          sidePanel={sidePanel} profileFlight={profileFlight} navigation={navigation} activeName={activeName}
          active={active} activeSlug={activeSlug} activeMemberName={activeMemberName}
        />
      </>)}
      </div>
      {/* Edit team (create is hidden for the phase). A saved team opens its
          team view; a deleted one that was open drops `?team=` and the bare
          URL falls to the page's default (the remembered or most recently used
          crewmate, or the hero on an empty roster). */}
      {teamDialog && (
        <TeamDialog
          open
          team={teamDialog.team}
          teams={teams}
          members={orderedMembers}
          onClose={() => setTeamDialog(null)}
          onSaved={(saved) => {
            setTeamDialog(null)
            openTeam(saved.id)
          }}
          onDeleted={(removed) => {
            setTeamDialog(null)
            if (urlTeam === removed.id) setSearchParams({}, { replace: true })
          }}
        />
      )}
      {/* The Schedules tab's discard prompt. Raised from the strip's own guard and from
          the section's collapse toggle, so it must sit outside the panel subtree the
          answer may unmount. */}
      {schedConfirmDialog}
      {freshStartConfirmDialog}
      {/* CREW-18688: the bot-edit modal, opened in place by the thread header's
          identity pill (member-identity-pill). Renders nothing until editingCrew is
          set; the hook returns open=false until its roster read resolves the
          record. Lazy + Suspense so its code is not in the main App chunk. The
          fallback is a small NON-lazy loading dialog shown only while a pill click
          is pending and the chunk is still downloading (first open on a cold cache,
          worst over remote access): without it that first click renders nothing,
          because the hook's own loading dialog lives inside the lazy chunk. When no
          edit is requested the fallback is null, so a closed dialog renders nothing. */}
      <Suspense fallback={editingCrew ? <CrewEditorChunkLoading /> : null}>
        <CrewEditorModal ctl={crewEditor} />
      </Suspense>
    </div>
    </RunInTerminalScope.Provider>
    </SidePanelDockHost>
  )
}
