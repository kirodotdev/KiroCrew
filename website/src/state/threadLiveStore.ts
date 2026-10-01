/**
 * A thread's live ANCHOR state, keyed `slot + mid`.
 *
 * It holds the anchor and nothing else: which slot a parent message's thread
 * resolved to, and whether that thread has been closed. The `chat.thread_anchor`
 * frame feeds it, so a thread opened or closed in another tab reaches this one's
 * footers without a poll.
 *
 * No transcript is mirrored here. A thread is an ordinary chat session
 * (NOTES D1), so its messages stream on the THREAD SLOT'S OWN frames and the
 * ordinary chat state owns them — the surface rendering a thread is the ordinary
 * chat pane, which already has them. The anchor is the one fact no ordinary chat
 * frame carries, which is why it needs a store of its own.
 *
 * `useSyncExternalStore`-shaped and framework-free. A reconnect drops every row
 * (`reset`): announcements made while the socket was down were never delivered,
 * so the anchor index is refetched rather than trusted from memory.
 *
 * The same module keeps each thread's unsent draft (`threadDrafts`), memory
 * only, as the main composer's is.
 */

export interface ThreadLive {
  /** The thread's own slot key, once known. */
  threadSlot: string
  title?: string
  /** Set by a `closed` announcement; the thread then reads as finished. The
   *  frame carries no timestamp of its own, so the moment it arrived is used —
   *  the anchor read that follows replaces it with the stored value. */
  closedAt?: string
}

/**
 * One `chat.thread_anchor` frame. `event` says what happened to the anchor —
 * the frame announces anchor changes only, never a thread's message text, which
 * is why it carries no `run_id`, no `role` and no `content`.
 */
export interface ThreadAnchorFrame {
  slot: string
  mid: string
  thread_slot: string
  event: 'opened' | 'closed'
  title?: string
  opened_by?: string
  summary_mid?: string
  ts?: number
}

const EMPTY_LISTENERS: ReadonlySet<() => void> = new Set()

export class ThreadLiveStore {
  private readonly rows = new Map<string, ThreadLive>()
  private readonly listeners = new Map<string, Set<() => void>>()

  static key(slot: string, mid: string): string {
    return slot + '\u0000' + mid
  }

  private notify(key: string): void {
    for (const fn of this.listeners.get(key) ?? EMPTY_LISTENERS) fn()
  }

  /** Apply one anchor announcement. A frame naming no thread slot says nothing
   *  this store can hold, so it is dropped rather than stored as a blank row. */
  apply(frame: ThreadAnchorFrame): void {
    if (!frame.thread_slot) return
    const key = ThreadLiveStore.key(frame.slot, frame.mid)
    // A CLOSE names the thread it ended, and the row may already hold a
    // different one: closing releases the message, so a replacement thread can be
    // opened on the same mid before a close frame for the old one arrives (two
    // tabs, or an agent ending a thread while the reader starts the next). Keyed
    // by `(slot, mid)` alone, that stale close would overwrite the live
    // replacement with the ended thread's slot -- the footer would read "Ended"
    // and point at a conversation nobody is in. So a close applies only to the
    // thread it actually names. An OPEN is the newest word on that mid by
    // definition and always replaces.
    const held = this.rows.get(key)
    if (frame.event === 'closed' && held && held.threadSlot !== frame.thread_slot) return
    const row: ThreadLive = { threadSlot: frame.thread_slot }
    if (frame.title) row.title = frame.title
    if (frame.event === 'closed') row.closedAt = new Date().toISOString()
    this.rows.set(key, row)
    this.notify(key)
  }

  subscribe(slot: string, mid: string, listener: () => void): () => void {
    const key = ThreadLiveStore.key(slot, mid)
    let set = this.listeners.get(key)
    if (!set) {
      set = new Set()
      this.listeners.set(key, set)
    }
    set.add(listener)
    return () => {
      set!.delete(listener)
      if (set!.size === 0) this.listeners.delete(key)
    }
  }

  get(slot: string, mid: string): ThreadLive | undefined {
    return this.rows.get(ThreadLiveStore.key(slot, mid))
  }

  /** Drop every row and tell every subscriber. Called on WS reconnect. */
  reset(): void {
    const keys = [...this.rows.keys()]
    this.rows.clear()
    for (const key of keys) this.notify(key)
  }
}

export const threadLiveStore = new ThreadLiveStore()

/** Unsent text per thread, kept while the thread's surface is closed. */
export const threadDrafts = {
  store: new Map<string, string>(),
  get(slot: string, mid: string): string {
    return this.store.get(ThreadLiveStore.key(slot, mid)) ?? ''
  },
  set(slot: string, mid: string, text: string): void {
    const key = ThreadLiveStore.key(slot, mid)
    if (text) this.store.set(key, text)
    else this.store.delete(key)
  },
}
