import { Clock, Code2, FolderGit2, PlugZap } from 'lucide-react'
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Card, CardTitle } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { findReport } from '../../utils/errorReport'
import { api } from '../../api/client'
import type { WakaTimeStats, WakaTimeStatsEntry } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { fmtDuration } from '../../i18n/format'

// Ranges the stats endpoint accepts, in the order shown in the toolbar. The
// value is the API's named range; labelKey is the full literal i18n key so the
// key never gets assembled from parts (unused-key + extractor tooling can see
// each one statically).
const RANGES = [
  { value: 'today', labelKey: 'pages.overview.wakatimeTab.range_today' },
  { value: 'last_7_days', labelKey: 'pages.overview.wakatimeTab.range_last7' },
  { value: 'last_30_days', labelKey: 'pages.overview.wakatimeTab.range_last30' },
  { value: 'last_6_months', labelKey: 'pages.overview.wakatimeTab.range_last6mo' },
] as const
const DEFAULT_RANGE = 'last_7_days'

// Locale-aware h/m formatting via the i18n duration helper: the unit labels
// and digit shaping follow the active locale, and dropZero hides a zero hour.
function fmtHm(totalSeconds: number): string {
  // Round to whole minutes FIRST, then split: rounding the remainder minutes
  // independently overflows to 60 (7199s -> 1h + round(59.98)=60m = "1h 60m").
  const totalMin = Math.round(Math.max(0, totalSeconds) / 60)
  const h = Math.floor(totalMin / 60)
  const m = totalMin % 60
  return fmtDuration([[h, 'hour'], [m, 'minute']], { dropZero: true })
}

// The export endpoint takes a start/end date range; derive them from the named
// range so the download covers the same window the stats view shows.
function rangeDates(range: string): { start: string; end: string } {
  const end = new Date()
  const start = new Date(end)
  if (range === 'last_6_months') {
    // Six CALENDAR months back. Set day 1 BEFORE shifting the month so setMonth
    // cannot overflow (Aug 31 - 6 = "Feb 31" would roll to Mar 3 and omit days),
    // then clamp the original day to the target month's last day.
    const day = end.getDate()
    start.setDate(1)
    start.setMonth(end.getMonth() - 6)
    const lastDay = new Date(start.getFullYear(), start.getMonth() + 1, 0).getDate()
    start.setDate(Math.min(day, lastDay))
  } else {
    const days: Record<string, number> = {
      today: 0,
      last_7_days: 6,
      last_30_days: 29,
    }
    start.setDate(end.getDate() - (days[range] ?? 6))
  }
  // Format from LOCAL date parts, not toISOString (which is UTC): a user east
  // of UTC near midnight would otherwise export the previous calendar day and
  // omit today, so the billable window would not match the day they see.
  const iso = (d: Date) =>
    `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  return { start: iso(start), end: iso(end) }
}

export default function WakaTimeTab() {
  const [range, setRange] = useState<string>(DEFAULT_RANGE)
  const [exportErr, setExportErr] = useState<string | null>(null)
  const { data, error: queryErr, isLoading } = useQuery<WakaTimeStats>({
    queryKey: ['wakatime-stats', range],
    queryFn: () => api.wakatimeStats(range),
  })

  // A 502 from the upstream-unavailable guard surfaces here as a query error;
  // show it through the shared ErrorNotice so the structured status, endpoint
  // and error-code context and the ask-agent affordance are preserved, rather
  // than a bare "0 hours" view.
  if (queryErr) {
    return (
      <Card>
        <ErrorNotice
          title={i18nT('pages.overview.wakatimeTab.unreachable_title')}
          message={i18nT('pages.overview.wakatimeTab.unreachable_body')}
          report={findReport(queryErr instanceof Error ? queryErr.message : undefined)}
          askAgent
        />
      </Card>
    )
  }

  if (isLoading || !data) return <Card><div className="skeleton h-40 rounded" /></Card>

  // Integration off / no key: an ordinary connect prompt, not an error.
  if (!data.configured) {
    return (
      <Card>
        <div className="flex flex-col items-center text-center gap-3 py-6">
          <PlugZap className="lucide-inline text-accent" />
          <div className="font-medium text-text-strong">{i18nT('pages.overview.wakatimeTab.connect_title')}</div>
          <div className="text-muted text-sm max-w-md">{i18nT('pages.overview.wakatimeTab.connect_body')}</div>
          <div className="text-muted text-[12.5px] max-w-md">{i18nT('pages.overview.wakatimeTab.connect_hint')}</div>
        </div>
      </Card>
    )
  }

  const stats = data.stats ?? {}
  const languages = stats.languages ?? []
  const projects = stats.projects ?? []
  const total = stats.total_seconds ?? 0
  const dailyAvg = stats.daily_average ?? 0
  const hasActivity = total > 0 || languages.length > 0 || projects.length > 0

  const download = async (format: 'csv' | 'json') => {
    setExportErr(null)
    try {
      // Compute the window at click time: a tab left open across local midnight
      // would otherwise reuse the render-time bounds and omit the current day.
      const { start, end } = rangeDates(range)
      // Fetch and save rather than navigating: a 502 here raises instead of
      // replacing the dashboard with the endpoint's raw error body.
      await api.wakatimeExportDownload(start, end, format)
    } catch (e) {
      setExportErr(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="space-y-4">
      <p className="text-muted text-[12.5px] -mt-1">{i18nT('pages.overview.wakatimeTab.subtitle')}</p>
      {/* Toolbar: range control + export */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="inline-flex rounded-md border border-border overflow-hidden" role="group"
          aria-label={i18nT('pages.overview.wakatimeTab.range_label')}>
          {RANGES.map(r => (
            <button
              key={r.value}
              type="button"
              onClick={() => setRange(r.value)}
              aria-pressed={range === r.value}
              className={`px-3 py-1.5 text-[12.5px] border-r border-border last:border-r-0 transition-colors ${
                range === r.value ? 'bg-accent-subtle text-accent' : 'text-muted hover:text-text'
              }`}
            >
              {i18nT(r.labelKey)}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={() => download('csv')}
          className="ml-auto px-3.5 py-1.5 rounded-md bg-accent text-accent-fg text-[12.5px] font-semibold"
        >
          {i18nT('pages.overview.wakatimeTab.export_csv')}
        </button>
        <button
          type="button"
          onClick={() => download('json')}
          className="px-3.5 py-1.5 rounded-md border border-border text-text text-[12.5px]"
        >
          {i18nT('pages.overview.wakatimeTab.export_json')}
        </button>
      </div>

      {exportErr && (
        <ErrorNotice
          title={i18nT('pages.overview.wakatimeTab.export_failed_title')}
          message={exportErr}
          askAgent
          onDismiss={() => setExportErr(null)}
        />
      )}

      {!hasActivity ? (
        <Card>
          <div className="flex flex-col items-center text-center gap-2 py-6 text-muted">
            <div className="font-medium text-text-strong">{i18nT('pages.overview.wakatimeTab.no_activity_title')}</div>
            <div className="text-sm">{i18nT('pages.overview.wakatimeTab.no_activity_body')}</div>
          </div>
        </Card>
      ) : (
        <>
          <Card>
            <CardTitle><Clock className="lucide-inline" /> {i18nT('pages.overview.wakatimeTab.coding_time')}</CardTitle>
            <div className="grid grid-cols-3 gap-4 max-[600px]:grid-cols-1">
              <Stat label={i18nT('pages.overview.wakatimeTab.this_range')} value={fmtHm(total)} />
              <Stat label={i18nT('pages.overview.wakatimeTab.daily_average')} value={fmtHm(dailyAvg)} />
              <Stat label={i18nT('pages.overview.wakatimeTab.active_projects')} value={String(projects.length)} />
            </div>
          </Card>

          {languages.length > 0 && (
            <Card>
              <CardTitle><Code2 className="lucide-inline" /> {i18nT('pages.overview.wakatimeTab.languages')}</CardTitle>
              <BarList entries={languages} />
            </Card>
          )}

          {projects.length > 0 && (
            <Card>
              <CardTitle><FolderGit2 className="lucide-inline" /> {i18nT('pages.overview.wakatimeTab.projects')}</CardTitle>
              <BarList entries={projects} />
            </Card>
          )}
        </>
      )}
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-2xl font-bold text-text-strong font-mono">{value}</div>
      <div className="text-muted text-[12px] mt-0.5">{label}</div>
    </div>
  )
}

function BarList({ entries }: { entries: WakaTimeStatsEntry[] }) {
  const max = Math.max(...entries.map(e => e.total_seconds), 1)
  // Show the top rows; a very long tail adds noise without insight.
  const rows = [...entries].sort((a, b) => b.total_seconds - a.total_seconds).slice(0, 8)
  return (
    <div className="space-y-1.5">
      {rows.map(e => (
        <div key={e.name} className="flex items-center gap-3 text-[13px]">
          <span className="w-28 shrink-0 truncate text-text" title={e.name}>{e.name}</span>
          <span className="flex-1 h-2 rounded-full bg-bg-elevated overflow-hidden">
            <span className="block h-full rounded-full bg-accent" style={{ width: `${(e.total_seconds / max) * 100}%` }} />
          </span>
          <span className="w-16 shrink-0 text-right text-muted font-mono text-[12px]">{fmtHm(e.total_seconds)}</span>
        </div>
      ))}
    </div>
  )
}
