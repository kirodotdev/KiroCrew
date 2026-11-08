import { useState, useCallback } from 'react'
import { Play } from 'lucide-react'
import { api } from '../api/client'
import { Btn, SendBtn } from '../components/ui'
import {
  Dialog, DialogContent, DialogHeader, DialogBody, DialogFooter, DialogTitle, DialogDescription,
} from '../components/ui/dialog'
import { useMenuOpensDialog } from '../hooks/useMenuOpensDialog'
import { batchActionLabel, batchTargets, type CronBatchAction } from '../components/CronBatchActionsMenu'
import type { CronJob } from '../types'
import { i18nT } from '../i18n/t'

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
      // The run and enable routes report some refusals in the body, not the status.
      const isFailure = (r: PromiseSettledResult<unknown>) =>
        r.status === 'rejected' || !!(r.value as { error?: string } | undefined)?.error
      const failedIds = targets.filter((_, i) => isFailure(results[i]))
      if (failedIds.length > 0) {
        setSelectedIds(new Set(failedIds))
        // The server's reason for the FIRST failure, if it carried one: an
        // ApiError's `message` (the friendly text) for a rejection, or the body
        // `error` string for an in-body refusal. One reason, not one per job, so
        // the notice stays a line; a rejection with no human message is omitted.
        const first = results[targets.findIndex((_, i) => isFailure(results[i]))]
        const reason = first.status === 'rejected'
          ? (first.reason instanceof Error ? first.reason.message : '')
          : ((first.value as { error?: string } | undefined)?.error ?? '')
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
  const onBatchAction = useCallback((action: CronBatchAction) => {
    if (action === 'run') {
      const ids = batchTargets('run', selectedJobs, running).map(j => j.id)
      setRunSkipped(selectedJobs.filter(j => !ids.includes(j.id)).map(j => ({ name: j.name, paused: !j.enabled })))
      setRunConfirmIds(ids)
    } else void applyBatch(action)
  }, [applyBatch, selectedJobs, running])
  return { batchBusy, runConfirmIds, runSkipped, batchMenuLayer, closeRunConfirm, closeBatchConfirm, applyBatch, onBatchAction }
}

/** A checked job Run now leaves out: paused, or else already running. */
export interface SkippedJob { name: string; paused: boolean }

/** How many skipped job names the Run now confirm lists before "+N more". */
const SKIPPED_SHOWN = 5

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
            <div className="mt-3 text-[13px] text-muted" data-testid="schedule-run-skipped">
              <p>{i18nT('pages.schedulePage.batchActions.run_skipped_heading')}</p>
              <ul className="mt-1 list-disc pl-5">
                {skipped.slice(0, SKIPPED_SHOWN).map((j, i) => (
                  <li key={i}>{i18nT(j.paused ? 'pages.schedulePage.batchActions.run_skipped_paused' : 'pages.schedulePage.batchActions.run_skipped_running', { name: j.name })}</li>
                ))}
              </ul>
              {skipped.length > SKIPPED_SHOWN && <p className="mt-1">{i18nT('pages.schedulePage.batchActions.run_skipped_more', { n: skipped.length - SKIPPED_SHOWN })}</p>}
            </div>
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
