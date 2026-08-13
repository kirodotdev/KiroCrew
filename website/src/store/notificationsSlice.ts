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
  mutationVersion: number
  latestFetchSequence: number
  fetchSnapshots: Record<string, { mutationVersion: number, sequence: number }>
  optimisticMutationCount: number
}

const initialState: NotificationsState = {
  items: [],
  clearSeq: 0,
  mutationVersion: 0,
  latestFetchSequence: 0,
  fetchSnapshots: {},
  optimisticMutationCount: 0,
}

/** Ring-buffer cap on the notifications list. Without it, `items` grows
 *  monotonically for the tab's lifetime (ack only flips a flag) — part of
 *  the long-lived-tab heap retention class. Applied on both the
 *  live SSE path and the fetch path so the page and the bell see one
 *  consistent bounded list; oldest entries drop first. Older history stays
 *  in the backend notification log. */
export const NOTIFICATIONS_RING_CAP = 200

const capped = (items: Notification[]): Notification[] =>
  items.length > NOTIFICATIONS_RING_CAP ? items.slice(items.length - NOTIFICATIONS_RING_CAP) : items

const markMutation = (state: NotificationsState) => {
  state.mutationVersion = (state.mutationVersion ?? 0) + 1
}

const beginOptimisticMutation = (state: NotificationsState) => {
  markMutation(state)
  state.optimisticMutationCount = (state.optimisticMutationCount ?? 0) + 1
}

const finishOptimisticMutation = (state: NotificationsState) => {
  if (!(state.optimisticMutationCount ?? 0)) return
  state.optimisticMutationCount--
  markMutation(state)
}

const beginFetch = (state: NotificationsState, requestId: string) => {
  const sequence = (state.latestFetchSequence ?? 0) + 1
  state.latestFetchSequence = sequence
  const snapshots = state.fetchSnapshots ?? (state.fetchSnapshots = {})
  snapshots[requestId] = { mutationVersion: state.mutationVersion ?? 0, sequence }
}

const applyFetchedNotifications = (
  state: NotificationsState,
  payload: { items: Notification[], seq: number },
  requestId: string,
) => {
  const snapshots = state.fetchSnapshots ?? {}
  const snapshot = snapshots[requestId]
  delete snapshots[requestId]
  if (!snapshot
    || snapshot.sequence !== state.latestFetchSequence
    || state.optimisticMutationCount
    || snapshot.mutationVersion < (state.mutationVersion ?? 0)
    || payload.seq !== (state.clearSeq ?? 0)) return
  state.items = capped(payload.items)
}

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
  async (ts: string) => { await api.ackNotification(ts); return ts },
)

export const unackNotification = createAsyncThunk(
  'notifications/unack',
  async (ts: string) => { await api.unackNotification(ts); return ts },
)

export const ackAllNotifications = createAsyncThunk(
  'notifications/ackAll',
  async () => { await api.ackAllNotifications() },
)

const notificationsSlice = createSlice({
  name: 'notifications',
  initialState,
  reducers: {
    addNotification(state, action: PayloadAction<Notification>) {
      if (!state.items.some(n => n.ts === action.payload.ts)) {
        state.items.push(action.payload)
        state.items = capped(state.items)
        markMutation(state)
      }
    },
    ackNotificationByTs(state, action: PayloadAction<string>) {
      if (action.payload === '*') {
        let changed = false
        for (const n of state.items) {
          if (!n.acked) {
            n.acked = true
            changed = true
          }
        }
        if (changed) markMutation(state)
      } else {
        const notification = state.items.find(n => n.ts === action.payload)
        if (notification && !notification.acked) {
          notification.acked = true
          markMutation(state)
        }
      }
    },
    unackNotificationByTs(state, action: PayloadAction<string>) {
      const notification = state.items.find(n => n.ts === action.payload)
      if (notification && notification.acked !== false) {
        notification.acked = false
        markMutation(state)
      }
    },
    removeNotificationByTs(state, action: PayloadAction<string>) {
      const items = state.items.filter(n => n.ts !== action.payload)
      if (items.length !== state.items.length) {
        state.items = items
        markMutation(state)
      }
    },
    /** WS `notifications_clear` sync drops this view's copy and advances the
     *  fetch epoch, so a response started before the clear cannot restore it. */
    clearAllNotifications(state) {
      state.items = []
      state.clearSeq = (state.clearSeq ?? 0) + 1
      markMutation(state)
    },
  },
  extraReducers: (builder) => {
    builder
      .addCase(fetchNotifications.pending, (state, action) => {
        beginFetch(state, action.meta.requestId)
      })
      .addCase(fetchNotifications.fulfilled, (state, action) => {
        applyFetchedNotifications(state, action.payload, action.meta.requestId)
      })
      .addCase(fetchNotifications.rejected, (state, action) => {
        delete (state.fetchSnapshots ?? {})[action.meta.requestId]
      })
      .addCase(clearNotifications.fulfilled, (state, action) => {
        // The clear's socket frame can already have emptied this view. In that
        // case, leave notifications delivered after the clear intact.
        if (action.payload.seq !== (state.clearSeq ?? 0)) return
        state.items = []
        state.clearSeq = (state.clearSeq ?? 0) + 1
        markMutation(state)
      })
      .addCase(deleteNotification.fulfilled, (state, action) => {
        state.items = state.items.filter(n => n.ts !== action.payload)
        markMutation(state)
      })
      .addCase(ackNotification.pending, (state, action) => {
        const n = state.items.find(i => i.ts === action.meta.arg)
        if (n) n.acked = true
        beginOptimisticMutation(state)
      })
      .addCase(ackNotification.fulfilled, (state) => {
        finishOptimisticMutation(state)
      })
      .addCase(ackNotification.rejected, (state) => {
        finishOptimisticMutation(state)
      })
      .addCase(unackNotification.pending, (state, action) => {
        const n = state.items.find(i => i.ts === action.meta.arg)
        if (n) n.acked = false
        beginOptimisticMutation(state)
      })
      .addCase(unackNotification.fulfilled, (state) => {
        finishOptimisticMutation(state)
      })
      .addCase(unackNotification.rejected, (state) => {
        finishOptimisticMutation(state)
      })
      .addCase(ackAllNotifications.pending, (state) => {
        for (const n of state.items) n.acked = true
        beginOptimisticMutation(state)
      })
      .addCase(ackAllNotifications.fulfilled, (state) => {
        finishOptimisticMutation(state)
      })
      .addCase(ackAllNotifications.rejected, (state) => {
        finishOptimisticMutation(state)
      })
  },
})

export const { addNotification, ackNotificationByTs, unackNotificationByTs, removeNotificationByTs, clearAllNotifications } = notificationsSlice.actions
export default notificationsSlice.reducer
