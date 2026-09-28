import { useState, useEffect } from 'react'
import { CheckCircle, Trash2 } from 'lucide-react'
import { useAppDispatch } from '../../store'
import { ackNotification, deleteNotification } from '../../store/notificationsSlice'
import { api } from '../../api/client'
import type { Notification } from '../../types'

import { i18nT } from '../../i18n/t'
export default function CronAckBar({ notification, onDone }: { notification: Notification; onDone: () => void }) {
  const [acked, setAcked] = useState(!!notification.acked)
  const dispatch = useAppDispatch()
  useEffect(() => { setAcked(!!notification.acked) }, [notification.acked])
  return (
    <div className="mt-4 pt-3 border-t border-border">
      {acked ? (
        <div className="flex items-center gap-3">
          <div className="flex-1 px-3.5 py-2.5 rounded-lg bg-accent-subtle border border-accent/20 text-sm text-text"><CheckCircle className="lucide-inline" /> {i18nT('pages.chat.cronAckBar.acknowledged_this_won_t_be_repeated_in_future_no')}</div>
          <button className="px-3 py-1.5 rounded-md text-[13px] text-danger border border-border hover:border-danger/50 hover:bg-danger/10 transition-all cursor-pointer bg-transparent" onClick={() => { dispatch(deleteNotification(notification.ts)); onDone() }}><Trash2 className="lucide-inline" /> {i18nT('pages.chat.cronAckBar.delete')}</button>
        </div>
      ) : (
        <button className="px-4 py-2 rounded-lg bg-accent text-accent-fg text-[13px] font-semibold cursor-pointer border-none hover:brightness-110 transition-all" onClick={async () => {
          const jobId = notification.job_id as string
          await api.ackCron(jobId, (notification.body || '').slice(0, 200), notification.ts)
          dispatch(ackNotification(notification.ts))
          setAcked(true)
        }}><CheckCircle className="lucide-inline" /> {i18nT('pages.chat.cronAckBar.acknowledge')}</button>
      )}
    </div>
  )
}
