import { useEffect, useId, useState } from 'react'
import { Sparkles } from 'lucide-react'
import { keepPreviousData, useMutation, useQuery } from '@tanstack/react-query'
import ErrorNotice from '../../components/ErrorNotice'
import { api } from '../../api/client'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import type { SidebarScope } from './scope'

const BULK_TITLES_POLL_MS = 1_000

export function useBulkTitles() {
  const [bulkTitlesOpen, setBulkTitlesOpen] = useState(false)
  const [includeRenamed, setIncludeRenamed] = useState(false)
  const [started, setStarted] = useState<number | null>(null)
  const [runId, setRunId] = useState<number | null>(null)
  const [appliedAt, setAppliedAt] = useState(0)
  const includeRenamedLabelId = useId()
  const status = useQuery({
    queryKey: ['bulk-titles-status'],
    queryFn: () => api.chatSlotsGenerateTitlesStatus(),
    enabled: bulkTitlesOpen,
    staleTime: 0,
    gcTime: 0,
    retry: false,
    refetchInterval: q => {
      const latest = q.state.data?.run
      const waiting = runId !== null && latest?.id !== runId && q.state.dataUpdatedAt < appliedAt
      return q.state.status === 'error' || waiting || (latest != null && !latest.finished) ? BULK_TITLES_POLL_MS : false
    },
  })
  const statusRun = status.data?.run ?? null
  useEffect(() => {
    if (runId === null && statusRun && !statusRun.finished) setRunId(statusRun.id)
  }, [runId, statusRun])
  const run = statusRun && (runId === null ? !statusRun.finished : statusRun.id === runId) ? statusRun : null
  const preview = useQuery({
    queryKey: ['bulk-titles-preview', includeRenamed],
    queryFn: () => api.chatSlotsGenerateTitles({ includeRenamed, dryRun: true }),
    enabled: bulkTitlesOpen && started === null && run === null && status.isFetched,
    staleTime: 0,
    gcTime: 0,
    retry: false,
    placeholderData: keepPreviousData,
  })
  const apply = useMutation({
    mutationFn: () => api.chatSlotsGenerateTitles({ includeRenamed, dryRun: false }),
    onSuccess: (res) => {
      if (res.run_id == null) {
        void preview.refetch()
        return
      }
      setStarted(res.queued.length)
      setRunId(res.run_id ?? null)
      setAppliedAt(Date.now())
      void status.refetch()
    },
    onError: () => {
      void status.refetch()
    },
  })
  const openBulkTitles = () => {
    setIncludeRenamed(false)
    setStarted(null)
    setRunId(null)
    setAppliedAt(0)
    apply.reset()
    setBulkTitlesOpen(true)
  }
  const closeBulkTitles = () => setBulkTitlesOpen(false)
  return { bulkTitlesOpen, openBulkTitles, closeBulkTitles, includeRenamed, setIncludeRenamed, includeRenamedLabelId, preview, apply, started, run, statusFailed: status.isError }
}

export function renderBulkTitlesPanel(shell: SidebarScope): React.ReactNode {
  const { apply, closeBulkTitles, includeRenamed, includeRenamedLabelId, preview, run, setIncludeRenamed, started, statusFailed } = shell.bulkTitles
  const plan = preview.data
  const queued = plan?.queued.length ?? 0
  const inProgress = started !== null || run !== null
  return (
    <div data-testid="bulk-titles-panel" className="mx-2 mb-2 p-3 rounded-lg bg-bg border border-border shadow-md text-sm animate-rise">
      <div className="font-medium text-text-strong mb-2"><Sparkles size={14} className="lucide-inline" /> {i18nT('pages.chatSidebar.regenerate_all_titles')}</div>
      <div className="text-muted text-[12px] mb-2">{i18nT('pages.chatSidebar.bulk_titles_description')}</div>
      <ErrorNotice message={statusFailed ? i18nT('pages.chatSidebar.bulk_titles_status_failed') : ''} askAgent className="mb-2" testId="bulk-titles-status-error" />
      {inProgress ? (
        <div data-testid="bulk-titles-started" role="status" className="text-[12px] text-muted mb-2 space-y-0.5">
          <div>{i18nT('pages.chatSidebar.bulk_titles_started', { count: run?.total ?? started ?? 0 })}</div>
          {run && !run.finished && (
            <div data-testid="bulk-titles-progress">{i18nT('pages.chatSidebar.bulk_titles_progress', { done: run.done, total: run.total })}</div>
          )}
          {run?.finished && (
            <div data-testid="bulk-titles-finished">
              <div>{i18nT('pages.chatSidebar.bulk_titles_finished', { n: run.retitled })}</div>
              {run.unchanged > 0 && <div>{i18nT('pages.chatSidebar.bulk_titles_unchanged', { n: run.unchanged })}</div>}
            </div>
          )}
        </div>
      ) : null}
      {inProgress && (
        <ErrorNotice message={run?.finished && run.failed > 0 ? i18nT('pages.chatSidebar.bulk_titles_failed_count', { n: run.failed }) : ''} askAgent className="mb-2" testId="bulk-titles-failed-count" />
      )}
      {inProgress ? null : (
        <>
          <ErrorNotice message={preview.isError ? i18nT('pages.chatSidebar.bulk_titles_preview_failed') : ''} askAgent className="mb-2" testId="bulk-titles-preview-error" />
          {plan && (
            <div data-testid="bulk-titles-counts" role="status" className="text-[12px] text-muted mb-2 space-y-0.5">
              <div>{i18nT('pages.chatSidebar.bulk_titles_to_retitle', { n: queued })}</div>
              {plan.skipped_running.length > 0 && <div>{i18nT('pages.chatSidebar.bulk_titles_skipped_running', { n: plan.skipped_running.length })}</div>}
              {plan.skipped_recent.length > 0 && <div>{i18nT('pages.chatSidebar.bulk_titles_skipped_recent', { n: plan.skipped_recent.length })}</div>}
              {plan.skipped_empty.length > 0 && <div>{i18nT('pages.chatSidebar.bulk_titles_skipped_empty', { n: plan.skipped_empty.length })}</div>}
            </div>
          )}
          {plan && plan.renamed > 0 && (
            <label className="flex items-center gap-2 text-[12px] text-muted mb-2 cursor-pointer">
              <input type="checkbox" aria-labelledby={includeRenamedLabelId} checked={includeRenamed} onChange={e => setIncludeRenamed(e.target.checked)} />
              <span id={includeRenamedLabelId}>{i18nT('pages.chatSidebar.bulk_titles_include_renamed', { n: plan.renamed })}</span>
            </label>
          )}
          <ErrorNotice message={apply.isError ? i18nT('pages.chatSidebar.bulk_titles_failed') : ''} askAgent className="mb-2" testId="bulk-titles-error" />
        </>
      )}
      <div className="flex items-center gap-2 justify-end">
        {inProgress ? (
          <Btn className="text-[12px] px-3 py-1" onClick={closeBulkTitles}>{i18nT('pages.chatSidebar.close')}</Btn>
        ) : (
          <>
            <Btn className="text-[12px] px-3 py-1" onClick={closeBulkTitles}>{i18nT('pages.chatSidebar.cancel')}</Btn>
            <Btn data-testid="bulk-titles-confirm" className="text-[12px] px-3 py-1 bg-accent text-accent-fg hover:bg-accent-hover" disabled={!plan || queued === 0 || apply.isPending || preview.isFetching} onClick={() => apply.mutate()}>{i18nT('pages.chatSidebar.bulk_titles_confirm')}</Btn>
          </>
        )}
      </div>
    </div>
  )
}
