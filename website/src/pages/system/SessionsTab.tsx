/**
 * Sessions — the Processes plane of the System page.
 *
 * Shaped after Task Manager's Processes tab: a resource is a COLUMN, never a
 * mode. Sorting picks the focus; a Columns menu toggles what shows.
 *
 * `Group by` folds rows on an ATTRIBUTE (agent, channel). Sorting, expansion,
 * grouping, and aggregation come from `@tanstack/react-table`.
 */
import { type MutableRefObject, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getExpandedRowModel,
  getFilteredRowModel,
  getGroupedRowModel,
  getSortedRowModel,
  useReactTable,

  type GroupingState,
  type SortingState,
  type VisibilityState,
} from '@tanstack/react-table'
import { ChevronDown, ChevronRight, ChevronUp, MemoryStick, Columns3, TriangleAlert } from 'lucide-react'
import { api } from '../../api/client'
import { Btn, Card, ContentSkeleton, EmptyState, IconButton, SearchInput } from '../../components/ui'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../../components/ui/table'
import InfoTip from '../../components/InfoTip'
import SegmentedControl, { type Segment } from '../../components/SegmentedControl'
import { fmtNumber, fmtPercent } from '../../i18n/format'
import {
  buildTree,
  columnMaxima,
  fmtCredits,
  fmtGb,
  fmtHostPct,
  fmtMb,
  fmtTurns,
  fmtUptime,
  heatLevel,
  type SessionRow,
} from './sessionRows'

import { i18nT } from '../../i18n/t'
import type { PlaneState, SessionsPlaneState } from '../SystemPage'

type Payload = Awaited<ReturnType<typeof api.sessionsMemory>>

/**
 * Shared empty fallbacks for a payload that carries no rows yet.
 *
 * These MUST be stable references, not inline `?? []` literals. An inline literal
 * mints a NEW array on every render, which changes the identity of `rows` (and so
 * of the `data` handed to `useReactTable`) even though nothing about the content
 * changed. TanStack reads a new `data` identity as "the data changed" and fires
 * its auto-reset queue, which calls `setState` — re-rendering, minting another
 * array, and looping. The window where it bites is any render with no `sessions`
 * field at all: the first fetch, and an error payload such as a 403.
 */
const EMPTY_SESSIONS: Payload['sessions'] = []
const EMPTY_TASKS: Payload['tasks'] = []

/** Attribute the table folds on. `none` is a flat ranking, Task Manager's default. */
export type GroupBy = 'none' | 'app' | 'agent' | 'channel'

/**
 * Folds that cannot be served yet. `app` requires sessions to carry an app
 * attribute, which they do not yet.
 */
export const UNAVAILABLE_GROUPINGS: ReadonlySet<GroupBy> = new Set<GroupBy>(['app'])

/**
 * Grouping state for a fold choice. Unavailable folds resolve to flat rather
 * than crashing on a missing column.
 */
export function groupingFor(by: GroupBy): GroupingState {
  if (by === 'none' || UNAVAILABLE_GROUPINGS.has(by)) return []
  return [by]
}

const NUM = 'text-right font-mono text-[12.5px] tabular-nums whitespace-nowrap'
const HEAT = ['', 'bg-accent/[0.05]', 'bg-accent/[0.12]', 'bg-accent/[0.22]'] as const

const helper = createColumnHelper<SessionRow>()

/**
 * Heat tint as a class. Three distinct levels — strengthened so first place is
 * visibly darker than third at a glance.
 */
export function heatClass(value: number | null, max: number | null): string {
  return HEAT[heatLevel(value, max)]
}

interface Props {
  planeStateRef: MutableRefObject<PlaneState>
}

export default function SessionsTab({ planeStateRef }: Props) {
  const navigate = useNavigate()
  const saved = planeStateRef.current.sessions

  const [sorting, setSorting] = useState<SortingState>(
    saved?.sorting ?? [{ id: 'rssMb', desc: true }],
  )
  const [groupBy, setGroupBy] = useState<GroupBy>(
    (saved?.groupBy as GroupBy) ?? 'none',
  )
  const [filter, setFilter] = useState(saved?.filter ?? '')
  const [visibility, setVisibility] = useState<VisibilityState>(
    saved?.visibility ?? { share: false, channel: false },
  )
  const [pickerOpen, setPickerOpen] = useState(false)
  const pickerRef = useRef<HTMLDivElement>(null)
  const pickerBtnRef = useRef<HTMLButtonElement>(null)

  // Persist state to planeStateRef on every change so it survives plane flips.
  useEffect(() => {
    const state: SessionsPlaneState = { sorting, groupBy, filter, visibility }
    planeStateRef.current = { ...planeStateRef.current, sessions: state }
  }, [sorting, groupBy, filter, visibility, planeStateRef])

  // Finding 4: Escape + outside-click to close the Columns popover
  useEffect(() => {
    if (!pickerOpen) return
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (pickerBtnRef.current?.contains(t)) return
      if (pickerRef.current?.contains(t)) return
      setPickerOpen(false)
      pickerBtnRef.current?.focus()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setPickerOpen(false)
        pickerBtnRef.current?.focus()
      }
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [pickerOpen])

  const { data, isPending, isError, isFetching, refetch } = useQuery<Payload>({
    queryKey: ['sessionsMemory'],
    queryFn: () => api.sessionsMemory(),
    refetchInterval: 5000,
  })

  const sessions = data?.sessions ?? EMPTY_SESSIONS
  const tasks = data?.tasks ?? EMPTY_TASKS
  const totals = data?.totals
  const unattributed = data?.unattributed ?? null
  const hostMb = totals?.host_mb ?? null
  const rows = useMemo(() => buildTree(sessions, tasks), [sessions, tasks])
  const maxima = useMemo(() => columnMaxima(rows), [rows])

  const columns = useMemo(
    () => [
        // `size` is load-bearing here, not decorative. The table is
        // `table-layout: fixed`, and a fixed table with no declared widths splits
        // the width EQUALLY across all ~12 columns — which left the name column
        // ~83px and clipped every session name to nothing. These sizes are
        // emitted as a <colgroup> below; the name column is oversized so it
        // absorbs the leftover width instead of the numeric columns growing.
        helper.accessor('name', {
          header: i18nT('pages.sessionsTab.session_task'),
          enableHiding: false,
          enableGrouping: false,
          size: 320,
          minSize: 200,
        }),
        helper.accessor('rssMb', {
          header: i18nT('pages.sessionsTab.memory'),
          enableGrouping: false,
          aggregationFn: 'sum',
          size: 88,
          cell: c => fmtMb(c.getValue<number | null>()),
        }),
        helper.accessor('peakMb', {
          header: i18nT('pages.sessionsTab.peak'),
          enableGrouping: false,
          aggregationFn: 'max',
          size: 78,
          cell: c => fmtMb(c.getValue<number | null>()),
        }),
        helper.accessor('cpuCores', {
          header: i18nT('pages.sessionsTab.cpu_cores'),
          enableGrouping: false,
          aggregationFn: 'sum',
          size: 88,
          cell: c => {
            const v = c.getValue<number | null>()
            return v == null ? '—' : fmtNumber(v, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
          },
        }),
        helper.accessor('procs', {
          header: i18nT('pages.sessionsTab.procs'),
          enableGrouping: false,
          aggregationFn: 'sum',
          size: 68,
          cell: c => {
            const v = c.getValue<number | null>()
            return v == null ? '—' : fmtNumber(v)
          },
        }),
        helper.accessor('mcp', {
          header: i18nT('pages.sessionsTab.mcp_stubs'),
          enableGrouping: false,
          aggregationFn: 'sum',
          size: 92,
          cell: c => {
            const v = c.getValue<number | null>()
            return v == null ? '—' : fmtNumber(v)
          },
        }),
        helper.accessor('credits', {
          header: i18nT('pages.sessionsTab.credits'),
          enableGrouping: false,
          aggregationFn: 'sum',
          size: 82,
          cell: c => fmtCredits(c.getValue<number | null>()),
        }),
        helper.accessor('turns', {
          header: i18nT('pages.sessionsTab.turns'),
          enableGrouping: false,
          aggregationFn: 'sum',
          size: 68,
          cell: c => fmtTurns(c.getValue<number | null>()),
        }),
        helper.accessor('uptimeS', {
          header: i18nT('pages.sessionsTab.uptime'),
          enableGrouping: false,
          aggregationFn: 'max',
          size: 82,
          cell: c => fmtUptime(c.getValue<number | null>()),
        }),
        helper.accessor('agent', { header: i18nT('pages.sessionsTab.agent'), size: 112 }),
        helper.accessor('channel', { header: i18nT('pages.sessionsTab.channel'), size: 100 }),
        helper.accessor('rssMb', {
          id: 'share',
          header: i18nT('pages.sessionsTab.host_share'),
          enableGrouping: false,
          aggregationFn: 'sum',
          size: 88,
          cell: c => fmtHostPct(c.getValue<number | null>(), hostMb),
        }),
        helper.accessor('pid', {
          header: i18nT('pages.sessionsTab.pid'),
          enableGrouping: false,
          size: 74,
          cell: c => {
            const v = c.getValue<number | null>()
            return v == null ? '—' : String(v)
          },
        }),
    ],
    [hostMb],
  )
  const table = useReactTable({
    data: rows,
    columns,
    state: { sorting, grouping: groupingFor(groupBy), globalFilter: filter, columnVisibility: visibility },
    onSortingChange: setSorting,
    onColumnVisibilityChange: setVisibility,
    getSubRows: row => row.subRows,
    getRowId: row => `${row.kind}:${row.id}`,
    globalFilterFn: (row, _col, value: string) => {
      const needle = String(value ?? '').trim().toLowerCase()
      if (!needle) return true
      const r = row.original
      return r.name.toLowerCase().includes(needle) || r.agent.toLowerCase().includes(needle)
    },
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getGroupedRowModel: getGroupedRowModel(),
    getExpandedRowModel: getExpandedRowModel(),
    autoResetExpanded: false,
    // This table does not paginate — `getPaginationRowModel` is never supplied, so
    // `pageIndex` / `pageSize` describe nothing. Auto-reset defaults to ON anyway,
    // and with no `onPaginationChange` supplied it routes through TanStack's own
    // `makeStateUpdater('pagination')`, i.e. `table.setState` → a React render.
    // Paired with any change in `data` identity that becomes a render loop, since
    // the render feeds the next auto-reset. Resetting a page index that cannot
    // exist has no upside to trade against that, so it is off.
    autoResetPageIndex: false,
    initialState: {
      expanded: true,
    },
  })

  const usedMb = totals?.rss_mb ?? 0
  const largestMb = sessions.reduce<number | null>(
    (m, s) => (s.rss_mb != null && (m == null || s.rss_mb > m) ? s.rss_mb : m),
    null,
  )
  const procTotal = sessions.reduce((n, s) => n + (s.procs ?? 0), 0)

  // Finding 7a: surface the disabled reason via InfoTip, not just title
  const groupSegments: Array<Segment<GroupBy>> = [
    { key: 'none', label: i18nT('pages.sessionsTab.group_none') },
    {
      key: 'app',
      label: i18nT('pages.sessionsTab.group_app'),
      disabled: true,
      tooltip: i18nT('pages.sessionsTab.group_app_unavailable'),
    },
    { key: 'agent', label: i18nT('pages.sessionsTab.group_agent') },
    { key: 'channel', label: i18nT('pages.sessionsTab.group_channel') },
  ]
  const hideable = table.getAllLeafColumns().filter(c => c.getCanHide())

  /** Whether to show the unattributed row: only when procs > 0. */
  const showUnattributed = unattributed != null && unattributed.procs > 0

  const closePicker = useCallback(() => {
    setPickerOpen(false)
    pickerBtnRef.current?.focus()
  }, [])

  return (
    <Card className="mb-6 overflow-hidden">
      {/* Stale-data notice. Shown when a poll has failed but a previous payload is
          still on screen: the rows below are real, just not current, and saying so
          is what lets the user trust them without mistaking them for live. */}
      {isError && data && (
        <div
          data-testid="sessions-stale"
          className="flex items-center gap-2 px-3.5 py-2 border-b border-border bg-warn-subtle text-[11.5px] text-warn"
        >
          <TriangleAlert size={13} aria-hidden="true" className="lucide-inline shrink-0" />
          <span>{i18nT('pages.sessionsTab.could_not_refresh')}</span>
          <Btn
            type="button"
            onClick={() => refetch()}
            disabled={isFetching}
            className="ml-auto text-[11px]"
          >
            {isFetching ? i18nT('pages.sessionsTab.retrying') : i18nT('pages.sessionsTab.retry')}
          </Btn>
        </div>
      )}
      {/* Toolbar: Group by + segments + filter on left, Columns on right */}
      <div className="flex items-center gap-2.5 px-3.5 py-2.5 flex-wrap">
        <span className="text-[10.5px] text-muted">{i18nT('pages.sessionsTab.group_by')}</span>
        <SegmentedControl<GroupBy> segments={groupSegments} value={groupBy} onChange={setGroupBy} collapse={false} />
        {/* Finding 7a: InfoTip next to the App segment explaining why it is disabled */}
        <InfoTip text={i18nT('pages.sessionsTab.group_app_unavailable')} />
        <SearchInput
          placeholder={i18nT('pages.sessionsTab.filter_sessions')}
          value={filter}
          onChange={e => setFilter(e.currentTarget.value)}
          className="w-[150px]"
        />
        <div className="relative ml-auto">
          <Btn
            ref={pickerBtnRef}
            type="button"
            aria-expanded={pickerOpen}
            aria-haspopup="true"
            onClick={() => setPickerOpen(o => !o)}
            className="text-[11.5px] gap-1.5"
          >
            <Columns3 size={13} aria-hidden="true" className="lucide-inline" />
            {i18nT('pages.sessionsTab.columns')}
          </Btn>
          {pickerOpen && (
            <div
              ref={pickerRef}
              role="dialog"
              aria-label={i18nT('pages.sessionsTab.columns')}
              className="absolute right-0 z-20 mt-1 min-w-40 rounded border border-border bg-bg-elevated p-1.5 shadow-lg"
            >
              {hideable.map(col => (
                <label key={col.id} className="flex items-center gap-2 px-1.5 py-1 text-[12px] cursor-pointer">
                  <input
                    type="checkbox"
                    checked={col.getIsVisible()}
                    onChange={col.getToggleVisibilityHandler()}
                  />
                  {flexRender(col.columnDef.header, {} as never) as never}
                </label>
              ))}
              <div className="mt-1 border-t border-border pt-1">
                <Btn type="button" onClick={closePicker} className="w-full text-[11px] justify-center">
                  {i18nT('pages.sessionsTab.done')}
                </Btn>
              </div>
            </div>
          )}
        </div>
      </div>

      {isPending ? (
        // "No active sessions" is a claim about the machine, and during the first
        // fetch it is one we cannot make — a slow or failing endpoint made the page
        // assert there were none while it was still asking. A skeleton says
        // "not known yet", which is the truth.
        <ContentSkeleton rows={6} />
      ) : isError && !data ? (
        // The same false claim, by a different route: a failed request resolves the
        // query with no data, so the empty state would render — indistinguishable
        // from a healthy idle host, and re-asserted every 5s. That lands hardest on
        // the shared-MCP-gateway users this page's own failure mode affects, so
        // silence here reads as "nothing is running" while the truth is "we cannot
        // tell".
        //
        // Gated on `!data` deliberately. react-query keeps the last payload while
        // flipping status to `error`, so an unguarded `isError` would let one failed
        // BACKGROUND poll unmount a table the user is mid-read on. Stale rows with a
        // "can't refresh" notice (below) beat correct rows replaced by a panel.
        <EmptyState
          testId="sessions-error"
          icon={<TriangleAlert className="lucide-inline" />}
          title={i18nT('pages.sessionsTab.could_not_read_sessions')}
          subtitle={i18nT('pages.sessionsTab.could_not_read_sessions_hint')}
          action={
            // Relabelled off `isFetching`, not decorative: the default retry +
            // backoff leaves the screen pixel-identical for several seconds after
            // the click, so an unacknowledged button reads as a dead one and gets
            // clicked again — exactly when the user is already anxious.
            <Btn
              type="button"
              onClick={() => refetch()}
              disabled={isFetching}
              className="text-[11.5px]"
            >
              {isFetching ? i18nT('pages.sessionsTab.retrying') : i18nT('pages.sessionsTab.retry')}
            </Btn>
          }
        />
      ) : table.getRowModel().rows.length === 0 && !showUnattributed ? (
        <EmptyState
          icon={<MemoryStick className="lucide-inline" />}
          title={i18nT('pages.sessionsTab.no_active_sessions')}
          subtitle={i18nT('pages.sessionsTab.no_active_sessions_hint')}
        />
      ) : (
        <Table className="table-striped" style={{ tableLayout: 'fixed' }}>
          {/* Without this, `table-layout: fixed` ignores the columnDef sizes and
              splits the width equally, starving the name column. Driven off the
              VISIBLE leaf columns so hiding a column via the picker re-flows the
              widths instead of leaving a dangling <col>. */}
          <colgroup>
            {table.getVisibleLeafColumns().map(col => (
              <col key={col.id} style={{ width: `${col.getSize()}px` }} />
            ))}
          </colgroup>
          <TableHeader>
            <TableRow className="bg-bg-elevated">
              {table.getHeaderGroups()[0]?.headers.map(h => {
                const first = h.column.id === 'name'
                const dir = h.column.getIsSorted()
                // Finding 5: InfoTip on cpu and mcp headers
                const infoTipKey = headerInfoTip(h.column.id)
                return (
                  <TableHead
                    key={h.id}
                    aria-sort={dir === 'desc' ? 'descending' : dir === 'asc' ? 'ascending' : 'none'}
                    className={`px-3 py-1.5 text-[10px] font-medium tracking-wider uppercase ${
                      first ? 'text-left' : 'text-right'
                    }`}
                  >
                    <Btn
                      type="button"
                      onClick={h.column.getToggleSortingHandler()}
                      className={`border-transparent bg-transparent px-0 py-0 gap-1 text-[10px] font-medium ${
                        dir ? 'text-accent' : 'text-muted'
                      }`}
                    >
                      {flexRender(h.column.columnDef.header, h.getContext())}
                      {dir === 'desc' && <ChevronDown size={12} aria-hidden="true" className="lucide-inline" />}
                      {dir === 'asc' && <ChevronUp size={12} aria-hidden="true" className="lucide-inline" />}
                    </Btn>
                    {infoTipKey && <InfoTip text={i18nT(infoTipKey)} />}
                  </TableHead>
                )
              })}
            </TableRow>
          </TableHeader>
          <TableBody>
            {/* Unattributed row — pinned above all sessions, outside sort.
                Finding 6: use warn tint instead of danger for a documented-healthy state. */}
            {showUnattributed && (
              <TableRow data-testid="unattributed-row" className="text-warn">
                {table.getHeaderGroups()[0]?.headers.map(h => {
                  const colId = h.column.id
                  const isName = colId === 'name'
                  let content: string
                  if (isName) content = ''
                  else if (colId === 'rssMb') content = fmtMb(unattributed!.rss_mb)
                  else if (colId === 'procs') content = fmtNumber(unattributed!.procs)
                  else if (colId === 'uptimeS') content = fmtUptime(unattributed!.oldest_uptime_s)
                  else content = '—'
                  return (
                    <TableCell
                      key={h.id}
                      className={isName ? 'px-3 py-1 text-left text-[12.5px] text-warn font-medium' : `px-3 py-1 ${NUM} text-warn`}
                    >
                      {isName ? (
                        <span className="inline-flex items-center gap-1.5">
                          <span>{i18nT('pages.sessionsTab.unattributed')}</span>
                          <InfoTip text={i18nT('pages.sessionsTab.unattributed_hint')} />
                        </span>
                      ) : content}
                    </TableCell>
                  )
                })}
              </TableRow>
            )}
            {table.getRowModel().rows.map(row => {
              const r = row.original
              const grouped = row.getIsGrouped()
              // On a fold, the name column is the only place the grouping value
              // can appear: `channel` (and `share`) are hidden on first paint, so
              // their own cell is never rendered. Without this the name cell fell
              // through to the first member's title and the row claimed to be a
              // session it merely contained.
              const groupLabel = grouped && row.groupingColumnId != null
                ? String(row.getGroupingValue(row.groupingColumnId) ?? '—')
                : null
              const href = grouped ? null : r.href
              return (
                <TableRow
                  key={row.id}
                  className={`${grouped ? 'bg-bg-elevated' : ''} ${href ? 'cursor-pointer hover:bg-bg-hover' : ''}`}
                  style={{ height: '28px' }}
                  {...(href ? { onClick: () => navigate(href) } : {})}
                >
                  {row.getVisibleCells().map(cell => {
                    const isName = cell.column.id === 'name'
                    const heat =
                      cell.column.id === 'rssMb'
                        ? heatClass(r.rssMb, maxima.rssMb)
                        : cell.column.id === 'cpuCores'
                          ? heatClass(r.cpuCores, maxima.cpuCores)
                          : ''
                    if (cell.getIsPlaceholder()) return <TableCell key={cell.id} className={NUM} />
                    return (
                      <TableCell
                        key={cell.id}
                        className={
                          isName
                            ? `px-3 py-1 text-left text-[12.5px] truncate ${
                                row.depth > 0 ? 'pl-9 text-text' : 'text-text-strong font-medium'
                              }`
                            : `px-3 py-1 ${NUM} ${heat}`
                        }
                        {...(isName ? { title: grouped ? (groupLabel ?? '') : r.name } : {})}
                      >
                        {isName ? (
                          <>
                            {row.getCanExpand() && (
                              <IconButton
                                aria-expanded={row.getIsExpanded()}
                                aria-label={i18nT(
                                  row.getIsExpanded()
                                    ? 'pages.sessionsTab.collapse_tasks'
                                    : 'pages.sessionsTab.expand_tasks',
                                  { name: r.name },
                                )}
                                onClick={e => {
                                  e.stopPropagation()
                                  row.toggleExpanded()
                                }}
                                className="inline-block w-3 -ml-3 mr-0.5 p-0 align-middle text-muted hover:text-text"
                              >
                                {row.getIsExpanded() ? (
                                  <ChevronDown size={12} aria-hidden="true" className="lucide-inline" />
                                ) : (
                                  <ChevronRight size={12} aria-hidden="true" className="lucide-inline" />
                                )}
                              </IconButton>
                            )}
                            {groupLabel != null ? (
                              <span className="truncate">{groupLabel}</span>
                            ) : href ? (
                              <Btn
                                type="button"
                                onClick={e => {
                                  e.stopPropagation()
                                  navigate(href)
                                }}
                                // `Btn` is inline-flex, which does not shrink below
                                // its content width, so without `min-w-0` the button
                                // overflows the cell instead of ellipsizing. The
                                // column now has a real declared width (columnDef
                                // `size` + the <colgroup> above), which is what keeps
                                // the name inside the cell the expander shares.
                                className="border-transparent bg-transparent px-0 py-0 text-left text-inherit font-inherit hover:underline min-w-0 max-w-full"
                              >
                                <span className="truncate">{r.name}</span>
                              </Btn>
                            ) : (
                              flexRender(cell.column.columnDef.cell, cell.getContext())
                            )}
                            {grouped && (
                              <span className="ml-2 text-[10.5px] text-muted font-mono">
                                {fmtNumber(row.subRows.length)}
                              </span>
                            )}
                            {r.shared && !grouped && (
                              <span className="ml-1.5 text-[10px] px-1.5 rounded border border-warn/40 text-warn align-[1px]">
                                {i18nT('pages.sessionsTab.shared')}
                              </span>
                            )}
                          </>
                        ) : (
                          flexRender(cell.column.columnDef.cell, cell.getContext())
                        )}
                      </TableCell>
                    )
                  })}
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      )}

      {/* Footer — single horizontal strip of stat pairs.
          Suppressed until a payload lands: the body above says "not known yet"
          (skeleton) or "cannot tell" (error), and a footer reading a concrete
          "0" beside either of those makes the card tell two different stories. */}
      {!isPending && data && (
      <div className="flex items-center flex-wrap px-3.5 py-2 border-t border-border bg-bg-elevated">
        <FooterStat label={i18nT('pages.sessionsTab.footer_kirocrew_gb')} value={fmtGb(usedMb)} />
        <FooterStat label={i18nT('pages.sessionsTab.footer_share_of_machine')} value={totals?.host_pct != null ? fmtPercent(totals.host_pct / 100, { maximumFractionDigits: 2 }) : '—'} />
        <FooterStat label={i18nT('pages.sessionsTab.footer_largest_session_gb')} value={fmtGb(largestMb)} />
        <FooterStat label={i18nT('pages.sessionsTab.footer_sessions')} value={fmtNumber(sessions.length)} />
        <FooterStat label={i18nT('pages.sessionsTab.footer_task_sessions')} value={fmtNumber(tasks.length)} />
        <FooterStat label={i18nT('pages.sessionsTab.footer_session_procs')} value={fmtNumber(procTotal)} />
        {showUnattributed && (
          <FooterStat
            label={i18nT('pages.sessionsTab.unattributed')}
            value={`${fmtNumber(unattributed!.procs)} · ${fmtGb(unattributed!.rss_mb)}`}
            warn
          />
        )}
      </div>
      )}
    </Card>
  )
}

/** Map column ids to their InfoTip i18n key. */
function headerInfoTip(colId: string): string | null {
  switch (colId) {
    case 'cpuCores': return 'pages.sessionsTab.cpu_cores_hint'
    case 'mcp': return 'pages.sessionsTab.mcp_stubs_hint'
    default: return null
  }
}

function FooterStat({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return (
    <span className="text-[11px] text-muted pr-3.5 mr-3.5 border-r border-border last:border-r-0 last:mr-0 last:pr-0">
      {label}
      <span className={`ml-1.5 font-mono tabular-nums text-[12px] font-medium ${warn ? 'text-warn' : 'text-text-strong'}`}>
        {value}
      </span>
    </span>
  )
}
