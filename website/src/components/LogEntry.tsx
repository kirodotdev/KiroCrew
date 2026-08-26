import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronRight } from 'lucide-react'
import { Badge } from './ui'
import { api } from '../api/client'

import { i18nT } from '../i18n/t'
import { fmtDateTimeNumeric, fmtDuration as fmtDurationParts, fmtUnit } from '../i18n/format'
export interface LogEntryData {
  run_id: string
  status: 'success' | 'failure' | 'timeout' | 'cancelled'
  started_at: number
  duration_ms?: number | null
  trigger: 'scheduled' | 'manual'
  summary?: string
}

function fmtDuration(ms?: number | null) {
  if (ms == null) return '—'
  if (ms < 1000) return fmtUnit(ms, 'millisecond', { maximumFractionDigits: 0 })
  const s = Math.floor(ms / 1000)
  if (s < 60) return fmtUnit(s, 'second', { maximumFractionDigits: 0 })
  return fmtDurationParts([[Math.floor(s / 60), 'minute'], [s % 60, 'second']])
}

export default function LogEntry({ entry, jobId }: { entry: LogEntryData; jobId: string }) {
  const [open, setOpen] = useState(false)

  const { data: trace, isLoading } = useQuery({
    queryKey: ['cron-trace', jobId, entry.run_id],
    queryFn: async () => {
      const res = await api.cronRunDetail(jobId, entry.run_id)
      return res.trace || res.output || '(no output)'
    },
    enabled: open,
  })

  const statusBadge = entry.status === 'success' ? <Badge variant="ok">{i18nT('components.logEntry.ok')}</Badge>
    : entry.status === 'failure' ? <Badge variant="err">{i18nT('components.logEntry.error')}</Badge>
    : entry.status === 'cancelled' ? <Badge variant="warn">{i18nT('components.logEntry.cancelled')}</Badge>
    : <Badge variant="warn">{i18nT('components.logEntry.timeout')}</Badge>

  const triggerPill = (
    <span className={`px-1.5 py-[1px] rounded-full text-[11px] font-medium ${
      entry.trigger === 'manual' ? 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-300' : 'bg-bg-elevated text-muted border border-border'
    }`}>{entry.trigger}</span>
  )

  return (
    <div className="border-b border-border last:border-b-0">
      <button
        onClick={() => setOpen(v => !v)}
        className="w-full flex items-center gap-3 px-3 py-2.5 text-left hover:bg-bg-hover transition-colors cursor-pointer bg-transparent border-none"
      >
        <ChevronRight size={14} className={`shrink-0 text-muted transition-transform ${open ? 'rotate-90' : ''}`} />
        {statusBadge}
        <span className="text-[12px] text-muted shrink-0">{fmtDateTimeNumeric(entry.started_at)}</span>
        <span className="text-[12px] text-muted shrink-0">{fmtDuration(entry.duration_ms)}</span>
        {triggerPill}
        <span className="text-sm text-text truncate min-w-0 flex-1">{entry.summary || '—'}</span>
      </button>
      {open && (
        <div className="px-3 pb-3 pl-9">
          {isLoading ? (
            <div className="text-[12px] text-muted animate-pulse">{i18nT('components.logEntry.loading_trace')}</div>
          ) : (
            <pre className="p-2.5 bg-bg-elevated border border-border rounded-md text-[12px] font-mono whitespace-pre-wrap break-words max-h-[300px] overflow-y-auto leading-relaxed">{trace}</pre>
          )}
        </div>
      )}
    </div>
  )
}
