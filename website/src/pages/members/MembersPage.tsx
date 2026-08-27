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
 * POST /api/members/{slug}/thread); the header chip merely SHOWS it.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft, Info, Pencil, Pin, Users, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api, type MemberRosterRow } from '../../api/client'
import { useAppSelector } from '../../store'
import CrewAvatar from '../../components/CrewAvatar'
import ChatPane from '../../components/ChatPane'
import ErrorBoundary from '../../components/ErrorBoundary'

/** The crew manager surface — the ONLY write path for member configuration.
 *  The explicit tab wins over CapabilitiesPage's remembered last tab. */
const CREW_MANAGER_PATH = '/capabilities?tab=crews'

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
        } w-full md:w-[264px] shrink-0 bg-bg-elevated border border-border rounded-xl shadow-sm flex-col min-h-0`}
      >
        <div className="px-4 pt-4 pb-1 flex items-center gap-2">
          <Users size={15} className="lucide-inline text-muted" />
          <h1 className="text-sm font-semibold flex-1">{t('pages.membersPage.title')}</h1>
        </div>
        <div className="px-4 pb-2 text-[11px] text-muted">
          {t('pages.membersPage.member_count', { count: members.length })}
        </div>
        <ul className="flex-1 overflow-y-auto list-none m-0 p-0" aria-label={t('pages.membersPage.title')}>
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
          {members.map((m) => (
            <li key={m.name}>
              <button
                onClick={() => openMember(m)}
                className={`w-full flex items-center gap-2.5 px-4 py-2.5 text-left hover:bg-accent/40 ${
                  m.name === activeName ? 'bg-accent/60' : ''
                }`}
                aria-current={m.name === activeName ? 'true' : undefined}
              >
                <span className="relative shrink-0">
                  <CrewAvatar seed={m.name} size={36} />
                  <span
                    className={`absolute -right-0.5 -bottom-0.5 w-2.5 h-2.5 rounded-full border-2 border-bg ${
                      isRunning(m) ? 'bg-ok' : 'bg-muted/50'
                    }`}
                    aria-hidden="true"
                  />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block text-[13px] font-medium truncate">{m.name}</span>
                  <span className="block text-[11px] text-muted truncate">
                    {isRunning(m)
                      ? t('pages.membersPage.status_working')
                      : t('pages.membersPage.status_idle')}
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      </aside>

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
                <div className="text-[11px] text-muted truncate">
                  {isRunning(active)
                    ? t('pages.membersPage.status_working')
                    : t('pages.membersPage.status_idle')}
                </div>
              </div>
              {/* The pin is a server-side property of member slots; this chip
                  only surfaces it. Hidden below sm — the drawer restates the
                  pin, and at phone widths the chip's text would wrap the whole
                  header into a three-line mess. */}
              <span
                className="ml-1 hidden sm:inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded bg-accent/60 text-accent-fg whitespace-nowrap shrink-0"
                data-testid="member-pin-chip"
              >
                <Pin size={11} className="lucide-inline" />
                {t('pages.membersPage.pinned_to', { name: active.name })}
              </span>
              <div className="flex items-center gap-1.5 shrink-0">
                <button
                  onClick={() => setDrawerOpen((v) => !v)}
                  className="inline-flex items-center gap-1 text-[11.5px] px-2 py-1 rounded border border-border hover:bg-accent/40 whitespace-nowrap"
                  aria-pressed={drawerOpen}
                  aria-controls="member-drawer"
                  aria-label={t('pages.membersPage.details')}
                >
                  <Info size={12} className="lucide-inline" />
                  {/* Icon-only below md: the label wraps at phone widths. */}
                  <span className="hidden md:inline">{t('pages.membersPage.details')}</span>
                </button>
                <button
                  onClick={() => navigate(CREW_MANAGER_PATH)}
                  // hidden below md: on narrow viewports the header row already
                  // carries Back + Details, and a third peer action can clip or
                  // wrap. Nothing is lost — the Details drawer exposes the same
                  // Edit jump, one tap away.
                  className="hidden md:inline-flex items-center gap-1 text-[11.5px] px-2 py-1 rounded border border-border hover:bg-accent/40 whitespace-nowrap"
                  data-testid="member-edit-jump"
                  aria-label={t('pages.membersPage.edit_in_crew_manager')}
                >
                  <Pencil size={12} className="lucide-inline" />
                  <span className="hidden md:inline">
                    {t('pages.membersPage.edit_in_crew_manager')}
                  </span>
                </button>
              </div>
            </header>
            {activeError && (
              <div className="px-4 py-2 text-xs text-danger" role="alert">
                {activeError}
              </div>
            )}
            {activeSlot ? (
              <div className="flex-1 min-h-0">
                <ErrorBoundary>
                  <ChatPane slotKey={activeSlot} agentLocked />
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
          width, and it starts closed there (the width-gated useState above). */}
      {active && drawerOpen && (
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
            {/* Drawer-local close: below md the overlay covers the header's
                Details toggle, so without this the drawer cannot be closed. */}
            <button
              onClick={() => setDrawerOpen(false)}
              className="inline-flex items-center p-1 -mr-1 rounded hover:bg-accent/40"
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
      )}
    </div>
  )
}
