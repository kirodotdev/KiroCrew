import { dispatchLiveNotification } from '../hooks/notificationEvent'
import type { Notification } from '../types'
import { uniqueNotificationTs } from './notificationTs'

/**
 * Surface a short-lived app-wide outcome when no mounted component owns an
 * inline notice. Hook and controller failures use the same notification feed
 * as the dashboard bell, rather than a blocking browser dialog, and are also
 * announced live so the in-app banner shows them where the user is: the banner
 * listens for live arrivals only, never for the store, so a feed entry alone
 * would leave the failure unseen until the user opened the bell.
 *
 * The feed is reached through a sink the store installs when it is created,
 * not through an import. Store-free surfaces (the chat embed's composer) reach
 * this module through the popout controller, and importing the store here
 * would pull the whole Redux store into them.
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
