/**
 * Fires a browser `Notification` whenever a new unacked notification lands in
 * the Redux store. Used by `App.tsx` to surface macOS notification-center
 * toasts.
 *
 * A muted channel's notes arrive with `silenced: true` and `priority:
 * "passive"` (`ChannelSettings.apply()`, `kiro_crew/notifications/settings.py`)
 * -- the backend's stated contract is that every attention surface (badge
 * count, sound, native banner, feed styling) skips them. The in-app feed
 * (`NotificationFeed.tsx`) already reads `silenced` for its styling; this hook
 * must exclude the same notes from BOTH its unread count and its
 * latest-note pick, or a muted note still increments the count and fires the
 * native banner even though the in-app row correctly shows "muted".
 *
 * A shared hook so the regression tests in
 * `integration/AppNotification.integration.test.tsx` exercise *this* code —
 * if the effect regresses, tests and production break together.
 */
import { useEffect, useRef } from 'react'
import { useAppSelector } from '../store'
// Shared with the tab-title attention count rather than kept file-local: two
// attention surfaces that spell this rule separately can drift apart, and the
// backend states it once for all of them.
import { isSilencedNote } from '../store/notificationsSlice'

export function useNativeNotification(botName: string, avatar: string) {
  const notifCount = useAppSelector(
    (s) => s.notifications.items.filter((n) => !n.acked && !isSilencedNote(n)).length,
  )
  const latestNotif = useAppSelector((s) => {
    const unacked = s.notifications.items.filter((n) => !n.acked && !isSilencedNote(n))
    return unacked.length > 0 ? unacked[unacked.length - 1] : null
  })

  const prev = useRef(0)
  useEffect(() => {
    if (notifCount > prev.current) {
      if (typeof Notification !== 'undefined') {
        if (Notification.permission === 'granted') {
          const delta = notifCount - prev.current
          const title = latestNotif?.title || botName
          const body =
            latestNotif?.body ||
            (delta > 1 ? `${delta} new notifications` : 'New notification')
          // Android Chrome throws "Illegal constructor" here even with
          // permission granted (page-context Notification is desktop-only);
          // the in-app notification center still shows the event, so the
          // native toast is best-effort.
          try {
            new Notification(title, {
              body,
              icon: avatar,
              // Always silent: WebAudio (useNotificationSound) is the single
              // source of notification sound. Without this the OS toast plays
              // its own system chime on top of the WebAudio tone — a double
              // sound. Browsers that ignore `silent` are no worse than before.
              silent: true,
              tag:
                latestNotif?.approval_id ||
                latestNotif?.job_id ||
                latestNotif?.task_id ||
                'kirocrew-notif',
            })
          } catch {
            /* unsupported platform */
          }
        } else if (Notification.permission === 'default') {
          Notification.requestPermission()
        }
      }
    }
    prev.current = notifCount
  }, [notifCount, botName, avatar, latestNotif])
}
