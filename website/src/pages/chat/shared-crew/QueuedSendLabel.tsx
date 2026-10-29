/** A small "Queued" line on the user message the host is holding for capacity,
 *  with its own Cancel. It is the only place a waiting send is shown: nothing
 *  about accepted work renders above the composer. Running, done and ended work
 *  show nothing here, because the transcript and the stop button already say so.
 *
 *  Server truth only: the item is the session's `/work` read plus
 *  `shared_crew.work` frames. Cancel targets exactly that item at the revision
 *  shown; a stale click adopts the item's current state. */
import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Clock } from 'lucide-react'
import type { AcceptedWork } from '../../../api/sharedCrew'
import { capabilityUsable, mergeWork, type WorkListResponse } from '../../../api/sharedCrewUi'
import { api } from '../../../api/client'
import { Btn } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import { sharedCrewKeys } from '../../../hooks/sharedCrew/sharedCrewSocket'
import { useSessionWork, useSharedCrewCapabilities, useSharedCrewCapacity } from '../../../hooks/sharedCrew/useSharedCrew'
import { i18nT } from '../../../i18n/t'
import { queuedReason } from './overloadCopy'

/** The newest chat turn of the session that is waiting for capacity, if any. */
export function queuedChatTurn(items: readonly AcceptedWork[]): AcceptedWork | null {
  for (let i = items.length - 1; i >= 0; i--) {
    const w = items[i]
    if (w.state === 'queued' && w.operation === 'chat_turn') return w
  }
  return null
}

export function QueuedSendLabel({ slotKey }: { slotKey: string }) {
  // SessionCollabStrip owns capability failures. This label is mounted only on
  // the newest queued-send bubble and owns the work and capacity read failures.
  const { capabilities } = useSharedCrewCapabilities()
  const queueOn = capabilityUsable(capabilities.queueing)
  const work = useSessionWork(slotKey, queueOn)
  const capacity = useSharedCrewCapacity(queueOn)
  const queryClient = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const item = queueOn ? queuedChatTurn(work.data?.items ?? []) : null
  const readErrors = (
    <>
      {queueOn && work.isError && <ErrorNotice message={work.error instanceof Error ? work.error.message : i18nT('sharedCrew.session_state_failed')} variant="inline" askAgent testId="queued-send-read-error" />}
      {queueOn && capacity.isError && <ErrorNotice message={capacity.error instanceof Error ? capacity.error.message : i18nT('sharedCrew.session_state_failed')} variant="inline" askAgent testId="queued-send-capacity-error" />}
    </>
  )
  if (!item) {
    return <>{readErrors}{notice && <p className="text-[12px] text-muted mb-1 pr-1" role="status">{notice}</p>}</>
  }
  const cancel = async () => {
    if (busy) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const result = await api.sharedCrewCancelWork(item.work_id, item.revision)
      const next = result.ok ? result.work : result.current
      if (next) {
        queryClient.setQueryData<WorkListResponse | null>(sharedCrewKeys.work(slotKey), prev => ({ ...(prev ?? {}), items: mergeWork(prev?.items ?? [], next) }))
      } else {
        void queryClient.invalidateQueries({ queryKey: sharedCrewKeys.work(slotKey) })
      }
      if (!result.ok) setNotice(i18nT('sharedCrew.cancel_conflict'))
    } catch (e) {
      setError(e instanceof Error ? e.message : i18nT('sharedCrew.cancel_failed'))
    } finally {
      setBusy(false)
    }
  }
  const why = queuedReason(item.reason, capacity.data)
  return (
    <div className="flex flex-col items-end gap-0.5 mb-1 pr-1 min-w-0" data-testid="queued-send" data-work-id={item.work_id}>
      <div className="inline-flex items-center gap-1.5 text-[12px] leading-5 font-medium text-muted">
        <span role="status" className="inline-flex items-center gap-1" title={why}>
          <Clock size={12} className="shrink-0" aria-hidden="true" /> {i18nT('sharedCrew.queued_send')}
        </span>
        <Btn
          className="h-6 px-2 text-[12px]"
          disabled={busy}
          onClick={() => { void cancel() }}
          aria-label={i18nT('sharedCrew.cancel_queued_send')}
        >
          {i18nT('sharedCrew.cancel_work')}
        </Btn>
      </div>
      {readErrors}
      <ErrorNotice message={error} variant="inline" askAgent testId="queued-send-cancel-error" />
    </div>
  )
}
