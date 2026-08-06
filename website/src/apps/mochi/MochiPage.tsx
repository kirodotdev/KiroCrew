/**
 * MochiPage — the Mochi builtin's dashboard page (route /mochi).
 *
 * PORT of the original `src/dashboard/index.tsx`: same information architecture
 * (status stat row → Activity | Memories → Watchlist | Plan → offline landing)
 * and the same primitives, which happen to carry the same names and signatures
 * here as in the original's UI kit — so the markup is a near-verbatim carry-over
 * with only the import path changed.
 *
 * Three deliberate divergences, all forced by the builtin architecture:
 *
 * 1. DATA SOURCE. The original reached a separate Electron backend process
 *    through the gateway's reverse proxy and spoke MCP JSON-RPC to it
 *    (`/apps/mochi/api/mcp`, `tools/call`). Here the backend is in-process,
 *    so every panel reads a plain same-origin route (see backend/routes.py).
 *    The MCP shim still exists for the AGENT; the page must not be a second
 *    client of it.
 *
 * 2. "ONLINE" MEANS "ENABLED". The original probed whether its app process was
 *    reachable. There is no such process now — the runtime is up whenever the
 *    app is enabled — so the landing state keys off the enable flag, which the
 *    backend guard already reports distinctly (403 disabled / 503 starting).
 *    That also makes the landing's CTA real: the original's "Launch Mochi"
 *    POSTed an openCommand this manifest does not declare, whereas enabling is
 *    something the page can genuinely do.
 *
 * 3. ICONS, NOT EMOJI. The original used emoji for every decorative glyph. The
 *    repo convention is lucide components (see AGENTS.md), which EmptyState's
 *    `icon: React.ReactNode` accepts directly. The one emoji kept is the mood
 *    glyph, because that is DATA the state machine produces, not decoration.
 *
 * Panels the original page did not have — the add-watch form, pinned files, and
 * the avatar/instance controls — are kept below the ported grid rather than
 * dropped, since they are the only dashboard-side access to those features.
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Bot,
  Brain,
  CalendarClock,
  Camera,
  Cat,
  ChevronDown,
  ChevronRight,
  ClipboardList,
  Clock,
  Eye,
  Flame,
  Footprints,
  MessageSquare,
  MessagesSquare,
  Moon,
  MousePointer2,
  Pin,
  Plus,
  RefreshCw,
  RotateCcw,
  ScrollText,
  Smile,
  Sun,
  XCircle,
} from 'lucide-react'
import {
  Badge,
  Btn,
  Card,
  CardTitle,
  EmptyState,
  PageHeader,
  StatCard,
} from '../../components/ui'
import { isElectron } from '../../lib/electron'
import { addToWatchlist } from './mochiHelpers'
import type { WatchKind } from './api'
import {
  enableMochi,
  getActivity,
  getMochiVersion,
  getPetState,
  getPinned,
  getPlan,
  getSettings,
  getStats,
  getWatchlist,
  markPinnedSeen,
  probeEnabled,
  unpinFile,
  updateWatchlist,
  type CompanionStats,
} from './api'
import {
  formatActivity,
  formatPlanTasks,
  formatWatchItems,
  statusTone,
  str,
  type TimelineRow,
  type WatchRowView,
} from './dashboardData'
import { DEFAULT_PET_NAME } from './builtinPacks'
import { activityTypeLabel, moodLabel, stateLabel, watchKindLabel, watchStatusLabel } from './i18nKeys'
import {
  formatCompanionTime,
  formatThinkingTime,
  getTopMoods,
  shouldShowStat,
} from './src/shared/statsFormatters'
import { i18nT } from '../../i18n/t'
import { PRODUCT_NAME } from './src/shared/config'
import { fmtNumber, fmtPercent } from '../../i18n/format'
import Clickable from '../../components/Clickable'
import SimpleSelect from '../../components/SimpleSelect'

/**
 * The app's own name, as shown in the page header.
 *
 * A constant rather than a catalog key: a product name is a proper noun and has
 * no translation, so a key would only add ten identical catalog entries. Also
 * deliberately NOT `DEFAULT_PET_NAME` — the pet can be renamed by the user, the
 * product cannot, and conflating them would retitle this page when someone
 * renames their pet.
 */
// The SAME picker the first-run window uses — see AvatarTab for why it is reused
// rather than reimplemented as a settings-flavoured variant.

// Polling cadence, carried over from the original: slower once live, faster
// while waiting for the app to come up. react-query pauses these while the
// window is unfocused, which is what the original's visibilitychange handler
// was hand-rolling.
const POLL_LIVE_MS = 10_000
const POLL_WAITING_MS = 5_000

const PER_PAGE = 10

export default function MochiPage() {
  const qc = useQueryClient()
  const { data: status, isLoading } = useQuery({
    queryKey: ['mochi', 'enabled'],
    queryFn: probeEnabled,
    refetchInterval: (q) => (q.state.data === 'enabled' ? POLL_LIVE_MS : POLL_WAITING_MS),
  })
  const enableMut = useMutation({
    mutationFn: enableMochi,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['mochi'] }),
  })

  if (isLoading || status === undefined) {
    return (
      <>
        <PageHeader title={PRODUCT_NAME} subtitle={i18nT('apps.mochi.instances.connecting')} />
        <div className="px-6 pb-8">
          <div className="space-y-4 animate-pulse">
            {[1, 2, 3].map((i) => (
              <div key={i} className="h-16 rounded bg-bg-elevated" />
            ))}
          </div>
        </div>
      </>
    )
  }

  if (status !== 'enabled') {
    return (
      <>
        <PageHeader title={PRODUCT_NAME} subtitle={i18nT('apps.mochi.mochiPage.desktop_pet_companion')} />
        <OfflineLanding
          starting={status === 'starting'}
          pending={enableMut.isPending}
          onEnable={() => enableMut.mutate()}
          onRetry={() => qc.invalidateQueries({ queryKey: ['mochi', 'enabled'] })}
        />
      </>
    )
  }

  return <LiveView />
}

// ── Offline landing ─────────────────────────────────────────────────────────

// A function, not a module-level constant: `i18nT` must run at RENDER time so
// the list re-resolves when the language changes (a const would freeze whatever
// locale happened to be active at import).
const features = (): Array<[React.ReactNode, string]> => [
  [<Cat className="w-4 h-4" />, i18nT('apps.mochi.mochiPage.feat_pet')],
  [<Eye className="w-4 h-4" />, i18nT('apps.mochi.mochiPage.feat_watch')],
  [<MessageSquare className="w-4 h-4" />, i18nT('apps.mochi.mochiPage.feat_chat')],
  [<Camera className="w-4 h-4" />, i18nT('apps.mochi.mochiPage.feat_screenshot')],
  [<CalendarClock className="w-4 h-4" />, i18nT('apps.mochi.mochiPage.feat_briefing')],
  [<Bot className="w-4 h-4" />, i18nT('apps.mochi.mochiPage.feat_subagent')],
]

function OfflineLanding({
  starting,
  pending,
  onEnable,
  onRetry,
}: {
  starting: boolean
  pending: boolean
  onEnable: () => void
  onRetry: () => void
}) {
  return (
    <div className="flex items-center justify-center py-16">
      <div className="text-center max-w-md">
        <Cat className="w-16 h-16 mx-auto mb-4 opacity-40" />
        <div className="text-xl font-bold text-text-strong mb-2">{i18nT('apps.mochi.mochiPage.mochi_is_sleeping')}</div>
        <div className="text-sm text-muted mb-6">
          {starting
            ? i18nT('apps.mochi.mochiPage.landing_starting')
            : i18nT('apps.mochi.mochiPage.landing_enable_hint')}
        </div>
        <div className="flex gap-2 justify-center mb-8">
          {!starting && (
            <Btn primary onClick={onEnable} disabled={pending}>
              {pending
                ? i18nT('apps.mochi.mochiPage.enabling')
                : i18nT('apps.mochi.mochiPage.enable_mochi')}
            </Btn>
          )}
          <Btn onClick={onRetry}>
            <RefreshCw className="w-3.5 h-3.5 lucide-inline" /> {i18nT('apps.mochi.mochiPage.check_again')}
          </Btn>
        </div>
        <Card className="text-left mt-4">
          <CardTitle>{i18nT('apps.mochi.mochiPage.what_mochi_can_do')}</CardTitle>
          <div className="space-y-3 px-1 pt-1">
            {features().map(([icon, text], i) => (
              <div key={i} className="flex items-center gap-3 text-[13px] text-muted">
                <span className="shrink-0">{icon}</span>
                <span>{text}</span>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  )
}

// ── Live view ───────────────────────────────────────────────────────────────

function LiveView() {
  const qc = useQueryClient()
  const petState = useQuery({
    queryKey: ['mochi', 'pet-state'],
    queryFn: getPetState,
    refetchInterval: POLL_LIVE_MS,
  })
  const plan = useQuery({
    queryKey: ['mochi', 'plan'],
    queryFn: getPlan,
    refetchInterval: POLL_LIVE_MS,
  })
  const activity = useQuery({
    queryKey: ['mochi', 'activity'],
    queryFn: getActivity,
    refetchInterval: POLL_LIVE_MS,
  })
  const version = useQuery({
    queryKey: ['mochi', 'version'],
    queryFn: getMochiVersion,
    staleTime: Infinity,
  })

  const narrative = str(plan.data?.narrative ?? '—')
  // The live mood the state machine holds wins over the planner's intended mood
  // (the original only had the latter, because its plan payload was the only
  // channel that carried one).
  //
  // `neutral` is a REAL mood — the manager's initial value — so it is labelled
  // rather than dropped. Treating it as falsy (which `||` did, once an older
  // backend answered without the key at all) left the card reading '—' whenever
  // the pet was simply calm, which is indistinguishable from the request having
  // failed. '—' now means exactly one thing: nothing was reported.
  const liveMood = petState.data?.mood
  const rawMood = str(
    (typeof liveMood === 'string' && liveMood !== '' ? liveMood : undefined) ??
      plan.data?.mood ??
      '—',
  )
  // '—' is the "nothing reported" sentinel and has no catalog entry; anything
  // else is a real mood and goes through the map.
  const mood = rawMood === '—' ? rawMood : moodLabel(rawMood)
  const state = petState.data?.state ?? 'offline'

  return (
    <>
      <PageHeader title={PRODUCT_NAME} subtitle={i18nT('apps.mochi.mochiPage.desktop_pet_companion')} />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        {!isElectron && (
          <div role="note" className="mb-4 text-[13px] text-muted">
            {i18nT('apps.mochi.mochiPage.this_is_the_browser_view_everything_here_is_live')}
          </div>
        )}

        <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
          <StatCard
            label={i18nT('apps.mochi.mochiPage.status')}
            // Through the label map, not a capitalize(): the raw enum renders
            // as 'Approval_pending' / 'PeekThinking' and stays English in every
            // locale, while the panel windows show the localized label for the
            // SAME value — the dashboard was the odd one out.
            value={stateLabel(state)}
            accent={state !== 'offline'}
          />
          <StatCard label={i18nT('apps.mochi.mochiPage.mood')} value={mood} />
          <StatCard label={i18nT('apps.mochi.mochiPage.version')} value={version.data || '—'} />
        </div>

        <div className="grid grid-cols-2 gap-4 mb-4">
          <Card>
            <CardTitle>{i18nT('apps.mochi.mochiPage.recent_activity')}</CardTitle>
            <ActivityList
              rows={formatActivity(activity.data?.entries, narrative)}
            />
          </Card>
          <Card>
            <CardTitle>{i18nT('apps.mochi.stats.title')}</CardTitle>
            <MemoriesCard />
          </Card>
        </div>

        <div className="grid grid-cols-2 gap-4 mb-4">
          <Card>
            <CardTitle>{i18nT('apps.mochi.watchPanel.title')}</CardTitle>
            <WatchlistCard />
          </Card>
          <Card>
            <CardTitle>{i18nT('apps.mochi.mochiPage.mochi_s_plan')}</CardTitle>
            {narrative !== '—' && narrative !== 'Active' && (
              <div className="text-[13px] text-muted mb-3 leading-relaxed">{narrative}</div>
            )}
            <PlanTimeline tasks={formatPlanTasks(plan.data)} />
          </Card>
        </div>

        {/* Appearance intentionally NOT here: the avatar/pack picker lives in the
            pet's own Avatars window (right-click > Avatars) and in Settings, and a
            third entry point drifted from those two. */}
        <div className="mb-4">
          <Card>
            <CardTitle>{i18nT('apps.mochi.mochiPage.pinned_files')}</CardTitle>
            <PinnedCard />
          </Card>
        </div>

        <Btn onClick={() => qc.invalidateQueries({ queryKey: ['mochi'] })}>
          <RefreshCw className="w-3.5 h-3.5 lucide-inline" /> {i18nT('apps.mochi.mochiPage.refresh')}
        </Btn>
      </div>
    </>
  )
}

// ── Pagination shared by the two paginated lists ────────────────────────────

function Pager({
  page,
  total,
  onPage,
}: {
  page: number
  total: number
  onPage: (p: number) => void
}) {
  if (total <= PER_PAGE) return null
  const last = (page + 1) * PER_PAGE >= total
  return (
    <div className="flex items-center justify-between mt-3 pt-2 border-t border-border text-[13px] text-muted">
      <span>
        {i18nT('apps.mochi.mochiPage.page_range', { from: page * PER_PAGE + 1, to: Math.min((page + 1) * PER_PAGE, total), total })}
      </span>
      <div className="flex gap-1.5">
        <Btn disabled={page === 0} onClick={() => onPage(page - 1)}>
          {i18nT('apps.mochi.mochiPage.prev')}
        </Btn>
        <Btn disabled={last} onClick={() => onPage(page + 1)}>
          {i18nT('apps.mochi.mochiPage.next')}
        </Btn>
      </div>
    </div>
  )
}

// ── Recent activity ─────────────────────────────────────────────────────────

/** Tone per activity kind. Keys are the kinds in `ACTIVITY_TYPE_KEY`; anything
 *  unlisted falls back to the neutral tone, so a new kind degrades rather than
 *  throwing. `sleep`/`wake` used to be listed here and are not kinds the backend
 *  ever writes — `budget`/`spawn`/`system` are. */
const ACTIVITY_TONES: Record<string, string> = {
  budget: 'bg-warn-subtle text-warn',
  memory: 'bg-aim-subtle text-aim',
  notification: 'bg-accent-subtle text-accent',
  plan: 'bg-accent-subtle text-accent',
  presence: 'bg-bg-elevated text-muted',
  sleep: 'bg-warn-subtle text-warn',
  spawn: 'bg-accent-subtle text-accent',
  system: 'bg-warn-subtle text-warn',
}

function ActivityList({ rows }: { rows: ReturnType<typeof formatActivity> }) {
  const [page, setPage] = useState(0)
  if (rows.length === 0) {
    return (
      <EmptyState
        icon={<ScrollText className="w-5 h-5" />}
        title={i18nT('apps.mochi.mochiPage.no_activity_yet')}
        subtitle={i18nT('apps.mochi.mochiPage.mochi_logs_events_here_as_they_happen')}
      />
    )
  }
  return (
    <>
      <div className="space-y-1 max-h-64 overflow-y-auto">
        {rows.slice(page * PER_PAGE, (page + 1) * PER_PAGE).map((e, i) => (
          <div key={i} className="flex items-start gap-3 px-2.5 py-1.5 text-[13px]">
            <span className="text-muted/60 font-mono w-16 shrink-0 whitespace-nowrap">
              {e.time}
            </span>
            <span
              className={`px-1.5 py-[1px] rounded text-[11px] font-medium shrink-0 ${
                ACTIVITY_TONES[e.type] ?? 'bg-bg-elevated text-muted'
              }`}
            >
              {activityTypeLabel(e.type)}
            </span>
            <span className="text-text">{e.content}</span>
          </div>
        ))}
      </div>
      <Pager page={page} total={rows.length} onPage={setPage} />
    </>
  )
}

// ── Plan timeline ───────────────────────────────────────────────────────────

function PlanTimeline({ tasks }: { tasks: TimelineRow[] }) {
  const [page, setPage] = useState(0)
  if (tasks.length === 0) {
    return (
      <EmptyState
        icon={<ClipboardList className="w-5 h-5" />}
        title={i18nT('apps.mochi.mochiPage.no_plan_yet')}
        subtitle={i18nT('apps.mochi.mochiPage.mochi_writes_a_plan_when_it_next_wakes_up')}
      />
    )
  }
  return (
    <>
      <div className="space-y-1.5">
        {tasks.slice(page * PER_PAGE, (page + 1) * PER_PAGE).map((t, i) => (
          <div
            key={i}
            className={`flex items-center gap-3 px-2.5 py-1.5 rounded-md text-[13px] ${
              t.done ? 'text-muted line-through' : 'text-text'
            }`}
          >
            <span className="text-muted/60 font-mono w-16 shrink-0 whitespace-nowrap">
              {t.time}
            </span>
            <span
              className={`w-2 h-2 rounded-full shrink-0 ${t.done ? 'bg-ok' : 'bg-border'}`}
            />
            <span>{t.action}</span>
          </div>
        ))}
      </div>
      <Pager page={page} total={tasks.length} onPage={setPage} />
    </>
  )
}

// ── Watch list ──────────────────────────────────────────────────────────────

function WatchlistCard() {
  const qc = useQueryClient()
  const invalidate = () => qc.invalidateQueries({ queryKey: ['mochi', 'watchlist'] })
  const { data } = useQuery({
    queryKey: ['mochi', 'watchlist'],
    queryFn: getWatchlist,
    refetchInterval: POLL_LIVE_MS,
  })
  const cancelMut = useMutation({
    mutationFn: (id: string) => updateWatchlist({ cancel: [id] }),
    onSuccess: invalidate,
  })
  // Reopen is the undo for the cancel button one row over: same endpoint the desktop
  // panel's "Reopen" uses, so the two surfaces cannot drift on what un-cancelling means.
  const reopenMut = useMutation({
    mutationFn: (id: string) => updateWatchlist({ update: [{ id, status: 'watching' }] }),
    onSuccess: invalidate,
  })
  const addMut = useMutation({ mutationFn: addToWatchlist, onSuccess: invalidate })

  const items = data?.items ?? []
  return (
    <>
      {/* `onAdd` takes a `done` callback instead of clearing on submit: the form
          used to blank itself the moment mutate() was called, so a failed add
          discarded what the user typed with no feedback anywhere. Now the fields
          survive until the server accepts, and a failure is shown inline. */}
      <AddWatchForm
        existingKinds={items.map((i) => i.kind).filter(Boolean)}
        failed={addMut.isError}
        onAdd={(label, kind, target, done) =>
          addMut.mutate({ label, kind: kind as WatchKind, target }, { onSuccess: done })
        }
      />
      <div className="mt-3">
        <WatchRows
          rows={formatWatchItems(items)}
          onCancel={(id) => cancelMut.mutate(id)}
          onReopen={(id) => reopenMut.mutate(id)}
        />
      </div>
    </>
  )
}

function WatchRows({
  rows,
  onCancel,
  onReopen,
}: {
  rows: WatchRowView[]
  onCancel: (id: string) => void
  onReopen: (id: string) => void
}) {
  const [expanded, setExpanded] = useState<string | null>(null)
  if (rows.length === 0) {
    return (
      <EmptyState
        icon={<Eye className="w-5 h-5" />}
        title={i18nT('apps.mochi.mochiPage.nothing_being_watched')}
        subtitle={i18nT('apps.mochi.mochiPage.add_something_above_or_ask_mochi_to_keep_an_eye')}
      />
    )
  }
  return (
    <div className="space-y-1">
      {rows.map((item) => {
        const isOpen = expanded === item.id
        const Chevron = isOpen ? ChevronDown : ChevronRight
        return (
          <div key={item.id} className="border border-border rounded-md overflow-hidden">
            {/* Clickable, not a bare div: this row IS the disclosure control for the
                detail panel below, so it needs a role, focus and Enter/Space —
                a pointer-only expander hides Notes / Trigger / Next-check from
                keyboard users entirely. */}
            <Clickable
              className="flex items-center gap-3 px-3 py-2.5 cursor-pointer hover:bg-bg-hover transition-colors"
              aria-expanded={isOpen}
              onClick={() => setExpanded(isOpen ? null : item.id)}
            >
              <span className="text-[13px] text-muted w-24 shrink-0 truncate">{watchKindLabel(item.type)}</span>
              <span className="text-[13px] text-text flex-1 truncate">{item.label}</span>
              <Badge variant={statusTone(item.status)}>{watchStatusLabel(item.status)}</Badge>
              <button
                title={i18nT(item.cancelled
                  ? 'apps.mochi.watchPanel.reopen'
                  : 'apps.mochi.mochiPage.stop_watching')}
                aria-label={i18nT(item.cancelled
                  ? 'apps.mochi.watchPanel.reopen'
                  : 'apps.mochi.mochiPage.stop_watching')}
                className="opacity-50 hover:opacity-100 shrink-0"
                onClick={(e) => {
                  // The row itself toggles the detail panel; without this the
                  // click would also expand the row it just acted on.
                  e.stopPropagation()
                  if (item.cancelled) onReopen(item.id)
                  else onCancel(item.id)
                }}
              >
                {item.cancelled
                  ? <RotateCcw className="w-4 h-4" />
                  : <XCircle className="w-4 h-4" />}
              </button>
              <Chevron className="w-3.5 h-3.5 text-muted shrink-0" />
            </Clickable>
            {isOpen && (
              <div className="px-3 pb-3 pt-1 border-t border-border bg-bg-elevated/50 space-y-1.5 text-[13px]">
                {item.notes && (
                  <div>
                    <span className="text-muted">{i18nT('apps.mochi.mochiPage.notes')}:</span>{' '}
                    <span className="text-text">{item.notes}</span>
                  </div>
                )}
                {item.trigger && (
                  <div>
                    <span className="text-muted">{i18nT('apps.mochi.mochiPage.trigger')}:</span>{' '}
                    <span className="text-text">{item.trigger}</span>
                  </div>
                )}
                <div className="flex gap-4 text-muted">
                  {item.created && <span>{i18nT('apps.mochi.mochiPage.created')} {item.created}</span>}
                  {item.checks && <span>{item.checks}</span>}
                  {item.nextCheck && <span>{i18nT('apps.mochi.mochiPage.next_check', { when: item.nextCheck })}</span>}
                </div>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

function AddWatchForm({
  existingKinds,
  onAdd,
  failed,
}: {
  existingKinds: string[]
  /** `done` is invoked only when the server accepted, and clears the fields. */
  onAdd: (label: string, kind: string, target: string, done: () => void) => void
  failed?: boolean
}) {
  const [label, setLabel] = useState('')
  const [kind, setKind] = useState('url')
  const [target, setTarget] = useState('')
  const [newKind, setNewKind] = useState('')
  const creating = kind === '__new__'

  // Presets stay simple (user decision: no cr/pipeline/ticket jargon); any
  // category the user has already created shows up alongside them.
  const kinds = Array.from(new Set(['url', 'custom', ...existingKinds]))

  return (
    <form
      // items-end, not items-center: each field is now a label + control column,
      // so centring would float the submit button against the labels instead of
      // the inputs it sits beside.
      className="flex gap-2 items-end flex-wrap"
      onSubmit={(e) => {
        e.preventDefault()
        const finalKind = creating ? newKind.trim().toLowerCase() : kind
        if (!label.trim() || !target.trim() || !finalKind) return
        onAdd(label.trim(), finalKind, target.trim(), () => {
          setLabel('')
          setTarget('')
          setNewKind('')
        })
        if (creating && finalKind) setKind(finalKind)
      }}
    >
      {/* Each control is WRAPPED in its label rather than relying on the
          placeholder: a placeholder disappears the moment you type, so the
          field's meaning was only available while it was empty — and a
          placeholder is not an accessible name. Wrapping also associates the
          two without needing generated ids. */}
      <label className="flex flex-col gap-1">
        <span className="text-[10px] text-muted">{i18nT('apps.mochi.mochiPage.field_name')}</span>
        <input
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder={i18nT('apps.mochi.mochiPage.what_to_call_it')}
          className="rounded-md border border-border bg-transparent px-2 py-1.5 text-sm w-32"
        />
      </label>
      <div className="flex flex-col gap-1">
        {/* A <div>, not a <label>: the control below renders a button, which a
            <label> cannot associate with — the accessible name rides on
            aria-label instead. Matches JobForm's sibling fields. */}
        <span className="text-[10px] text-muted">{i18nT('apps.mochi.mochiPage.field_category')}</span>
        {/* "New category…" is an ACTION, not a value: SimpleSelect's `action` row fires
            onSelect instead of onChange, so `__new__` never has to masquerade as a
            selectable category. It does still name the creating state, which is why the
            trigger falls back to that same label while the extra field is open.
            Only the seed kinds have catalog entries; a category the user typed themselves
            is their own word and falls through verbatim, which is watchKindLabel's
            fallback — including for a just-created one the server has not echoed yet. */}
        <SimpleSelect
          options={kinds}
          optionLabels={kinds.map((k) => watchKindLabel(k))}
          value={kind}
          onChange={setKind}
          action={{
            label: i18nT('apps.mochi.mochiPage.new_category'),
            onSelect: () => setKind('__new__'),
          }}
          triggerFallback={creating ? i18nT('apps.mochi.mochiPage.new_category') : watchKindLabel(kind)}
          aria-label={i18nT('apps.mochi.mochiPage.field_category')}
        />
      </div>
      {creating && (
        <label className="flex flex-col gap-1">
          <span className="text-[10px] text-muted">{i18nT('apps.mochi.mochiPage.category_name')}</span>
          <input
            value={newKind}
            onChange={(e) => setNewKind(e.target.value)}
            placeholder={i18nT('apps.mochi.mochiPage.category_name')}
            autoFocus
            className="rounded-md border border-border bg-transparent px-2 py-1.5 text-sm w-28"
          />
        </label>
      )}
      <label className="flex flex-col gap-1 flex-1 min-w-32">
        <span className="text-[10px] text-muted">{i18nT('apps.mochi.mochiPage.field_target')}</span>
        <input
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          placeholder={i18nT('apps.mochi.mochiPage.url_or_what_to_watch')}
          className="w-full rounded-md border border-border bg-transparent px-2 py-1.5 text-sm"
        />
      </label>
      <Btn primary type="submit">
        <Plus className="w-3.5 h-3.5 lucide-inline" /> {i18nT('apps.mochi.mochiPage.watch')}
      </Btn>
      {failed && (
        <span role="alert" className="text-[11px] text-[var(--danger)] w-full">
          {i18nT('apps.mochi.mochiPage.add_failed')}
        </span>
      )}
    </form>
  )
}

// ── Memories ────────────────────────────────────────────────────────────────

/**
 * Build the memory rows.
 *
 * The arithmetic comes from `panel/statsFormatters` — the very module the panel's
 * MemoriesView uses — so the two surfaces cannot drift on what "6 days together"
 * means. Only the icon and layout are dashboard-flavoured. (MemoriesView itself
 * is not reusable here: it fetches over the panel bridge and is styled inline for
 * the frameless window.)
 */
function memoryRows(s: CompanionStats, petName: string): Array<[React.ReactNode, string]> {
  const rows: Array<[React.ReactNode, string]> = []
  const totalMsgs = s.messages.sent + s.messages.received

  if (shouldShowStat(s.companionSeconds)) {
    const streak =
      s.streak > 1 ? ` ${i18nT('apps.mochi.stats.streak', { streak: s.streak })}` : ''
    rows.push([
      <Clock className="w-4 h-4" />,
      `${i18nT('apps.mochi.stats.companion_days', {
        time: formatCompanionTime(s.companionSeconds),
      })}${streak}`,
    ])
  }
  if (shouldShowStat(totalMsgs)) {
    rows.push([
      <MessageSquare className="w-4 h-4" />,
      i18nT('apps.mochi.stats.messages', {
        total: fmtNumber(totalMsgs), sent: fmtNumber(s.messages.sent),
        received: fmtNumber(s.messages.received), name: petName,
      }),
    ])
  }
  if (shouldShowStat(s.walkSteps)) {
    rows.push([<Footprints className="w-4 h-4" />,
      i18nT('apps.mochi.stats.walks', { count: fmtNumber(s.walkSteps) })])
  }
  if (shouldShowStat(s.screenshots)) {
    rows.push([<Camera className="w-4 h-4" />,
      i18nT('apps.mochi.stats.screenshots', { count: fmtNumber(s.screenshots) })])
  }
  if (shouldShowStat(s.peeks)) {
    rows.push([<Eye className="w-4 h-4" />,
      i18nT('apps.mochi.stats.peeks', { count: fmtNumber(s.peeks) })])
  }
  if (shouldShowStat(s.drags)) {
    rows.push([
      <MousePointer2 className="w-4 h-4" />,
      i18nT('apps.mochi.stats.drags', { count: fmtNumber(s.drags) }),
    ])
  }
  if (shouldShowStat(s.thinkingSeconds)) {
    rows.push([<Brain className="w-4 h-4" />, formatThinkingTime(s.thinkingSeconds)])
  }
  if (s.latestActiveTime) {
    rows.push([<Moon className="w-4 h-4" />,
      i18nT('apps.mochi.stats.latest_time', { time: s.latestActiveTime })])
  }
  if (s.earliestActiveTime) {
    rows.push([<Sun className="w-4 h-4" />,
      i18nT('apps.mochi.stats.earliest_time', { time: s.earliestActiveTime })])
  }
  if (s.busiestDay && s.busiestDay.messages > 0) {
    rows.push([
      <Flame className="w-4 h-4" />,
      i18nT('apps.mochi.stats.busiest_day', {
        date: s.busiestDay.date, count: fmtNumber(s.busiestDay.messages),
      }),
    ])
  }
  if (shouldShowStat(s.longestChat)) {
    rows.push([<MessagesSquare className="w-4 h-4" />,
      i18nT('apps.mochi.stats.longest_chat', { count: fmtNumber(s.longestChat) })])
  }
  return rows
}

function MemoriesCard() {
  const { data } = useQuery({
    queryKey: ['mochi', 'stats'],
    queryFn: getStats,
    refetchInterval: 60_000,
  })
  // The message row names the pet ("… 12 you · 8 Mochi"), so it needs the
  // user's actual name, not the packaged default.
  const { data: settings } = useQuery({
    queryKey: ['mochi', 'settings'],
    queryFn: getSettings,
  })
  const petName = settings?.petName || DEFAULT_PET_NAME
  if (!data) {
    return (
      <EmptyState
        icon={<Brain className="w-5 h-5" />}
        title={i18nT('apps.mochi.mochiPage.no_memories_yet')}
        subtitle={i18nT('apps.mochi.mochiPage.spend_time_with_mochi_to_build_memories')}
      />
    )
  }
  const rows = memoryRows(data, petName)
  const topMoods = getTopMoods(data.moods, 3)
  return (
    <div className="space-y-1">
      {rows.map(([icon, text], i) => (
        <div key={i} className="flex items-center gap-2.5 px-1 py-1 rounded-md text-[13px]">
          <span className="shrink-0 text-muted">{icon}</span>
          <span className="text-text">{text}</span>
        </div>
      ))}
      {topMoods.length > 0 && (
        <div className="flex items-center gap-2.5 px-1 py-1 text-[13px]">
          <span className="shrink-0 text-muted">
            <Smile className="w-4 h-4" />
          </span>
          <span className="text-text shrink-0">{i18nT('apps.mochi.mochiPage.top_moods')}:</span>
          <div className="flex gap-1.5 flex-wrap">
            {topMoods.map((m) => (
              <span
                key={m.mood}
                className="px-2 py-[1px] rounded-full text-[11px] font-medium bg-bg-elevated border border-border text-text"
              >
                {moodLabel(m.mood)} {fmtPercent(m.percent / 100)}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// ── Pinned files ────────────────────────────────────────────────────────────

function PinnedCard() {
  const qc = useQueryClient()
  const invalidate = () => qc.invalidateQueries({ queryKey: ['mochi', 'pinned'] })
  const { data } = useQuery({
    queryKey: ['mochi', 'pinned'],
    queryFn: getPinned,
    refetchInterval: POLL_LIVE_MS,
  })
  const unpinMut = useMutation({ mutationFn: unpinFile, onSuccess: invalidate })
  const seenMut = useMutation({ mutationFn: markPinnedSeen, onSuccess: invalidate })

  const pins = data?.pins ?? []
  if (pins.length === 0) {
    return (
      <EmptyState
        icon={<Pin className="w-5 h-5" />}
        title={i18nT('apps.mochi.mochiPage.no_pinned_files')}
        subtitle={i18nT('apps.mochi.mochiPage.mochi_pins_files_here_when_you_ask_it_to_watch_o')}
      />
    )
  }
  return (
    <ul className="space-y-2">
      {pins.map((pin) => (
        <li
          key={pin.path}
          className="flex items-center justify-between rounded-md border border-border px-3 py-2"
        >
          <div className="min-w-0">
            <div className="text-[13px] text-text truncate">
              {pin.label}
              {pin.updatedAt !== undefined && (
                <span className="ml-2 text-[11px] rounded bg-accent-subtle text-accent px-1.5 py-[1px]">
                  {i18nT('apps.mochi.mochiPage.changed')}
                </span>
              )}
            </div>
            <div className="text-[12px] text-muted truncate">{pin.path}</div>
          </div>
          <div className="flex gap-2 shrink-0 ml-3 items-center">
            {pin.updatedAt !== undefined && (
              <button
                onClick={() => seenMut.mutate(pin.path)}
                className="text-[12px] text-muted hover:text-text"
              >
                {i18nT('apps.mochi.mochiPage.mark_seen')}
              </button>
            )}
            <button
              onClick={() => unpinMut.mutate(pin.path)}
              title={i18nT('apps.mochi.pinned.unpin')}
              aria-label={i18nT('apps.mochi.pinned.unpin')}
              className="opacity-50 hover:opacity-100"
            >
              <XCircle className="w-4 h-4" />
            </button>
          </div>
        </li>
      ))}
    </ul>
  )
}

