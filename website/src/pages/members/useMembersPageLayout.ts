/**
 * Where the Crewmates page's columns sit: the roster's width, pin and fold, the
 * phone top bar's state, whether the side panel docks beside the thread or
 * overlays it, and which of the two visibility flags is the live one.
 *
 * The `MembersPage` facade calls each hook here at the point its code sits in
 * the page's hook order, which `MembersPage.owners.test.tsx` pins: React runs
 * a component's effects in registration order, so moving one is a behaviour
 * change even when every test of the moved code still passes.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { isAssistantMember } from '../../lib/assistantMember'
import { usePersistedBool } from '../../hooks/usePersistedBool'
import { useIsMobile } from '../../hooks/useIsMobile'
import { hasNoCrewmates } from './rosterFilter'
import { useMobileRosterBar, type MobileRosterBarState } from '../../components/MobileNavRailContext'
import { SIDE_PANEL_MIN_W } from '../chat/SidePanel'
import { useColumnResize } from '../../hooks/useColumnResize'
import { loadRosterWidth, panelChrome, panelSitsBeside, ROSTER_MAX, ROSTER_MIN, ROSTER_WIDTH_KEY } from './membersPageModel'
import { type MembersPageRosterRead } from './useMembersPageRoster'
import { type MembersPageTeamsRead } from './useMembersPageTeams'

/** Whether the DOCKED side panel is shown. Same `mc-` family and the same one
 *  key per origin as the roster filters (MembersPageRoster.tsx): the panel is
 *  the page's furniture, not a per-member state, so the choice follows the user
 *  across members and reloads exactly as the chat page's own panel flag does. */
const PANEL_OPEN_KEY = 'mc-members-panel-open'

/** The Assistant's docked panel choice, hidden until the user opens it. The
 *  Assistant is the landing chat for a first-time user, so its page opens on
 *  the conversation alone; opening the panel there is remembered here without
 *  moving the choice every other crewmate reads from `PANEL_OPEN_KEY`. */
const ASSISTANT_PANEL_OPEN_KEY = 'mc-members-assistant-panel-open'

interface MembersPageLayoutOptions {
  rosterRead: MembersPageRosterRead
  teamsRead: MembersPageTeamsRead
  activeName: string
}

/** Column placement: roster width, pin and fold; panel docked or overlaid, and shown. */
export function useMembersPageLayout({ rosterRead, teamsRead, activeName }: MembersPageLayoutOptions) {
  const { loaded, loadError, members } = rosterRead
  const { teamsQ, urlTeam, activeTeam } = teamsRead
  // Roster width is user-adjustable on md+ (drag handle on the right edge),
  // mirroring the chat sidebar. Below md the roster is full-width single-pane
  // and the stored width is simply unused. Clamp + persist live in the shared
  // useColumnResize hook — the same primitive every resizable column uses.
  const roster = useColumnResize(ROSTER_WIDTH_KEY, loadRosterWidth, ROSTER_MIN, ROSTER_MAX)
  // Where the side panel lives. Wide enough (see panelSitsBeside) it is a
  // column beside the thread, hidden and shown by the header toggle and by the
  // panel's own close control — the chat page's panel, docked. Narrower, it is
  // an overlay the same toggle opens, because a column that narrow would fold
  // the thread to nothing. The window width is tracked live (not sampled at
  // mount) so crossing the boundary re-docks.
  const isMobile = useIsMobile()
  const [winW, setWinW] = useState(() => (typeof window !== 'undefined' ? window.innerWidth : 0))
  useEffect(() => {
    const onResize = () => setWinW(window.innerWidth)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])
  // The roster column is folded into the header's crewmate switcher while a
  // thread is open (crewmate-panel IA), so it reserves no width then — unless
  // the user pinned it back beside the thread from the switcher's "Show the
  // full roster" action. The switcher lists crewmates only; team headers, New
  // team, the star, the filters and the sort live on the roster column, and on
  // desktop (where the thread's back control is hidden and a bare /members
  // reopens a crewmate) this pin is the one way to reach them. It is page
  // state, not persisted: the folded roster is the desktop norm and a pin is a
  // detour from it. Picking a row keeps the pin; the roster header's close
  // control or the switcher action (now "Hide the roster") lifts it. A TEAM
  // view is not folded: it draws no switcher and its Back is `md:hidden`, so on
  // desktop the roster column is the only way out of `/members?team=<id>` and
  // stays beside the pane there, pinned or not.
  const [rosterPinned, setRosterPinned] = useState(false)
  // A desktop landing that has not opened its crewmate YET. The URL sync
  // effect (useMembersPageNavigation.ts) opens one on every desktop visit that names no team (the
  // URL's member, else the remembered or last-chatted one), but an effect runs
  // after paint, so `activeName` is '' on the first frame even when the
  // roster is cached. Read as "nothing open", that frame drew the full roster
  // column and the next one folded it: the flash on every switch to this tab.
  // Treated as already folded until the open lands. Not pending once the
  // effect can open no one: a failed read, an empty roster (the hero), or a
  // team view. A phone lands the same way, so it waits the same way: its
  // roster would otherwise draw as the page for that frame, then slide into
  // the drawer.
  const landingPending =
    !activeName && !urlTeam && !loadError && !(loaded && hasNoCrewmates(members))
  const rosterFolded = (!!activeName || landingPending) && !rosterPinned
  // Phone: tell the shell which top bar this page needs (see
  // `MobileRosterBarContext`). `open` once a crewmate or team is on screen,
  // with its header in the bar; `pending` while one is still being read (the
  // landing, or a `?team=` link before the teams read answers), which keeps
  // the single bar and the shell's nav-drawer swipe; `roster` for the bare
  // roster, which has no drawer and needs the regular bar's logo. A `?team=` link the teams read failed on,
  // or that names no team, opens nothing. A layout effect, so the shell
  // re-renders before paint; `roster` on unmount, so a page taken down by its
  // error boundary does not keep the navigation hidden.
  const reportRosterBar = useMobileRosterBar()
  const teamPending = !!urlTeam && !activeTeam && teamsQ.data === undefined && !teamsQ.isError
  const rosterBar: MobileRosterBarState = activeName || activeTeam
    ? 'open'
    : landingPending || teamPending ? 'pending' : 'roster'
  useLayoutEffect(() => {
    if (!reportRosterBar || !isMobile) return
    reportRosterBar(rosterBar)
  }, [reportRosterBar, isMobile, rosterBar])
  useLayoutEffect(() => (reportRosterBar ? () => reportRosterBar('roster') : undefined), [reportRosterBar])
  const rosterShown = !rosterFolded
  // The switcher chip fades out while the pin holds (the column IS its list),
  // so each of the two controls that flip the pin goes away under the press:
  // the chip's footer action, and the column's own X. Each asks for focus to
  // move to what replaced it -- the column's search, then the chip again --
  // so a keyboard user is not dropped on the page body.
  const rosterAsideRef = useRef<HTMLElement>(null)
  const switcherSlotRef = useRef<HTMLDivElement>(null)
  const pinFocusRef = useRef<'roster' | 'chip' | null>(null)
  const setRosterPin = useCallback((pinned: boolean) => {
    pinFocusRef.current = pinned ? 'roster' : 'chip'
    setRosterPinned(pinned)
  }, [])
  useEffect(() => {
    const target = pinFocusRef.current
    pinFocusRef.current = null
    if (target === 'roster' && rosterPinned) rosterAsideRef.current?.querySelector('input')?.focus()
    if (target === 'chip' && !rosterPinned) switcherSlotRef.current?.querySelector('button')?.focus()
  }, [rosterPinned])
  const beside = panelSitsBeside({ winW, rosterW: rosterShown ? roster.width : 0, isMobile })
  // On a phone the overlay must FILL its scrim. SidePanel's own mobile
  // fallback is `width: 100%`, which cannot resolve here: the overlay's inner
  // wrapper is a shrink-to-fit flex item, so a percentage child falls back to
  // the panel's max-content width and the panel lands at SIDE_PANEL_MIN_W
  // with a dimmed sliver of the thread showing beside it (issue #9979). The
  // chat page hands its panel an explicit px width for exactly this reason
  // (sidePanelFillWidth's mobile branch); this is that branch. `undefined` off
  // the phone, where the panel keeps its own resizable width in both
  // placements — the docked/overlay split is panelSitsBeside's, not this.
  const panelFillWidth = isMobile ? Math.max(SIDE_PANEL_MIN_W, winW) : undefined
  const [overlayOpen, setOverlayOpen] = useState(false)
  // `overlayOpen` is overlay-mode state only. Reset it whenever the panel docks
  // (a widening window, a narrower roster), so an open overlay does not lie in
  // wait and pop back over the thread the moment the window narrows again.
  useEffect(() => { if (beside) setOverlayOpen(false) }, [beside])
  // Docked visibility, persisted and cross-window synced, shown by default so
  // the page opens the way it always has. Kept SEPARATE from `overlayOpen`
  // because the two placements answer different questions — whether the
  // permanent column is wanted at all, and whether the drawer is up right now
  // — so dismissing the drawer must not also hide the column the next time the
  // window widens.
  const [memberDockedOpen, setMemberDockedOpen] = usePersistedBool(PANEL_OPEN_KEY, true)
  const [assistantDockedOpen, setAssistantDockedOpen] = usePersistedBool(ASSISTANT_PANEL_OPEN_KEY, false)
  const onAssistant = useMemo(() => {
    const open = members.find((m) => m.name === activeName)
    return !!open && isAssistantMember(open)
  }, [members, activeName])
  const dockedOpen = onAssistant ? assistantDockedOpen : memberDockedOpen
  const setDockedOpen = onAssistant ? setAssistantDockedOpen : setMemberDockedOpen
  const { panelVisible, showOpener } = panelChrome({ beside, dockedOpen, overlayOpen })

  return {
    roster, isMobile, winW, rosterPinned, landingPending, rosterFolded, rosterBar, rosterShown, rosterAsideRef,
    switcherSlotRef, setRosterPin, beside, panelFillWidth, overlayOpen, setOverlayOpen, dockedOpen,
    setDockedOpen, panelVisible, showOpener,
  }
}

export type MembersPageLayout = ReturnType<typeof useMembersPageLayout>
