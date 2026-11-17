import { useState, useEffect } from 'react'
import { CheckCircle, Trash2 } from 'lucide-react'
import { useAppDispatch } from '../../store'
import { ackNotification, deleteNotification } from '../../store/notificationsSlice'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import type { Notification } from '../../types'

import { i18nT } from '../../i18n/t'

/** Why an action did not land: the job is gone, the ack failed, or the Delete failed. */
type Failure = 'gone' | 'retry' | 'delete'

function failureCopy(failure: Failure): string {
  if (failure === 'gone') return i18nT('pages.chat.cronAckBar.ack_job_gone')
  if (failure === 'delete') return i18nT('pages.chat.cronAckBar.delete_failed')
  return i18nT('pages.chat.cronAckBar.ack_failed')
}

export default function CronAckBar({ notification, onDone }: { notification: Notification; onDone: () => void }) {
  const [acked, setAcked] = useState(!!notification.acked)
  const [failure, setFailure] = useState<Failure | null>(null)
  const dispatch = useAppDispatch()
  // Once the job is known to be gone, Delete is the note's only way out, so it
  // stays offered when a Delete fails too.
  const [jobGone, setJobGone] = useState(false)
  useEffect(() => { setAcked(!!notification.acked) }, [notification.acked])
  const deleteButton = (
    <button className="px-3 py-1.5 rounded-md text-[13px] text-danger hover:text-danger-text border border-border hover:border-danger/50 hover:bg-danger/10 transition-all cursor-pointer bg-transparent" onClick={async () => {
      setFailure(null)
      // Close only once the note is gone: a failed DELETE leaves it in place.
      try {
        await dispatch(deleteNotification(notification.ts)).unwrap()
      } catch {
        setFailure('delete')
        return
      }
      onDone()
    }}><Trash2 className="lucide-inline" /> {i18nT('pages.chat.cronAckBar.delete')}</button>
  )
  /* No hand-off: this bar renders inside NotificationDetailPanel, an OVERLAY
     over whatever page the reader was on, such as the /chat composer holding an
     unsent draft. That panel runs every action through `useGuardedLeave`; the
     hand-off navigates to the chat directly, bypassing that gate, so it would
     discard the draft without asking. Each sentence names its own remedy
     (retry, or Delete when the job is gone). */
  const notice = <ErrorNotice variant="inline" message={failure ? failureCopy(failure) : null} />
  return (
    <div className="mt-4 pt-3 border-t border-border">
      {acked ? (
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-3">
            <div className="flex-1 px-3.5 py-2.5 rounded-lg bg-accent-subtle border border-accent/20 text-sm text-text"><CheckCircle className="lucide-inline" /> {i18nT('pages.chat.cronAckBar.acknowledged_this_won_t_be_repeated_in_future_no')}</div>
            {deleteButton}
          </div>
          {notice}
        </div>
      ) : (
        <div className="flex flex-col items-start gap-2">
          <div className="flex items-center gap-3">
            {jobGone ? deleteButton : (
              <button className="px-4 py-2 rounded-lg bg-accent text-accent-fg text-[13px] font-semibold cursor-pointer border-none hover:brightness-110 transition-all" onClick={async () => {
                const jobId = notification.job_id as string
                setFailure(null)
                // Only a saved acknowledgement reads as "Acknowledged": a refusal
                // ({ ok: false }) or a failed request keeps the note unread.
                let next: Failure | null
                try {
                  const res = await api.ackCron(jobId, (notification.body || '').slice(0, 200), notification.ts)
                  next = res?.ok === true ? null : res?.code === 'job_not_found' ? 'gone' : 'retry'
                } catch {
                  next = 'retry'
                }
                if (next) {
                  if (next === 'gone') setJobGone(true)
                  setFailure(next)
                  return
                }
                dispatch(ackNotification(notification.ts))
                setAcked(true)
              }}><CheckCircle className="lucide-inline" /> {i18nT('pages.chat.cronAckBar.acknowledge')}</button>
            )}
          </div>
          {notice}
        </div>
      )}
    </div>
  )
}
