/**
 * Crew Members — one durable, pinned DM thread per crew member.
 *
 * The page realizes the B+C merged design: a member list on the left, the
 * selected member's pinned DM thread in the center (the real chat stack,
 * hosted the way split-view panes host it), and a toggleable read-only
 * detail drawer on the right. Configuration WRITES are deliberately absent —
 * both Edit affordances navigate to the existing crew manager
 * (/capabilities?tab=crews), so this page never becomes a second editor.
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
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft, Circle, Clock, ExternalLink, Pencil, UserPlus, Users, Webhook } from 'lucide-react'
import { PanelRightSolid } from '../../components/icons/panels'
import { useTranslation } from 'react-i18next'
import { api, type MemberActivityEntry, type MemberRosterRow, type WebhookTokenEntry } from '../../api/client'
import type { CronJob } from '../../types'
import { wakesCrew, webhookBoundToCrew } from '../../components/crew/wakesCrew'
import { timeAgo } from '../../utils/timeAgo'
import { useAppDispatch, useAppSelector } from '../../store'
import { markSlotRead } from '../../store/dashboardSlice'
import CrewAvatar from '../../components/CrewAvatar'
import ChatPane from '../../components/ChatPane'
import DetailPanel from '../../components/DetailPanel'
import ErrorBoundary from '../../components/ErrorBoundary'
import { useIsMobile } from '../../hooks/useIsMobile'
import { SearchInput } from '../../components/ui'
import { AnimatePresence, motion } from 'framer-motion'
import { sidePanelDockMotion } from '../chat/sidePanelMount'
import { CHAT_PANE_MIN_W } from '../chat/SidePanel'
import ResizeHandle from '../../components/ResizeHandle'
import { useColumnResize } from '../../hooks/useColumnResize'
import { loadColumnWidth } from '../../lib/columnWidth'
import { compareText } from '../../i18n/format'
import { tabStatus, type TabStatus } from '../../lib/sessionTabs'
import { lastActivityEpoch } from '../chat/sessionOrder'

/** The crew manager surface — the ONLY write path for member configuration.
 *  The explicit tab wins over CapabilitiesPage's remembered last tab. */
const CREW_MANAGER_PATH = '/capabilities?tab=crews'

/** Roster width bounds, persisted like the chat sidebar's (mc-sidebar-width). */
const ROSTER_MIN = 200
const ROSTER_MAX = 420
const ROSTER_DEFAULT = 264
const ROSTER_WIDTH_KEY = 'mc-members-roster-width'
/** Detail drawer width bounds. The default matches the pre-DetailPanel fixed
 *  300px so the migration changes capability (drag-to-resize), not the resting
 *  look. Width persists under its own key, independent of the roster's. */
const DRAWER_MIN = 240
const DRAWER_DEFAULT = 300
const DRAWER_WIDTH_KEY = 'mc-members-drawer-width'
/** Space DetailPanel must keep clear for its left-side siblings when dragged
 *  wide: the live roster width is added at the call site. The thread minimum
 *  is chat's own CHAT_PANE_MIN_W (the members thread IS a ChatPane), plus this
 *  page's three inter-column gap-2s (24px), so one constant owns the
 *  usable-pane floor and a future change there carries over. */
const THREAD_MIN_RESERVE = CHAT_PANE_MIN_W + 24
/** Punctuation, not prose: joins an activity label to its project name, and a
 *  driving row's title to its status word in the hover title. */
const PROJECT_SEPARATOR = ' \u00b7 '
/** Driving-sessions rows shown before the list folds behind "Show all". */
const DRIVING_VISIBLE = 5
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
// Module-level so the resize hook's memoised resolver isn't invalidated every render.
const loadRosterWidth = () => loadColumnWidth(ROSTER_WIDTH_KEY, ROSTER_MIN, ROSTER_MAX, ROSTER_DEFAULT)

export default function MembersPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const [members, setMembers] = useState<MemberRosterRow[]>([])
  const [loaded, setLoaded] = useState(false)
  const [loadError, setLoadError] = useState(false)
  // Identity is the exact crew name (unique in the registry); the slug is not.
  const [activeName, setActiveName] = useState<string>('')
  // name -> slot key / name -> error, filled ONLY by the thread endpoint's
  // response. The roster's `bound`/`slot_key` are never trusted as mountable:
  // dm.json outlives the live slot (a restart drops an unmessaged slot while
  // the binding survives), and mounting an unconfirmed key would let the
  // first message auto-create an ordinary UNPINNED slot on the member key.
  // POST /api/members/{slug}/thread is idempotent and is the only creator/
  // repairer of member slots — so every open goes through it. Keying results
  // by the member they were requested FOR makes a late completion of a
  // previously selected member harmless.
  const [slots, setSlots] = useState<Record<string, string>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  // Roster width is user-adjustable on md+ (drag handle on the right edge),
  // mirroring the chat sidebar. Below md the roster is full-width single-pane
  // and the stored width is simply unused. Clamp + persist live in the shared
  // useColumnResize hook — the same primitive every resizable column uses.
  const roster = useColumnResize(ROSTER_WIDTH_KEY, loadRosterWidth, ROSTER_MIN, ROSTER_MAX)
  // Open by default only where the 300px rail has room; on narrow viewports
  // the drawer overlays the thread, so it must start closed. Initializer-only
  // (no resize listener): matching the width at mount is enough — the toggle
  // is one tap away and chasing live resizes would fight the user's choice.
  const [drawerOpen, setDrawerOpen] = useState(
    () => typeof window !== 'undefined' && window.matchMedia('(min-width: 768px)').matches,
  )
  // Live presence rides the already-subscribed WS `slots` frames — the roster
  // endpoint only fills the cold-start gap (its `running` is a snapshot).
  const liveSlots = useAppSelector((s) => s.dashboard.slots)
  // Whether a real slots snapshot has arrived. Before it, an empty `slots` is
  // ambiguous (the store itself refuses to treat a pre-first-frame empty frame
  // as authoritative), so the driving-sessions block must not assert "not
  // driving" on a cold open or a WS reconnect — it shows a skeleton instead,
  // the same three-state discipline the Recent-activity section keeps.
  const slotsLoaded = useAppSelector((s) => s.dashboard.slotsLoaded)
  const liveRunning = useMemo(() => {
    const byKey: Record<string, boolean> = {}
    for (const s of liveSlots) if (s.mode === 'member') byKey[s.key] = !!s.running
    return byKey
  }, [liveSlots])
  const isRunning = useCallback(
    (m: MemberRosterRow) => {
      const key = slots[m.name] || m.slot_key
      return key && key in liveRunning ? liveRunning[key] : m.running
    },
    [slots, liveRunning],
  )

  useEffect(() => {
    let alive = true
    api
      .members()
      .then((r) => {
        if (!alive) return
        setMembers(r.members)
        setLoaded(true)
      })
      .catch(() => {
        if (!alive) return
        setLoadError(true)
        setLoaded(true)
      })
    return () => {
      alive = false
    }
  }, [])

  const active = useMemo(
    () => members.find((m) => m.name === activeName),
    [members, activeName],
  )
  // Most-recently-active first (like any IM member list); never-talked
  // members fall to the bottom alphabetically. Sorted once from the roster
  // snapshot — live re-sorting mid-session would move rows under the cursor.
  const [filter, setFilter] = useState('')
  // The chat side panel's right-dock mount preset — module-pure, so one
  // constant serves every render.
  const drawerMotion = sidePanelDockMotion('right')
  // Drives the drawer's two shells: fixed overlay + dock motion below md,
  // DetailPanel's own docked width animation on md+ (same breakpoint as the
  // width-gated drawerOpen initializer above).
  const isMobile = useIsMobile()
  const sortedMembers = useMemo(() => {
    const ordered = [...members].sort(
      (a, b) =>
        (b.last_active_ts ?? 0) - (a.last_active_ts ?? 0) || compareText(a.name, b.name),
    )
    const q = filter.trim().toLowerCase()
    return q ? ordered.filter((m) => m.name.toLowerCase().includes(q)) : ordered
  }, [members, filter])
  const activeSlot = active ? slots[active.name] ?? '' : ''
  const activeError = active ? errors[active.name] ?? '' : ''

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
  const drivingSessions = useMemo(() => {
    if (!activeMemberKey) return []
    return liveSlots
      .filter((s) => !!s.created_by && s.created_by === activeMemberKey)
      .sort((a, b) => lastActivityEpoch(b) - lastActivityEpoch(a))
  }, [liveSlots, activeMemberKey])
  // Collapsed past DRIVING_VISIBLE rows. Keyed to the member: the fold is a
  // reading position in ONE member's list, so switching members starts the
  // next list folded rather than inheriting the previous member's expansion.
  const [drivingExpandedFor, setDrivingExpandedFor] = useState('')
  const drivingExpanded = drivingExpandedFor === activeMemberKey
  const visibleDriving = drivingExpanded ? drivingSessions : drivingSessions.slice(0, DRIVING_VISIBLE)

  // Recent-activity pointers for the drawer, fetched when it opens for a
  // member and cached for the page's lifetime. Keyed by the exact member
  // NAME, not the slug — slugs are lossy, and the whole point of the
  // backend's member filter is that two names sharing a slug have distinct
  // histories. Real recorded signal only — the drawer derives its counts
  // from these instead of fabricating stats. Three states per member:
  // absent = still loading, 'error' = fetch failed, object = loaded.
  // A pending or failed read must not render the affirmative "no activity".
  const [activity, setActivity] = useState<
    Record<string, { entries: MemberActivityEntry[]; capped: boolean } | 'error'>
  >({})
  const activeSlug = active?.slug ?? ''
  const activeMemberName = active?.name ?? ''
  useEffect(() => {
    if (!activeSlug || !activeMemberName || !drawerOpen) return
    let cancelled = false
    api
      .memberActivity(activeSlug, activeMemberName)
      .then((r) => {
        if (!cancelled)
          setActivity((prev) => ({
            ...prev,
            [activeMemberName]: { entries: r.entries, capped: !!r.capped },
          }))
      })
      .catch(() => {
        if (!cancelled) setActivity((prev) => ({ ...prev, [activeMemberName]: 'error' }))
      })
    return () => {
      cancelled = true
    }
  }, [activeSlug, activeMemberName, drawerOpen])
  const activityState = activeMemberName ? activity[activeMemberName] : undefined
  const activityLoading = activityState === undefined
  const activityError = activityState === 'error'
  const activeEntries = useMemo(
    () => (typeof activityState === 'object' ? activityState.entries : []),
    [activityState],
  )
  const activityCapped = typeof activityState === 'object' && activityState.capped

  // Wake sources — global lists (crons, webhook tokens, the default crew),
  // fetched ONCE on the first drawer open and filtered per member at render.
  // `failed` is kept distinct from empty: absence of an answer and an answer
  // of "none" must not render the same (a failed fetch would otherwise show
  // the affirmative "nothing wakes this member", a false statement).
  const [wake, setWake] = useState<{
    loaded: boolean
    failed: boolean
    jobs: CronJob[]
    tokens: WebhookTokenEntry[]
    defaultAgent: string
  }>({ loaded: false, failed: false, jobs: [], tokens: [], defaultAgent: '' })
  useEffect(() => {
    if (!drawerOpen || wake.loaded || wake.failed) return
    let cancelled = false
    Promise.all([api.crons(), api.webhooks(), api.kirocrewAgents()])
      .then(([crons, hooks, agents]) => {
        if (cancelled) return
        setWake({
          loaded: true,
          failed: false,
          jobs: crons?.jobs || [],
          tokens: hooks?.tokens || [],
          defaultAgent: agents?.default_agent || '',
        })
      })
      .catch(() => {
        if (!cancelled) setWake((prev) => ({ ...prev, failed: true }))
      })
    return () => {
      cancelled = true
    }
  }, [drawerOpen, wake.loaded, wake.failed])
  const wakeJobs = useMemo(
    () =>
      active
        ? wake.jobs.filter((j) => wakesCrew(j, active.name, active.name === wake.defaultAgent))
        : [],
    [active, wake.jobs, wake.defaultAgent],
  )
  const wakeHooks = useMemo(
    () => (active ? wake.tokens.filter((t) => webhookBoundToCrew(t, active.name)) : []),
    [active, wake.tokens],
  )
  const { todayCount, weekCount, todayFloorTs, weekFloorTs } = useMemo(() => {
    const midnight = new Date()
    midnight.setHours(0, 0, 0, 0)
    const todayFloor = midnight.getTime() / 1000
    const weekFloor = Date.now() / 1000 - 7 * 86400
    let today = 0
    let week = 0
    for (const e of activeEntries) {
      if (e.ts >= todayFloor) today += 1
      if (e.ts >= weekFloor) week += 1
    }
    return { todayCount: today, weekCount: week, todayFloorTs: todayFloor, weekFloorTs: weekFloor }
  }, [activeEntries])
  // When the display window is saturated (server capped the entries) AND the
  // oldest returned entry still falls inside a counting window, more in-window
  // events exist beyond the cap — the count is a floor, rendered as "N+"
  // rather than asserted as exact.
  const oldestTs = activeEntries.length ? activeEntries[activeEntries.length - 1].ts : 0
  const todayIsFloor = activityCapped && oldestTs >= todayFloorTs
  const weekIsFloor = activityCapped && oldestTs >= weekFloorTs

  // Mounting a member thread IS reading it, but nothing on this page moves
  // `chat.activeSlot` (that transition belongs to the Sessions page's
  // switchSlot, the only other markSlotRead caller), so the websocket
  // unread-marker keeps flagging this slot even while the user is looking at
  // it. Drain it here instead: once when the thread opens, and again every
  // time a live message re-flags the mounted thread. Without this the rail
  // badge is permanent — no code path clears a live member slot's unread
  // until the slot itself is deleted.
  const dispatch = useAppDispatch()
  const activeSlotUnread = useAppSelector(
    (s) => !!activeSlot && s.dashboard.unreadSlots.includes(activeSlot),
  )
  useEffect(() => {
    if (activeSlot && activeSlotUnread) dispatch(markSlotRead(activeSlot))
  }, [activeSlot, activeSlotUnread, dispatch])

  // Per-row unread marker: the rail badge says "1", this says WHICH member.
  // Keyed the same way isRunning resolves a member's slot (thread-endpoint
  // cache first, roster binding as the cold-start fallback), and read straight
  // from unreadSlots so the drain effect above clears the dot the moment the
  // thread is opened.
  const unreadSlots = useAppSelector((s) => s.dashboard.unreadSlots)
  const isUnread = useCallback(
    (m: MemberRosterRow) => {
      const key = slots[m.name] || m.slot_key
      return !!key && unreadSlots.includes(key)
    },
    [slots, unreadSlots],
  )

  const openMember = useCallback(
    (m: MemberRosterRow) => {
      setActiveName(m.name)
      setErrors((prev) => {
        if (!(m.name in prev)) return prev
        const next = { ...prev }
        delete next[m.name]
        return next
      })
      // ALWAYS post, even when a slot key is already cached: the endpoint is
      // the idempotent creator/repairer, and the backend can lose the live
      // slot between opens (archive, restart with a stale binding) — a cached
      // key mounted without the POST would point at nothing. The cache only
      // decides what to render while the POST is in flight.
      api
        .memberThread(m.slug)
        .then((r) => {
          if (r.member !== m.name) {
            // The slug's thread belongs to another crew (lossy-slug collision,
            // first-bound-wins). Mounting it would be a silent misroute — the
            // defining failure for a page whose premise is identity.
            setSlots((prev) => {
              if (!(m.name in prev)) return prev
              const next = { ...prev }
              delete next[m.name]
              return next
            })
            setErrors((prev) => ({
              ...prev,
              [m.name]: t('pages.membersPage.slug_collision', { name: r.member }),
            }))
            return
          }
          setSlots((prev) => ({ ...prev, [m.name]: r.slot_key }))
        })
        .catch(() =>
          setErrors((prev) => ({
            ...prev,
            [m.name]: t('pages.membersPage.thread_open_failed'),
          })),
        )
    },
    [t],
  )

  return (
    // pb-2 on the root is the one shared bottom inset: the card columns and
    // the detail drawer all end 8px above the window edge (the chat SidePanel's
    // mb-2). No right padding here — the drawer docks FLUSH to the window's
    // right edge; the card columns' pr-2 lives on the inner wrapper below. The
    // drawer must stay a DIRECT child of this row: DetailPanel measures its
    // parent to cap the drag width against roster + thread.
    <div className="flex h-full min-h-0 pb-2" data-testid="members-page">
      {/* Card columns (roster + thread) keep the page's original insets. */}
      <div className="flex flex-1 min-w-0 gap-2 pr-2">
      {/* Member list. Below md the page is single-pane: the roster IS the
          page until a member is picked, then the thread takes over and the
          header's back button returns here. Two fixed rails (264+300px)
          otherwise crush the flex-1 thread to zero at narrow widths.
          Carded like the Sessions page's chat list (ChatSidebar) so the two
          conversation surfaces read as one family. */}
      <aside
        className={`${
          activeName ? 'hidden md:flex' : 'flex'
        } relative w-full md:w-[var(--roster-w)] shrink-0 bg-bg-elevated border border-border rounded-xl shadow-sm flex-col min-h-0`}
        // CSS owns the breakpoint: the var is set unconditionally and only the
        // md: class consumes it, so resizing the window across 768px reacts
        // without any JS media-query snapshot going stale.
        style={{ '--roster-w': `${roster.width}px` } as React.CSSProperties}
        data-testid="member-roster"
      >
        <div className="px-4 pt-4 pb-1 flex items-center gap-2">
          <Users size={15} className="lucide-inline text-muted" />
          <h1 className="text-sm font-semibold flex-1">{t('pages.membersPage.title')}</h1>
          {/* Adding a member IS creating a crew, and the crew manager is the
              only write path — so this is a navigation, not an inline form. */}
          <button
            onClick={() => navigate(CREW_MANAGER_PATH)}
            className="flex items-center justify-center w-7 h-7 rounded-md transition-colors bg-transparent border-none shrink-0 text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
            aria-label={t('pages.membersPage.add_member')}
            title={t('pages.membersPage.add_member')}
            data-testid="member-add"
          >
            <UserPlus size={15} />
          </button>
        </div>
        <div className="px-4 pb-2 text-[11px] text-muted">
          {t('pages.membersPage.member_count', { count: members.length })}
        </div>
        {/* Same search idiom as the Sessions sidebar. */}
        <div className="px-2 pb-1">
          <SearchInput
            className="w-full"
            placeholder={t('pages.membersPage.search_members')}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            data-testid="member-search"
          />
        </div>
        <ul
          className="flex-1 overflow-y-auto scrollbar-none list-none m-0 px-2 pb-2"
          style={{ scrollbarWidth: 'none' }}
          aria-label={t('pages.membersPage.title')}
        >
          {loaded && !loadError && members.length === 0 && (
            <li className="px-4 py-6 text-xs text-muted">
              <p>{t('pages.membersPage.empty_roster')}</p>
              {/* The copy names the crew manager; give it the way there. */}
              <button
                onClick={() => navigate(CREW_MANAGER_PATH)}
                className="mt-2 inline-flex items-center gap-1 text-[11.5px] px-2 py-1 rounded border border-border hover:bg-accent/40"
                data-testid="member-empty-cta"
              >
                <Pencil size={12} className="lucide-inline" />
                {t('pages.membersPage.edit_in_crew_manager')}
              </button>
            </li>
          )}
          {loadError && (
            <li className="px-4 py-6 text-xs text-muted" role="alert">
              {t('pages.membersPage.roster_load_failed')}
            </li>
          )}
          {sortedMembers.map((m) => (
            <li key={m.name}>
              {/* Same rounded-row idiom as ChatSidebar's session rows, so the
                  two conversation lists read as one family. */}
              <button
                onClick={() => openMember(m)}
                className={`w-full flex items-center gap-2.5 pl-2.5 pr-2 py-2 rounded-md text-left transition-all select-none ${
                  m.name === activeName
                    ? 'text-text-strong bg-accent-subtle'
                    : 'text-muted hover:text-text hover:bg-bg-hover'
                }`}
                aria-current={m.name === activeName ? 'true' : undefined}
              >
                <span className="relative shrink-0">
                  <CrewAvatar
                    seed={m.name}
                    avatar={m.avatar}
                    size={36}
                    working={isRunning(m) ? 'subtle' : undefined}
                  />
                  {/* Presence dot renders only while the member is working —
                      an idle member shows nothing rather than a gray dot,
                      which read as a broken/disabled state. */}
                  {isRunning(m) && (
                    <span
                      className="absolute -right-0.5 -bottom-0.5 w-2.5 h-2.5 rounded-full border-2 border-bg bg-ok"
                      aria-hidden="true"
                      data-testid="member-presence-dot"
                    />
                  )}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block text-[13px] font-medium truncate">{m.name}</span>
                  {/* Last-message preview, like a session row — presence
                      already rides the avatar dot, so a textual Idle/Working
                      label said nothing the dot did not. */}
                  <span className="block text-[11px] text-muted truncate">
                    {m.last_message || '\u00a0'}
                  </span>
                </span>
                {/* Unread marker on the row's right edge — the IM convention
                    (and where the rail badge sits), vertically centered by the
                    row's items-center. Accent-filled w-2 h-2 like ChatSidebar's
                    unread dot, with a real accessible name: nothing else on
                    the row says "unread". The left side is taken — presence
                    rides the avatar. */}
                {isUnread(m) && (
                  <span
                    className="w-2 h-2 rounded-full shrink-0"
                    style={{ background: 'var(--accent)' }}
                    role="img"
                    aria-label={t('pages.membersPage.unread_message')}
                    title={t('pages.membersPage.unread_message')}
                    data-testid="member-unread-dot"
                  />
                )}
              </button>
            </li>
          ))}
        </ul>
      </aside>

      {/* Shared window-splitter between roster and thread: keyboard-operable,
          md+ only (below md the page is single-pane, nothing to resize). */}
      <div className="hidden md:flex" data-testid="member-roster-resize">
        <ResizeHandle
          handleProps={roster.handleProps}
          label={t('pages.membersPage.title')}
          onNudge={roster.nudge}
          value={roster.width}
          min={ROSTER_MIN}
          max={ROSTER_MAX}
        />
      </div>

      {/* DM thread */}
      <section
        className={`${activeName ? 'flex' : 'hidden md:flex'} flex-1 min-w-0 flex-col min-h-0`}
      >
        {!active && (
          <div className="flex-1 flex items-center justify-center text-sm text-muted px-6 text-center">
            {t('pages.membersPage.pick_a_member')}
          </div>
        )}
        {active && (
          <>
            <header className="flex items-center gap-2.5 px-4 py-2 border-b border-border">
              <button
                onClick={() => setActiveName('')}
                className="md:hidden inline-flex items-center p-1 -ml-1 rounded hover:bg-accent/40"
                aria-label={t('pages.membersPage.title')}
                data-testid="member-back"
              >
                <ArrowLeft size={16} className="lucide-inline" />
              </button>
              <CrewAvatar
                seed={active.name}
                avatar={active.avatar}
                size={30}
                working={isRunning(active) ? 'full' : undefined}
              />
              <div className="min-w-0 flex-1">
                <div className="text-[13.5px] font-semibold truncate">{active.name}</div>
              </div>
              {/* One action in the header: toggle the detail drawer — same
                  icon and hit-target as the chat page's side-panel toggle, so
                  the two surfaces teach one gesture. The pin chip was removed:
                  every member thread is pinned by construction (a server
                  invariant, not a per-thread state), so announcing it taught
                  the user a term for a thing that can never be otherwise.
                  Edit lives inside the drawer: it is a rare, secondary
                  action, not a header-level peer of the drawer toggle. */}
              <button
                onClick={() => setDrawerOpen((v) => !v)}
                className="flex items-center justify-center w-7 h-7 rounded-md transition-colors bg-transparent border-none shrink-0 text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
                aria-pressed={drawerOpen}
                aria-controls="member-drawer"
                aria-label={t('pages.membersPage.details')}
                title={t('pages.membersPage.details')}
                data-testid="member-drawer-toggle"
              >
                <PanelRightSolid size={15} />
              </button>
            </header>
            {activeError && (
              <div className="px-4 py-2 text-xs text-danger" role="alert">
                {activeError}
              </div>
            )}
            {activeSlot ? (
              <div className="flex-1 min-h-0">
                <ErrorBoundary>
                  {/* Same reading measure as the main chat transcript — the
                      pane resolves the user's Content width setting itself
                      (transcript and composer both). The DM column is the
                      page's widest region, and an uncapped line length is
                      unreadable on wide screens. */}
                  <ChatPane slotKey={activeSlot} agentLocked frameless followContentWidth />
                </ErrorBoundary>
              </div>
            ) : (
              !activeError && (
                <div className="flex-1 flex items-center justify-center text-xs text-muted">
                  {t('pages.membersPage.opening_thread')}
                </div>
              )
            )}
          </>
        )}
      </section>
      </div>

      {/* Detail drawer — read-only observation; writes live in the crew manager.
          The shell is the shared DetailPanel wearing the chat SidePanel's
          frame: a left-rounded card docked FLUSH to the window's right edge,
          top/bottom/left borders, an 8px bottom inset, and an elevated header
          band — so the two right panels read as one family. Same
          header idiom (close + identity + title), same body padding, and the
          same drag-to-resize handle with a persisted width. On md+
          DetailPanel's own width spring is the one mount animation. Below md
          the drawer overlays the thread instead of claiming row width (and
          starts closed there — the width-gated useState above); that branch
          keeps the side-panel dock motion on a fixed-position wrapper, where
          drag-resize is moot because the overlay spans a fixed 300px. */}
      <AnimatePresence>
        {active && drawerOpen && (() => {
          const body = (
            /* Keeps the old aside's id: the roster header's Details toggle
               points here via aria-controls. */
            <div id="member-drawer" data-testid="member-drawer" aria-label={t('pages.membersPage.details')}>
          {/* Live status line — working now, or the last time anything
              happened on the thread. Identity (avatar + name) moved into the
              DetailPanel header, so the body opens with the status alone. */}
          <div className="text-[11px] truncate mb-3" data-testid="member-drawer-status">
            {isRunning(active) ? (
              <span className="text-ok">{t('pages.membersPage.drawer_working')}</span>
            ) : active.last_active_ts ? (
              <span className="text-muted">{timeAgo(active.last_active_ts)}</span>
            ) : (
              <span className="text-muted">{'\u00a0'}</span>
            )}
          </div>
          {/* Honest counters only — both derive from the recorded activity
              log. Semantic stats the backend cannot attest (PRs, triages,
              spend) are deliberately absent rather than fabricated. */}
          <div className="grid grid-cols-2 gap-2 mb-4" data-testid="member-stats">
            <div className="border border-border rounded-lg px-3 py-2">
              <div className="text-lg font-semibold leading-tight">
                {activityLoading || activityError ? '\u2013' : `${todayCount}${todayIsFloor ? '+' : ''}`}
              </div>
              <div className="text-[11px] text-muted">{t('pages.membersPage.stat_today')}</div>
            </div>
            <div className="border border-border rounded-lg px-3 py-2">
              <div className="text-lg font-semibold leading-tight">
                {activityLoading || activityError ? '\u2013' : `${weekCount}${weekIsFloor ? '+' : ''}`}
              </div>
              <div className="text-[11px] text-muted">{t('pages.membersPage.stat_week')}</div>
            </div>
          </div>
          {/* Sessions this member is driving — the worker sessions it opened
              and steers. Live rows off the WS slots frames (see the
              drivingSessions memo); each row is a jump into that session.
              The status dot is the sidebar's vocabulary: approval (warn) >
              needs input (info) > running (ok) > idle (muted). */}
          <div className="text-[11px] font-semibold tracking-wide text-muted mb-1.5">
            {t('pages.membersPage.driving_sessions')}
          </div>
          {drivingSessions.length === 0 && !slotsLoaded ? (
            <div className="mb-4 space-y-1.5" data-testid="member-driving-loading" aria-hidden>
              <div className="h-3 rounded bg-accent/40 animate-pulse" />
              <div className="h-3 w-3/4 rounded bg-accent/40 animate-pulse" />
            </div>
          ) : drivingSessions.length === 0 ? (
            <div className="text-[11px] text-muted mb-4" data-testid="member-driving-empty">
              {t('pages.membersPage.driving_none')}
            </div>
          ) : (
            <div className="mb-4">
              <ul className="list-none m-0 p-0 space-y-0.5" data-testid="member-driving-sessions">
                {visibleDriving.map((s) => {
                  // Precedence is the shared tab-status contract (approval and
                  // question outrank running); no unread set here, so the
                  // fourth state is plain idle.
                  const kind = tabStatus(s, [], s.key)
                  const status = DRIVING_STATUS[kind]
                  const label = t(status.label)
                  // Slot timestamps are ISO strings; timeAgo wants epoch seconds.
                  const activityTs = lastActivityEpoch(s)
                  const title = s.title || s.key
                  return (
                    <li key={s.key}>
                      <button
                        type="button"
                        onClick={() => navigate(`/chat?sid=${encodeURIComponent(s.key)}`)}
                        className="w-full text-left flex items-center gap-2 text-[11px] px-1.5 py-1 -mx-1.5 rounded hover:bg-accent/40"
                        title={title + PROJECT_SEPARATOR + label}
                        data-testid="member-driving-row"
                        data-status={kind}
                      >
                        <Circle size={8} className={`shrink-0 ${status.cls}`} aria-hidden />
                        <span className="min-w-0 truncate flex-1">{title}</span>
                        {/* The two states parked on the user get words, not
                            just a colour — the sidebar's own idiom for the
                            same signals; running/idle stay dot-only (the
                            label is in the hover title and for AT). */}
                        {status.spoken ? (
                          <span className={`shrink-0 font-medium ${status.text}`}>{label}</span>
                        ) : (
                          <span className="sr-only">{label}</span>
                        )}
                        {activityTs > 0 && (
                          <span className="text-muted shrink-0 whitespace-nowrap">{timeAgo(activityTs)}</span>
                        )}
                      </button>
                    </li>
                  )
                })}
              </ul>
              {drivingSessions.length > DRIVING_VISIBLE && (
                <button
                  type="button"
                  onClick={() => setDrivingExpandedFor(drivingExpanded ? '' : activeMemberKey)}
                  className="mt-1 text-[11px] text-muted hover:text-text"
                  aria-expanded={drivingExpanded}
                  data-testid="member-driving-toggle"
                >
                  {drivingExpanded
                    ? t('pages.membersPage.driving_show_less')
                    : t('pages.membersPage.driving_show_all', { count: drivingSessions.length })}
                </button>
              )}
            </div>
          )}
          <div className="text-[11px] font-semibold tracking-wide text-muted mb-1.5">
            {t('pages.membersPage.recent_activity')}
          </div>
          {/* Three states, never conflated: a pending or failed read must not
              render the affirmative "no recorded activity". */}
          {activityLoading ? (
            <div className="mb-4 space-y-1.5" data-testid="member-activity-loading" aria-hidden>
              <div className="h-3 rounded bg-accent/40 animate-pulse" />
              <div className="h-3 w-3/4 rounded bg-accent/40 animate-pulse" />
            </div>
          ) : activityError ? (
            <div className="text-[11px] text-muted mb-4" role="alert" data-testid="member-activity-error">
              {t('pages.membersPage.activity_error')}
            </div>
          ) : activeEntries.length === 0 ? (
            <div className="text-[11px] text-muted mb-4">
              {t('pages.membersPage.activity_empty')}
            </div>
          ) : (
            <ul className="list-none m-0 p-0 mb-4 space-y-1.5" data-testid="member-activity">
              {activeEntries.slice(0, 8).map((e, i) => (
                <li
                  key={`${e.ts}-${i}`}
                  className="flex gap-2 text-[11px] border-b border-border/60 pb-1.5 last:border-b-0"
                >
                  <span className="text-muted shrink-0 whitespace-nowrap">{timeAgo(e.ts)}</span>
                  <span className="min-w-0 truncate">
                    {e.via === 'select_crew'
                      ? t('pages.membersPage.activity_routed')
                      : t('pages.membersPage.activity_chat')}
                    {e.project ? PROJECT_SEPARATOR + e.project : ''}
                  </span>
                </li>
              ))}
            </ul>
          )}
          <div className="text-[11px] font-semibold tracking-wide text-muted mb-1.5 flex items-center">
            <span className="flex-1">{t('pages.membersPage.wake_sources')}</span>
            {/* Read-only view; managing schedules stays on the Schedule page
                (same jump idiom as the crew editor's wake pane). */}
            <button
              onClick={() => navigate('/schedule')}
              className="inline-flex items-center p-0.5 rounded hover:bg-accent/40 text-muted hover:text-text"
              aria-label={t('pages.membersPage.open_schedule')}
              title={t('pages.membersPage.open_schedule')}
              data-testid="member-wake-jump"
            >
              <ExternalLink size={12} className="lucide-inline" />
            </button>
          </div>
          {!wake.loaded && !wake.failed ? (
            <div className="mb-4 space-y-1.5" data-testid="member-wake-loading" aria-hidden>
              <div className="h-3 rounded bg-accent/40 animate-pulse" />
            </div>
          ) : wake.failed ? (
            <div className="text-[11px] text-muted mb-4" role="alert" data-testid="member-wake-error">
              {t('pages.membersPage.wake_error')}
            </div>
          ) : wakeJobs.length === 0 && wakeHooks.length === 0 ? (
            <div className="text-[11px] text-muted mb-4">{t('pages.membersPage.wake_none')}</div>
          ) : (
            <ul className="list-none m-0 p-0 mb-4 space-y-1.5" data-testid="member-wake-sources">
              {wakeJobs.map((jb) => (
                <li key={jb.id} className="flex items-center gap-2 text-[11px]">
                  <Clock size={12} className="lucide-inline text-muted shrink-0" />
                  <span className={`min-w-0 truncate flex-1 ${jb.enabled ? '' : 'text-muted'}`}>
                    {jb.name}
                    {!jb.enabled && ` (${t('pages.membersPage.wake_paused')})`}
                  </span>
                  <span className="font-mono text-muted shrink-0 max-w-[45%] truncate" title={jb.schedule}>
                    {jb.schedule}
                  </span>
                </li>
              ))}
              {wakeHooks.map((tk) => (
                <li key={tk.id} className="flex items-center gap-2 text-[11px]">
                  <Webhook size={12} className="lucide-inline text-muted shrink-0" />
                  <span className={`min-w-0 truncate flex-1 ${tk.enabled === false ? 'text-muted' : ''}`}>
                    {tk.label}
                    {tk.enabled === false && ` (${t('pages.membersPage.wake_paused')})`}
                  </span>
                  <span className="text-muted shrink-0">{t('pages.membersPage.wake_webhook')}</span>
                </li>
              ))}
            </ul>
          )}
          <div className="text-[11px] font-semibold tracking-wide text-muted mb-2">
            {t('pages.membersPage.configuration')}
          </div>
          <dl className="text-xs space-y-2">
            <div className="flex gap-2">
              <dt className="w-24 shrink-0 text-muted">
                {t('pages.membersPage.agent_template')}
              </dt>
              <dd className="min-w-0 truncate">{active.kiro_agent || t('pages.membersPage.inherited')}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="w-24 shrink-0 text-muted">{t('pages.membersPage.model')}</dt>
              <dd className="min-w-0 truncate">{active.model || t('pages.membersPage.inherited')}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="w-24 shrink-0 text-muted">
                {t('pages.membersPage.workspace')}
              </dt>
              <dd className="min-w-0 truncate">{String(active.workspace ?? '')}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="w-24 shrink-0 text-muted">
                {t('pages.membersPage.memory_store')}
              </dt>
              <dd className="min-w-0 truncate">{String(active.memory_store ?? '')}</dd>
            </div>
          </dl>
          {/* Honest disclosure: per-member memory isolation does not exist yet. */}
          <div className="mt-3 text-[11px] text-muted border border-border rounded-md px-2.5 py-2">
            {t('pages.membersPage.memory_shared_note')}
          </div>
          <button
            onClick={() => navigate(CREW_MANAGER_PATH)}
            className="mt-4 w-full inline-flex items-center justify-center gap-1.5 text-xs px-3 py-2 rounded-md border border-border hover:bg-accent/40"
          >
            <Pencil size={12} className="lucide-inline" />
            {t('pages.membersPage.edit_in_crew_manager')}
          </button>
            </div>
          )
          const panelProps = {
            icon: <CrewAvatar seed={active.name} avatar={active.avatar} size={22} />,
            title: active.name,
            onClose: () => setDrawerOpen(false),
            initialWidth: DRAWER_DEFAULT,
            minWidth: DRAWER_MIN,
            storageKey: DRAWER_WIDTH_KEY,
          }
          return isMobile ? (
            <motion.div
              key="member-drawer-motion"
              initial={drawerMotion.initial}
              animate={drawerMotion.animate}
              exit={drawerMotion.exit}
              transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
              className="fixed top-safe bottom-safe right-safe z-40 w-[300px] max-w-full bg-bg-elevated border-l border-border"
            >
              {/* `embedded`: fill the fixed wrapper AND drop the resize
                  handle — the overlay spans a fixed 300px, and a live handle
                  here would persist a mobile-clamped width over the user's
                  chosen desktop width. Edge chrome (left border, elevated bg)
                  lives on the wrapper above. */}
              <DetailPanel {...panelProps} embedded>
                {body}
              </DetailPanel>
            </motion.div>
          ) : (
            /* The frame is the chat SidePanel's exact recipe (SidePanel.tsx
               root + strip): left-rounded card with top/bottom/left borders,
               flush against the window's right edge, 8px bottom inset, and an
               elevated header band that carries the top-left corner; the 8px
               bottom inset comes from the page root's pb-2. reserveWidth keeps
               the live roster width plus a usable thread minimum clear, so
               dragging the panel wide can never collapse the DM thread to zero
               (same contract as ChatPage's panelReserve). */
            <DetailPanel
              key="member-drawer-panel"
              {...panelProps}
              reserveWidth={roster.width + THREAD_MIN_RESERVE}
              frameClassName="border-l border-t border-b border-border rounded-l-xl bg-bg"
              headerClassName="border-border bg-bg-elevated rounded-tl-xl"
            >
              {body}
            </DetailPanel>
          )
        })()}
      </AnimatePresence>
    </div>
  )
}
