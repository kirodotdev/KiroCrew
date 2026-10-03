import { createSlice, createAsyncThunk, createSelector, type PayloadAction } from '@reduxjs/toolkit'
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
  /** ts → why an approval row can no longer be decided although it is still
   *  listed: `gone` (the server reported it expired or
   *  stale), `refused` (a decide from this tab was refused with a 404/410, so
   *  the request FAILED and the row says so as an error) or the recorded
   *  outcome (`approve`/`reject`). A retired row is also read: it no longer
   *  asks for anything, so `retireApprovalRow` acks it and it leaves the unread
   *  badge. A retired row is never removed on that signal: it stays, its
   *  Approve/Reject are withdrawn, and only a dismiss that succeeds removes it.
   *  An approval row is this tab's copy of a pending request (the `approval`
   *  frame and the reconnect reconcile add it; the notification store holds
   *  none), so a reload drops every one and the reconcile adds back only
   *  what `/api/approvals` still lists as pending.
   *  Held here rather than in a component so the page feed, the bell popover
   *  (which remounts on every open) and the detail panel agree. */
  retiredApprovals?: Record<string, RetiredApprovalReason>
  /** ts → the last DELETE for that row was refused, so the row is still
   *  listed and every view that shows it says the dismiss failed. Set by a
   *  rejected `deleteNotification`, cleared when the next one starts. Held
   *  here for the same reason as `retiredApprovals`: the press can come from
   *  one view (a landed decision's cleanup, the detail panel's Dismiss) while
   *  the row is read in another. */
  dismissFailed?: Record<string, true>
}

export type RetiredApprovalReason = 'gone' | 'refused' | 'approve' | 'reject'

const initialState: NotificationsState = {
  items: [], clearSeq: 0, ackSeq: 0, ackSeqByTs: {}, retiredApprovals: {}, dismissFailed: {},
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

/** True when an attention surface must skip *n*.
 *
 *  The backend stamps both halves and states the contract in
 *  `kiro_crew/notifications/settings.py`: muting a channel keeps the note in
 *  history but sets `silenced: true` and forces `priority: "passive"`, "so every
 *  attention surface (badge count, sound, native banner, feed styling) skips
 *  it". `notification_coordinator.deliver()` holds its own `_unread_count` to
 *  the priority half of that rule.
 *
 *  Both halves are checked, not just `silenced`: a channel default or a
 *  producer-requested `passive` (a subagent completion, say) is never muted and
 *  so carries no `silenced` flag, yet the backend already leaves it out of the
 *  unread count. It lives beside the notifications state rather than inside one
 *  consumer, so the attention surfaces that read it cannot drift apart: two of
 *  them disagreeing about what "silenced" means is the class of defect this
 *  predicate exists to close. */
export function isSilencedNote(n: Pick<Notification, 'silenced' | 'priority'>): boolean {
  return !!n.silenced || n.priority === 'passive'
}

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
  const live = new Set(state.items.map(n => n.ts))
  // The per-row marks go with their row, so a refetch that no longer lists a
  // row drops its retired mark with it.
  for (const map of [state.ackSeqByTs, state.retiredApprovals, state.dismissFailed]) {
    if (!map) continue
    for (const ts of Object.keys(map)) {
      if (!live.has(ts)) delete map[ts]
    }
  }
}

/** Empties every per-row mark, for the paths that empty the list. */
const resetRowMarks = (state: NotificationsState) => {
  state.ackSeqByTs = {}
  state.retiredApprovals = {}
  state.dismissFailed = {}
}

/**
 * Boot-time notifications dedupe (#765). App's mount effect and the
 * WebSocket's FIRST-connect handler used to each dispatch `fetchNotifications`
 * -- two identical round-trips on every boot. The first-connect copy is the
 * one that must stay authoritative: its HTTP snapshot is taken AFTER the
 * socket is registered, so a notification created after the snapshot is
 * guaranteed to arrive as a WS push. A mount-time snapshot has no such
 * guarantee -- a notification created between it and socket registration is
 * pushed to nobody and would be invisible until a reconnect. So the mount
 * effect no longer fetches; it arms this fallback instead, which fires only
 * when no boot fetch has happened within the window (a socket that never
 * connects, e.g. a proxy that strips Upgrade) so the inbox still populates
 * over plain HTTP. First-connect marks the flag before dispatching; the
 * fallback marks it too, so the two can never double-fire.
 */
export const BOOT_NOTIFICATIONS_FALLBACK_MS = 5000
let bootNotificationsFetched = false
// In-flight fallback fetch, held so a late first connect can SERIALIZE its own
// fetch after it. Without this, a connect landing just after the fallback
// fired could resolve its (newer) snapshot first and then have the older
// fallback response replace membership wholesale -- notifications gone until
// a reconnect. fetchNotifications.fulfilled has generation guards for clears
// and acks, not for two overlapping boot fetches.
let inflightFallbackFetch: Promise<unknown> | null = null
/**
 * Marks the boot fetch as owned by the caller (the first-connect handler).
 * Returns the in-flight fallback fetch when one already fired, so the caller
 * can chain its own fetch after it settles -- guaranteeing the newest
 * (post-registration) snapshot always lands last. Null when no fallback fired.
 */
export function markBootNotificationsFetched(): Promise<unknown> | null {
  bootNotificationsFetched = true
  const p = inflightFallbackFetch
  inflightFallbackFetch = null
  return p
}
/** Arms the no-WS fallback; returns the disarm function for effect cleanup. */
export function armBootNotificationsFallback(run: () => unknown, ms: number = BOOT_NOTIFICATIONS_FALLBACK_MS): () => void {
  const t = setTimeout(() => {
    if (bootNotificationsFetched) return
    bootNotificationsFetched = true
    // Redux thunk dispatch promises settle with the action (never reject);
    // Promise.resolve covers a non-promise return defensively.
    inflightFallbackFetch = Promise.resolve(run()).catch(() => undefined)
  }, ms)
  return () => clearTimeout(t)
}
/** Test-only: restores the pristine boot state between cases. */
export function resetBootNotificationsForTest(): void {
  bootNotificationsFetched = false
  inflightFallbackFetch = null
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

export const ackNotification = createAsyncThunk<
  { ts: string; stamp: number | undefined },
  string,
  { rejectValue: { ts: string; stamp: number | undefined } }
>(
  'notifications/ack',
  async (ts: string, { getState, rejectWithValue }) => {
    // Read AFTER `pending` has stamped: this is our own optimistic stamp, so a
    // later value means something newer than this request moved the flag.
    const stamp = ackStampOf((getState() as { notifications: NotificationsState }).notifications, ts)
    try {
      await api.ackNotification(ts)
    } catch {
      // The rejection carries the same stamp the fulfilment would, so the
      // rollback below can be held to the same one-rule-per-write check.
      return rejectWithValue({ ts, stamp })
    }
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
    /** An approval that can no longer be decided: an `approval_resolved`
     *  frame, a decision from the feed or detail panel that landed, or a 404/410 on
     *  decide. Marks a listed row only and never removes it: the row stays
     *  until a dismiss succeeds. A recorded
     *  outcome is kept over a later `gone`/`refused` (the frame for this tab's
     *  own decision can arrive after it), and the first of those two is kept
     *  over the other. Dispatch `retireApprovalRow`, which also acks the row. */
    retireApprovalNote(state, action: PayloadAction<{ ts: string; why: RetiredApprovalReason }>) {
      const { ts, why } = action.payload
      if (!state.items.some(n => n.ts === ts)) return
      const marks = (state.retiredApprovals ??= {})
      if ((why === 'gone' || why === 'refused') && marks[ts]) return
      marks[ts] = why
    },
    /** A chat-card decision landed: its feed row goes with it. The row is the
     *  tab's own copy of the approval (the server keeps no approval note), so
     *  nothing is sent. */
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
      resetRowMarks(state)
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
        resetRowMarks(state)
      })
      .addCase(deleteNotification.pending, (state, action) => {
        if (state.dismissFailed) delete state.dismissFailed[action.meta.arg]
      })
      .addCase(deleteNotification.rejected, (state, action) => {
        // Only for a row still listed: one a refetch dropped has nothing left
        // to say the failure on.
        if (state.items.some(n => n.ts === action.meta.arg)) {
          ;(state.dismissFailed ??= {})[action.meta.arg] = true
        }
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
      // The server refused or never heard the ack, so it still holds the note
      // unread: undo the optimistic flip rather than leave a read row the next
      // fetch (or another tab) will flip back -- but ONLY under the same rule
      // as the confirmation: a rollback is evidence about the request it
      // belongs to, and a newer ack that already moved the stamp (a second
      // press that succeeded while the first was still in flight) outranks it.
      // Stamped like every other local ack change so an in-flight fetch cannot
      // resurrect the optimistic value.
      .addCase(ackNotification.rejected, (state, action) => {
        const ts = action.meta.arg
        // A throw before the stamp was read (no payload) carries no evidence
        // about the flag, so it rolls nothing back.
        if (!action.payload) return
        if (!stampUnchangedSince(state, ts, action.payload.stamp)) return
        const n = state.items.find(i => i.ts === ts)
        if (n) {
          n.acked = false
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

export const { addNotification, ackNotificationByTs, unackNotificationByTs, retireApprovalNote, removeNotificationByTs, clearAllNotifications } = notificationsSlice.actions
export default notificationsSlice.reducer

/** Retire an approval row (see `retireApprovalNote`) and mark it read. A
 *  retired row asks for nothing, so it must not keep the bell and dock badges
 *  lit while it waits for its Dismiss. The ack goes through the ordinary
 *  `ackNotification` write, so the server's read flag agrees and a later
 *  fetch does not light the row again. No DELETE is sent: the row leaves only
 *  when the reader dismisses it (or a landed decision removes it). */
export const retireApprovalRow = (ts: string, why: RetiredApprovalReason) =>
  (dispatch: (a: unknown) => unknown, getState: () => unknown) => {
    dispatch(retireApprovalNote({ ts, why }))
    const row = (getState() as { notifications: NotificationsState }).notifications.items.find(n => n.ts === ts)
    if (row && !row.acked) void dispatch(ackNotification(ts))
  }

/** A decision that landed on an approval, from the feed or the detail
 *  panel: retire its row with the recorded outcome, then remove it
 *  once the server confirms. Resolves with the DELETE's settled action. */
export const settleDecidedApproval = (ts: string, action: 'approve' | 'reject') =>
  (dispatch: (a: unknown) => unknown, getState: () => unknown) => {
    retireApprovalRow(ts, action)(dispatch, getState)
    return dispatch(deleteNotification(ts)) as ReturnType<ReturnType<typeof deleteNotification>>
  }

/** The key a decide is sent under. */
export const approvalDecisionKey = (n: Pick<Notification, 'approval_id' | 'ts'>): string => n.approval_id || n.ts

/** Where a decide for this row is sent. A coordinator approval's id is the
 *  caller's and recurs, so a row that carries the server-issued instance names
 *  it, together with the owning slot ('' for a slotless approval, such as a
 *  cron job's): the server then resolves only the request this row showed, and
 *  refuses once another request holds the id. A row without one (a chat-runner
 *  approval) keeps the bare-id path. */
export const approvalDecideTarget = (
  n: Pick<Notification, 'approval_instance' | 'slot'>,
): { origin: 'coordinator'; slot: string; instance: string } | undefined =>
  n.approval_instance ? { origin: 'coordinator', slot: n.slot || '', instance: n.approval_instance } : undefined

/** The listed rows for approval *id* that can still be decided, i.e. are not
 *  retired. The id recurs, so a retired row for an earlier request can share it
 *  with the live one; a retirement or a settled decision must land on the live
 *  row, never on that earlier one. */
export const liveApprovalRows = (notifications: NotificationsState, id: string): Notification[] => {
  const retired = notifications.retiredApprovals ?? {}
  return notifications.items.filter(n =>
    n.approval_id === id && !Object.hasOwn(retired, n.ts))
}

const NO_RETIRED_MARKS: Readonly<Record<string, RetiredApprovalReason>> = {}

/** The rows every unread surface counts (the bell and dock badges, the tab
 *  title, the native banner): not acked, not silenced or passive, and not a
 *  retired approval. A retired row asks for nothing whatever its ack flag
 *  says, and that flag goes back to unread when the ack `retireApprovalRow`
 *  sends is refused. The row's own dot already ignores the flag, so the counts
 *  must too, or a badge stays lit with no highlighted row to clear it. One
 *  selector, so those surfaces cannot drift apart. */
export const selectUnreadNotes = createSelector(
  [
    (s: { notifications: NotificationsState }) => s.notifications.items,
    (s: { notifications: NotificationsState }) => s.notifications.retiredApprovals ?? NO_RETIRED_MARKS,
  ],
  (items, retired) => items.filter(n =>
    !n.acked && !isSilencedNote(n) && !Object.hasOwn(retired, n.ts)),
)
