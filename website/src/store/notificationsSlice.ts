import { createSlice, createAsyncThunk, createSelector, type PayloadAction } from '@reduxjs/toolkit'
import { api } from '../api/client'
import type { Notification } from '../types'
import { coordinatorTarget, approvalTargetKey, sameApprovalTarget, type CoordinatorApprovalTarget } from '../types/approvalTarget'
import { isNotFoundError } from '../api/apiError'
import { parseTs } from '../utils/notificationTimestamp'

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
  /** ts → why an approval row can no longer be decided although the server
   *  still lists its notification: `gone` (the server reported it expired or
   *  stale), `refused` (a decide from this tab was refused with a 404/410, so
   *  the request FAILED and the row says so as an error) or the recorded
   *  outcome (`approve`/`reject`). Retiring does not read a row: an approval
   *  that expired while the reader was away is still news (the job was
   *  denied), so it stays unread until the reader opens or dismisses it, like
   *  any note. The server is the source of truth for which rows
   *  exist, so a retired row is never removed on that signal: it stays, its
   *  Approve/Reject are withdrawn, and only a DELETE that succeeds removes it.
   *  Held here rather than in a component so the page feed, the bell popover
   *  (which remounts on every open) and the detail panel agree. */
  retiredApprovals?: Record<string, RetiredApprovalReason>
  /** approval id → a decide for that approval is in flight from some view.
   *  Held here, not in a component, because the page feed, the bell popover
   *  and the detail panel can each show the same approval's Approve/Reject,
   *  and a per-view flag would let two of them send conflicting decisions.
   *  Keyed by `approvalDecisionKey`: the request the row names (the row's ts
   *  when it names none). Claimed by `claimApprovalDecision`.
   *  The value is the action pressed, so every view labels that button as
   *  in progress, and only that one. */
  decidingApprovals?: Record<string, ApprovalDecideAction>
}

/** Why an approval row stopped being decidable. `expired` is the server's
 *  own expiry frame, which means the request was denied; `gone` is a
 *  retirement whose outcome this tab does not know (a fresh tab's
 *  reconcile, a replaced instance), so its line cannot say which. */
export type RetiredApprovalReason = 'gone' | 'expired' | 'refused' | 'approve' | 'reject'
export type ApprovalDecideAction = 'approve' | 'reject'

const initialState: NotificationsState = {
  items: [], clearSeq: 0, ackSeq: 0, ackSeqByTs: {}, retiredApprovals: {},
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
  for (const map of [state.ackSeqByTs, state.retiredApprovals]) {
    if (!map) continue
    for (const ts of Object.keys(map)) {
      if (!live.has(ts)) delete map[ts]
    }
  }
}

/** The key an approval row is matched under across a snapshot: the request it
 *  names (its owner-bound target), or its ts when it names none. */
const approvalRowKey = (n: Notification): string => {
  const target = coordinatorTarget(n.approval_id, n.slot || '', n.approval_instance)
  // A target key opens with its origin, so it never equals a bare ts.
  return target ? approvalTargetKey(target) : n.ts
}

/** A row's position in time. Served rows carry an ISO 8601 ts and locally
 *  raised approval rows an epoch, so `Number()` reads every served ts as NaN;
 *  this goes through the feed's own parser, and keeps a bare number for a ts
 *  that parser rejects. */
const tsOrder = (ts: string): number => {
  const at = parseTs(ts).getTime()
  return Number.isNaN(at) ? Number(ts) : at
}

/** *served* plus the approval rows this tab holds that the snapshot cannot
 *  speak for. Approval rows are raised by the `approval` frame and the
 *  approvals reconcile, never written to the server's notification log, so a
 *  snapshot that lacks one says nothing about it. Replacing membership
 *  wholesale would drop a retired row, and the expiry or refusal it explains,
 *  before the reader dismissed it. Such a row stays until its own Dismiss or a
 *  Clear all removes it; a served row naming the same request supersedes it.
 *  Each kept row goes back in ts order. */
const withLocalApprovalRows = (served: Notification[], local: Notification[]): Notification[] => {
  const servedTs = new Set(served.map(n => n.ts))
  const servedKeys = new Set(served.filter(n => n.kind === 'approval').map(approvalRowKey))
  const kept = local.filter(n => n.kind === 'approval' && !servedTs.has(n.ts) && !servedKeys.has(approvalRowKey(n)))
  if (kept.length === 0) return served
  const out = [...served]
  for (const row of kept) {
    const rowAt = tsOrder(row.ts)
    const at = out.findIndex(n => tsOrder(n.ts) > rowAt)
    if (at < 0) out.push(row)
    else out.splice(at, 0, row)
  }
  return out
}

/** Empties every per-row mark, for the paths that empty the list. */
const resetRowMarks = (state: NotificationsState) => {
  state.ackSeqByTs = {}
  state.retiredApprovals = {}
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

/** Removes one notification once the server has. A 404/410 means it is
 *  already gone, so it counts as success: a retired approval row whose note
 *  the server dropped must still be dismissable. */
export const deleteNotification = createAsyncThunk(
  'notifications/delete',
  async (ts: string) => {
    try {
      await api.deleteNotification(ts)
    } catch (e) {
      if (!isNotFoundError(e) && (e as { status?: unknown } | null)?.status !== 410) throw e
    }
    return ts
  },
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
     *  frame, a chat-card decision, a decision that landed, or a 404/410 on
     *  decide. Marks a listed row only and never removes it: the server still
     *  holds the notification until a DELETE says otherwise. A recorded
     *  outcome is kept over a later `gone`/`expired`/`refused` (the frame for
     *  this tab's own decision can arrive after it), and the first of those is
     *  kept over the others. Dispatch `retireApprovalRow`. */
    retireApprovalNote(state, action: PayloadAction<{ ts: string; why: RetiredApprovalReason }>) {
      const { ts, why } = action.payload
      if (!state.items.some(n => n.ts === ts)) return
      const marks = (state.retiredApprovals ??= {})
      if ((why === 'gone' || why === 'expired' || why === 'refused') && marks[ts]) return
      marks[ts] = why
    },
    approvalDecideStarted(state, action: PayloadAction<{ id: string; action: ApprovalDecideAction }>) {
      ;(state.decidingApprovals ??= {})[action.payload.id] = action.payload.action
    },
    approvalDecideSettled(state, action: PayloadAction<string>) {
      if (state.decidingApprovals) delete state.decidingApprovals[action.payload]
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
        const served = action.payload.items.map(item => {
          const stamped = stamps[item.ts]
          if (stamped === undefined || stamped <= requestAckSeq) return item
          const local = state.items.find(n => n.ts === item.ts)
          return local ? { ...item, acked: local.acked } : item
        })
        state.items = capped(withLocalApprovalRows(served, state.items))
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

export const { addNotification, ackNotificationByTs, unackNotificationByTs, retireApprovalNote, approvalDecideStarted, approvalDecideSettled, clearAllNotifications } = notificationsSlice.actions
export default notificationsSlice.reducer

/** Retire an approval row (see `retireApprovalNote`). A retirement the
 *  reader did not see (an expiry while they were away, a fresh tab's
 *  reconcile at boot, a decision made in another view) leaves the read flag
 *  alone, so the bell and dock badges stay lit until someone learns the job
 *  was denied. *seen* is for a retirement the reader caused by pressing the
 *  row's own Approve or Reject: they are looking at the outcome, so the row is
 *  read through the ordinary ack. Otherwise it is read the way any note is:
 *  opened, marked read, or dismissed. No DELETE is sent: the row leaves only
 *  when the reader dismisses it (or a landed decision removes it). */
export const retireApprovalRow = (ts: string, why: RetiredApprovalReason, opts: { seen?: boolean } = {}) =>
  (dispatch: (a: unknown) => unknown, getState: () => unknown) => {
    dispatch(retireApprovalNote({ ts, why }))
    if (!opts.seen) return
    const row = (getState() as { notifications: NotificationsState }).notifications.items.find(n => n.ts === ts)
    if (row && !row.acked) void dispatch(ackNotification(ts))
  }

/** A decision that landed on an approval, from any surface (feed, detail
 *  panel, chat card): retire its row with the recorded outcome, then remove it
 *  once the server confirms. Resolves with the DELETE's settled action. */
export const settleDecidedApproval = (ts: string, action: 'approve' | 'reject') =>
  (dispatch: (a: unknown) => unknown, getState: () => unknown) => {
    // The reader made this decision, so its outcome is not news to them.
    retireApprovalRow(ts, action, { seen: true })(dispatch, getState)
    return dispatch(deleteNotification(ts)) as ReturnType<ReturnType<typeof deleteNotification>>
  }

/** The request an approval row was raised for, or null when the row cannot
 *  name one. Feed rows are only ever raised by the coordinator, so the target
 *  is the row's id, its owning slot ('' for a slotless approval, such as a cron
 *  job's) and the server-issued instance. A row with no instance (written by an
 *  older build) names no request, and nothing decides it. */
export const approvalDecideTarget = (
  n: Pick<Notification, 'approval_id' | 'approval_instance' | 'slot'>,
): CoordinatorApprovalTarget | null => coordinatorTarget(n.approval_id, n.slot || '', n.approval_instance)

/** The key a decide is claimed under: the request the row names, so a claim
 *  for one request under a recurring id never holds back another's (the row's
 *  ts when it names none). */
export const approvalDecisionKey = (n: Pick<Notification, 'approval_id' | 'approval_instance' | 'slot' | 'ts'>): string => {
  const target = approvalDecideTarget(n)
  return target ? approvalTargetKey(target) : n.ts
}

/** Every listed row raised for *target*, retired or not. */
export const approvalRowsFor = (notifications: NotificationsState, target: CoordinatorApprovalTarget): Notification[] =>
  notifications.items.filter(n => sameApprovalTarget(approvalDecideTarget(n), target))

/** The listed rows raised for *target* that can still be decided. */
export const liveApprovalRows = (notifications: NotificationsState, target: CoordinatorApprovalTarget): Notification[] => {
  const retired = notifications.retiredApprovals ?? {}
  return notifications.items.filter(n =>
    !Object.hasOwn(retired, n.ts) && sameApprovalTarget(approvalDecideTarget(n), target))
}

/** Claim the one in-flight decide for approval *id*. Returns false when any
 *  view already holds it, so the caller sends nothing; the holder releases it
 *  with `approvalDecideSettled` once its request settles. */
export const claimApprovalDecision = (id: string, action: ApprovalDecideAction) =>
  (dispatch: (a: unknown) => unknown, getState: () => unknown): boolean => {
    if ((getState() as { notifications: NotificationsState }).notifications.decidingApprovals?.[id]) return false
    dispatch(approvalDecideStarted({ id, action }))
    return true
  }

/** The rows every unread surface counts (the bell and dock badges, the tab
 *  title, the native banner): not acked, and not silenced or passive. A
 *  retired approval counts while it is unread, and its row shows a quiet dot
 *  for as long, so a lit badge always has a highlighted row to clear it. One
 *  selector, so those surfaces cannot drift apart. */
export const selectUnreadNotes = createSelector(
  [(s: { notifications: NotificationsState }) => s.notifications.items],
  items => items.filter(n => !n.acked && !isSilencedNote(n)),
)
