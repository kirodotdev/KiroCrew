/** What a socket does once it opens: the first connect's boot reads, or a
 *  reconnect's catch-up for every frame family the gap may have lost. The
 *  order is part of the contract: each step is placed relative to the
 *  others for a stated reason below. */
import type { MutableRefObject } from 'react'
import type { QueryClient } from '@tanstack/react-query'
import { store, type AppDispatch } from '../../store'
import { fetchSlots } from '../../store/dashboardSlice'
import { markGatewayConnected } from './gatewayConnectivity'
import { fetchNotifications, markBootNotificationsFetched } from '../../store/notificationsSlice'
import { refreshSlot, warmSlotCache, clearSubagentsForSnapshot, resyncWaitingSideConversations } from '../../store/chatSlice'
import { anchorForSlot, loadLayout, sessionSlots } from '../splitLayoutStore'
import { observedPaneSlots } from '../../api/slotMessagesQuery'
import { consumeUpdateRestartLatch } from './bundleReload'
import { refreshServerStateAfterReconnect } from './serverState'
import type { SocketConnection } from './connection'
import type { StreamBuffers } from './streamBuffers'
import { replaySlotFrames, subagentReplaySlots } from '../../lib/subagentReplayScope'

export interface CatchUpContext {
  dispatch: AppDispatch
  queryClient: QueryClient
  socket: SocketConnection
  buffers: StreamBuffers
  seedAutomations: () => void
  syncPendingApprovals: () => Promise<void>
  syncPendingQuestions: () => Promise<void>
  syncWorkflowRuns: () => Promise<void>
  focusedSlot: () => string | null
  reconnectSnapshot: ReconnectSnapshotWait
}

/** How long a reconnect waits for the socket's connect snapshot before it reads
 *  the slot list over HTTP. The gateway sends the snapshot as the socket's first
 *  frame, right after the handshake, so only a connect whose snapshot failed
 *  waits this long. */
export const RECONNECT_SNAPSHOT_WAIT_MS = 10_000

/** The reconnect catch-up window (`reconnectingRef`): open from a reconnect's
 *  open until the connect snapshot of that socket has been applied. The snapshot
 *  is the list `GET /api/chat/slots?crew=all` answers, so the catch-up reads no
 *  second copy; every such GET is a full slot list pass on the gateway's loop,
 *  and a reconnect storm sends one per tab. */
export interface ReconnectSnapshotWait {
  /** A reconnect opened: hold the window open until its snapshot. */
  arm(): void
  /** A `slots` frame of the live socket, after its rows were applied. The first
   *  one after `arm` ends the window. An empty list is read once more over HTTP
   *  first, and the window ends when that read settles: the list reducers skip
   *  an empty frame before a reconnect's first list, so only a GET reply tears
   *  down a list that went empty while the socket was down. */
  onSlots(rows: unknown): void
  /** The socket went away or the hook unmounts: end the window, stop waiting. */
  disarm(): void
}

export function createReconnectSnapshotWait(
  dispatch: AppDispatch,
  reconnectingRef: MutableRefObject<boolean>,
): ReconnectSnapshotWait {
  let count = 0
  let open = 0  // the window holding `reconnectingRef`, 0 when none
  let waiting = 0  // the window still waiting for its snapshot, 0 when none
  let timer: ReturnType<typeof setTimeout> | undefined
  const close = (gen: number): void => {
    if (open !== gen) return
    open = 0
    reconnectingRef.current = false
  }
  const readList = (gen: number): void => {
    void dispatch(fetchSlots()).finally(() => close(gen))
  }
  return {
    arm() {
      clearTimeout(timer)
      const gen = ++count
      open = gen
      waiting = gen
      reconnectingRef.current = true
      timer = setTimeout(() => {
        if (waiting !== gen) return
        waiting = 0
        readList(gen)
      }, RECONNECT_SNAPSHOT_WAIT_MS)
    },
    onSlots(rows) {
      const gen = waiting
      if (!gen) return
      waiting = 0
      clearTimeout(timer)
      if (Array.isArray(rows) && rows.length > 0) close(gen)
      else readList(gen)
    },
    disarm() {
      clearTimeout(timer)
      waiting = 0
      close(open)
    },
  }
}

/** `subscribe_subagents` naming the slots whose finished runs the gateway
 *  rebuilds from the crew log; names past one frame's cap follow as
 *  `subagent_replay` frames. */
function sendSubagentSubscribe(ws: WebSocket, ctx: CatchUpContext): void {
  const [first = [], ...rest] = replaySlotFrames(
    subagentReplaySlots(ctx.focusedSlot(), observedPaneSlots(ctx.queryClient)),
  )
  ws.send(JSON.stringify({ type: 'subscribe_subagents', slots: first }))
  for (const slots of rest) ws.send(JSON.stringify({ type: 'subagent_replay', slots }))
}

/** The first successful open of this hook: register as connected and take
 *  the boot snapshots the socket is authoritative for. */
export function runFirstConnect(ws: WebSocket, ctx: CatchUpContext): void {
  const { dispatch, socket } = ctx
  socket.wasConnectedRef.current = true
  markGatewayConnected(dispatch)
  ctx.seedAutomations()
  // FIRST connect only: App's mount effect already dispatched fetchSlots,
  // and the socket's open handler fires strictly after it, so repeating it
  // here is a redundant round-trip at the worst possible moment. The
  // reconnect catch-up reads no list either: the socket's connect snapshot
  // carries it (`createReconnectSnapshotWait`). fetchNotifications is the opposite: THIS is its authoritative
  // boot dispatch (#765). The snapshot must be taken after the socket is
  // registered, or a notification created between an earlier snapshot and
  // registration is pushed to nobody and stays invisible until a reconnect
  // — so the mount effect no longer fetches; it only arms a fallback for a
  // socket that never connects, and the mark below keeps that fallback
  // from double-firing. syncPendingApprovals stays chained on the fetch
  // settling, because fetchNotifications.fulfilled replaces membership and
  // ordering wholesale and would wipe any approval notifications synced
  // before it (its merge preserves local ack flags only, so it is no
  // protection for a row the response does not carry).
  // A fallback that already fired (connect took >5s) has a snapshot in
  // flight; serialize behind it so the older response can never replace
  // this (newer, post-registration) one after it lands.
  const firedFallback = markBootNotificationsFetched()
  ;(firedFallback
    ? firedFallback.then(() => dispatch(fetchNotifications()))
    : dispatch(fetchNotifications())
  ).then(() => ctx.syncPendingApprovals())
  ctx.syncPendingQuestions()
  // FIRST connect: seeds rows for runs already in flight (a reload, or a new
  // tab on a session whose workflow is still going). The WS stream only
  // carries events from here on, so without this such a run is invisible
  // until its next phase event — and one that ends first never appears.
  ctx.syncWorkflowRuns()
  // The same gap for Side Chat: a side turn that ended between a panel's
  // restore read and this socket's registration sent its final frame to no
  // socket of this tab, so every restored panel reads its conversation again.
  void dispatch(resyncWaitingSideConversations())
  // Eagerly subscribe to subagent events on first connect too.
  dispatch(clearSubagentsForSnapshot())
  sendSubagentSubscribe(ws, ctx)
  // Flush a log subscription that was requested before the socket opened.
  // subscribeLogs() stores the callback but returns early when readyState
  // is not OPEN, so a page mounting during the handshake (a cold load of
  // /logs) would otherwise never send subscribe_logs and would show no
  // lines at all until an unrelated reconnect. The reconnect path does
  // this too; the two paths must stay symmetric.
  if (socket.logCbRef.current) ws.send(JSON.stringify({ type: 'subscribe_logs' }))
  // Announce only a visible mounted transcript. A hidden first connect has no
  // return edge; visibilitychange will send the later visible frame. The
  // `announce` mark lets the gateway tell a connect from a return.
  if (!document.hidden) {
    ws.send(JSON.stringify({ type: 'slot_focused', slot: ctx.focusedSlot(), announce: true }))
  }
}

/** A later open: re-fetch state instead of reloading the page, which keeps
 *  unsent messages, scroll position and form inputs. */
export function runReconnectCatchUp(ws: WebSocket, ctx: CatchUpContext): void {
  const { dispatch, queryClient, socket, buffers } = ctx
  // Reconnecting after an in-app update's restart: the disconnect WAS the
  // gateway exec'ing its updated self, and this tab's JS predates the
  // rebuild. Reload instead of the state re-fetch below — on a git
  // checkout the version often does not change, so the 'dashboard'
  // frame's version comparison would never fire and the tab would sit on
  // the stale bundle (with the update overlay's spinner) forever.
  if (consumeUpdateRestartLatch()) {
    window.location.reload()
    return
  }
  // Suppress markSlotUnread until the slot list of this socket is applied.
  // The list is the connect snapshot, the socket's first frame, so the
  // catch-up reads no copy of it over HTTP (an empty or missing snapshot
  // still is, see `createReconnectSnapshotWait`).
  ctx.reconnectSnapshot.arm()
  buffers.dropForReconnect()
  markGatewayConnected(dispatch)
  refreshServerStateAfterReconnect(queryClient)
  ctx.seedAutomations()
  dispatch(fetchNotifications()).then(() => ctx.syncPendingApprovals())
  ctx.syncPendingQuestions()
  // Same one-shot problem, different stream: a run that ENDED while the
  // socket was down pushed a terminal `workflow_run_event` nobody received,
  // and a run that STARTED while it was down has no row at all.
  ctx.syncWorkflowRuns()
  // A side turn that ended while the socket was down sent its final frame to
  // nobody here, and so did every other side frame, so each restored Side Chat
  // panel reads its conversation again.
  void dispatch(resyncWaitingSideConversations())
  // Re-fetch active slot messages to recover from missed chunks
  const active = store.getState().chat.activeSlot
  if (active) dispatch(refreshSlot(active))
  // refreshSlot self-guards to the ACTIVE slot, but the queue event family
  // (queue_push/cancel/edit/reorder) is broadcast fire-and-forget with no
  // replay — a mutation that happened while the socket was down never
  // reaches this client, so a pane co-rendered in the active slot's split
  // keeps rendering the queue it held at the drop (#2348). Warm every
  // OTHER live member of that persisted split: warmSlotCache is the
  // sanctioned background hydration (self-guards against the active slot,
  // writes only the per-slot caches and never the active `messages`,
  // rebuilds queued rows from the server's canonical queue), so the
  // re-hydration is authoritative and idempotent. Members are validated
  // against live slots (the ChatPage.splitAnchorForActive pattern) so a
  // stale layout naming a deleted session costs no 404. With no persisted
  // split nothing is dispatched. The catch keeps a corrupt persisted
  // layout from aborting the rest of reconnect setup (resubscribes and
  // focus re-announce below).
  const warmed = new Set<string>()
  if (active) {
    try {
      const liveKeys = new Set(store.getState().dashboard.slots.map(s => s.key))
      for (const member of new Set(sessionSlots(loadLayout(anchorForSlot(active))))) {
        if (member !== active && liveKeys.has(member)) {
          warmed.add(member)
          dispatch(warmSlotCache(member))
        }
      }
    } catch (err) {
      // This catch deliberately swallows so a corrupt persisted layout cannot
      // abort the rest of reconnect setup; the cost is that the skipped
      // re-hydration's only symptom is a co-rendered pane still showing the
      // queue it held at the drop.
      // eslint-disable-next-line no-console -- only trace of a skipped re-hydration
      console.warn('reconnect split-pane warm skipped', err)
    }
  }
  // A mounted ChatPane whose slot is NEITHER the active slot NOR one of
  // its split members — the Crew Members DM thread is the standing case:
  // its `member-<slug>` slot never becomes the Redux active slot and no
  // persisted split names it — is covered by neither branch above. The
  // same fire-and-forget frames it lives on (tool_result, a later
  // tool_call, the final _done) are lost across the drop, and the pane's
  // own hydrate query is one-shot (staleTime Infinity), so without this
  // warm the pane keeps rendering the tool-call row it held when the
  // socket died — for good, until a remount. The observed hydrate queries
  // are the registry of on-screen panes (api/slotMessagesQuery.ts): warm
  // each once through the same sanctioned path, which reconciles the rows
  // to the server's canonical transcript, settles the run indicator to
  // the server's answer (idle when the turn ended, streaming when one
  // started during the outage) unless a live frame ordered after the
  // warm already wrote it, and raises the chunk replay floor when the
  // turn is still live. The active slot is skipped here and
  // again inside the thunk.
  for (const slot of observedPaneSlots(queryClient)) {
    if (slot === active || warmed.has(slot)) continue
    warmed.add(slot)
    dispatch(warmSlotCache(slot))
  }
  // Eagerly subscribe to subagent events so chunks arrive even when
  // Activity Panel isn't open — final result still comes via done event.
  dispatch(clearSubagentsForSnapshot())
  sendSubagentSubscribe(ws, ctx)
  if (socket.logCbRef.current) ws.send(JSON.stringify({ type: 'subscribe_logs' }))
  // Re-announce only while visible. Reconnect itself is never a return edge;
  // a hidden reconnect waits for visibilitychange.
  if (!document.hidden) {
    ws.send(JSON.stringify({ type: 'slot_focused', slot: ctx.focusedSlot(), announce: true }))
  }
}
