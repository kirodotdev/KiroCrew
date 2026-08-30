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
import { ArrowLeft, Pencil, Users, X } from 'lucide-react'
import { PanelRightSolid } from '../../components/icons/panels'
import { useTranslation } from 'react-i18next'
import { api, type MemberRosterRow } from '../../api/client'
import { useAppSelector } from '../../store'
import CrewAvatar from '../../components/CrewAvatar'
import ChatPane from '../../components/ChatPane'
import ErrorBoundary from '../../components/ErrorBoundary'
import { SearchInput } from '../../components/ui'
import { AnimatePresence, motion } from 'framer-motion'
import { sidePanelDockMotion } from '../chat/sidePanelMount'
import ResizeHandle from '../../components/ResizeHandle'
import { useColumnResize } from '../../hooks/useColumnResize'
import { loadColumnWidth } from '../../lib/columnWidth'
import { compareText } from '../../i18n/format'

/** The crew manager surface — the ONLY write path for member configuration.
 *  The explicit tab wins over CapabilitiesPage's remembered last tab. */
const CREW_MANAGER_PATH = '/capabilities?tab=crews'

/** Roster width bounds, persisted like the chat sidebar's (mc-sidebar-width). */
const ROSTER_MIN = 200
const ROSTER_MAX = 420
const ROSTER_DEFAULT = 264
const ROSTER_WIDTH_KEY = 'mc-members-roster-width'
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
    <div className="flex h-full min-h-0 gap-2 p-2" data-testid="members-page">
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
                  <CrewAvatar seed={m.name} size={36} />
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
              <CrewAvatar seed={active.name} size={30} />
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
                  <ChatPane slotKey={activeSlot} agentLocked frameless />
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

      {/* Detail drawer — read-only observation; writes live in the crew manager.
          Below md it overlays the thread instead of claiming 300px of row
          width, and it starts closed there (the width-gated useState above).
          Mount/unmount reuses the chat page's side-panel motion preset
          (sidePanelDockMotion + the same 0.18s ease), so the two right panels
          open with one gesture AND one animation. On mobile the aside is
          position:fixed (out of flow), so the width tween is inert there and
          only the opacity fade applies — acceptable, not a defect. */}
      <AnimatePresence>
        {active && drawerOpen && (
          <motion.div
            key="member-drawer-motion"
            initial={drawerMotion.initial}
            animate={drawerMotion.animate}
            exit={drawerMotion.exit}
            transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
            className="h-full overflow-visible flex justify-end md:shrink-0"
          >
            <aside
              id="member-drawer"
              className="fixed top-safe bottom-safe right-safe z-40 w-[300px] max-w-full bg-bg-elevated border-l border-border p-4 overflow-y-auto md:static md:z-auto md:shrink-0 md:border md:rounded-xl md:shadow-sm"
              data-testid="member-drawer"
              aria-label={t('pages.membersPage.details')}
            >
          <div className="flex items-center mb-2">
            <div className="text-[11px] font-semibold tracking-wide text-muted flex-1">
              {t('pages.membersPage.configuration')}
            </div>
            {/* Drawer-local close, MOBILE ONLY: below md the overlay covers
                the header's toggle, so without this the drawer cannot be
                closed there. On md+ the header toggle is the one close
                gesture, same as the chat page's side panel. */}
            <button
              onClick={() => setDrawerOpen(false)}
              className="md:hidden inline-flex items-center p-1 -mr-1 rounded hover:bg-accent/40"
              aria-label={t('app.close')}
              data-testid="member-drawer-close"
            >
              <X size={14} className="lucide-inline" />
            </button>
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
        </aside>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
