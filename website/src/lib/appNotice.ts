import { dispatchLiveNotification } from '../hooks/notificationEvent'
import type { Notification } from '../types'
import { uniqueNotificationTs } from './notificationTs'

/**
 * Reports a failure no mounted component can show inline. It goes to the bell's
 * feed and is also announced live, because the banner shows live arrivals only.
 *
 * The store installs the feed sink, so store-free frames that reach this module
 * through the popout controller do not import the Redux store.
 */
let feedSink: ((note: Notification) => void) | null = null

/** Called by the store module: where an app notice is recorded in the feed. */
export function installAppNoticeFeed(sink: ((note: Notification) => void) | null): void {
  feedSink = sink
}

export function appErrorNotice(message: string): void {
  if (!message.trim()) return
  const note: Notification = {
    // The feed parses `ts` as a time; `uniqueNotificationTs` keeps it parseable
    // and unique within a millisecond.
    ts: uniqueNotificationTs(),
    title: message,
    body: '',
    kind: 'error',
  }
  feedSink?.(note)
  dispatchLiveNotification(note)
  for (const listener of frameListeners) listener({ id: note.ts, message })
}

/** One app notice: `id` is unique per call, so a repeat of the same message is
 *  still a new arrival. */
export interface AppErrorNoticeEvent { id: string; message: string }

/** The popout and embed frames mount no bell or banner, so they show app
 *  notices through `AppErrorNoticeOutlet`, which subscribes here. */
const frameListeners = new Set<(notice: AppErrorNoticeEvent) => void>()

export function subscribeAppErrorNotice(listener: (notice: AppErrorNoticeEvent) => void): () => void {
  frameListeners.add(listener)
  return () => { frameListeners.delete(listener) }
}
