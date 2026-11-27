import { useState, useCallback } from 'react'
import { Play } from 'lucide-react'
import { api } from '../api/client'
import { Btn, SendBtn } from '../components/ui'
import ErrorNotice from '../components/ErrorNotice'
import { TELEGRAM_CHANNEL } from '../components/TelegramDestinationSelect'
import {
  Dialog, DialogContent, DialogHeader, DialogBody, DialogFooter, DialogTitle, DialogDescription,
} from '../components/ui/dialog'
import { useMenuOpensDialog } from '../hooks/useMenuOpensDialog'
import { batchActionLabel, batchTargets, telegramTargets, type CronBatchAction } from '../components/CronBatchActionsMenu'
import type { CronJob } from '../types'
import { i18nT } from '../i18n/t'

// The run and enable routes report some refusals in the body, not the status.
const isFailure = (r: PromiseSettledResult<unknown>) =>
  r.status === 'rejected' || !!(r.value as { error?: string } | undefined)?.error

/** The server's reason for one failed call: an ApiError's `message` (the friendly
 *  text) for a rejection, or the body `error` string for an in-body refusal; `''`
 *  for a rejection with no human message. */
const failureReason = (r: PromiseSettledResult<unknown>) => r.status === 'rejected'
  ? (r.reason instanceof Error ? r.reason.message : '')
  : ((r.value as { error?: string } | undefined)?.error ?? '')

/** State and handlers behind the Schedule page's batch Actions menu and its Run now confirm. */
export function useScheduleBatchActions({
  selectedJobs, running, load, setSelectedIds, setActionError, setBatchConfirm,
}: {
  selectedJobs: CronJob[]
  running: Set<string>
  load: () => Promise<void>
  setSelectedIds: (ids: Set<string>) => void
  setActionError: (err: { id: string; msg: string } | null) => void
  setBatchConfirm: (open: boolean) => void
}) {
  const [batchBusy, setBatchBusy] = useState(false)
  const [runConfirmIds, setRunConfirmIds] = useState<string[] | null>(null)
  const [runSkipped, setRunSkipped] = useState<SkippedJob[]>([])
  const [telegramResult, setTelegramResult] = useState<TelegramResult>({ updated: 0, destination: '', kept: [] })
  const batchMenuLayer = useMenuOpensDialog()
  const closeRunConfirm = useCallback(() => { setRunConfirmIds(null); batchMenuLayer.returnFocus() }, [batchMenuLayer])
  const closeBatchConfirm = useCallback(() => { setBatchConfirm(false); batchMenuLayer.returnFocus() }, [batchMenuLayer, setBatchConfirm])
  const applyBatch = useCallback(async (action: CronBatchAction, ids?: string[]) => {
    const eligible = batchTargets(action, selectedJobs, running).map(j => j.id)
    // A confirmed id list may be stale: drop jobs paused or started since it was shown.
    const targets = ids ? ids.filter(id => eligible.includes(id)) : eligible
    if (targets.length === 0) {
      if (action === 'run') closeRunConfirm()
      return
    }
    const call = (id: string) => {
      switch (action) {
        case 'pause': return api.toggleCron(id, false)
        case 'resume': return api.toggleCron(id, true)
        case 'strict_on': return api.updateCron(id, { strict_schedule: true })
        case 'strict_off': return api.updateCron(id, { strict_schedule: false })
        case 'run': return api.runCron(id)
      }
    }
    setBatchBusy(true); setActionError(null)
    try {
      const results = await Promise.allSettled(targets.map(call))
      const failedIds = targets.filter((_, i) => isFailure(results[i]))
      if (failedIds.length > 0) {
        setSelectedIds(new Set(failedIds))
        // The reason for the FIRST failure only, so the notice stays a line.
        const reason = failureReason(results[targets.findIndex((_, i) => isFailure(results[i]))])
        // `count` (the total) picks the plural form; `failed`, `action` and
        // `reason` are interpolation only.
        const common = { count: targets.length, failed: failedIds.length, action: batchActionLabel(action) }
        const msg = reason
          ? i18nT('pages.schedulePage.batchActions.batch_failed_reason', { ...common, reason })
          : i18nT('pages.schedulePage.batchActions.batch_failed', common)
        setActionError({ id: 'batch-action', msg })
      } else {
        setSelectedIds(new Set())
      }
      await load()
    } finally {
      setBatchBusy(false)
      if (action === 'run') closeRunConfirm()
    }
  }, [selectedJobs, running, load, setSelectedIds, setActionError, closeRunConfirm])
  // A job delivers to a Slack channel or a Telegram destination, never both, so
  // a Slack-pinned job is left as it is. Every job not updated stays checked.
  const sendToTelegram = useCallback(async (targetId: string, destination: string) => {
    const kept = selectedJobs.filter(j => j.channel)
    const targets = telegramTargets(selectedJobs)
    setBatchBusy(true); setActionError(null)
    try {
      const results = await Promise.allSettled(targets.map(j =>
        api.updateCron(j.id, { channel_type: TELEGRAM_CHANNEL, target_id: targetId })))
      const failed = targets.flatMap((j, i) => isFailure(results[i]) ? [{ job: j, reason: failureReason(results[i]) }] : [])
      setSelectedIds(new Set([...failed.map(f => f.job.id), ...kept.map(j => j.id)]))
      setTelegramResult({ updated: targets.length - failed.length, destination, kept: kept.map(j => j.name) })
      // One line per failed job, so the agent hand-off carries each name and reason.
      const lines = failed.map(f => f.reason
        ? i18nT('pages.schedulePage.batchActions.telegram_failed_reason', { name: f.job.name, reason: f.reason })
        : i18nT('pages.schedulePage.batchActions.telegram_failed', { name: f.job.name }))
      const msg = lines.length === 0 ? '' : [i18nT('pages.schedulePage.batchActions.telegram_failed_heading'), ...cappedLines(lines)].join('\n')
      // Set even when nothing failed: the notice region also reports what was updated.
      setActionError({ id: 'batch-telegram', msg })
      await load()
    } finally {
      setBatchBusy(false)
    }
  }, [selectedJobs, load, setSelectedIds, setActionError])
  const onBatchAction = useCallback((action: CronBatchAction) => {
    if (action === 'run') {
      const ids = batchTargets('run', selectedJobs, running).map(j => j.id)
      setRunSkipped(selectedJobs.filter(j => !ids.includes(j.id)).map(j => ({ name: j.name, paused: !j.enabled })))
      setRunConfirmIds(ids)
    } else void applyBatch(action)
  }, [applyBatch, selectedJobs, running])
  return {
    batchBusy, runConfirmIds, runSkipped, batchMenuLayer, closeRunConfirm, closeBatchConfirm, applyBatch, onBatchAction,
    sendToTelegram, telegramResult,
  }
}

/** What the last Set Telegram destination did: how many jobs now report to
 *  `destination` (its label), and the names of the Slack-pinned jobs it kept. */
export interface TelegramResult { updated: number; destination: string; kept: string[] }

/** A checked job Run now leaves out: paused, or else already running. */
export interface SkippedJob { name: string; paused: boolean }

/** How many job lines a skipped list or a failure notice shows before "+N more". */
const SKIPPED_SHOWN = 5

/** "+N more" for the lines past the first `SKIPPED_SHOWN`. */
const moreLine = (lines: string[]) =>
  i18nT('pages.schedulePage.batchActions.run_skipped_more', { n: lines.length - SKIPPED_SHOWN })

/** The first `SKIPPED_SHOWN` lines, then "+N more" for the rest. */
const cappedLines = (lines: string[]) =>
  lines.length > SKIPPED_SHOWN ? [...lines.slice(0, SKIPPED_SHOWN), moreLine(lines)] : lines

/** A heading over the checked jobs an action left alone, one per bullet. */
function SkippedJobsList({ heading, lines, testId, className = '' }: {
  heading: string; lines: string[]; testId: string; className?: string
}) {
  return (
    <div className={`text-[13px] text-muted ${className}`} data-testid={testId}>
      <p>{heading}</p>
      <ul className="mt-1 list-disc pl-5">
        {lines.slice(0, SKIPPED_SHOWN).map((line, i) => <li key={i}>{line}</li>)}
      </ul>
      {lines.length > SKIPPED_SHOWN && <p className="mt-1">{moreLine(lines)}</p>}
    </div>
  )
}

/** What the last Set Telegram destination did: the failures as a notice (`failed`,
 *  one line per job), whose hand-off is safe as the updated jobs are saved and the
 *  rest stay checked; then, as status, how many jobs now report to the destination
 *  and the Slack-pinned jobs it did not update. */
export function TelegramResultNotice({ failed, result: { updated, destination, kept } }: {
  failed: string; result: TelegramResult
}) {
  return (
    <div className="mb-2 flex flex-col gap-2">
      <ErrorNotice message={failed} askAgent testId="schedule-telegram-failed" />
      {(updated > 0 || kept.length > 0) && (
        <div role="status" className="flex flex-col gap-2">
          {updated > 0 && (
            <p className="text-[13px]" data-testid="schedule-telegram-updated">
              {i18nT('pages.schedulePage.batchActions.telegram_updated', { count: updated, destination })}
            </p>
          )}
          {kept.length > 0 && (
            <SkippedJobsList
              heading={i18nT('pages.schedulePage.batchActions.telegram_kept_slack_heading')}
              lines={kept}
              testId="schedule-telegram-kept"
            />
          )}
        </div>
      )}
    </div>
  )
}

/** Confirm before a batch Run now; `ids` are the jobs the menu counted when it opened. */
export function BatchRunConfirmDialog({ ids, skipped, busy, onClose, onConfirm }: {
  ids: string[] | null
  skipped: SkippedJob[]
  busy: boolean
  onClose: () => void
  onConfirm: (ids: string[]) => void
}) {
  return (
    <Dialog open={ids !== null} onOpenChange={next => { if (!next && !busy) onClose() }}>
      <DialogContent maxWidth={420}>
        <DialogHeader>
          <Play size={16} className="text-accent shrink-0" aria-hidden="true" />
          <DialogTitle>{i18nT('pages.schedulePage.batchActions.run_confirm_title', { count: ids?.length ?? 0 })}</DialogTitle>
        </DialogHeader>
        <DialogBody>
          <DialogDescription>{i18nT('pages.schedulePage.batchActions.run_confirm_body')}</DialogDescription>
          {skipped.length > 0 && (
            <SkippedJobsList
              className="mt-3"
              testId="schedule-run-skipped"
              heading={i18nT('pages.schedulePage.batchActions.run_skipped_heading')}
              lines={skipped.map(j => i18nT(j.paused ? 'pages.schedulePage.batchActions.run_skipped_paused' : 'pages.schedulePage.batchActions.run_skipped_running', { name: j.name }))}
            />
          )}
        </DialogBody>
        <DialogFooter>
          <Btn onClick={onClose} disabled={busy}>{i18nT('pages.schedulePage.cancel')}</Btn>
          <SendBtn disabled={busy} onClick={() => { if (ids) onConfirm(ids) }}>
            {i18nT('pages.schedulePage.run_now')}
          </SendBtn>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
