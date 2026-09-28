/**
 * The browser's OS-notification permission as React state.
 *
 * `Notification.permission` is a live platform value with no change event, so
 * the hook re-reads it at the moments it can plausibly have moved: after a
 * request it made itself settles, and whenever the window regains focus (the
 * user may have changed it in the browser's site settings and come back).
 *
 * The two surfaces that call `request()` — Settings › Notifications and the
 * bell popover's hint — are the user-gesture homes for the prompt. A click IS
 * the gesture browsers require; asking from an effect (as
 * `useNativeNotification` still does on a first unacked arrival) is refused
 * by most browsers and by all of them once the user has dismissed the prompt.
 */
import { useCallback, useEffect, useState } from 'react'
import { relayTargetOrigin } from '../lib/nativeNotify'

export type NotificationPermissionState = 'unsupported' | 'default' | 'granted' | 'denied'

/**
 * `unsupported` also covers an embedded instance pane whose banners are posted
 * by the hub window (`lib/nativeNotify.ts`): this frame's own verdict is
 * `denied` by design and is not the user's switch -- the hub's Settings row
 * is -- so the pane shows no permission row or hint that would contradict the
 * banners it relays, and offers no `request()` that could never succeed.
 */
export function readNotificationPermission(): NotificationPermissionState {
  if (typeof Notification === 'undefined') return 'unsupported'
  if (relayTargetOrigin() !== null) return 'unsupported'
  const p = Notification.permission
  return p === 'granted' || p === 'denied' ? p : 'default'
}

export function useNotificationPermission(): {
  permission: NotificationPermissionState
  /** Ask the browser. Resolves with the re-read state, whatever the verdict. */
  request: () => Promise<NotificationPermissionState>
} {
  const [permission, setPermission] = useState<NotificationPermissionState>(readNotificationPermission)

  useEffect(() => {
    const refresh = () => setPermission(readNotificationPermission())
    window.addEventListener('focus', refresh)
    return () => window.removeEventListener('focus', refresh)
  }, [])

  const request = useCallback(async () => {
    if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
      try {
        // A callback-only implementation returns undefined; awaiting it is a
        // no-op and the re-read below still reports the truth.
        await Notification.requestPermission()
      } catch {
        /* unsupported platform */
      }
    }
    const next = readNotificationPermission()
    setPermission(next)
    return next
  }, [])

  return { permission, request }
}
