import { useState, useEffect, Fragment } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronRight } from 'lucide-react'
import { api } from '../api/client'
import { Badge, Btn, Skeleton } from './ui'
import { useAppSelector } from '../store'

import { i18nT } from '../i18n/t'
interface HistoryEntry {
  job_id: string
  job_name: string
  run_id: string
  started_at: number
  duration_ms: number
  status: 'success' | 'failure' | 'timeout' | 'cancelled'
  trigger: string
  summary?: string
}

const PAGE_SIZE = 20

const fmtDuration = (ms: number) => {
  if (ms < 1000) return `${ms}ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(1)}s`
  return `${Math.floor(s / 60)}m ${Math.floor(s % 60)}s`
}

const fmtTime = (ts: number) => new Date(ts * 1000).toLocaleString()

export default function ExecutionsView({ selectedJobId }: { selectedJobId?: string }) {
  const [page, setPage] = useState(0)
  const [expanded, setExpanded] = useState<string | null>(null)
  const refreshTrigger = useAppSelector(s => s.dashboard.refreshTrigger)

  const { data: jobsData } = useQuery({
    queryKey: ['cron-jobs', refreshTrigger],
    queryFn: () => api.crons().then(r => r.jobs || []),
  })
  const jobIds = new Set((jobsData || []).map((j: { id: string }) => j.id))

  const { data, isLoading, error } = useQuery({
    queryKey: ['cron-history-all', page, selectedJobId, refreshTrigger],
    queryFn: async () => {
      const res = await api.cronHistoryAll({ offset: page * PAGE_SIZE, limit: PAGE_SIZE + 1, jobId: selectedJobId })
      const items: HistoryEntry[] = res.runs || []
      return { entries: items.slice(0, PAGE_SIZE), hasMore: items.length > PAGE_SIZE }
    },
  })

  useEffect(() => { setPage(0) }, [selectedJobId])

  const entries = data?.entries ?? []
  const hasMore = data?.hasMore ?? false

  const toggleExpand = (entry: HistoryEntry) => {
    setExpanded(prev => prev === entry.run_id ? null : entry.run_id)
  }

  if (isLoading) return <div className="flex justify-center py-12"><Skeleton className="h-6 w-32 rounded" /></div>
  if (error) return <div className="text-danger text-sm text-center py-8">{error instanceof Error ? error.message : i18nT('components.executionsView.failed_to_load')}</div>
  if (entries.length === 0) return <div className="text-muted text-sm text-center py-12">{i18nT('components.executionsView.no_execution_history_yet')}</div>

  return (
    <div>
      <div className="overflow-x-auto">
        <table className="w-full border-collapse table-striped">
          <thead><tr>
            <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[70px]">{i18nT('components.executionsView.status')}</th>
            <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('components.executionsView.job_name')}</th>
            <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[160px]">{i18nT('components.executionsView.timestamp')}</th>
            <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[90px]">{i18nT('components.executionsView.duration')}</th>
            <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[80px]">{i18nT('components.executionsView.trigger')}</th>
            <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('components.executionsView.summary')}</th>
          </tr></thead>
          <tbody>
            {entries.map(e => {
              const isExpanded = expanded === e.run_id
              const isDeleted = jobIds.size > 0 && !jobIds.has(e.job_id)
              return (<Fragment key={e.run_id}>
                <tr className={`hover:bg-bg-hover transition-colors cursor-pointer ${isDeleted ? 'opacity-50' : ''}`} onClick={() => toggleExpand(e)}>
                  <td className="px-2.5 py-2 border-b border-border text-sm">
                    {e.status === 'success' ? <Badge variant="ok">{i18nT('components.executionsView.ok')}</Badge> : e.status === 'failure' ? <Badge variant="err">{i18nT('components.executionsView.error')}</Badge> : e.status === 'cancelled' ? <Badge variant="warn">{i18nT('components.executionsView.cancelled')}</Badge> : <Badge variant="warn">{i18nT('components.executionsView.timeout')}</Badge>}
                  </td>
                  <td className="px-2.5 py-2 border-b border-border text-sm">{e.job_name}{isDeleted && <span className="ml-1.5 text-muted text-[11px]">{i18nT('components.executionsView.deleted')}</span>}</td>
                  <td className="px-2.5 py-2 border-b border-border text-sm text-muted">{fmtTime(e.started_at)}</td>
                  <td className="px-2.5 py-2 border-b border-border text-sm text-muted">{e.status === 'timeout' ? '…' : fmtDuration(e.duration_ms)}</td>
                  <td className="px-2.5 py-2 border-b border-border text-sm"><span className="px-1.5 py-[1px] rounded text-[11px] bg-bg-elevated border border-border">{e.trigger}</span></td>
                  <td className="px-2.5 py-2 border-b border-border text-sm text-muted truncate max-w-[300px]">
                    <span className="flex items-center gap-1">
                      <ChevronRight size={12} className={`shrink-0 transition-transform ${isExpanded ? 'rotate-90' : ''}`} />
                      {e.summary || '—'}
                    </span>
                  </td>
                </tr>
                {isExpanded && <TraceRow entry={e} />}
              </Fragment>)
            })}
          </tbody>
        </table>
      </div>
      <div className="flex items-center justify-between mt-3 px-1">
        <span className="text-[12px] text-muted">{i18nT('components.executionsView.page')} {page + 1}</span>
        <div className="flex gap-2">
          <Btn onClick={() => setPage(p => p - 1)} disabled={page === 0}>{i18nT('components.executionsView.prev')}</Btn>
          <Btn onClick={() => setPage(p => p + 1)} disabled={!hasMore}>{i18nT('components.executionsView.next')}</Btn>
        </div>
      </div>
    </div>
  )
}

function TraceRow({ entry }: { entry: HistoryEntry }) {
  const { data: trace, isLoading } = useQuery({
    queryKey: ['cron-trace', entry.job_id, entry.run_id],
    queryFn: async () => {
      const res = await api.cronRunDetail(entry.job_id, entry.run_id)
      return res.trace || res.output || JSON.stringify(res, null, 2)
    },
  })

  return (
    <tr>
      <td colSpan={6} className="px-4 py-3 border-b border-border bg-bg-elevated">
        {isLoading ? <Skeleton className="h-4 w-48 rounded" /> : (
          <pre className="text-[12px] font-mono whitespace-pre-wrap break-words max-h-[300px] overflow-y-auto leading-relaxed">{trace}</pre>
        )}
      </td>
    </tr>
  )
}
