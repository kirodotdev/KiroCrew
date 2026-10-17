/** The Telemetry panel's shared building blocks: the sortable data table
 *  (`DataTable`, its column model `Col` and header cell), the summary strip
 *  (`Sums`) and the bucket histogram the Spend and Startup tabs draw with. */
import React, { useState } from 'react'
import { Activity, ChevronDown, ChevronRight, ChevronUp } from 'lucide-react'
import InfoTip from '../components/InfoTip'
import { Btn, EmptyState } from '../components/ui'
import { useSortableTable } from '../hooks/useSortableTable'
import { compareText, fmtNumber } from '../i18n/format'
import { i18nT } from '../i18n/t'

// ── Sortable table ─────────────────────────────────────────────
//
// The sort MODEL is the shared `useSortableTable` hook the other five tables on
// the dashboard use, so a chosen sort persists per table exactly as it does on
// Hooks, Schedule, Cron, MCP and Memory.
//
// The header markup is local rather than `SortableHeader` for two reasons the
// shared component cannot express without changing those five pages: this is
// the first numeric-heavy table, so most columns must right-align (SortableHeader
// hard-codes `text-left`, and its className is appended rather than merged, so
// a competing `text-right` would resolve by stylesheet order), and it carries
// the ▾/▴ caret plus accent-coloured active column from the Session & Task
// Memory card. Giving SortableHeader a caret is worth doing for all six tables,
// but as its own change.
//
// The comparison is also local, because the hook's direction model negates by
// swapping arguments — which cannot express "nulls last in BOTH directions".
// That property is load-bearing here: `unknown` is not a small value, and
// flipping a column must not promote unmeasured rows to the top.
const NUM_CELL = 'px-3 py-1.5 text-right font-mono text-[12.5px] tabular-nums whitespace-nowrap'

const TXT_CELL = 'px-3 py-1.5 text-left text-[12.5px]'

const HEAD_BASE = 'px-3 py-1.5 text-[11px] font-medium text-muted whitespace-nowrap'

export type Col<R> = {
  key: string
  label: string
  /** Text columns render left-aligned and collate with compareText. */
  left?: boolean
  /** Sort value. `null` sorts last in both directions — unknown is not small. */
  sort: (r: R) => number | string | null
  render: (r: R) => React.ReactNode
  color?: (r: R) => string | undefined
  /** Responsive drop class, applied to the header and its cells together. */
  hide?: string
  /**
   * Hover text on the column head, for a column whose VALUES cannot say where
   * they came from. Two tabs of this page each carry a session-origin column
   * derived a different way, and a bare noun in the head leaves a reader no way
   * to tell which question a column answers.
   */
  tip?: string
}

/**
 * Defined at module scope on purpose. As a function declared INSIDE DataTable it
 * was a new component type on every render, so React unmounted and re-mounted
 * every `<th>` whenever the sort changed — which detached the very button the
 * user had just clicked, and a detached node's click never reaches React's root
 * listener. The visible symptom was a header that sorted once and then went
 * dead.
 */
function HeadCell({
  label,
  left,
  hide,
  active,
  desc,
  onToggle,
  tip,
}: {
  label: string
  left?: boolean
  hide?: string
  active: boolean
  desc: boolean
  onToggle: () => void
  tip?: string
}) {
  return (
    <th
      className={`${HEAD_BASE} ${left ? 'text-left' : 'text-right'} ${hide ?? ''}`}
      aria-sort={active ? (desc ? 'descending' : 'ascending') : 'none'}
    >
      {/* One line, whatever the column width: the tip is a 16px circle after a
          short label, and letting it wrap put the glyph on a row of its own and
          made every tipped header two lines tall. */}
      <span className="inline-flex items-center gap-1 whitespace-nowrap align-middle">
      <Btn
        type="button"
        onClick={onToggle}
        // `Btn` rather than a raw <button>, and a Lucide chevron rather than a
        // ▾/▴ glyph: a text symbol ignores `currentColor` and the theme tokens,
        // so it would not follow the accent colour the active column is marked
        // with, and it renders differently on each platform.
        //
        // The overrides strip Btn's border, fill and 13px body type — a boxed
        // control in every column head would read as an action rather than a
        // label. twMerge resolves them over the defaults.
        className={`border-transparent bg-transparent px-0 py-0 gap-1 text-[11px] font-medium ${
          active ? 'text-accent' : 'text-muted'
        }`}
      >
        {label}
        {active &&
          (desc ? (
            <ChevronDown size={12} aria-hidden="true" className="lucide-inline" />
          ) : (
            <ChevronUp size={12} aria-hidden="true" className="lucide-inline" />
          ))}
      </Btn>
      {/* Outside the Btn, never inside it: nesting the tip's own control in the
          sort button would make one click both sort the table and open the tip,
          and a button inside a button is invalid markup a screen reader cannot
          announce as two actions. */}
      {tip ? <InfoTip text={tip} placement="top" /> : null}
      </span>
    </th>
  )
}

export function DataTable<R>({
  rows,
  cols,
  rowKey,
  tableId,
  defaultSort,
  emptyTitle,
  renderExpanded,
}: {
  rows: R[]
  cols: Col<R>[]
  rowKey: (r: R) => string
  /** Namespaces the persisted sort, so each tab and grouping remembers its own. */
  tableId: string
  defaultSort: string
  emptyTitle: string
  /** Optional per-row drill-down. When set, every row grows a leading chevron
   *  that toggles a full-width detail row beneath it. One row open at a time:
   *  the drill-down is a comparison against the row above it, not a second
   *  table to scroll. */
  renderExpanded?: (r: R) => React.ReactNode
}) {
  // A text column opens A→Z; a measurement opens largest-first, which is the
  // question being asked of it ("what cost the most", "what was slowest").
  //
  // Rebuilt each render rather than memoised: `cols` is a fresh array on every
  // parent render, so a `[cols]` memo never once hit its cache — it paid the
  // dependency check for nothing and read as if it were saving work.
  const initialDirs = Object.fromEntries(cols.map(c => [c.key, c.left ? 'asc' : 'desc'] as const))
  // Comparators are deliberately empty: this hook is used for the sort STATE
  // and its persistence, and the ordering is applied below so nulls can stay
  // last in both directions.
  const { sort, toggle } = useSortableTable<R>(
    [],
    tableId,
    {},
    // The opening direction is the column's own, not a blanket 'desc': a column
    // ordered by an index (the latency buckets) has to open ascending or the
    // distribution renders back to front.
    { key: defaultSort, dir: initialDirs[defaultSort] ?? 'desc' },
    { bidirectional: true, initialDirs },
  )
  const desc = sort.dir === 'desc'
  // The column that ACTUALLY orders the rows, which is not always the one the
  // persisted sort names: a sort saved by an older column layout survives in
  // localStorage, and the lookup then falls back to the first column. Resolving
  // it once and marking the header from THIS key means the caret and `aria-sort`
  // can never disagree with the order on screen — previously that case left the
  // table sorted by its first column with no header marked at all.
  const activeKey = (cols.find(c => c.key === sort.key) ?? cols[0])?.key

  // The one open drill-down, by row key. Keyed state (not per-row booleans) so
  // a re-sort keeps the same ROW open rather than the same position.
  const [openKey, setOpenKey] = useState<string | null>(null)

  // Not memoised, for the same reason `initialDirs` is not: `cols` is rebuilt
  // by the parent on every render, so a dependency array naming it could never
  // hit its cache. Hoisting the column builders to module scope WOULD stabilise
  // them and is the wrong fix — they call `i18nT` per render on purpose, and
  // freezing the labels at import time would leave the table in the old
  // language after a locale switch. Sorting a handful of rows is cheaper than
  // that bug.
  const shown = (() => {
    const col = cols.find(c => c.key === activeKey) ?? cols[0]
    return rows.slice().sort((a, b) => {
      const av = col.sort(a)
      const bv = col.sort(b)
      if (av == null && bv == null) return 0
      if (av == null) return 1
      if (bv == null) return -1
      const d =
        typeof av === 'string' && typeof bv === 'string' ? compareText(av, bv) : Number(av) - Number(bv)
      return desc ? -d : d
    })
  })()

  return (
    <>
      {shown.length === 0 ? (
        <EmptyState icon={<Activity className="lucide-inline" />} title={emptyTitle} />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse table-striped">
            <thead>
              <tr className="bg-bg-elevated border-b border-border">
                {renderExpanded ? (
                  <th className="w-7" aria-sort="none" aria-label={i18nT('pages.telemetryPanel.detail_col')} />
                ) : null}
                {cols.map(c => (
                  <HeadCell
                    key={c.key}
                    label={c.label}
                    left={c.left}
                    hide={c.hide}
                    active={c.key === activeKey}
                    desc={desc}
                    onToggle={() => toggle(c.key)}
                    tip={c.tip}
                  />
                ))}
              </tr>
            </thead>
            <tbody>
              {shown.map(r => {
                const k = rowKey(r)
                const open = renderExpanded != null && openKey === k
                return (
                  <React.Fragment key={k}>
                    <tr className="border-b border-border/60 last:border-b-0">
                      {renderExpanded ? (
                        <td className="w-7 px-1">
                          <Btn
                            type="button"
                            className="p-0.5 border-none text-muted hover:text-text rounded"
                            aria-expanded={open}
                            aria-label={i18nT(
                              open
                                ? 'pages.telemetryPanel.hide_turn_detail'
                                : 'pages.telemetryPanel.show_turn_detail',
                            )}
                            onClick={() => setOpenKey(open ? null : k)}
                          >
                            {open ? (
                              <ChevronDown className="lucide-inline" size={14} />
                            ) : (
                              <ChevronRight className="lucide-inline" size={14} />
                            )}
                          </Btn>
                        </td>
                      ) : null}
                      {cols.map(c => (
                        <td
                          key={c.key}
                          className={`${c.left ? TXT_CELL : NUM_CELL} ${c.hide ?? ''}`}
                          style={c.color?.(r) ? { color: c.color(r) } : undefined}
                        >
                          {c.render(r)}
                        </td>
                      ))}
                    </tr>
                    {open ? (
                      <tr className="border-b border-border/60 last:border-b-0">
                        <td colSpan={cols.length + 1} className="px-2 py-2 bg-[var(--bg-accent)]">
                          {renderExpanded(r)}
                        </td>
                      </tr>
                    ) : null}
                  </React.Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

/**
 * The per-tab summary. One borderless strip replaces what were fifteen
 * identically-shaped bordered tiles spread over four sections: with every
 * number in the same card as the rows it summarises, nothing has to be
 * cross-referenced between sections, and a headline total no longer competes
 * visually with a row of a table.
 */
type Sum = { label: string; value: string; unit?: string; sub?: string; note?: React.ReactNode; color?: string }

export function Sums({ items }: { items: Sum[] }) {
  return (
    <div className="flex flex-wrap gap-x-9 gap-y-3 border-t border-border mt-3 pt-3">
      {items.map(i => (
        <div key={i.label}>
          <div className="text-[10px] text-muted uppercase tracking-wide">{i.label}</div>
          <div className="text-[19px] font-bold leading-tight" style={i.color ? { color: i.color } : undefined}>
            {i.value}
            {i.unit && <span className="text-[11px] text-muted font-normal ml-0.5">{i.unit}</span>}
          </div>
          {i.sub && <div className="text-[10px] text-muted mt-0.5">{i.sub}</div>}
          {i.note}
        </div>
      ))}
    </div>
  )
}

/**
 * A horizontal histogram. Bars carry magnitude by length and share one scale,
 * which is exactly what a distribution needs — unlike the sortable bucket table
 * this replaces, where sorting by count destroyed the bound order that IS the
 * shape.
 *
 * `label` is a node so a caller can style or link it; the widths either side of
 * the bar are the caller's because they set how much track is left, and a track
 * squeezed to nothing is a row that has stopped saying anything.
 *
 * `barDecorative` hides the bar from assistive technology, for a caller whose
 * label and figure already carry the row's facts as text — reading the bar too
 * announces the row twice. It is opt-in because muting a bar is a change to what
 * a reader is told, and that belongs to the caller that wants it rather than to
 * every caller of this function.
 */
export function Histogram({
  rows,
  labelClass = 'w-20 text-right font-mono text-muted',
  valueClass = 'w-12',
  barDecorative = false,
}: {
  rows: { key: string; label: React.ReactNode; count: number; title?: string }[]
  labelClass?: string
  valueClass?: string
  barDecorative?: boolean
}) {
  const peak = Math.max(1, ...rows.map(r => r.count))
  return (
    <div className="flex flex-col gap-1">
      {rows.map(r => (
        <div key={r.key} className="flex items-center gap-2.5 text-[11px]" title={r.title}>
          <span className={`${labelClass} shrink-0 min-w-0`}>{r.label}</span>
          <div
            aria-hidden={barDecorative || undefined}
            className="h-2 min-w-0 flex-1 overflow-hidden rounded-sm bg-[var(--bg)]"
          >
            <span
              className="block h-full rounded-sm"
              style={{ width: `${(r.count / peak) * 100}%`, background: 'var(--muted-strong)' }}
            />
          </div>
          <span className={`${valueClass} shrink-0 text-right font-mono tabular-nums`}>{fmtNumber(r.count)}</span>
        </div>
      ))}
    </div>
  )
}
