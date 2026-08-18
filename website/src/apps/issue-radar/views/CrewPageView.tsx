/**
 * CrewPageView — one crew's page: who it is, what it is holding, and what it did.
 *
 * Five regions, top to bottom (the layout is `temp-screenshots/crews-mock/02-crew-page-*.png`):
 *
 *   1. header       identity + derived state + the three actions
 *   2. extra prompt the crew's standing instruction, verbatim
 *   3. stat row     slot usage, 24h throughput, replies waited on
 *   4. work items   what it is holding RIGHT NOW, and what it will do next
 *   5. work log     what it has actually done, newest first
 *
 * ## The `next` column is the point of this page
 *
 * A phase says where an item sits; it does not say what to do about it. `next`
 * holds a RESUMABLE INTENT written by the crew for its own future self ("add the
 * Windows branch to `_safe_chmod`, the regression test already fails"), which is
 * also the only thing a human can read to decide whether to intervene. So it is
 * rendered as full prose — no clamp, no ellipsis, no fixed-height cell. Truncating
 * it would leave the column technically present and practically useless.
 *
 * ## Phase classification is imported, never re-derived
 *
 * `TERMINAL_PHASES` / `EDITING_PHASES` / `countsTowardOpen` mirror
 * `crew_store.py`'s frozensets of the same names. A local
 * `phase === 'resolved' || phase === 'skipped'` here would be a fourth definition
 * that drifts the first time a phase is added, so every classification question on
 * this page goes through the exports in `../api`.
 *
 * ## Two derivations this page owns, and why
 *
 * `GET /crew` does NOT carry the `status` field that `GET /crews` adds per crew
 * (`crew_routes._crew_page` vs `_crews_page`), so the header's state badge is
 * derived here from the same input the backend's own `working` flag uses: the
 * NEWEST non-terminal item, `list_work_items` being newest-progress-first. Paused
 * outranks it (a paused crew is doing nothing regardless of what it holds), and no
 * non-terminal item at all reads as idle.
 *
 * The work log's OUTCOME column shows the event's `kind`. A `CrewEvent` carries no
 * outcome or phase field, and the phase an event moved an item to is not
 * recoverable after the fact — the item's CURRENT phase would be wrong for every
 * row but the newest. `kind` is the honest answer the record actually holds.
 */
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { CirclePlus, Inbox, ListChecks, Pause, Pencil, Play, ScrollText } from 'lucide-react'

import { Badge, Btn, Card, CardTitle, EmptyState, StatCard } from '../../../components/ui'
import { fmtDate, fmtDateFields, fmtDateTime, fmtNumber, fmtRelative, toDate } from '../../../i18n/format'
import { useAppDispatch } from '../../../store'
import { switchSlot } from '../../../store/chatSlice'
import {
  EDITING_PHASES, TERMINAL_PHASES, countsTowardOpen, issueRadarApi,
  type Crew, type CrewEvent, type CrewEventKind, type CrewPhase, type WorkItem,
} from '../api'
import CrewGhost from '../components/CrewGhost'
import { useIssueRadar } from '../context'
import { issueUrlFor, repoScopeKey } from '../lib/links'

const DAY_MS = 86_400_000

/** The repo's table header cell, as `apps/papyrus/ProjectList.tsx` and the
 *  dashboard's own tables spell it. */
const TH = 'text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium'
const TD = 'px-2.5 py-2.5 border-b border-border/60 align-top text-[13px] text-text'

/** Epoch-ms for a wire timestamp; 0 when it cannot be parsed, which sorts an
 *  unreadable stamp to the END of a newest-first list rather than the top. */
function stampMs(iso: string | null | undefined): number {
  return toDate(iso)?.getTime() ?? 0
}

/** Newest recorded progress first — the order `list_work_items` already returns
 *  and the order the state badge's "newest item" reading depends on. */
function byProgressDesc(a: WorkItem, b: WorkItem): number {
  return stampMs(b.last_progress_at) - stampMs(a.last_progress_at)
}

/** Badge colour for a phase.
 *
 * Reads the imported classifications rather than listing phases: `EDITING_PHASES`
 * is exactly "the crew is typing right now" (green), and `TERMINAL_PHASES` minus
 * `resolved` is "over, without a fix" (grey). Only the three parked phases are
 * named, because parked-on-what is the distinction the colour is carrying. */
/** Catalog key for a phase's human label.
 *
 *  An explicit map, not a computed `phase_${phase.replace('-','_')}` key: a
 *  constructed key is invisible to the i18n key-reference gate, so a missing or
 *  renamed entry would ship as the raw token instead of failing the build. The
 *  tokens themselves are machine vocabulary (`awaiting-merge`, `handed-back`) —
 *  rendering them verbatim, as this view used to, put kebab-case English in front
 *  of every reader and showed the SAME English in all 12 non-English locales,
 *  which the untranslated-literal gate cannot catch because the value is dynamic.
 */
const PHASE_LABEL_KEY: Record<CrewPhase, string> = {
  'selected': 'apps.issueRadar.views.crews.page.phase_selected',
  'claimed': 'apps.issueRadar.views.crews.page.phase_claimed',
  'investigating': 'apps.issueRadar.views.crews.page.phase_investigating',
  'implementing': 'apps.issueRadar.views.crews.page.phase_implementing',
  'awaiting-ci': 'apps.issueRadar.views.crews.page.phase_awaiting_ci',
  'addressing-review': 'apps.issueRadar.views.crews.page.phase_addressing_review',
  'awaiting-merge': 'apps.issueRadar.views.crews.page.phase_awaiting_merge',
  'awaiting-reply': 'apps.issueRadar.views.crews.page.phase_awaiting_reply',
  'resolved': 'apps.issueRadar.views.crews.page.phase_resolved',
  'skipped': 'apps.issueRadar.views.crews.page.phase_skipped',
  'yielded': 'apps.issueRadar.views.crews.page.phase_yielded',
  'handed-back': 'apps.issueRadar.views.crews.page.phase_handed_back',
  'preempted': 'apps.issueRadar.views.crews.page.phase_preempted',
}

/** Catalog key for a ledger line's kind. Same reasoning as `PHASE_LABEL_KEY`. */
const KIND_LABEL_KEY: Record<CrewEventKind, string> = {
  'claim': 'apps.issueRadar.views.crews.page.kind_claim',
  'investigate': 'apps.issueRadar.views.crews.page.kind_investigate',
  'reply': 'apps.issueRadar.views.crews.page.kind_reply',
  'implement': 'apps.issueRadar.views.crews.page.kind_implement',
  'ci': 'apps.issueRadar.views.crews.page.kind_ci',
  'review': 'apps.issueRadar.views.crews.page.kind_review',
  'conflict': 'apps.issueRadar.views.crews.page.kind_conflict',
  'merge': 'apps.issueRadar.views.crews.page.kind_merge',
  'handback': 'apps.issueRadar.views.crews.page.kind_handback',
  'skip': 'apps.issueRadar.views.crews.page.kind_skip',
  'yield': 'apps.issueRadar.views.crews.page.kind_yield',
}

function phaseVariant(phase: CrewPhase): 'ok' | 'err' | 'warn' | 'aim' | 'muted' {
  if (phase === 'resolved') return 'ok'
  if (TERMINAL_PHASES.has(phase)) return 'muted'
  if (EDITING_PHASES.has(phase)) return 'ok'
  if (phase === 'awaiting-reply') return 'aim'
  if (phase === 'awaiting-ci' || phase === 'awaiting-merge') return 'warn'
  return 'aim'
}

/** Badge colour for a ledger line's kind — what happened, in the same colour
 *  vocabulary the phases use. */
function kindVariant(kind: CrewEventKind): 'ok' | 'err' | 'warn' | 'aim' | 'muted' {
  if (kind === 'merge') return 'ok'
  if (kind === 'ci' || kind === 'conflict') return 'warn'
  if (kind === 'skip' || kind === 'yield' || kind === 'handback') return 'muted'
  return 'aim'
}

/** A calendar date with the year elided while it is THIS year — `Aug 6` now,
 *  `Aug 6, 2025` once the year turns, so an old stamp can never read as recent.
 *  Both widths come from `Intl` under the active UI locale (never the host's). */
function shortDate(iso: string | null | undefined, nowMs: number): string {
  const d = toDate(iso)
  if (!d) return '—'
  return d.getFullYear() === new Date(nowMs).getFullYear()
    ? fmtDateFields(d, { month: 'short', day: 'numeric' })
    : fmtDate(d)
}

/**
 * A `StatCard` plus one line of explanation under its value.
 *
 * `StatCard` renders `label` + `value` and nothing else — `children` land in its
 * `...rest` spread, which JSX's own children override, so they are silently
 * dropped. The note is therefore a second element, joined to the card by dropping
 * the seam between them (`border-b-0` above, `border-t-0` below): one continuous
 * bordered block, and the note is in normal flow so a long translation wraps and
 * grows the card instead of overflowing it.
 */
function StatBlock({ label, value, note, colorClass, testId }: {
  label: string
  value: string | number
  note: string
  colorClass?: string
  testId: string
}) {
  return (
    // `h-full` + a growing note is what keeps the four blocks the same height:
    // the grid stretches each cell to the tallest, and the NOTE takes the slack,
    // so every block's bottom border lands on one line however long a
    // translation runs. Without it each block is its own natural height and the
    // row goes ragged.
    <div className="flex flex-col h-full">
      <StatCard
        label={label}
        value={value}
        colorClass={colorClass}
        className="rounded-b-none border-b-0 pb-1.5 hover:translate-y-0 hover:border-border"
        data-testid={testId}
      />
      <div
        className="flex-1 rounded-b-md border border-t-0 border-border bg-card px-4 pb-3.5 pt-1 text-[12px] leading-snug text-muted"
        data-testid={`${testId}-note`}
      >
        {note}
      </div>
    </div>
  )
}

export interface CrewPageViewProps {
  crewId: string
  /** Open the crew editor. The dialog lives outside this view, so the page only
   *  reports the intent — and the Edit action is hidden when nothing can handle
   *  it rather than rendering a dead control. */
  onEdit?: (crew: Crew) => void
}

export default function CrewPageView({ crewId, onEdit }: CrewPageViewProps) {
  const { t } = useTranslation()
  const { active, refreshPrefs } = useIssueRadar()
  const qc = useQueryClient()
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const [sessionError, setSessionError] = useState<string | null>(null)

  const queryKey = ['issue-radar', 'crew', repoScopeKey(active), crewId]
  // Polled exactly the way `context.tsx` polls its lists: the crew record moves
  // on the crew's own turns, not on the user's, so this page is a live view.
  const detail = useQuery({
    queryKey,
    queryFn: () => issueRadarApi.crew(active, crewId),
    refetchInterval: refreshPrefs.listPollMs,
    refetchIntervalInBackground: refreshPrefs.pollInBackground,
    staleTime: refreshPrefs.staleTimeMs,
  })

  const crew = detail.data?.crew ?? null
  // Memoized, not `detail.data?.items ?? []` inline: a fresh `[]` on every render
  // makes it a new dependency identity and every `useMemo` below re-runs, which is
  // what `react-hooks/exhaustive-deps` flags. One identity per query result.
  const items = useMemo(() => detail.data?.items ?? [], [detail.data])
  const events = useMemo(() => detail.data?.events ?? [], [detail.data])
  const counts = detail.data?.counts ?? { open: 0 }

  const pause = useMutation({
    mutationFn: (paused: boolean) => issueRadarApi.setCrewPaused(active, crewId, paused),
    onSuccess: (res) => {
      // Write the returned record straight into the cache so the header flips on
      // the response rather than on the next poll, then let the poll reconcile.
      qc.setQueryData(queryKey, (prev: typeof detail.data) =>
        prev ? { ...prev, crew: res.crew } : prev)
      void qc.invalidateQueries({ queryKey })
    },
  })

  // ── open work items ──
  // `countsTowardOpen` is the same predicate `counts.open` is measured with, so
  // the row count and the "N / max" stat can never disagree.
  const openItems = useMemo(
    () => items.filter((it) => countsTowardOpen(it.phase)).sort(byProgressDesc),
    [items],
  )

  // ── work log: the 24h split ──
  // A ROLLING 24-hour window on absolute epoch milliseconds (`now - 86_400_000`),
  // not a calendar day: the crew works around the clock and across timezones, so
  // "since yesterday 00:00" would mean something different every hour of the day
  // and something different again for a user who moved. Only the RENDERING of the
  // stamps is locale-aware (`fmtRelative` / `fmtDateFields`); the boundary itself
  // is timezone-free. `now` is read once per recompute, so every row in one paint
  // is split against the SAME instant and the divider cannot land twice.
  //
  // That instant is the LAST SUCCESSFUL POLL (`dataUpdatedAt`), not the render
  // clock. Two reasons. It keeps the whole page one coherent snapshot — the
  // divider, every age, and the 24h resolved count are all "as of this data" —
  // and it is what makes the ages advance at all: react-query's structural sharing
  // hands back the SAME `events` reference when a poll returns identical data, so
  // a memo keyed on `events` alone would pin the clock to the first fetch and
  // freeze every "12m ago" for as long as the crew records nothing new.
  const dataUpdatedAt = detail.dataUpdatedAt
  const log = useMemo(() => {
    const nowMs = dataUpdatedAt > 0 ? dataUpdatedAt : Date.now()
    const cutoff = nowMs - DAY_MS
    const sorted = [...events].sort((a, b) => stampMs(b.ts) - stampMs(a.ts))
    // Sorted newest-first, so the first row older than the cutoff starts the tail —
    // one boundary, and recent/earlier stay contiguous.
    const split = sorted.findIndex((e) => stampMs(e.ts) < cutoff)
    return split === -1
      ? { nowMs, recent: sorted, earlier: [] as CrewEvent[] }
      : { nowMs, recent: sorted.slice(0, split), earlier: sorted.slice(split) }
  }, [events, dataUpdatedAt])

  // ── stats ──
  const resolved24h = useMemo(
    () => items.filter((it) => it.phase === 'resolved' && stampMs(it.finished_at) >= log.nowMs - DAY_MS).length,
    [items, log.nowMs],
  )
  const askedRequester = useMemo(
    () => items.filter((it) => it.phase === 'awaiting-reply').length,
    [items],
  )

  if (detail.isPending) {
    return (
      <div className="px-4 md:px-6 pt-4 pb-6 text-[13px] text-muted" data-testid="crew-page-loading">
        {t('apps.issueRadar.views.crews.page.loading')}
      </div>
    )
  }
  if (detail.isError || !crew) {
    return (
      <div className="px-4 md:px-6 pt-4 pb-6" data-testid="crew-page-error">
        <EmptyState
          icon={<Inbox className="lucide-inline" />}
          title={t('apps.issueRadar.views.crews.page.load_failed')}
          subtitle={detail.error instanceof Error ? detail.error.message : undefined}
        />
      </div>
    )
  }

  const paused = !crew.enabled
  const newestLive = items.filter((it) => !TERMINAL_PHASES.has(it.phase)).sort(byProgressDesc)[0]
  const stateLabel = paused
    ? t('apps.issueRadar.views.crews.page.state_paused')
    : newestLive
      ? t(PHASE_LABEL_KEY[newestLive.phase])
      : t('apps.issueRadar.views.crews.page.state_idle')
  const stateVariant = paused ? 'muted' : newestLive ? phaseVariant(newestLive.phase) : 'muted'
  const atLimit = counts.open >= crew.max_open

  const openSession = async () => {
    setSessionError(null)
    try {
      // The crew's session is a normal chat slot, so this is the dashboard's own
      // switch — the same call `lib/agentSession.ts` makes to RESUME a session.
      await dispatch(switchSlot(crew.slot_key)).unwrap()
      navigate('/chat')
    } catch {
      // A 404 here means the slot was deleted; the crew opens a fresh one on its
      // next turn, so this is a message and not a retry.
      setSessionError(t('apps.issueRadar.views.crews.page.session_gone'))
    }
  }

  // Narrow-first gutter, same value as this page's own loading and error
  // states above — a gutter that changed as the read landed would shift the
  // content sideways on arrival.
  return (
    <div className="px-4 md:px-6 pt-4 pb-6 flex flex-col gap-4" data-testid="crew-page">
      {/* ── 1. header ── */}
      <div className="flex items-start gap-4">
        <CrewGhost seed={crew.avatar_seed} variant={crew.avatar_variant} size={78} blush />
        <div className="min-w-0 flex-1">
          <h2 className="text-[22px] leading-none text-text-strong truncate" data-testid="crew-name">
            {crew.name}
          </h2>
          <div className="mt-2.5 flex flex-wrap items-center gap-2">
            <Badge variant={stateVariant} className="font-body" data-testid="crew-state">{stateLabel}</Badge>
            <Badge variant="muted" data-testid="crew-agent">{crew.agent}</Badge>
            {crew.labels.map((label) => (
              <Badge key={label} variant="muted">{label}</Badge>
            ))}
            <span className="text-[13px] text-muted" title={fmtDateTime(crew.created_at)}>
              {t('apps.issueRadar.views.crews.page.on_duty_since', { date: shortDate(crew.created_at, log.nowMs) })}
            </span>
          </div>
          {paused && crew.paused_reason.trim() !== '' && (
            <div className="mt-1.5 text-[13px] text-muted" data-testid="crew-paused-reason">
              {t('apps.issueRadar.views.crews.page.paused_reason', { reason: crew.paused_reason })}
            </div>
          )}
          {sessionError !== null && (
            <div className="mt-1.5 text-[13px] text-danger" data-testid="crew-session-error">{sessionError}</div>
          )}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <Btn
            onClick={() => { void openSession() }}
            disabled={crew.slot_key === ''}
            title={crew.slot_key === '' ? t('apps.issueRadar.views.crews.page.session_unavailable') : undefined}
            data-testid="crew-session"
          >
            <CirclePlus className="lucide-inline" />
            {t('apps.issueRadar.views.crews.page.session')}
          </Btn>
          {onEdit && (
            <Btn onClick={() => { onEdit(crew) }} data-testid="crew-edit">
              <Pencil className="lucide-inline" />
              {t('apps.issueRadar.views.crews.page.edit')}
            </Btn>
          )}
          <Btn
            onClick={() => { pause.mutate(!paused) }}
            disabled={pause.isPending}
            data-testid="crew-pause-toggle"
          >
            {paused ? <Play className="lucide-inline" /> : <Pause className="lucide-inline" />}
            {paused
              ? t('apps.issueRadar.views.crews.page.resume')
              : t('apps.issueRadar.views.crews.page.pause')}
          </Btn>
        </div>
      </div>

      {/* ── 2. additional prompt ──
        * The label is its own element rather than a prefix on the text: gluing a
        * translated label onto free-form user content is a concatenation seam, and
        * this block renders the prompt VERBATIM (whitespace preserved) so a crew's
        * standing instruction reads exactly as it was written. Prose, though — it is
        * an instruction a human typed, not code — so it follows the user's Font
        * Family choice rather than being pinned to `--mono`. */}
      {crew.extra_prompt.trim() !== '' && (
        <div className="border-l-2 border-accent bg-card rounded-r-md px-4 py-3" data-testid="crew-extra-prompt">
          <div className="text-[11px] uppercase tracking-[.06em] text-muted/70 mb-1.5">
            {t('apps.issueRadar.views.crews.page.additional_prompt')}
          </div>
          <div className="text-[13px] leading-relaxed text-text whitespace-pre-wrap break-words">
            {crew.extra_prompt}
          </div>
        </div>
      )}

      {/* ── 3. stat row ──
        * Three cards, one grid track each at every width ≥ sm, so they stay equal
        * width — and `StatBlock`'s `h-full` keeps them equal height when one
        * translation's note wraps and another's does not. */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <StatBlock
          testId="stat-open-items"
          label={t('apps.issueRadar.views.crews.page.open_work_items')}
          value={t('apps.issueRadar.views.crews.page.open_of_max', {
            open: fmtNumber(counts.open), max: fmtNumber(crew.max_open),
          })}
          note={atLimit
            ? t('apps.issueRadar.views.crews.page.at_its_limit')
            : t('apps.issueRadar.views.crews.page.slots_free', { count: Math.max(0, crew.max_open - counts.open) })}
        />
        <StatBlock
          testId="stat-resolved"
          label={t('apps.issueRadar.views.crews.page.resolved_24h')}
          value={fmtNumber(resolved24h)}
          colorClass={resolved24h > 0 ? 'text-ok' : undefined}
          note={t('apps.issueRadar.views.crews.page.resolved_note')}
        />
        <StatBlock
          testId="stat-asked"
          label={t('apps.issueRadar.views.crews.page.asked_requester')}
          value={fmtNumber(askedRequester)}
          note={t('apps.issueRadar.views.crews.page.asked_note')}
        />
      </div>

      {/* ── 4. open work items ── */}
      <Card className="mb-0">
        <CardTitle>
          {t('apps.issueRadar.views.crews.page.open_work_items')}
          <span className="font-normal text-muted">{fmtNumber(openItems.length)}</span>
        </CardTitle>
        {openItems.length === 0 ? (
          <EmptyState
            icon={<ListChecks className="lucide-inline" />}
            title={t('apps.issueRadar.views.crews.page.no_open_work_items')}
            subtitle={t('apps.issueRadar.views.crews.page.no_open_work_items_hint')}
            testId="work-items-empty"
          />
        ) : (
          <table className="w-full border-collapse table-striped" data-testid="work-items-table">
            <thead>
              <tr>
                <th className={TH}>{t('apps.issueRadar.views.crews.page.col_issue')}</th>
                <th className={TH}>{t('apps.issueRadar.views.crews.page.col_phase')}</th>
                <th className={TH}>{t('apps.issueRadar.views.crews.page.col_next')}</th>
                <th className={`${TH} text-right`}>{t('apps.issueRadar.views.crews.page.col_last_progress')}</th>
              </tr>
            </thead>
            <tbody>
              {openItems.map((item) => (
                <tr key={item.number} data-testid={`work-item-${item.number}`}>
                  <td className={`${TD} whitespace-nowrap`}>
                    <a
                      href={issueUrlFor(active, item.number)}
                      target="_blank"
                      rel="noreferrer"
                      className="font-mono text-text hover:text-accent hover:underline"
                    >
                      #{item.number}
                    </a>
                  </td>
                  <td className={`${TD} whitespace-nowrap`}>
                    <Badge variant={phaseVariant(item.phase)} className="font-body">{t(PHASE_LABEL_KEY[item.phase])}</Badge>
                  </td>
                  {/* Prose, in full. No truncate/line-clamp here — see the header note. */}
                  <td className={`${TD} whitespace-pre-wrap break-words leading-relaxed`} data-testid={`work-item-next-${item.number}`}>
                    {item.next.trim() === ''
                      ? <span className="text-muted italic">{t('apps.issueRadar.views.crews.page.no_next_recorded')}</span>
                      : item.next}
                  </td>
                  <td className={`${TD} text-right whitespace-nowrap text-muted`} title={fmtDateTime(item.last_progress_at)}>
                    {fmtRelative(item.last_progress_at, { now: log.nowMs })}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {/* ── 5. work log ──
        * The window is the newest N ledger lines (`crew_store.read_events` is
        * count-limited, not day-limited), so the hint claims only what is true:
        * the last 24 hours are highlighted. */}
      <Card className="mb-0">
        <CardTitle>
          {t('apps.issueRadar.views.crews.page.work_log')}
          <span className="font-normal text-muted">{t('apps.issueRadar.views.crews.page.work_log_hint')}</span>
        </CardTitle>
        {log.recent.length === 0 && log.earlier.length === 0 ? (
          <EmptyState
            icon={<ScrollText className="lucide-inline" />}
            title={t('apps.issueRadar.views.crews.page.no_events')}
            subtitle={t('apps.issueRadar.views.crews.page.no_events_hint')}
            testId="work-log-empty"
          />
        ) : (
          <table className="w-full border-collapse table-striped" data-testid="work-log-table">
            <thead>
              <tr>
                <th className={TH}>{t('apps.issueRadar.views.crews.page.col_when')}</th>
                <th className={TH}>{t('apps.issueRadar.views.crews.page.col_issue')}</th>
                <th className={TH}>{t('apps.issueRadar.views.crews.page.col_event')}</th>
                <th className={`${TH} text-right`}>{t('apps.issueRadar.views.crews.page.col_outcome')}</th>
              </tr>
            </thead>
            <tbody>
              {log.recent.map((e) => (
                <tr key={e.id} data-testid={`work-log-row-${e.id}`} data-recent="true">
                  <td className={`${TD} border-l-2 border-l-accent whitespace-nowrap text-muted`} title={fmtDateTime(e.ts)}>
                    {fmtRelative(e.ts, { now: log.nowMs })}
                  </td>
                  <td className={`${TD} whitespace-nowrap font-mono`}>#{e.number}</td>
                  <td className={`${TD} break-words leading-relaxed`}>{e.text}</td>
                  <td className={`${TD} text-right whitespace-nowrap`}>
                    <Badge variant={kindVariant(e.kind)} className="font-body">{t(KIND_LABEL_KEY[e.kind])}</Badge>
                  </td>
                </tr>
              ))}
              {log.earlier.length > 0 && (
                <tr data-testid="work-log-earlier">
                  <td colSpan={4} className="px-2.5 pt-4 pb-1.5 border-b border-border text-[11px] uppercase tracking-[.06em] text-muted/70">
                    {t('apps.issueRadar.views.crews.page.earlier')}
                  </td>
                </tr>
              )}
              {log.earlier.map((e) => (
                <tr key={e.id} data-testid={`work-log-row-${e.id}`} data-recent="false">
                  <td className={`${TD} border-l-2 border-l-transparent whitespace-nowrap text-muted`} title={fmtDateTime(e.ts)}>
                    {shortDate(e.ts, log.nowMs)}
                  </td>
                  <td className={`${TD} whitespace-nowrap font-mono`}>#{e.number}</td>
                  <td className={`${TD} break-words leading-relaxed`}>{e.text}</td>
                  <td className={`${TD} text-right whitespace-nowrap`}>
                    <Badge variant={kindVariant(e.kind)} className="font-body">{t(KIND_LABEL_KEY[e.kind])}</Badge>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  )
}
