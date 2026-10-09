import { createSlice, createAsyncThunk, type PayloadAction } from '@reduxjs/toolkit'
import { api } from '../api/client'
import { ApiError, isTerminalApprovalRefusal } from '../api/apiError'
import type { Notification } from '../types'
import { coordinatorTarget, approvalTargetKey, sameApprovalTarget, type CoordinatorApprovalTarget } from '../types/approvalTarget'
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
  /** The ts of every approval row that can no longer be decided although it
   *  is still listed: a decide this tab sent was refused as no longer pending
   *  (`isTerminalApprovalRefusal`: a 404, or the `no pending approval` 400), so
   *  the request FAILED and the row says so as an error. An
   *  `approval_resolved` frame removes a live row instead: only a refused
   *  press keeps one, to say why. A retired row is also read: it no longer
   *  asks for anything, so `approvalDecisionSettled` marks it read in this tab and
   *  it leaves the unread badge. Its Approve/Reject are withdrawn. A dismiss that succeeds
   *  removes it, and so do a clear-all and a reload. A reconnect refetch keeps
   *  it (`withLocalApprovalRows`).
   *  An approval row is this tab's copy of a pending request (the `approval`
   *  frame and the reconnect reconcile add it; the notification store holds
   *  none), so a reload drops every one and the reconcile adds back only
   *  what `/api/approvals` still lists as pending.
   *  Held here rather than in a component so the page feed, the bell popover
   *  (which remounts on every open) and the detail panel agree. */
  retiredApprovals?: Record<string, true>
  /** ts → the decision this tab has in flight on that approval row. A row's
   *  decision lifecycle is idle (no entry) -> deciding (an entry) -> settled
   *  (the entry is gone and the row has left, is retired, or is in
   *  `dismissFailed`). `inFlight` counts the presses still awaiting a
   *  response. `ended` records that a frame (resolved, superseded, decided on
   *  another surface) ended the request while it was deciding: the row stays
   *  until the response settles, because a refusal must still be shown on
   *  it, and leaves then if nothing else claimed it. */
  approvalDecisions?: Record<string, { inFlight: number; ended: boolean }>
  /** ts → why a stored note the server refused to delete is still listed:
   *  `dismiss` after its close X, `decided` after a decision on it landed.
   *  The row stays with the matching notice, its Approve/Reject are
   *  withdrawn, and its close X (always shown) tries again. */
  dismissFailed?: Record<string, 'dismiss' | 'decided'>
}

const initialState: NotificationsState = {
  items: [], clearSeq: 0, ackSeq: 0, ackSeqByTs: {}, retiredApprovals: {}, approvalDecisions: {}, dismissFailed: {},
}

/** Ring-buffer cap on the notifications list. Without it, `items` grows
 *  monotonically for the tab's lifetime (ack only flips a flag) — part of
 *  the long-lived-tab heap retention class. Applied on both the
 *  live SSE path and the fetch path so the page and the bell see one
 *  consistent bounded list; oldest entries drop first. Older history stays
 *  in the backend notification log. */
export const NOTIFICATIONS_RING_CAP = 200


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
  for (const map of [state.ackSeqByTs, state.retiredApprovals, state.approvalDecisions, state.dismissFailed]) {
    if (!map) continue
    for (const ts of Object.keys(map)) {
      if (!live.has(ts)) delete map[ts]
    }
  }
}

/** The one ring cap, used by every path that grows the list. It evicts the
 *  oldest rows that are not held. A row is held while a decision is in flight
 *  on it (its response must still land on it), while it shows a notice
 *  (retired, or a failed DELETE): evicting it would drop that notice unseen,
 *  and while it is this tab's own undecided approval row: it is a request the
 *  reader can still decide, and no snapshot or push can bring it back. The
 *  list can exceed the cap only by held rows, which only this tab's own
 *  approvals and presses create, and a held row's marks leave with it. */
const capRows = (state: NotificationsState, items: Notification[]): Notification[] => {
  let excess = items.length - NOTIFICATIONS_RING_CAP
  if (excess <= 0) return items
  const held = (n: Notification) =>
    isLocalApprovalRow(n)
    || Object.hasOwn(state.approvalDecisions ?? {}, n.ts)
    || Object.hasOwn(state.retiredApprovals ?? {}, n.ts)
    || Object.hasOwn(state.dismissFailed ?? {}, n.ts)
  return items.filter(n => {
    if (excess > 0 && !held(n)) { excess -= 1; return false }
    return true
  })
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
  const servedKeys = new Set(served.filter(n => n.kind === 'approval' && !!n.approval_id).map(approvalRowKey))
  const kept = local.filter(n => isLocalApprovalRow(n) && !servedTs.has(n.ts) && !servedKeys.has(approvalRowKey(n)))
  if (kept.length === 0) return served
  // The kept rows are held in the ring cap, so the cap `replaceRows` applies
  // after them takes their room from the served rows, oldest first.
  const out = [...served]
  for (const row of kept) {
    const rowAt = tsOrder(row.ts)
    const at = out.findIndex(n => tsOrder(n.ts) > rowAt)
    if (at < 0) out.push(row)
    else out.splice(at, 0, row)
  }
  return out
}

const removeRow = (state: NotificationsState, ts: string) => {
  state.items = state.items.filter(n => n.ts !== ts)
  pruneAckStamps(state)
}

/** The one way a snapshot or a clear replaces the whole list. A row with a
 *  decision still in flight survives it, together with its marks, so that
 *  decision's response can still land on it: a refusal is rendered on the
 *  row, a landed decision removes it. A clear also ends such a row's request
 *  for this tab (`ended`), so it leaves once its response settles unless a
 *  refusal or a failed DELETE claims it. Each kept row goes back right
 *  after the nearest earlier row the incoming list still holds (ts formats
 *  differ between stored notes and this tab's own rows, so position, not ts,
 *  places it). The list is then held to the ring cap by `capRows`. */
const replaceRows = (state: NotificationsState, incoming: Notification[], { endDeciding }: { endDeciding: boolean }) => {
  const deciding = state.approvalDecisions ?? {}
  const listed = new Set(incoming.map(n => n.ts))
  const prior = state.items
  const keptAt = prior.flatMap((n, i) => (Object.hasOwn(deciding, n.ts) && !listed.has(n.ts) ? [i] : []))
  if (endDeciding) for (const i of keptAt) deciding[prior[i].ts].ended = true
  const next = [...incoming]
  for (const i of keptAt) {
    let at = 0
    for (let j = i - 1; j >= 0; j--) {
      const anchor = next.findIndex(m => m.ts === prior[j].ts)
      if (anchor >= 0) { at = anchor + 1; break }
    }
    next.splice(at, 0, prior[i])
  }
  state.items = capRows(state, next)
  pruneAckStamps(state)
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
        state.items = capRows(state, state.items)
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
    /** A chat-card decision landed: its feed row goes with it. The row is the
     *  tab's own copy of the approval (the server keeps no approval note), so
     *  nothing is sent. */
    removeNotificationByTs(state, action: PayloadAction<string>) {
      removeRow(state, action.payload)
    },
    /** A frame ended this approval row's request: it was resolved, replaced
     *  under the same id, or decided on another surface. An idle row leaves.
     *  A row whose decision is still in flight is only marked, so the
     *  response can still land on it (`approvalDecisionSettled`). */
    endApprovalRow(state, action: PayloadAction<string>) {
      const decision = state.approvalDecisions?.[action.payload]
      if (decision) decision.ended = true
      else removeRow(state, action.payload)
    },
    approvalDecisionBegan(state, action: PayloadAction<string>) {
      const ts = action.payload
      if (!state.items.some(n => n.ts === ts)) return
      const map = (state.approvalDecisions ??= {})
      const d = map[ts]
      if (d) d.inFlight += 1
      else map[ts] = { inFlight: 1, ended: false }
    },
    /** One press's response settled. `refused` retires the row (the error is
     *  rendered on it), `dismiss_failed` keeps a decided stored note with its
     *  controls withdrawn, `failed` (retryable) leaves the row as it was.
     *  When the last press settles on a row a frame ended meanwhile, the row
     *  leaves unless a refusal or a failed DELETE claimed it.
     *  A landed decision whose row was removed needs no settle: removal
     *  prunes the entry. */
    approvalDecisionSettled(state, action: PayloadAction<{ ts: string; outcome: 'refused' | 'failed' | 'dismiss_failed' }>) {
      const { ts, outcome } = action.payload
      const map = state.approvalDecisions ?? {}
      const d = map[ts]
      // The record settles with the last press in flight, so a frame that
      // lands between two responses still finds the row deciding.
      if (d && --d.inFlight <= 0) delete map[ts]
      const row = state.items.find(n => n.ts === ts)
      if (!row) return
      if (outcome === 'refused') {
        ;(state.retiredApprovals ??= {})[ts] = true
        row.acked = true
      } else if (outcome === 'dismiss_failed') {
        ;(state.dismissFailed ??= {})[ts] = 'decided'
      }
      const claimed = Object.hasOwn(state.retiredApprovals ?? {}, ts) || Object.hasOwn(state.dismissFailed ?? {}, ts)
      if (d?.ended && !Object.hasOwn(map, ts) && !claimed) removeRow(state, ts)
    },
    dismissFailedSet(state, action: PayloadAction<{ ts: string; failed: boolean }>) {
      const { ts, failed } = action.payload
      const map = (state.dismissFailed ??= {})
      // A note whose decision already landed keeps saying so on a retry.
      if (failed && state.items.some(n => n.ts === ts)) map[ts] = map[ts] === 'decided' ? 'decided' : 'dismiss'
      else if (!failed && map[ts] !== 'decided') delete map[ts]
    },
    /** WS `notifications_clear` sync: another view cleared the inbox, so this
     *  view drops its copy too (the bell badge derives from `items`).
     *  Idempotent — clearing an already-empty list is a no-op. */
    clearAllNotifications(state) {
      state.clearSeq += 1
      replaceRows(state, [], { endDeciding: true })
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
        replaceRows(state, withLocalApprovalRows(served, state.items), { endDeciding: false })
      })
      .addCase(clearNotifications.fulfilled, (state, action) => {
        // The generation moved while the request was in flight, so the
        // `notifications_clear` frame for this clear already emptied the list.
        // Anything present now was delivered AFTER the clear and is still held
        // by the backend — emptying again would delete a live notification.
        // This reducer remains the fallback for a view whose socket is down;
        // such a view converges here, and on reconnect via the refetch.
        if (action.payload.seq !== state.clearSeq) return
        state.clearSeq += 1
        replaceRows(state, [], { endDeciding: true })
      })
      .addCase(deleteNotification.fulfilled, (state, action) => {
        removeRow(state, action.payload)
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

export const { addNotification, ackNotificationByTs, unackNotificationByTs, removeNotificationByTs, endApprovalRow, clearAllNotifications, approvalDecisionBegan, approvalDecisionSettled } = notificationsSlice.actions
const { dismissFailedSet } = notificationsSlice.actions
export default notificationsSlice.reducer

/** True for an approval row this tab raised itself, from an `approval` frame
 *  or the reconnect reconcile: those carry `_local`, and the notification
 *  store holds no copy of them. A stored note can carry `kind: "approval"`
 *  (an app channel named `approval`) and even an `approval_id` from its meta,
 *  but never `_local` (the bus drops underscore-prefixed meta keys), so it is
 *  deleted on the server like any other note. */
export const isLocalApprovalRow = (n: Pick<Notification, 'kind' | '_local'>): boolean =>
  n.kind === 'approval' && n._local === true

/** The action that takes a row out of the feed: a local approval row leaves
 *  this tab only, and every other row is deleted on the server. */
export const dropNotificationRow = (n: Pick<Notification, 'kind' | '_local' | 'ts'>) =>
  isLocalApprovalRow(n) ? removeNotificationByTs(n.ts) : deleteNotification(n.ts)

/** Takes a row out of the feed through its close X or a retired panel's
 *  Close: a stored note the server refuses to delete stays, marked
 *  `dismissFailed`. A local approval row ends the same way a frame ends it
 *  (`endApprovalRow`), so while its decision is in flight it stays until the
 *  response settles and a refusal still shows on it. Resolves true when the
 *  row left. */
export const dismissNotificationRow = createAsyncThunk(
  'notifications/dismissRow',
  async (n: Pick<Notification, 'kind' | '_local' | 'ts'>, { dispatch, getState }) => {
    if (isLocalApprovalRow(n)) {
      dispatch(endApprovalRow(n.ts))
      const { items } = (getState() as { notifications: NotificationsState }).notifications
      return !items.some(i => i.ts === n.ts)
    }
    dispatch(dismissFailedSet({ ts: n.ts, failed: false }))
    const result = await dispatch(deleteNotification(n.ts))
    const failed = deleteNotification.rejected.match(result)
    if (failed) dispatch(dismissFailedSet({ ts: n.ts, failed: true }))
    return !failed
  },
)

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
  const decided = notifications.dismissFailed ?? {}
  return notifications.items.filter(n =>
    !Object.hasOwn(retired, n.ts) && !Object.hasOwn(decided, n.ts) && sameApprovalTarget(approvalDecideTarget(n), target))
}

/** What one Approve/Reject press on a feed row or the detail panel came to. */
export type ApprovalDecisionOutcome =
  | { kind: 'landed' }
  | { kind: 'refused' }
  | { kind: 'failed'; reason: string }
  | { kind: 'dismiss_failed' }

/** The reason a failed decide quotes on the row. The decide route's own
 *  refusals (4xx) carry a sentence written for the user, so it is quoted. A
 *  429 or a 5xx comes from whatever sits between the tab and the route, in
 *  that layer's words, so it gets `''` and the row shows the plain
 *  "may not have been recorded" copy instead. */
export const plainDecisionReason = (e: unknown): string =>
  e instanceof ApiError && e.status < 500 && e.status !== 429 && e.message ? e.message : ''

/** Sends a decision for an approval row and settles that row's decision
 *  lifecycle (`approvalDecisions`) on the response, so a frame that ends the
 *  request meanwhile cannot remove the row the refusal belongs to. A decision
 *  that lands removes the row, awaiting the server DELETE for a stored note;
 *  a DELETE that fails settles as `dismiss_failed`. The one decide path for
 *  the feed and the detail panel. */
export const decideApprovalRow = createAsyncThunk<
  ApprovalDecisionOutcome,
  { n: Notification; action: 'approve' | 'reject' }
>(
  'notifications/decideApprovalRow',
  async ({ n, action }, { dispatch }) => {
    // Bound to the request this row showed: the id recurs, so only the row's
    // own target may be decided. A row that names none has nothing to decide,
    // and settles as refused with nothing sent.
    const target = approvalDecideTarget(n)
    dispatch(approvalDecisionBegan(n.ts))
    if (!target) {
      dispatch(approvalDecisionSettled({ ts: n.ts, outcome: 'refused' }))
      return { kind: 'refused' }
    }
    try {
      await api.decideApproval(target, action)
    } catch (e) {
      if (isTerminalApprovalRefusal(e)) {
        dispatch(approvalDecisionSettled({ ts: n.ts, outcome: 'refused' }))
        return { kind: 'refused' }
      }
      // eslint-disable-next-line no-console -- keep the raw failure for diagnosis
      console.error(`Approval ${action} failed`, e)
      dispatch(approvalDecisionSettled({ ts: n.ts, outcome: 'failed' }))
      return { kind: 'failed', reason: plainDecisionReason(e) }
    }
    const result = await dispatch(dropNotificationRow(n))
    if (deleteNotification.rejected.match(result)) {
      dispatch(approvalDecisionSettled({ ts: n.ts, outcome: 'dismiss_failed' }))
      return { kind: 'dismiss_failed' }
    }
    return { kind: 'landed' }
  },
)
