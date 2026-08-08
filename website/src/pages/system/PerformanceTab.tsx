/**
 * Performance — the resource-monitoring plane of the System page.
 *
 * Shaped after Task Manager's Performance tab: a left rail selects one resource
 * at a time, a right body shows a large live graph plus that resource's numbers.
 * No process/session table — that belongs to Sessions.
 */
import { type CSSProperties, type MutableRefObject, useCallback, useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { api } from '../../api/client'
import { Card } from '../../components/ui'
import { fmtBytes, fmtNumber, fmtPercent, fmtUnit } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import type { SessionStorageReport, SystemData } from '../../types'
import type { PlaneState } from '../SystemPage'
import SessionStorageScreen from './SessionStorageScreen'

type Resource = 'cpu' | 'memory' | 'disk' | 'network'

/** Rolling history length — ~60s at 2s poll interval. */
const HISTORY_LEN = 30
/** Seconds between samples — must match the query's `refetchInterval` below, and
 *  is what turns a sample count into the window label under each graph. */
const SAMPLE_INTERVAL_S = 2

interface HistoryPoint {
  cpu: number
  mem: number
  disk: number
  netRx: number
  netTx: number
}

/** Headline value shown on the left rail tile for each resource. */
function headline(d: SystemData | null, resource: Resource): string {
  if (!d) return '—'
  switch (resource) {
    case 'cpu': return fmtPercent(d.cpu_pct / 100)
    case 'memory': return fmtPercent(d.mem_used_gb / (d.mem_total_gb || 1))
    case 'disk': {
      const used = d.disk_total_gb - d.disk_free_gb
      return fmtPercent(used / (d.disk_total_gb || 1))
    }
    case 'network': return fmtUnit(d.net_rx_kbs + d.net_tx_kbs, 'kilobyte-per-second', { maximumFractionDigits: 0 })
  }
}

/** Map resource to the history value used for its graph (0–100 scale for cpu/mem/disk). */
function graphValue(pt: HistoryPoint, resource: Resource): number {
  switch (resource) {
    case 'cpu': return pt.cpu
    case 'memory': return pt.mem
    case 'disk': return pt.disk
    case 'network': return pt.netRx + pt.netTx
  }
}

/**
 * Resource labels as a FUNCTION, not a module-level constant: a constant is
 * evaluated once at import and would freeze whichever language was active at
 * boot, leaving the rail untranslated after a language switch. Spelling the four
 * keys out also keeps them static literals, which is what the i18n key-reference
 * gate can verify — a template key cannot be checked against the catalogs.
 */
function resourceLabels(): Record<Resource, string> {
  return {
    cpu: i18nT('pages.performanceTab.resource_cpu'),
    memory: i18nT('pages.performanceTab.resource_memory'),
    disk: i18nT('pages.performanceTab.resource_disk'),
    network: i18nT('pages.performanceTab.resource_network'),
  }
}

export default function PerformanceTab({ planeStateRef }: { planeStateRef: MutableRefObject<PlaneState> }) {
  const savedResource = planeStateRef.current.performance?.selected as Resource | undefined
  const savedHistory = planeStateRef.current.performance?.history as HistoryPoint[] | undefined
  const [selected, setSelected] = useState<Resource>(savedResource ?? 'cpu')
  const [history, setHistory] = useState<HistoryPoint[]>(savedHistory ?? [])
  // The drill-down lives in the URL so it is shareable and survives a reload,
  // the same reason the plane itself does.
  const [params, setParams] = useSearchParams()
  const showStorage = params.get('view') === 'storage'
  const setStorageView = useCallback((on: boolean) => {
    setParams(prev => {
      const next = new URLSearchParams(prev)
      if (on) next.set('view', 'storage')
      else next.delete('view')
      return next
    }, { replace: true })
  }, [setParams])
  const openStorage = useCallback(() => setStorageView(true), [setStorageView])
  const closeStorage = useCallback(() => setStorageView(false), [setStorageView])
  // Restored, not zeroed: see PerformancePlaneState.lastSampleAt.
  const lastDataId = useRef<number>(planeStateRef.current.performance?.lastSampleAt ?? 0)

  // Persist the resource choice, the collected samples AND the de-dup guard so
  // all three survive a plane flip. Writing a ref costs no re-render, so running
  // this on every new sample is cheap.
  useEffect(() => {
    planeStateRef.current = {
      ...planeStateRef.current,
      performance: { selected, history, lastSampleAt: lastDataId.current },
    }
  }, [selected, history, planeStateRef])

  const { data, dataUpdatedAt } = useQuery<SystemData>({
    queryKey: ['system'],
    queryFn: () => api.system(),
    refetchInterval: SAMPLE_INTERVAL_S * 1000,
  })

  // Accumulate one sample per successful fetch. The signal must be
  // `dataUpdatedAt`, not `data`: react-query's structural sharing hands back the
  // SAME object reference when a poll returns a byte-identical payload, so keying
  // on `data` silently stops sampling an idle machine whose metrics have not
  // moved. `dataUpdatedAt` advances on every fetch regardless.
  useEffect(() => {
    if (!data || !dataUpdatedAt) return
    if (dataUpdatedAt === lastDataId.current) return
    lastDataId.current = dataUpdatedAt
    const point: HistoryPoint = {
      cpu: data.cpu_pct,
      mem: data.mem_total_gb > 0 ? (data.mem_used_gb / data.mem_total_gb) * 100 : 0,
      disk: data.disk_total_gb > 0 ? ((data.disk_total_gb - data.disk_free_gb) / data.disk_total_gb) * 100 : 0,
      netRx: data.net_rx_kbs,
      netTx: data.net_tx_kbs,
    }
    setHistory(prev => {
      const next = [...prev, point]
      return next.length > HISTORY_LEN ? next.slice(-HISTORY_LEN) : next
    })
  }, [data, dataUpdatedAt])

  const d = data ?? null
  const resources: Resource[] = ['cpu', 'memory', 'disk', 'network']
  const labels = resourceLabels()

  // Its own room, not a panel wedged under the graph: the drill-down is a
  // control plane and shares none of Performance's monitoring furniture.
  if (showStorage) return <SessionStorageScreen onBack={closeStorage} />

  return (
    <Card>
      <div style={{ display: 'grid', gridTemplateColumns: '196px minmax(0, 1fr)', gap: '1rem' }}>
        {/* Left rail — resource selector tiles */}
        <nav className="flex flex-col gap-1.5" aria-label={i18nT('pages.performanceTab.resource_nav')}>
          {resources.map(r => (
            <button
              key={r}
              type="button"
              aria-pressed={selected === r}
              onClick={() => setSelected(r)}
              className={`text-left rounded-md border px-3 py-2.5 transition-all ${
                selected === r
                  ? 'border-accent bg-bg-elevated shadow-sm'
                  : 'border-border bg-card hover:border-border-strong hover:bg-bg-hover'
              }`}
            >
              <div className="text-[12px] font-semibold text-text-strong">
                {labels[r]}
              </div>
              <div className="text-[11px] text-muted font-mono tabular-nums mt-0.5">
                {headline(d, r)}
              </div>
              <MiniSparkline history={history} resource={r} />
            </button>
          ))}
        </nav>

        {/* Right body — graph + stats */}
        <div className="flex flex-col gap-4 min-w-0">
          <div>
            <h3 className="text-sm font-semibold text-text-strong">
              {labels[selected]}
            </h3>
            <p className="text-[11.5px] text-muted mt-0.5">
              <ResourceSubtitle d={d} resource={selected} />
            </p>
          </div>

          {/* Large graph */}
          <div className="relative h-36 border border-border rounded bg-bg-elevated overflow-hidden">
            <LargeGraph history={history} resource={selected} />
          </div>

          {/* Stats grid */}
          <div className="grid grid-cols-2 gap-x-6 gap-y-1.5 lg:grid-cols-3">
            <ResourceStats d={d} resource={selected} />
          </div>

          {/* Disk answers "how full is this machine". "What is using it, and can
              I have it back" is the next question, and it is a control plane —
              so it is one quiet row that opens its own screen, not more numbers
              here. Same shape as the Skills page entry row. */}
          {selected === 'disk' && <StorageEntryRow onOpen={openStorage} />}

          {/* Machine identity strip */}
          <div className="border-t border-border pt-3 mt-auto flex flex-wrap gap-x-6 gap-y-1 text-[11.5px] text-muted">
            <span>{i18nT('pages.performanceTab.hostname')}: <strong className="text-text font-mono">{d?.hostname ?? '—'}</strong></span>
            <span>{i18nT('pages.performanceTab.os')}: <strong className="text-text font-mono">{d?.os ?? '—'}</strong></span>
            <span>{i18nT('pages.performanceTab.python')}: <strong className="text-text font-mono">{d?.python ?? '—'}</strong></span>
            <span>{i18nT('pages.performanceTab.working_directory')}: <strong className="text-text font-mono text-[11px] break-all">{d?.cwd ?? '—'}</strong></span>
          </div>
        </div>
      </div>
    </Card>
  )
}

/* ── Session-storage entry row (Disk pane only) ── */

/**
 * One quiet line with a figure and an arrow — the pattern already used on the
 * Skills page. It fetches the report itself rather than lifting it into the
 * plane, so nothing is requested until someone is actually looking at Disk.
 */
function StorageEntryRow({ onOpen }: { onOpen: () => void }) {
  const { data } = useQuery<SessionStorageReport>({
    queryKey: ['session-storage'],
    queryFn: api.sessionStorage,
    refetchOnWindowFocus: false,
    staleTime: 60_000,
  })

  return (
    <button
      type="button"
      onClick={onOpen}
      className="w-full text-left rounded-md border border-border bg-card hover:border-border-strong hover:bg-bg-hover transition-all px-3 py-2.5 flex items-center gap-3"
    >
      <div className="min-w-0 flex-1">
        <div className="text-[12.5px] font-semibold text-text-strong">
          {i18nT('pages.performanceTab.session_storage')}
        </div>
        <div className="text-[11px] text-muted mt-0.5">
          {data
            ? i18nT('pages.performanceTab.session_storage_detail', {
                sessions: fmtNumber(data.total_sessions),
                reclaimable: fmtBytes(data.reclaimable_bytes),
              })
            : i18nT('pages.performanceTab.session_storage_measuring')}
        </div>
      </div>
      {data && (
        <span className="text-[12px] text-text font-mono tabular-nums">{fmtBytes(data.total_bytes)}</span>
      )}
      <ChevronRight className="w-4 h-4 text-muted shrink-0" />
    </button>
  )
}

/* ── Mini sparkline for the left rail tiles ── */

/* ── Traces are CSS clip-path polygons, NOT inline SVG ──────────────────────
 * `use-lucide-icons` in code-review.yml is a BLOCKING gate that greps ADDED
 * lines for an inline svg tag carrying a viewBox attribute, exempting only
 * brand assets (KiroGhost, *Logo, *Ghost). An SVG polyline here fails CI, so
 * the trace is drawn by clipping a filled div. Coordinates are percentages,
 * which is what lets these scale with the container without measuring it.
 *
 * Do not spell that tag-and-attribute pair out on one line anywhere in this
 * file, comments included: the gate is a plain grep and cannot tell prose
 * about the rule from a violation of it. */

/** Vertices as `x% y%` pairs across a 0–100 box; y is inverted for screen space. */
function vertices(values: number[], max: number): { x: number; y: number }[] {
  const lastX = values.length - 1
  return values.map((v, i) => ({
    x: lastX === 0 ? 0 : (i / lastX) * 100,
    y: 100 - Math.min(100, (v / max) * 100),
  }))
}

/** Filled region under the trace.
 *
 * Returns the style OBJECT, not a bare clip-path string. Two reasons, and the
 * second is load-bearing: a bare string can be dropped into any attribute, and
 * the `unitLiterals` gate recognises CSS context by the shape of the code —
 * "an object property whose key is a CSS property". Building `${n}%` inside a
 * function that returns a plain string reads to that gate as user-visible copy
 * that should have gone through `fmtPercent`, and it fails the build. Naming
 * `clipPath` here is what makes the CSS intent visible at the construction
 * site instead of only at the call site. */
function areaStyle(pts: { x: number; y: number }[]): CSSProperties {
  const closed = [{ x: 0, y: 100 }, ...pts, { x: 100, y: 100 }]
  return {
    clipPath: `polygon(${closed.map(p => `${p.x.toFixed(2)}% ${p.y.toFixed(2)}%`).join(', ')})`,
  }
}

/**
 * The trace itself, as a band of constant PIXEL thickness: forward along the
 * top edge, back along the bottom. `calc(y% ± half)` is what keeps the line the
 * same weight in a 16px rail and a 240px graph — a purely percentage-based band
 * would grow with the container, which is the same distortion an SVG stroke
 * suffers under `preserveAspectRatio: none`.
 */
function strokeStyle(pts: { x: number; y: number }[], weightPx: number): CSSProperties {
  const half = (weightPx / 2).toFixed(2)
  const fwd = pts.map(p => `${p.x.toFixed(2)}% calc(${p.y.toFixed(2)}% - ${half}px)`)
  const back = [...pts].reverse().map(p => `${p.x.toFixed(2)}% calc(${p.y.toFixed(2)}% + ${half}px)`)
  return { clipPath: `polygon(${[...fwd, ...back].join(', ')})` }
}

function MiniSparkline({ history, resource }: { history: HistoryPoint[]; resource: Resource }) {
  if (history.length < 2) return null
  const values = history.map(pt => graphValue(pt, resource))
  const max = resource === 'network' ? Math.max(...values, 1) : 100
  const pts = vertices(values, max)

  return (
    <div className="relative w-full h-4 mt-1.5" aria-hidden="true">
      <div
        className="absolute inset-0 bg-accent/55"
        style={strokeStyle(pts, 1.25)}
      />
    </div>
  )
}

/* ── Large graph (line + area, matching Task Manager's trace) ── */

function LargeGraph({ history, resource }: { history: HistoryPoint[]; resource: Resource }) {
  if (history.length < 2) {
    return (
      <div className="flex items-center justify-center h-full text-[11.5px] text-muted">
        {i18nT('pages.performanceTab.collecting_samples')}
      </div>
    )
  }

  const values = history.map(pt => graphValue(pt, resource))
  const max = resource === 'network' ? Math.max(...values, 1) : 100
  const pts = vertices(values, max)
  // Network is the only self-scaled trace, so idle chatter would otherwise fill
  // the frame and read as a crisis. Naming the full-scale value and the window
  // is what lets a viewer tell 3 KB/s from 3 MB/s at the same trace height.
  const caption =
    resource === 'network'
      ? i18nT('pages.performanceTab.graph_scale_network', {
          max: max.toFixed(1),
          seconds: String(values.length * SAMPLE_INTERVAL_S),
        })
      : i18nT('pages.performanceTab.graph_scale_percent', {
          seconds: String(values.length * SAMPLE_INTERVAL_S),
        })

  return (
    <div className="absolute inset-0 flex flex-col">
      <div className="flex-1 min-h-0 px-1 pt-1">
        <div className="relative w-full h-full" role="img" aria-label={caption}>
          <div
            className="absolute inset-0 bg-accent/15"
            style={areaStyle(pts)}
          />
          <div
            className="absolute inset-0 bg-accent"
            style={strokeStyle(pts, 1.5)}
          />
        </div>
      </div>
      <div className="px-1 pb-0.5 text-[10px] text-muted tabular-nums">{caption}</div>
    </div>
  )
}

/* ── Subtitle (static facts about the selected resource) ── */

function ResourceSubtitle({ d, resource }: { d: SystemData | null; resource: Resource }) {
  if (!d) return <>{'—'}</>
  // One interpolated string per resource rather than a label glued onto a
  // number: word order after a quantity is not the same in every language, and
  // a fragment translated alone has no grammatical context to translate into.
  switch (resource) {
    case 'cpu':
      return (
        <>
          {i18nT('pages.performanceTab.cpu_subtitle', { arch: d.arch, count: fmtNumber(d.cpu_count) })}
        </>
      )
    case 'memory':
      return (
        <>
          {i18nT('pages.performanceTab.memory_subtitle', {
            size: fmtUnit(d.mem_total_gb, 'gigabyte', { maximumFractionDigits: 1 }),
          })}
        </>
      )
    case 'disk':
      return (
        <>
          {i18nT('pages.performanceTab.disk_subtitle', {
            size: fmtUnit(d.disk_total_gb, 'gigabyte', { maximumFractionDigits: 0 }),
          })}
        </>
      )
    case 'network':
      return <>{d.ip}</>
  }
}

/* ── Per-resource stats ── */

function ResourceStats({ d, resource }: { d: SystemData | null; resource: Resource }) {
  if (!d) return null
  switch (resource) {
    case 'cpu':
      return (
        <>
          <Stat label={i18nT('pages.performanceTab.utilization')} value={fmtPercent(d.cpu_pct / 100)} />
          <Stat label={i18nT('pages.performanceTab.gateway_cpu')} value={fmtPercent(d.proc_cpu_pct / 100)} />
          <Stat label={i18nT('pages.performanceTab.cores')} value={fmtNumber(d.cpu_count)} />
          <Stat label={i18nT('pages.performanceTab.load_1m')} value={fmtNumber(d.load_1m, { maximumFractionDigits: 2 })} />
          <Stat label={i18nT('pages.performanceTab.load_5m')} value={fmtNumber(d.load_5m, { maximumFractionDigits: 2 })} />
          <Stat label={i18nT('pages.performanceTab.load_15m')} value={fmtNumber(d.load_15m, { maximumFractionDigits: 2 })} />
          <Stat label={i18nT('pages.performanceTab.threads')} value={fmtNumber(d.thread_count)} />
          <Stat label={i18nT('pages.performanceTab.processes_mcp')} value={fmtNumber(d.mcp_total ?? 0)} />
          {d.mcp_processes && (
            <>
              <Stat label={i18nT('pages.performanceTab.sandbox_procs')} value={fmtNumber(d.mcp_processes.sandbox)} />
              <Stat label={i18nT('pages.performanceTab.kiro_cli_procs')} value={fmtNumber(d.mcp_processes.kiro_cli)} />
              <Stat label={i18nT('pages.performanceTab.builder_mcp_procs')} value={fmtNumber(d.mcp_processes.builder_mcp)} />
            </>
          )}
          {/* child_processes excluded: reads /proc/<pid>/task (threads), contradicts thread_count */}
        </>
      )
    case 'memory':
      return (
        <>
          <Stat label={i18nT('pages.performanceTab.total')} value={fmtUnit(d.mem_total_gb, 'gigabyte', { maximumFractionDigits: 1 })} />
          <Stat label={i18nT('pages.performanceTab.used')} value={fmtUnit(d.mem_used_gb, 'gigabyte', { maximumFractionDigits: 1 })} />
          <Stat label={i18nT('pages.performanceTab.free')} value={fmtUnit(d.mem_free_gb, 'gigabyte', { maximumFractionDigits: 1 })} />
          <Stat label={i18nT('pages.performanceTab.gateway_rss')} value={fmtUnit(d.proc_mem_mb, 'megabyte', { maximumFractionDigits: 0 })} />
        </>
      )
    case 'disk':
      return (
        <>
          <Stat label={i18nT('pages.performanceTab.total')} value={fmtUnit(d.disk_total_gb, 'gigabyte', { maximumFractionDigits: 0 })} />
          <Stat label={i18nT('pages.performanceTab.free')} value={fmtUnit(d.disk_free_gb, 'gigabyte', { maximumFractionDigits: 0 })} />
          <Stat
            label={i18nT('pages.performanceTab.used_pct')}
            value={fmtPercent((d.disk_total_gb - d.disk_free_gb) / (d.disk_total_gb || 1))}
          />
        </>
      )
    case 'network':
      return (
        <>
          <Stat label={i18nT('pages.performanceTab.ip_address')} value={d.ip} />
          <Stat label={i18nT('pages.performanceTab.download')} value={fmtUnit(d.net_rx_kbs, 'kilobyte-per-second', { maximumFractionDigits: 0 })} />
          <Stat label={i18nT('pages.performanceTab.upload')} value={fmtUnit(d.net_tx_kbs, 'kilobyte-per-second', { maximumFractionDigits: 0 })} />
        </>
      )
  }
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2 py-1">
      <span className="text-[11.5px] text-muted whitespace-nowrap">{label}</span>
      <span className="text-[12.5px] font-mono tabular-nums text-text-strong">{value}</span>
    </div>
  )
}
