import { store } from '../store'
import { addNotification } from '../store/notificationsSlice'

/**
 * Surface a short-lived app-wide outcome when no mounted component owns an
 * inline notice. Hook and controller failures use the same Redux notification
 * feed as the dashboard bell, rather than a blocking browser dialog.
 */
export type AppNoticeKind = 'error' | 'success' | 'info'

let sequence = 0

export function publishAppNotice(message: string, kind: AppNoticeKind = 'info'): void {
  if (!message.trim()) return
  sequence += 1
  store.dispatch(addNotification({
    ts: `${Date.now()}-${sequence}`,
    title: message,
    body: '',
    kind,
  }))
}

/** Backwards-compatible concise alias for utility callers. */
export const appNotice = publishAppNotice
