import { flushSharedUnread } from '../store/dashboard/unread'

/** Read an unread storage key the way a sibling window would after the
 *  queued shared-record write lands: markSlotUnread queues that write, so
 *  flush the queue before reading. */
export const sharedItem = (key: string): string | null => {
  flushSharedUnread()
  return localStorage.getItem(key)
}
