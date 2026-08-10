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
}

const initialState: NotificationsState = { items: [], clearSeq: 0 }

/** Ring-buffer cap on the notifications list. Without it, `items` grows
 *  monotonically for the tab's lifetime (ack only flips a flag) — part of
 *  the long-lived-tab heap retention class. Applied on both the
 *  live SSE path and the fetch path so the page and the bell see one
 *  consistent bounded list; oldest entries drop first. Older history stays
 *  in the backend notification log. */
export const NOTIFICATIONS_RING_CAP = 200

const capped = (items: Notification[]): Notification[] =>
  items.length > NOTIFICATIONS_RING_CAP ? items.slice(items.length - NOTIFICATIONS_RING_CAP) : items

export const fetchNotifications = createAsyncThunk(
  'notifications/fetch',
  async (_arg: void, { getState }) => {
    // Captured BEFORE the request so a clear landing mid-flight changes the
    // generation and marks this payload stale.
    const seq = (getState() as { notifications: NotificationsState }).notifications.clearSeq
    const d = await api.notifications()
    return { items: (d.notifications || []) as Notification[], seq }
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

export const ackNotification = createAsyncThunk(
  'notifications/ack',
  async (ts: string) => { api.ackNotification(ts).catch(() => {}); return ts },
)

export const unackNotification = createAsyncThunk(
  'notifications/unack',
  async (ts: string) => { api.unackNotification(ts).catch(() => {}); return ts },
)

export const ackAllNotifications = createAsyncThunk(
  'notifications/ackAll',
  async () => { api.ackAllNotifications().catch(() => {}) },
)

const notificationsSlice = createSlice({
  name: 'notifications',
  initialState,
  reducers: {
    addNotification(state, action: PayloadAction<Notification>) {
      if (!state.items.some(n => n.ts === action.payload.ts)) {
        state.items.push(action.payload)
        state.items = capped(state.items)
      }
    },
    ackNotificationByTs(state, action: PayloadAction<string>) {
      if (action.payload === '*') {
        for (const n of state.items) n.acked = true
      } else {
        state.items = state.items.map(n =>
          n.ts === action.payload ? { ...n, acked: true } : n
        )
      }
    },
    unackNotificationByTs(state, action: PayloadAction<string>) {
      state.items = state.items.map(n =>
        n.ts === action.payload ? { ...n, acked: false } : n
      )
    },
    removeNotificationByTs(state, action: PayloadAction<string>) {
      state.items = state.items.filter(n => n.ts !== action.payload)
    },
    /** WS `notifications_clear` sync: another view cleared the inbox, so this
     *  view drops its copy too (the bell badge derives from `items`).
     *  Idempotent — clearing an already-empty list is a no-op. */
    clearAllNotifications(state) {
      state.items = []
      state.clearSeq += 1
    },
  },
  extraReducers: (builder) => {
    builder
      .addCase(fetchNotifications.fulfilled, (state, action) => {
        // Stale response: a clear landed while this fetch was in flight, so
        // its payload predates the clear. Applying it would restore the rows
        // and the badge with them.
        if (action.payload.seq !== state.clearSeq) return
        state.items = capped(action.payload.items)
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
      })
      .addCase(deleteNotification.fulfilled, (state, action) => {
        state.items = state.items.filter(n => n.ts !== action.payload)
      })
      // Optimistic: update Redux immediately, fire-and-forget to backend
      .addCase(ackNotification.pending, (state, action) => {
        const n = state.items.find(i => i.ts === action.meta.arg)
        if (n) n.acked = true
      })
      .addCase(unackNotification.pending, (state, action) => {
        const n = state.items.find(i => i.ts === action.meta.arg)
        if (n) n.acked = false
      })
      .addCase(ackAllNotifications.pending, (state) => {
        for (const n of state.items) n.acked = true
      })
  },
})

export const { addNotification, ackNotificationByTs, unackNotificationByTs, removeNotificationByTs, clearAllNotifications } = notificationsSlice.actions
export default notificationsSlice.reducer
