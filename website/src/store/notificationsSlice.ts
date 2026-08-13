import { createSlice, createAsyncThunk, type PayloadAction } from '@reduxjs/toolkit'
import { api } from '../api/client'
import type { Notification } from '../types'

interface NotificationsState {
  items: Notification[]
  /** Bumped by every clear-all (local thunk or the `notifications_clear` WS
   *  frame from another view). A fetch stamps the generation it started under,
   *  so a response rendered BEFORE a clear is recognised as stale and dropped
   *  instead of replacing the emptied list — which would resurrect the rows
   *  and the bell badge with them. */
  clearSeq: number
  /** Monotonic counter of LOCAL ack-state changes (optimistic ack/unack
   *  thunks, their confirmations, and ack/unack WS frames from any view). A
   *  fetch snapshots it at request start; the fulfilled reducer keeps any local
   *  ack flag stamped after that snapshot, because the response was rendered
   *  before the change and applying it verbatim would revert the newer local
   *  state. Optional because state persisted before these fields existed
   *  rehydrates without them, and every read must stay defensive. */
  ackSeq?: number
  /** ts → the `ackSeq` at which that item's ack flag last changed locally.
   *  Read by the fetch-merge to decide, per item, whether the local flag is
   *  newer than the response. An entry is dropped whenever its item leaves
   *  `items` — deleted, cleared, or evicted by the ring cap — so the map is
   *  bounded by the same cap and cannot retain stamps for rows the list no
   *  longer holds. */
  ackSeqByTs?: Record<string, number>
}

const initialState: NotificationsState = { items: [], clearSeq: 0, ackSeq: 0, ackSeqByTs: {} }

/** Ring-buffer cap on the notifications list. Without it, `items` grows
 *  monotonically for the tab's lifetime (ack only flips a flag) — part of
 *  the long-lived-tab heap retention class. Applied on both the
 *  live SSE path and the fetch path so the page and the bell see one
 *  consistent bounded list; oldest entries drop first. Older history stays
 *  in the backend notification log. */
export const NOTIFICATIONS_RING_CAP = 200

const capped = (items: Notification[]): Notification[] =>
  items.length > NOTIFICATIONS_RING_CAP ? items.slice(items.length - NOTIFICATIONS_RING_CAP) : items

/** Stamp a local ack-state change on `ts`. Called for EVERY ack/unack signal
 *  that reaches an item, including one whose flag already matches: the backend
 *  broadcasts an ack to every socket with no originator exclusion, so the view
 *  that acked receives its own echo, and that echo is recency evidence even
 *  though it changes no pixel. Skipping it would leave the stamp equal to the
 *  snapshot of a fetch already in flight, and the merge below would then adopt
 *  that fetch's stale copy. The `?? 0` / `??=` guards keep the reducer safe on
 *  state rehydrated before these fields existed. */
const markAck = (state: NotificationsState, ts: string) => {
  state.ackSeq = (state.ackSeq ?? 0) + 1
  ;(state.ackSeqByTs ??= {})[ts] = state.ackSeq
}

/** Drop stamps for items `items` no longer holds. The one chokepoint for it,
 *  called from every path that shrinks the list — delete, WS remove, clear, a
 *  ring-cap eviction, and the fetch merge — because a stamp outliving its item
 *  is unreachable state that would accumulate for the tab's lifetime, which is
 *  the retention class `NOTIFICATIONS_RING_CAP` exists to close. */
const pruneAckStamps = (state: NotificationsState) => {
  const stamps = state.ackSeqByTs
  if (!stamps) return
  const live = new Set(state.items.map(n => n.ts))
  for (const ts of Object.keys(stamps)) {
    if (!live.has(ts)) delete stamps[ts]
  }
}

export const fetchNotifications = createAsyncThunk(
  'notifications/fetch',
  async (_arg: void, { getState }) => {
    // Captured BEFORE the request so a clear landing mid-flight changes the
    // generation and marks this payload stale. ackSeq is captured for the
    // same reason at item granularity: an ack landing mid-flight outranks
    // this payload's copy of that item.
    const notif = (getState() as { notifications: NotificationsState }).notifications
    const seq = notif.clearSeq
    const ackSeq = notif.ackSeq ?? 0
    const d = await api.notifications()
    return { items: (d.notifications || []) as Notification[], seq, ackSeq }
  },
)

export const clearNotifications = createAsyncThunk(
  'notifications/clear',
  async (_arg: void, { getState }) => {
    // Captured BEFORE the request: if the generation has moved by the time
    // this resolves, the `notifications_clear` frame for this very clear
    // already emptied the list and the reducer must not empty it again.
    const seq = (getState() as { notifications: NotificationsState }).notifications.clearSeq
    await api.clearNotifications()
    return { seq }
  },
)

export const deleteNotification = createAsyncThunk(
  'notifications/delete',
  async (ts: string) => { await api.deleteNotification(ts); return ts },
)

// One rule governs every write to an item's ack flag, whatever produced it:
// a response may only apply to an item whose local ack stamp has not advanced
// since that request began. The fetch merge below is one instance (its snapshot
// is the whole-state `ackSeq`); these confirmations are the other (their
// snapshot is the item's own stamp, read after the optimistic `pending` wrote
// it). Without the rule a confirmation is stale evidence: tab A's slow ack can
// land after tab B's unack has already arrived by WS, and re-asserting read
// would contradict the backend the confirmation supposedly proves.
const ackStampOf = (state: NotificationsState, ts: string): number | undefined =>
  (state.ackSeqByTs ?? {})[ts]

const stampUnchangedSince = (
  state: NotificationsState,
  ts: string,
  since: number | undefined,
): boolean =>
  // `undefined` since means the item carried no stamp when the request began —
  // it was absent then, so this response says nothing about the row now present.
  since !== undefined && ackStampOf(state, ts) === since

export const ackNotification = createAsyncThunk(
  'notifications/ack',
  async (ts: string, { getState }) => {
    // Read AFTER `pending` has stamped: this is our own optimistic stamp, so a
    // later value means something newer than this request moved the flag.
    const stamp = ackStampOf((getState() as { notifications: NotificationsState }).notifications, ts)
    await api.ackNotification(ts)
    return { ts, stamp }
  },
)

export const unackNotification = createAsyncThunk(
  'notifications/unack',
  async (ts: string, { getState }) => {
    const stamp = ackStampOf((getState() as { notifications: NotificationsState }).notifications, ts)
    await api.unackNotification(ts)
    return { ts, stamp }
  },
)

export const ackAllNotifications = createAsyncThunk(
  'notifications/ackAll',
  async (_arg: void, { getState }) => {
    // Per-item snapshot, so one row moved by another tab does not veto the rest
    // and a notification that ARRIVES during the request is never marked read
    // (it carries no entry here, so the rule refuses it).
    const notif = (getState() as { notifications: NotificationsState }).notifications
    const stamps: Record<string, number | undefined> = {}
    for (const n of notif.items) stamps[n.ts] = ackStampOf(notif, n.ts)
    await api.ackAllNotifications()
    return { stamps }
  },
)

const notificationsSlice = createSlice({
  name: 'notifications',
  initialState,
  reducers: {
    addNotification(state, action: PayloadAction<Notification>) {
      if (!state.items.some(n => n.ts === action.payload.ts)) {
        state.items.push(action.payload)
        state.items = capped(state.items)
        pruneAckStamps(state)
      }
    },
    ackNotificationByTs(state, action: PayloadAction<string>) {
      if (action.payload === '*') {
        // A wildcard broadcast names no item: it says "what the server acked is
        // read", not "this row is read". Deliberately does NOT stamp, and the
        // asymmetry with the named branch below is the point — a notification
        // that arrived while the ack-all was being applied server-side is not
        // covered by it, so stamping here would mint authority for a flag the
        // backend never set and pin it against the fetch that would correct it.
        // Local ack-all keeps its protection through the per-item snapshots on
        // its own pending/fulfilled, which exclude exactly those late arrivals.
        for (const n of state.items) n.acked = true
      } else {
        const n = state.items.find(i => i.ts === action.payload)
        if (n) {
          n.acked = true
          markAck(state, n.ts)
        }
      }
    },
    unackNotificationByTs(state, action: PayloadAction<string>) {
      const n = state.items.find(i => i.ts === action.payload)
      if (n) {
        n.acked = false
        markAck(state, n.ts)
      }
    },
    removeNotificationByTs(state, action: PayloadAction<string>) {
      state.items = state.items.filter(n => n.ts !== action.payload)
      pruneAckStamps(state)
    },
    /** WS `notifications_clear` sync: another view cleared the inbox, so this
     *  view drops its copy too (the bell badge derives from `items`).
     *  Idempotent — clearing an already-empty list is a no-op. */
    clearAllNotifications(state) {
      state.items = []
      state.clearSeq += 1
      // No items left to protect, so the stamps only take space.
      state.ackSeqByTs = {}
    },
  },
  extraReducers: (builder) => {
    builder
      .addCase(fetchNotifications.fulfilled, (state, action) => {
        // Stale response: a clear landed while this fetch was in flight, so
        // its payload predates the clear. Applying it would restore the rows
        // and the badge with them.
        if (action.payload.seq !== state.clearSeq) return
        // Merge rather than replace. The payload is the server's view as of
        // request start, so an ack stamped after that snapshot is NEWER than
        // the payload's copy of that item and must survive — otherwise an item
        // the user already acked reappears as unread, and two overlapping
        // fetches resolving out of order make it flip back and forth.
        // Membership, ordering, and every other field still come from the
        // server, so this narrows to ack state only.
        const requestAckSeq = action.payload.ackSeq ?? 0
        const stamps = state.ackSeqByTs ?? {}
        state.items = capped(action.payload.items).map(item => {
          const stamped = stamps[item.ts]
          if (stamped === undefined || stamped <= requestAckSeq) return item
          const local = state.items.find(n => n.ts === item.ts)
          return local ? { ...item, acked: local.acked } : item
        })
        pruneAckStamps(state)
      })
      .addCase(clearNotifications.fulfilled, (state, action) => {
        // The generation moved while the request was in flight, so the
        // `notifications_clear` frame for this clear already emptied the list.
        // Anything present now was delivered AFTER the clear and is still held
        // by the backend — emptying again would delete a live notification.
        // This reducer remains the fallback for a view whose socket is down;
        // such a view converges here, and on reconnect via the refetch.
        if (action.payload.seq !== state.clearSeq) return
        state.items = []
        state.clearSeq += 1
        state.ackSeqByTs = {}
      })
      .addCase(deleteNotification.fulfilled, (state, action) => {
        state.items = state.items.filter(n => n.ts !== action.payload)
        pruneAckStamps(state)
      })
      // Optimistic: update Redux immediately. The confirmation then re-asserts
      // the value — which matters because a stale fetch can install the
      // server's pre-write value in between, and a stamp alone would mark that
      // wrong value fresh — but ONLY under the rule above, so a confirmation
      // never overwrites a change newer than its own request.
      .addCase(ackNotification.pending, (state, action) => {
        const n = state.items.find(i => i.ts === action.meta.arg)
        if (n) {
          n.acked = true
          markAck(state, n.ts)
        }
      })
      .addCase(ackNotification.fulfilled, (state, action) => {
        const { ts, stamp } = action.payload
        if (!stampUnchangedSince(state, ts, stamp)) return
        const n = state.items.find(i => i.ts === ts)
        if (n) {
          n.acked = true
          markAck(state, ts)
        }
      })
      .addCase(unackNotification.pending, (state, action) => {
        const n = state.items.find(i => i.ts === action.meta.arg)
        if (n) {
          n.acked = false
          markAck(state, n.ts)
        }
      })
      .addCase(unackNotification.fulfilled, (state, action) => {
        const { ts, stamp } = action.payload
        if (!stampUnchangedSince(state, ts, stamp)) return
        const n = state.items.find(i => i.ts === ts)
        if (n) {
          n.acked = false
          markAck(state, ts)
        }
      })
      .addCase(ackAllNotifications.pending, (state) => {
        for (const n of state.items) {
          n.acked = true
          markAck(state, n.ts)
        }
      })
      .addCase(ackAllNotifications.fulfilled, (state, action) => {
        const stamps = action.payload?.stamps ?? {}
        for (const n of state.items) {
          if (!stampUnchangedSince(state, n.ts, stamps[n.ts])) continue
          n.acked = true
          markAck(state, n.ts)
        }
      })
  },
})

export const { addNotification, ackNotificationByTs, unackNotificationByTs, removeNotificationByTs, clearAllNotifications } = notificationsSlice.actions
export default notificationsSlice.reducer
