/** The Telemetry panel's two conversation-row tabs: Spend (credits by session,
 *  category and model, with the per-session turn drill-down) and Context
 *  (per-session occupancy), with the share and delta formatters and the
 *  occupancy and headroom colour thresholds they read. Both name and link a
 *  conversation row through the same two rules, `sessionLabel` and
 *  `linksToConversation`. */
import React, { useContext, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, Coins } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import ErrorNotice from '../components/ErrorNotice'
import InfoTip from '../components/InfoTip'
import SegmentedControl from '../components/SegmentedControl'
import { Btn, Card, CardTitle, EmptyState } from '../components/ui'
import { usePersistedBool } from '../hooks/usePersistedBool'
import { fmtDateNumeric, fmtNumber, fmtPercent, fmtTimeNumeric, fmtUnit } from '../i18n/format'
import { i18nT } from '../i18n/t'
import {
  type ContextSession,
  type Context,
  type CostRow,
  type CostConvo,
  type Cost,
  PanelWindowCtx,
  usePersistedChoice,
} from './telemetryPanelModel'
import { type Col, DataTable, Sums, Histogram } from './TelemetryPanelBlocks'

/**
 * A share that is real but rounds to zero must not read as zero.
 *
 * Computed from the credits and the window total rather than read from the
 * payload's `share_pct`, which the backend has already rounded: a model with 7
 * credits arrived as a flat `0` and rendered "0%", while its 9.5-credit
 * neighbour arrived as 0.05 and rendered "<1%". Same magnitude, two different
 * claims, and the smaller one was the lie. Deriving it here means the only
 * rounding is the one being described.
 */
const fmtSharePct = (credits: number, total: number): string => {
  if (!total) return '—'
  const ratio = credits / total
  return ratio > 0 && ratio < 0.005 ? `<${fmtPercent(0.01)}` : fmtPercent(ratio)
}

const fmtDelta = (d?: number | null): string =>
  d == null ? i18nT('pages.telemetryPanel.cost_new') : (d > 0 ? '+' : '') + fmtPercent(d / 100)

/**
 * Occupancy colour thresholds, applied ONLY to the aggregate percentiles.
 *
 * Compaction triggers at 90% of the window, so that is the danger line rather
 * than an arbitrary "nearly full" guess; 70% is the point where a long session
 * still has room but is worth watching.
 *
 * Deliberately NOT applied to the per-row peak-occupancy column. A peak is a
 * high-water mark over the session's whole life, so a busy window puts nearly
 * every row above 90% — the measured page had 6 of 8 rows in red, at which
 * point the danger colour marks "this row exists" rather than "act on this
 * row". The actionable companion number is turns-to-compaction, which IS
 * threshold-coloured below, because it is the one that can still be acted on.
 */
const occColor = (p: number): string =>
  p >= 90 ? 'var(--danger)' : p >= 70 ? 'var(--warn)' : 'var(--accent)'

/**
 * Turns remaining before the next compaction. Unlike a peak, this is a forecast
 * a reader can act on, so it carries the page's only per-row alarm: under two
 * turns of headroom the next turn may compact, under five it is imminent.
 */
const headroomColor = (turns: number): string | undefined =>
  turns <= 2 ? 'var(--danger)' : turns <= 5 ? 'var(--warn)' : undefined

// ── Spend ──────────────────────────────────────────────────────

type SpendGroup = 'session' | 'category' | 'model'

/** One row of GET /api/usage/turns — the same shard rows the totals above
 *  aggregate, returned individually. Every numeric field is optional: the
 *  reader drops what it cannot vouch for rather than inventing zeros. */
type TurnUsageRow = {
  ts: string
  model: string
  credits?: number
  cost?: number
  duration_ms?: number
  context_used?: number
  context_window?: number
}

const DRILL_TH = 'text-left font-normal text-[10px] text-muted uppercase tracking-wide px-2 py-1'

const DRILL_TD = 'px-2 py-[3px] font-mono text-[11px] tabular-nums'

// Same narrow-viewport convention as the parent table's columns: lower-priority
// columns yield before the table overflows. Credits is the column the surface
// exists for and never hides; the model column truncates instead of pushing.
const DRILL_HIDE_TIME = 'max-[720px]:hidden'

const DRILL_HIDE_DURATION = 'max-[720px]:hidden'

const DRILL_HIDE_CONTEXT = 'max-[480px]:hidden'

/**
 * The per-turn rows behind one session's spend total. The aggregate answers
 * "which session cost the most"; this answers "which TURNS did it" — a model
 * switch mid-session or one runaway turn is invisible in an average.
 */
function SessionTurnsDrilldown({ slot }: { slot: string }) {
  const { range } = useContext(PanelWindowCtx)
  const q = useQuery<{ turns: TurnUsageRow[] }>({
    queryKey: ['usage-turns', slot, range],
    queryFn: () => api.usageTurns(slot, range),
  })
  if (q.isLoading) {
    return <div className="text-[11px] text-muted px-2 py-1">{i18nT('pages.telemetryPanel.turns_loading')}</div>
  }
  if (q.isError) {
    // A failed fetch must not read as "no rows": asserting the data does not
    // exist when the request failed sends the reader away with a wrong fact
    // and no reason to retry.
    // askAgent on: a read of already-recorded usage rows; the drilldown holds
    // nothing editable. Retry stays beside it — a different next step.
    return (
      <div className="flex items-center gap-2 px-2 py-1">
        <ErrorNotice
          variant="inline"
          message={i18nT('pages.telemetryPanel.turns_error')}
          askAgent
          testId="telemetry-turns-error"
        />
        <Btn className="px-1.5 py-0.5 text-[11px]" onClick={() => void q.refetch()}>
          {i18nT('pages.telemetryPanel.turns_retry')}
        </Btn>
      </div>
    )
  }
  const turns = q.data?.turns ?? []
  if (turns.length === 0) {
    return <div className="text-[11px] text-muted px-2 py-1">{i18nT('pages.telemetryPanel.turns_empty')}</div>
  }
  return (
    <table className="w-full border-collapse">
      <thead>
        <tr className="border-b border-border/60">
          <th className={DRILL_TH}>{i18nT('pages.telemetryPanel.turn_col')}</th>
          <th className={`${DRILL_TH} ${DRILL_HIDE_TIME}`}>{i18nT('pages.telemetryPanel.time_col')}</th>
          <th className={DRILL_TH}>{i18nT('pages.telemetryPanel.model_col')}</th>
          <th className={`${DRILL_TH} text-right`}>{i18nT('pages.telemetryPanel.credits_col')}</th>
          <th className={`${DRILL_TH} text-right ${DRILL_HIDE_DURATION}`}>{i18nT('pages.telemetryPanel.duration_col')}</th>
          <th className={`${DRILL_TH} text-right ${DRILL_HIDE_CONTEXT}`}>{i18nT('pages.telemetryPanel.context_col')}</th>
        </tr>
      </thead>
      <tbody>
        {turns.map((t, i) => (
          <tr key={`${t.ts}-${i}`} className="border-b border-border/40 last:border-b-0">
            <td className={`${DRILL_TD} text-muted`}>{fmtNumber(i + 1)}</td>
            <td className={`${DRILL_TD} ${DRILL_HIDE_TIME}`}>
              {fmtDateNumeric(t.ts)} {fmtTimeNumeric(t.ts)}
            </td>
            {/* Model ids are data, not copy — rendered verbatim like the model
                column one table up, truncated so a long id cannot widen the
                narrow layout the hidden columns just paid for. */}
            <td className={`${DRILL_TD} text-muted`}>
              <span className="block max-w-[160px] truncate" title={t.model}>
                {t.model || '—'}
              </span>
            </td>
            <td className={`${DRILL_TD} text-right`}>
              {t.credits !== undefined ? fmtNumber(t.credits, { maximumFractionDigits: 2 }) : '—'}
            </td>
            <td className={`${DRILL_TD} text-right text-muted ${DRILL_HIDE_DURATION}`}>
              {t.duration_ms !== undefined
                ? fmtUnit(t.duration_ms / 1000, 'second', { maximumFractionDigits: 1 })
                : '—'}
            </td>
            <td className={`${DRILL_TD} text-right text-muted ${DRILL_HIDE_CONTEXT}`}>
              {t.context_used && t.context_window ? fmtPercent(t.context_used / t.context_window) : '—'}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/**
 * Session / category / model are the same credits regrouped, so they are a
 * group-by over one table rather than three sections. As three sections they
 * were three separately-scrolled bar lists whose columns did not align and
 * whose empty rows — a model with a 0.4% share still drew a full-width rail —
 * made half the section blank.
 */
function convoCols(navigable: string): Col<CostConvo>[] {
  return [
    {
      key: 'name',
      label: i18nT('pages.telemetryPanel.session_col'),
      left: true,
      sort: v => v.title ?? v.slot,
      render: v =>
        // The rule itself is `linksToConversation` -- shared with the Context
        // table's session column so the two cannot drift apart. The tooltip
        // differs on purpose: here it reveals the underlying slot, which is
        // otherwise unreachable on this tab, whereas Context shows the slot as
        // the row's text when no conversation joined.
        linksToConversation(v, navigable) ? (
          <Link
            to={`/chat?sid=${encodeURIComponent(v.slot)}`}
            className="block max-w-[320px] truncate text-[var(--accent)] hover:underline"
            title={v.title}
          >
            {v.title}
          </Link>
        ) : (
          <span className="block max-w-[320px] truncate text-muted" title={v.slot}>
            {v.title ??
              i18nT('pages.telemetryPanel.untitled_conversation_on', {
                date: fmtDateNumeric(v.first_ts * 1000),
              })}
          </span>
        ),
    },
    {
      key: 'category',
      label: i18nT('pages.telemetryPanel.category_col'),
      // Names which surface OWNS the session, from the session key. The Context
      // tab's own origin column reads the token row's `surface` field instead,
      // which answers a different question — which code path RAN one turn — so
      // the same session can legitimately read `bg` here and `heartbeat` there.
      // The tip is what carries that, because the values alone cannot.
      tip: i18nT('pages.telemetryPanel.category_col_tip'),
      left: true,
      // Rendered verbatim: these are backend enum values (`dashboard`, `bg`,
      // `telegram`, `slack`), i.e. data, not copy to translate — the same
      // treatment model names get one column over.
      sort: v => v.category,
      // Every sibling column has a breakpoint; without one this pushed Credits —
      // the column the page exists for — off a narrow viewport.
      hide: 'max-[900px]:hidden',
      render: v => (
        <span className="text-muted font-mono text-[11.5px]">{categoryLabel(v.category)}</span>
      ),
    },
    {
      key: 'credits',
      label: i18nT('pages.telemetryPanel.credits_col'),
      sort: v => v.credits,
      render: v => fmtNumber(v.credits),
    },
    {
      key: 'turns',
      label: i18nT('pages.telemetryPanel.turns_col'),
      sort: v => v.turns,
      render: v => fmtNumber(v.turns),
    },
    {
      key: 'per_turn',
      // Derived here rather than read from the payload, which carries a per-turn
      // figure only for the window as a whole. Rounded to the same single
      // decimal the backend uses for its own averages, so the column and the
      // total below it do not quote the same quantity at two precisions.
      label: i18nT('pages.telemetryPanel.per_turn_col'),
      sort: v => (v.turns ? v.credits / v.turns : null),
      render: v => (v.turns ? fmtNumber(v.credits / v.turns, { maximumFractionDigits: 1 }) : '—'),
    },
    {
      key: 'peak',
      label: i18nT('pages.telemetryPanel.peak_ctx_col'),
      sort: v => v.peak_pct,
      render: v => fmtPercent(v.peak_pct / 100),
    },
    {
      key: 'span',
      label: i18nT('pages.telemetryPanel.span_col'),
      hide: 'max-[720px]:hidden',
      sort: v => v.span_days,
      render: v => fmtUnit(v.span_days, 'day'),
    },
    {
      key: 'growth',
      label: i18nT('pages.telemetryPanel.growth_col'),
      hide: 'max-[900px]:hidden',
      sort: v => v.growth_pct_per_turn ?? null,
      render: v =>
        v.growth_pct_per_turn == null
          ? '—'
          : i18nT('pages.telemetryPanel.growth_per_turn', { rate: fmtNumber(v.growth_pct_per_turn) }),
    },
    {
      key: 'headroom',
      label: i18nT('pages.telemetryPanel.to_compaction_col'),
      hide: 'max-[900px]:hidden',
      sort: v => v.turns_to_compaction ?? null,
      color: v => (v.turns_to_compaction == null ? undefined : headroomColor(v.turns_to_compaction)),
      render: v =>
        v.turns_to_compaction == null
          ? '—'
          : i18nT('pages.telemetryPanel.turns_to_compaction', {
              count: v.turns_to_compaction,
              n: fmtNumber(v.turns_to_compaction),
            }),
    },
  ]
}

/**
 * `bg` is a coined abbreviation with no external referent, so it gets a translated
 * label; a transport name (`dashboard`, `telegram`, `slack`) is a proper noun the
 * reader already knows and is shown as the backend sent it.
 *
 * This lives in ONE place because the category is rendered by two different
 * surfaces — the Session table's column and the Group-by-Category table — and
 * mapping it in only one produced two labels for the same value.
 */
function categoryLabel(name: string): string {
  return name === 'bg' ? i18nT('pages.telemetryPanel.category_bg') : name
}

function shareCols(first: string, total: number, firstTip?: string): Col<CostRow>[] {
  return [
    {
      key: 'name',
      label: first,
      // The grouped view asks the same question of the same values as the
      // session table's column, so it earns the same tip when that is the
      // grouping in force; grouping by model leaves it unset.
      tip: firstTip,
      left: true,
      sort: r => r.name,
      render: r => categoryLabel(r.name),
    },
    {
      key: 'credits',
      label: i18nT('pages.telemetryPanel.credits_col'),
      sort: r => r.credits,
      render: r => fmtNumber(r.credits),
    },
    {
      key: 'turns',
      label: i18nT('pages.telemetryPanel.turns_col'),
      sort: r => r.turns,
      render: r => fmtNumber(r.turns),
    },
    {
      key: 'per_turn',
      label: i18nT('pages.telemetryPanel.per_turn_col'),
      sort: r => r.per_turn,
      render: r => fmtNumber(r.per_turn),
    },
    {
      key: 'share',
      label: i18nT('pages.telemetryPanel.share_col'),
      sort: r => r.credits,
      render: r => fmtSharePct(r.credits, total),
    },
  ]
}

const SPEND_GROUPS = ['session', 'category', 'model'] as const

/**
 * How many rows a spend block plots before the remainder folds into one row.
 *
 * Five keeps the two grouping blocks the same height beside each other. The
 * session list gets eight because it is the block a reader scans for a name,
 * and five names is too few to find one in.
 */
const SPEND_GROUP_ROWS = 5

const SPEND_SESSION_ROWS = 8

type SpendBar = { label: React.ReactNode; credits: number; title?: string }

/**
 * A titled bar block: heading, its own empty state, and an optional footnote.
 *
 * Credits render as bars on one shared scale, longest first, so the block answers
 * "how much of the window landed here" by shape rather than by reading five
 * numbers and dividing. The figure stays beside the bar because a bar cannot be
 * quoted. The scale is per block: session totals and grouping totals are
 * different populations — one session against every session's origin — so a
 * shared axis would flatten whichever block holds the smaller numbers.
 *
 * The heading is a flex row because `InfoTip` renders a `display: flex` button.
 * In a normal-flow block that button is a block-level box, so it drops onto a
 * line of its own and the bare "?" reads as a stray control.
 *
 * `note` describes the rows, so it renders only when there are rows; under an
 * empty state it is a caption for nothing.
 */
function SpendBlock({
  title,
  tip,
  rows,
  labelClass,
  note,
}: {
  title: string
  tip?: string
  rows: SpendBar[]
  labelClass: string
  note?: string
}) {
  return (
    <div className="min-w-0">
      <div className="flex items-center gap-1 text-[10px] text-muted uppercase tracking-wide mb-1.5">
        {title}
        {tip && <InfoTip text={tip} />}
      </div>
      {rows.length > 0 ? (
        <>
          <Histogram
            rows={rows.map((r, i) => ({ key: String(i), label: r.label, count: r.credits, title: r.title }))}
            labelClass={labelClass}
            valueClass="w-14"
            barDecorative
          />
          {note && <div className="text-[10px] text-muted mt-1.5">{note}</div>}
        </>
      ) : (
        <EmptyState
          icon={<Coins className="lucide-inline" />}
          title={i18nT('pages.telemetryPanel.no_spend_recorded')}
        />
      )}
    </div>
  )
}

/**
 * One grouping's rows as bars, with everything past `limit` summed into a single
 * trailing row.
 *
 * A grouping carries as many names as the window saw, most of them a bar a pixel
 * wide. Folding the tail keeps the block scannable while every credit stays on
 * screen, so the block and the table below it account for the same total.
 */
function groupBars(rows: CostRow[], limit: number): SpendBar[] {
  const sorted = [...rows].sort((a, b) => b.credits - a.credits)
  const bars: SpendBar[] = sorted.slice(0, limit).map(r => ({
    credits: r.credits,
    // Monospace for the same reason the table's cells use it: a model id and a
    // session-origin enum are tokens to compare character by character. The
    // title reveals the raw value behind a translated label.
    label: (
      <span className="block truncate font-mono text-muted" title={r.name}>
        {categoryLabel(r.name)}
      </span>
    ),
  }))
  const rest = sorted.slice(limit)
  if (rest.length > 0) {
    bars.push({
      credits: rest.reduce((sum, r) => sum + r.credits, 0),
      // The folded names themselves, so the row says WHAT it folded. The row is
      // inert, and a fold that names nothing reads as something to click.
      title: rest.map(r => categoryLabel(r.name)).join(', '),
      label: (
        <span className="block truncate text-muted">
          {i18nT('pages.telemetryPanel.other_group', { n: fmtNumber(rest.length) })}
        </span>
      ),
    })
  }
  return bars
}

/**
 * The biggest-spending sessions as bars.
 *
 * Named and linked through `sessionLabel` and `linksToConversation`, the same two
 * rules the table's own session column uses, so the block and the table cannot
 * disagree about what a row is called or whether it can be opened.
 */
function sessionBars(convos: CostConvo[], navigable: string, limit: number): SpendBar[] {
  return [...convos]
    .sort((a, b) => b.credits - a.credits)
    .slice(0, limit)
    .map(v => ({
      credits: v.credits,
      label: linksToConversation(v, navigable) ? (
        <Link
          to={`/chat?sid=${encodeURIComponent(v.slot)}`}
          className="block truncate text-[var(--accent)] hover:underline"
          title={v.title}
        >
          {v.title}
        </Link>
      ) : (
        // The title attribute holds the slot, which this block has no column to
        // show: an inert row still has to be identifiable.
        <span className="block truncate text-muted" title={v.slot}>
          {sessionLabel(v, v.slot)}
        </span>
      ),
    }))
}

export function SpendTab({ c }: { c: Cost }) {
  const win = useContext(PanelWindowCtx)
  const [group, setGroup] = usePersistedChoice<SpendGroup>(
    'telemetry:spend-group',
    SPEND_GROUPS,
    'session',
  )
  // Closed on first paint. The table answers "which row exactly", which is a
  // second question: the blocks above it already say where the credits went, and
  // opening on 45 rows puts the answer below the fold.
  const [tableOpen, setTableOpen] = usePersistedBool('telemetry:spend-table-open', false)
  const bands = c.context_bands
  // Shown as "8 / 252" when the payload clamps the list. With the count alone, a
  // reader who looks for a conversation outside the slice reads "no rows match"
  // as "it spent nothing"; the ratio states the truncation. The pair is equal on
  // a full payload, where the plain count is the honest form.
  const sessionsValue =
    c.conversations.length === c.conversation_count
      ? fmtNumber(c.conversation_count)
      : `${fmtNumber(c.conversations.length)} / ${fmtNumber(c.conversation_count)}`
  return (
    <Card className="mb-4">
      <CardTitle>
        {i18nT('pages.telemetryPanel.credits')}
        <InfoTip text={win.measured} />
        <span className="ml-auto text-[12px] text-muted font-mono tabular-nums font-normal">{win.period}</span>
      </CardTitle>
      {/* In the open, not only in the title's InfoTip. The spend and OTEL
          numbers come from different stores over different windows, so a
          950-turn spend total sits beside a 685-turn throughput; without the
          source stated where both are visible, fewer turns over a longer
          window reads as broken data rather than as two measurements. */}
      <div className="text-[10px] text-muted -mt-2 mb-2.5">{win.measured}</div>
      <Sums
        items={[
          {
            label: i18nT('pages.telemetryPanel.credits_col'),
            value: fmtNumber(c.credits),
            color: 'var(--accent)',
            // The change rides on the total it describes. A percentage standing
            // on its own cannot say whether it moved 12 credits or 1,200, and
            // the label comes first so the "no prior spend" case reads as a
            // sentence rather than as a number's unit.
            //
            // Both lines live in `note`, in this order, so "vs previous period"
            // introduces the pair below it. This tile's label is "Credits", which
            // lends the prior pair no period sense of its own: sitting directly
            // under the current total, an unqualified "credits 9,400" reads as a
            // component of it rather than as the window before.
            note: (
              <>
                <div className="text-[10px] text-muted mt-0.5">
                  {c.delta_pct != null && <>{i18nT('pages.telemetryPanel.vs_previous_period')} </>}
                  <span className="text-text tabular-nums">{fmtDelta(c.delta_pct)}</span>
                </div>
                <div className="text-[10px] text-muted">
                  {i18nT('pages.telemetryPanel.prior_credits_turns', {
                    credits: fmtNumber(c.prior_credits),
                    turns: fmtNumber(c.prior_turns),
                  })}
                </div>
              </>
            ),
          },
          {
            label: i18nT('pages.telemetryPanel.per_turn_col'),
            value: fmtNumber(c.per_turn),
            sub: i18nT('pages.telemetryPanel.was_value', { value: fmtNumber(c.prior_per_turn) }),
          },
          {
            label: i18nT('pages.telemetryPanel.turns_col'),
            value: fmtNumber(c.turns),
          },
          {
            label: i18nT('pages.telemetryPanel.priciest_turn'),
            value: fmtNumber(c.priciest.credits),
            // The unit, because a bare number under "Priciest turn" reads as a
            // turn's ordinal just as readily as its cost.
            sub: i18nT('pages.telemetryPanel.credits_col'),
          },
        ]}
      />
      {/* Both groupings at once. As a group-by over one table they were mutually
          exclusive: seeing which origin spent the credits meant giving up the
          model split, so no single screen answered where the window went.

          Side by side only from `xl`. Each row spends a fixed label column plus a
          fixed figure column, and in a half-width column below that the bar track
          is what absorbs the shortfall — it reaches nearly zero, which removes the
          compare-by-shape these blocks exist for. Below `xl` they stack, so both
          still answer without a click and both keep their bars. */}
      <div className="grid gap-x-8 gap-y-4 mt-4 xl:grid-cols-2">
        <SpendBlock
          title={i18nT('pages.telemetryPanel.credits_by_origin')}
          tip={i18nT('pages.telemetryPanel.category_col_tip')}
          rows={groupBars(c.by_category, SPEND_GROUP_ROWS)}
          labelClass="w-32"
        />
        <SpendBlock
          title={i18nT('pages.telemetryPanel.credits_by_model')}
          rows={groupBars(c.by_model, SPEND_GROUP_ROWS)}
          labelClass="w-32"
        />
      </div>
      <div className="mt-4">
        <SpendBlock
          title={i18nT('pages.telemetryPanel.top_sessions')}
          rows={sessionBars(c.conversations, c.navigable_category, SPEND_SESSION_ROWS)}
          labelClass="w-[46%]"
        />
      </div>
      <div className="border-t border-border mt-4 pt-3">
        <Btn
          type="button"
          className="flex items-center gap-2 px-1 py-0.5 border-none text-muted hover:text-text rounded"
          aria-expanded={tableOpen}
          onClick={() => setTableOpen(!tableOpen)}
        >
          {tableOpen ? (
            <ChevronDown className="lucide-inline" size={14} />
          ) : (
            <ChevronRight className="lucide-inline" size={14} />
          )}
          <span className="text-[10px] uppercase tracking-wide">
            {i18nT('pages.telemetryPanel.full_table')}
          </span>
          {/* The row count belongs on the closed header: it is what tells a
              reader whether opening the table is worth it. */}
          <span className="text-[10px] uppercase tracking-wide">
            {i18nT('pages.telemetryPanel.sessions_col')}{' '}
            <span className="font-mono tabular-nums text-text normal-case">{sessionsValue}</span>
          </span>
        </Btn>
        {tableOpen && (
          <div className="mt-2.5">
            <div className="flex flex-wrap items-center gap-2 mb-2">
              <span className="text-[10px] text-muted uppercase tracking-wide">
                {i18nT('pages.telemetryPanel.group_by')}
              </span>
              <SegmentedControl<SpendGroup>
                collapse={false}
                value={group}
                onChange={setGroup}
                segments={[
                  { key: 'session', label: i18nT('pages.telemetryPanel.session_col') },
                  { key: 'category', label: i18nT('pages.telemetryPanel.category_col') },
                  { key: 'model', label: i18nT('pages.telemetryPanel.model_col') },
                ]}
              />
            </div>
            {group === 'session' ? (
              <DataTable<CostConvo>
                rows={c.conversations}
                key="telemetry-spend-conversation"
                tableId="telemetry-spend-conversation"
                cols={convoCols(c.navigable_category)}
                rowKey={v => v.slot}
                defaultSort="credits"
                emptyTitle={i18nT('pages.telemetryPanel.no_spend_recorded')}
                renderExpanded={v => <SessionTurnsDrilldown slot={v.slot} />}
              />
            ) : (
              <DataTable<CostRow>
                rows={group === 'model' ? c.by_model : c.by_category}
                key="telemetry-spend-share"
                tableId="telemetry-spend-share"
                cols={shareCols(
                  group === 'model'
                    ? i18nT('pages.telemetryPanel.model_col')
                    : i18nT('pages.telemetryPanel.category_col'),
                  c.credits,
                  group === 'model' ? undefined : i18nT('pages.telemetryPanel.category_col_tip'),
                )}
                rowKey={r => r.name}
                defaultSort="credits"
                emptyTitle={i18nT('pages.telemetryPanel.no_spend_recorded')}
              />
            )}
          </div>
        )}
      </div>
      {bands.length > 0 && (
        // The former "cost by context size" section, which was five rows of bar
        // to carry five numbers. Occupancy is already a column on the rows
        // above; what the section actually added was the shape of the
        // relationship, and that fits on one line.
        <div className="text-[10px] text-muted mt-3 leading-relaxed">
          <span className="uppercase tracking-wide">
            {i18nT('pages.telemetryPanel.mean_credits_by_occupancy')}
          </span>{' '}
          {bands.map((b, i) => (
            <span key={b.label}>
              {i > 0 && ' · '}
              <span className="font-mono">{b.label}</span>{' '}
              <span className="text-text tabular-nums">{fmtNumber(b.mean_credits)}</span>{' '}
              <span className="tabular-nums">
                ({i18nT('pages.telemetryPanel.sample_count', { count: fmtNumber(b.turns) })})
              </span>
            </span>
          ))}
        </div>
      )}
    </Card>
  )
}

// ── Context ────────────────────────────────────────────────────

/**
 * Rows are the per-session occupancy samples the API has always returned and
 * the page never rendered: `context.sessions[]` was computed, serialised and
 * dropped on the floor, so the only way to see which conversation was near
 * compaction was to read the spend ranking and hope the two windows agreed.
 */
/**
 * What a Context row calls itself, given the spend row it joined (or none).
 *
 * Three outcomes, in the order the reader benefits from: the conversation's title;
 * the dated "untitled conversation" wording Spend already uses when a joined row has
 * no title (a title exists only while a conversation is open, so a closed one joins
 * and still has nothing to show); and the raw slot when nothing joined at all, which
 * is the only case with no conversation record to name.
 *
 * The middle case matters for cross-tab reading: the same conversation read
 * "Untitled conversation on <date>" in Spend and a bare slot here, so its rows could
 * not be matched between the two tabs. It needs no new copy - the key exists.
 */
/**
 * Whether a conversation row should render as a link to the conversation.
 *
 * TWO conditions, not one, and the rule lives here rather than in each cell
 * because both tables ask it and a silent divergence between them is exactly the
 * cross-tab inconsistency this column set is trying to remove.
 *
 * A title alone is not enough: ChatPage resolves `?sid` against the live
 * DASHBOARD slot list, so a Telegram thread -- a real conversation, often titled
 * -- would render as a link that lands on `Session "..." not found` after a 5s
 * timeout. And a dashboard row with no title is untitled BECAUSE its slot is
 * gone, so it has nothing to resolve either.
 *
 * The `!!navigable` guard makes it fail CLOSED: without it a payload missing
 * `navigable_category` would satisfy the comparison for every row that also has
 * no category, and link all of them.
 */
function linksToConversation(
  convo: Pick<CostConvo, 'title' | 'category'> | undefined,
  navigable: string | undefined,
): boolean {
  return !!convo?.title && !!navigable && convo.category === navigable
}

function sessionLabel(convo: CostConvo | undefined, slot: string): string {
  if (convo?.title) return convo.title
  if (convo) {
    return i18nT('pages.telemetryPanel.untitled_conversation_on', {
      date: fmtDateNumeric(convo.first_ts * 1000),
    })
  }
  return slot
}

function sessionCols(
  convoFor: (slot: string) => CostConvo | undefined,
  navigable: string | undefined,
): Col<ContextSession>[] {
  return [
    {
      key: 'slot',
      label: i18nT('pages.telemetryPanel.session_col'),
      left: true,
      // Sorted on what the row SHOWS. Sorting on the slot while displaying a
      // title puts the column in an order the reader cannot see.
      sort: s => sessionLabel(convoFor(s.slot), s.slot),
      render: s => {
        // The occupancy payload carries no title - it never has - so the whole spend
        // row is joined on the slot the two share. The fallback is the raw slot, and
        // it is load-bearing rather than defensive: the two measurements come from
        // the same store over DIFFERENT windows, so a conversation sampled for
        // occupancy can legitimately have no spend row. Either way the row still has
        // to identify itself.
        const convo = convoFor(s.slot)
        const label = sessionLabel(convo, s.slot)
        // Same rule as Spend, held in one place. Sharing the affordance is the
        // point: this tab's task is "find which conversation is near compaction
        // and go deal with it", and a title that is a link one tab over and inert
        // here dead-ends exactly that.
        //
        // No explicit type size on these: TXT_CELL already sets 12.5px for every
        // left-aligned cell in these tables.
        if (linksToConversation(convo, navigable)) {
          return (
            <Link
              to={`/chat?sid=${encodeURIComponent(s.slot)}`}
              className="block max-w-[260px] truncate text-[var(--accent)] hover:underline"
              title={convo?.title}
            >
              {convo?.title}
            </Link>
          )
        }
        return convo ? (
          // Muted, like every non-link title on Spend: the same conversation should
          // read the same weight on both tabs, and the contrast with the accent link
          // is what makes "this one is clickable" legible at a glance.
          <span className="block max-w-[260px] truncate text-muted" title={label}>
            {label}
          </span>
        ) : (
          // Monospace only for the raw id: an id is a token to compare
          // character by character, a title is prose.
          <span className="block max-w-[260px] truncate font-mono text-[11.5px]" title={s.slot}>
            {s.slot}
          </span>
        )
      },
    },
    {
      key: 'peak',
      label: i18nT('pages.telemetryPanel.peak_ctx_col'),
      sort: s => s.peak_pct,
      render: s => fmtPercent(s.peak_pct / 100),
    },
    {
      key: 'used',
      label: i18nT('pages.telemetryPanel.used_tokens_col'),
      sort: s => s.used,
      render: s => fmtNumber(s.used),
    },
    {
      key: 'window',
      label: i18nT('pages.telemetryPanel.window_tokens_col'),
      hide: 'max-[720px]:hidden',
      sort: s => s.window,
      render: s => fmtNumber(s.window),
    },
    {
      key: 'turns',
      label: i18nT('pages.telemetryPanel.turns_col'),
      sort: s => s.turns,
      render: s => fmtNumber(s.turns),
    },
    {
      key: 'model',
      label: i18nT('pages.telemetryPanel.model_col'),
      left: true,
      hide: 'max-[900px]:hidden',
      sort: s => s.model,
      render: s => <span className="font-mono text-[11.5px]">{s.model}</span>,
    },
    {
      key: 'agent',
      label: i18nT('pages.telemetryPanel.agent_col'),
      left: true,
      hide: 'max-[1100px]:hidden',
      sort: s => s.agent,
      render: s => <span className="font-mono text-[11.5px]">{s.agent}</span>,
    },
    {
      key: 'surface',
      label: i18nT('pages.telemetryPanel.surface_col'),
      // Read from the turn's own row, so it names the code path that RAN the
      // turn. A monitor nudge or a webhook turn says so here while its credits
      // stay booked to the conversation that owns the session, which is what
      // the Spend tab's own origin column reports.
      tip: i18nT('pages.telemetryPanel.surface_col_tip'),
      left: true,
      hide: 'max-[1100px]:hidden',
      sort: s => s.surface,
      render: s => <span className="font-mono text-[11.5px]">{s.surface}</span>,
    },
  ]
}

export function ContextTab({
  c,
  convos,
  navigable,
}: {
  c: Context
  convos?: CostConvo[]
  navigable?: string
}) {
  // The whole spend row is kept, not just its title: the row is what decides
  // linkability (category vs the payload's navigable category) and what supplies the
  // dated untitled wording. Built once per render rather than scanned per row - the
  // spend payload is capped but still hundreds of conversations on a busy install,
  // and the table sorts, which would re-scan it for every comparison.
  const convoFor = useMemo(() => {
    const bySlot = new Map<string, CostConvo>()
    for (const v of convos ?? []) bySlot.set(v.slot, v)
    return (slot: string) => bySlot.get(slot)
  }, [convos])
  const win = useContext(PanelWindowCtx)
  return (
    <Card className="mb-4">
      <CardTitle>
        {i18nT('pages.telemetryPanel.context_window')}
        <InfoTip text={win.measured} />
        <span className="ml-auto text-[12px] text-muted font-mono tabular-nums font-normal">{win.period}</span>
      </CardTitle>
      <div className="text-[10px] text-muted -mt-2 mb-2.5">{win.measured}</div>
      <DataTable<ContextSession>
        rows={c.sessions ?? []}
          key="telemetry-context"
          tableId="telemetry-context"
        cols={sessionCols(convoFor, navigable)}
        rowKey={s => s.slot}
        defaultSort="peak"
        emptyTitle={i18nT('pages.telemetryPanel.no_occupancy_samples')}
      />
      <Sums
        items={[
          {
            label: i18nT('pages.telemetryPanel.occupancy_p50'),
            value: fmtNumber(c.p50_pct),
            unit: '%',
            color: occColor(c.p50_pct),
            sub: i18nT('pages.telemetryPanel.turns_measured', { count: c.turns, n: fmtNumber(c.turns) }),
          },
          {
            label: i18nT('pages.telemetryPanel.occupancy_p90'),
            value: fmtNumber(c.p90_pct),
            unit: '%',
            color: occColor(c.p90_pct),
          },
          {
            // Uncoloured, unlike the two percentiles beside it. This is the
            // high-water mark over every session in the window, so on any busy
            // install it sits at or near 100% permanently — a red that never
            // changes is wallpaper, not an alarm. p50 and p90 keep the colour
            // because they can actually move.
            label: i18nT('pages.telemetryPanel.peak_occupancy'),
            value: fmtNumber(c.max_pct),
            unit: '%',
          },
        ]}
      />
    </Card>
  )
}
