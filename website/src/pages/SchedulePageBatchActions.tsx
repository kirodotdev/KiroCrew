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
      const failedIds = targets.filter((_, i) => {
        const r = results[i]
        return r.status === 'rejected' || !!(r.value as { error?: string } | undefined)?.error
      })
      if (failedIds.length > 0) {
        setSelectedIds(new Set(failedIds))
        // `count` (the total) picks the plural form; `failed` and `action` are interpolation only.
        setActionError({ id: 'batch-action', msg: i18nT('pages.schedulePage.batchActions.batch_failed', { count: targets.length, failed: failedIds.length, action: batchActionLabel(action) }) })
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
    if (action === 'run') setRunConfirmIds(batchTargets('run', selectedJobs, running).map(j => j.id))
    else void applyBatch(action)
  }, [applyBatch, selectedJobs, running])
  return { batchBusy, runConfirmIds, batchMenuLayer, closeRunConfirm, closeBatchConfirm, applyBatch, onBatchAction }
}

/** Confirm before a batch Run now; `ids` are the jobs the menu counted when it opened. */
export function BatchRunConfirmDialog({ ids, busy, onClose, onConfirm }: {
  ids: string[] | null
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
