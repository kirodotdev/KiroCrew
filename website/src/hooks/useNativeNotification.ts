/**
 * Fires a browser `Notification` whenever a new unacked notification lands in
 * the Redux store. Used by `App.tsx` to surface macOS notification-center
 * toasts.
 *
 * A shared hook so the regression tests in
 * `integration/AppNotification.integration.test.tsx` exercise *this* code —
 * if the effect regresses, tests and production break together.
 */
import { useEffect, useRef } from 'react'
import { useAppSelector } from '../store'

export function useNativeNotification(botName: string, avatar: string) {
  const notifCount = useAppSelector(
    (s) => s.notifications.items.filter((n) => !n.acked).length,
  )
  const latestNotif = useAppSelector((s) => {
    const unacked = s.notifications.items.filter((n) => !n.acked)
    return unacked.length > 0 ? unacked[unacked.length - 1] : null
  })

  const prev = useRef(0)
  useEffect(() => {
    if (notifCount > prev.current && prev.current >= 0) {
      if (typeof Notification !== 'undefined') {
        if (Notification.permission === 'granted') {
          const delta = notifCount - prev.current
          const title = latestNotif?.title || botName
          const body =
            latestNotif?.body ||
            (delta > 1 ? `${delta} new notifications` : 'New notification')
          new Notification(title, {
            body,
            icon: avatar,
            tag:
              latestNotif?.approval_id ||
              latestNotif?.job_id ||
              latestNotif?.task_id ||
              'kirocrew-notif',
          })
        } else if (Notification.permission === 'default') {
          Notification.requestPermission()
        }
      }
    }
    prev.current = notifCount
  }, [notifCount, botName, avatar, latestNotif])
}
