import { useState, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { Btn } from './ui'
import LogEntry, { type LogEntryData } from './LogEntry'

const PAGE_SIZE = 10

export default function JobLogsView({ jobId, isRunning, runningSince, onCancel }: { jobId: string; isRunning?: boolean; runningSince?: number | null; onCancel?: () => void }) {
  const [page, setPage] = useState(0)
  const [elapsed, setElapsed] = useState(0)

  useEffect(() => { setPage(0) }, [jobId])

  const { data, isLoading, error } = useQuery({
    queryKey: ['cron-history', jobId, page],
    queryFn: async () => {
      const res = await api.cronHistory(jobId, page * PAGE_SIZE, PAGE_SIZE)
      return { entries: (res.runs || []) as LogEntryData[], total: res.total ?? 0 }
    },
  })

  // Live elapsed timer
  useEffect(() => {
    if (!isRunning || !runningSince) { setElapsed(0); return }
    setElapsed(Math.floor(Date.now() / 1000 - runningSince))
    const id = setInterval(() => setElapsed(Math.floor(Date.now() / 1000 - runningSince)), 1000)
    return () => clearInterval(id)
  }, [isRunning, runningSince])

  const entries = data?.entries ?? []
  const total = data?.total ?? 0
  const totalPages = Math.ceil(total / PAGE_SIZE)

  return (
    <div className="flex flex-col gap-3">
      {isRunning && (
        <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-ok-subtle border border-ok/20">
          <span className="w-2 h-2 rounded-full bg-ok animate-pulse" />
          <span className="text-[13px] text-ok font-medium">Currently running…</span>
          <span className="ml-auto flex items-center gap-2">
            {elapsed > 0 && <span className="text-[12px] text-muted">{elapsed}s elapsed</span>}
            {onCancel && <Btn danger onClick={onCancel} title="Cancel running execution">Cancel</Btn>}
          </span>
        </div>
      )}
      {error ? (
        <div className="text-danger text-sm px-3">{error instanceof Error ? error.message : 'Failed to load history'}</div>
      ) : isLoading ? (
        <div className="text-muted text-sm px-3 animate-pulse">Loading logs…</div>
      ) : entries.length === 0 ? (
        <div className="text-muted text-sm px-3 py-4 text-center">No execution history yet</div>
      ) : (
        <>
          <div className="border border-border rounded-lg overflow-hidden">
            {entries.map(e => <LogEntry key={e.run_id} entry={e} jobId={jobId} />)}
          </div>
          {totalPages > 1 && (
            <div className="flex items-center justify-between px-1">
              <Btn onClick={() => setPage(p => p - 1)} disabled={page === 0}>← Prev</Btn>
              <span className="text-[12px] text-muted">{page + 1} / {totalPages}</span>
              <Btn onClick={() => setPage(p => p + 1)} disabled={page >= totalPages - 1}>Next →</Btn>
            </div>
          )}
        </>
      )}
    </div>
  )
}
