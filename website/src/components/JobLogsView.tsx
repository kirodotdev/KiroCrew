import { useState, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { Btn, Checkbox } from './ui'
import ErrorNotice from './ErrorNotice'
import LogEntry, { type LogEntryData } from './LogEntry'
import { usePagerFocus } from '../hooks/usePagerFocus'
import { usePersistedBool } from '../hooks/usePersistedBool'

import { i18nT } from '../i18n/t'
const PAGE_SIZE = 10
/** The "Hide idle runs" choice, kept per browser for every script job. */
const HIDE_IDLE_KEY = 'kc-cron-logs-hide-idle'

export default function JobLogsView({ jobId, jobName, isRunning, runningSince, onCancel, cancelError, canHideIdle }: { jobId: string; jobName?: string; isRunning?: boolean; runningSince?: number | null; onCancel?: () => void; cancelError?: string | null; /** A script job, whose Skip() runs can be hidden. */ canHideIdle?: boolean }) {
  const [page, setPage] = useState(0)
  const [hideIdleChoice, setHideIdleChoice] = usePersistedBool(HIDE_IDLE_KEY, false)
  const hideIdle = !!canHideIdle && hideIdleChoice
  const [elapsed, setElapsed] = useState(0)
  const pager = usePagerFocus()

  useEffect(() => { setPage(0) }, [jobId, hideIdle])

  const { data, isLoading, error } = useQuery({
    queryKey: ['cron-history', jobId, page, hideIdle],
    queryFn: async () => {
      const res = await api.cronHistory(jobId, page * PAGE_SIZE, PAGE_SIZE, hideIdle)
      return { entries: (res.runs || []) as LogEntryData[], total: res.total ?? 0, idleHidden: res.idle_hidden ?? 0 }
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
  const idleHidden = data?.idleHidden ?? 0
  const totalPages = Math.ceil(total / PAGE_SIZE)

  // A page past the end (the filtered total shrank under it) moves back to the last page.
  useEffect(() => { if (data && page > 0 && page >= totalPages) setPage(Math.max(0, totalPages - 1)) }, [data, page, totalPages])

  return (
    <div className="flex flex-col gap-3">
      {isRunning && (
        <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-ok-subtle border border-ok/20">
          <span className="w-2 h-2 rounded-full bg-ok animate-pulse" />
          <span className="text-[13px] text-ok font-medium">{i18nT('components.jobLogsView.currently_running')}</span>
          <span className="ml-auto flex items-center gap-2">
            {elapsed > 0 && <span className="text-[12px] text-muted">{elapsed}{i18nT('components.jobLogsView.s_elapsed')}</span>}
            {/* The stock Btn is transparent, so on the ok tint it has no edge until hovered. */}
            {onCancel && <Btn danger onClick={onCancel} className="border-border-strong bg-bg-elevated" title={i18nT('components.jobLogsView.cancel_running_execution')}>{i18nT('components.jobLogsView.cancel')}</Btn>}
          </span>
        </div>
      )}
      {/* A cancel failure sets panelError in the parent; on the Logs tab that
          error had no DOM anchor (it only rendered in the Details branch), so a
          failed Cancel showed no feedback. Render it here too. */}
      {cancelError && <ErrorNotice message={cancelError} variant="inline" askAgent className="px-3" />}
      {canHideIdle && (
        <div className="flex items-center gap-2 px-1 text-[12px] text-muted">
          <label className="flex items-center gap-2 cursor-pointer">
            <Checkbox checked={hideIdle} onChange={e => setHideIdleChoice(e.target.checked)} />
            {i18nT('components.jobLogsView.hide_idle_runs')}
          </label>
          {idleHidden > 0 && <span className="ml-auto">{i18nT('components.jobLogsView.idle_runs_hidden', { count: idleHidden })}</span>}
        </div>
      )}
      {error ? (
        <div className="px-3"><ErrorNotice message={error instanceof Error ? error.message : i18nT('components.jobLogsView.failed_to_load_history')} askAgent /></div>
      ) : isLoading ? (
        <div className="text-muted text-sm px-3 animate-pulse">{i18nT('components.jobLogsView.loading_logs')}</div>
      ) : entries.length === 0 ? (
        idleHidden > 0 ? null : <div className="text-muted text-sm px-3 py-4 text-center">{i18nT('components.jobLogsView.no_execution_history_yet')}</div>
      ) : (
        <>
          <div className="border border-border rounded-lg overflow-hidden">
            {entries.map(e => <LogEntry key={e.run_id} entry={e} jobId={jobId} jobName={jobName} />)}
          </div>
          {totalPages > 1 && (
            <div className="flex items-center justify-between px-1">
              <Btn ref={pager.prevRef} onClick={() => { setPage(p => p - 1); pager.pressedPrev() }} disabled={page === 0}>{i18nT('components.jobLogsView.prev')}</Btn>
              <span className="text-[12px] text-muted">{page + 1} / {totalPages}</span>
              <Btn ref={pager.nextRef} onClick={() => { setPage(p => p + 1); pager.pressedNext() }} disabled={page >= totalPages - 1}>{i18nT('components.jobLogsView.next')}</Btn>
            </div>
          )}
        </>
      )}
    </div>
  )
}
