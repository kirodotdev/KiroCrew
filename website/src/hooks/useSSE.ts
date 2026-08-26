import { useEffect, useRef } from 'react'
import { useAppDispatch } from '../store'
import { sseStatus, sseConnected, sseDisconnected, sseSlots, sseSlotTitle, triggerRefresh } from '../store/dashboardSlice'
import { addNotification } from '../store/notificationsSlice'
import { fetchHistory, sseChatMessage } from '../store/chatSlice'
import type { StatusData, ChatSlot, Notification } from '../types'
import { MC_NOTIFICATION_EVENT, type McNotificationDetail } from './notificationEvent'

export function useSSE() {
  const dispatch = useAppDispatch()
  const ref = useRef<EventSource | null>(null)

  useEffect(() => {
    let wasDisconnected = false
    const sse = new EventSource('/api/stream')
    ref.current = sse

    sse.addEventListener('dashboard', (e) => {
      try { dispatch(sseStatus(JSON.parse(e.data) as StatusData)) } catch { /* ignore */ }
    })
    sse.addEventListener('notification', (e) => {
      try {
        const n = JSON.parse(e.data) as Notification
        dispatch(addNotification(n))
        try {
          const detail: McNotificationDetail = { kind: n.kind }
          window.dispatchEvent(new CustomEvent(MC_NOTIFICATION_EVENT, { detail }))
        } catch { /* notification listener error — non-fatal, ignore */ }
      } catch { /* ignore */ }
    })
    sse.addEventListener('slots', (e) => {
      try { dispatch(sseSlots(JSON.parse(e.data) as ChatSlot[])) } catch { /* ignore */ }
    })
    sse.addEventListener('slot_title', (e) => {
      try { dispatch(sseSlotTitle(JSON.parse(e.data))) } catch { /* ignore */ }
    })
    sse.addEventListener('refresh', (e) => {
      try {
        const kinds = e.data.split(',')
        dispatch(triggerRefresh())
        if (kinds.includes('history')) dispatch(fetchHistory(false))
      } catch { /* ignore */ }
    })
    sse.addEventListener('chat_message', (e) => {
      try { dispatch(sseChatMessage(JSON.parse(e.data))) } catch { /* ignore */ }
    })
    sse.onerror = () => { wasDisconnected = true; dispatch(sseDisconnected()) }
    sse.onopen = () => {
      // If reconnecting after disconnect (gateway restart), reload to pick up new build
      if (wasDisconnected) { window.location.reload(); return }
      dispatch(sseConnected())
    }

    return () => { sse.close(); ref.current = null }
  }, [dispatch])
}
