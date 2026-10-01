/** What a socket does once it opens: the first connect's boot reads, or a
 *  reconnect's catch-up for every frame family the gap may have lost. The
 *  order is part of the contract: each step is placed relative to the
 *  others for a stated reason below. */
import type { QueryClient } from '@tanstack/react-query'
import { store, type AppDispatch } from '../../store'
import { sseConnected, fetchSlots } from '../../store/dashboardSlice'
import { fetchNotifications, markBootNotificationsFetched } from '../../store/notificationsSlice'
import { refreshSlot, warmSlotCache, clearSubagentsForSnapshot } from '../../store/chatSlice'
import { anchorForSlot, loadLayout, sessionSlots } from '../splitLayoutStore'
import { observedPaneSlots } from '../../api/slotMessagesQuery'
import { consumeUpdateRestartLatch } from './bundleReload'
import { refreshServerStateAfterReconnect } from './serverState'
import type { SocketConnection } from './connection'
import type { StreamBuffers } from './streamBuffers'

export interface CatchUpContext {
  dispatch: AppDispatch
  queryClient: QueryClient
  socket: SocketConnection
  buffers: StreamBuffers
  seedAutomations: () => void
  syncPendingApprovals: () => Promise<void>
  syncPendingQuestions: () => Promise<void>
  syncWorkflowRuns: () => Promise<void>
}

/** The first successful open of this hook: register as connected and take
 *  the boot snapshots the socket is authoritative for. */
export function runFirstConnect(ws: WebSocket, ctx: CatchUpContext): void {
  const { dispatch, socket } = ctx
  socket.wasConnectedRef.current = true
  dispatch(sseConnected())
  ctx.seedAutomations()
  // FIRST connect only: App's mount effect already dispatched fetchSlots,
  // and the socket's open handler fires strictly after it, so repeating it
  // here is a redundant round-trip at the worst possible moment. The
  // reconnect catch-up still refetches — there it recovers state missed
  // while the socket was down. fetchNotifications is the opposite: THIS is its authoritative
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
  // Eagerly subscribe to subagent events on first connect too.
  dispatch(clearSubagentsForSnapshot())
  ws.send(JSON.stringify({ type: 'subscribe_subagents' }))
  // Flush a log subscription that was requested before the socket opened.
  // subscribeLogs() stores the callback but returns early when readyState
  // is not OPEN, so a page mounting during the handshake (a cold load of
  // /logs) would otherwise never send subscribe_logs and would show no
  // lines at all until an unrelated reconnect. The reconnect path does
  // this too; the two paths must stay symmetric.
  if (socket.logCbRef.current) ws.send(JSON.stringify({ type: 'subscribe_logs' }))
  // Announce the restored active slot so a resumable session prefetches
  // while the user reads its transcript (resume prefetch).
  ws.send(
    JSON.stringify({ type: 'slot_focused', slot: store.getState().chat.activeSlot || null })
  )
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
  // Suppress markSlotUnread during the post-reconnect catch-up burst.
  // Assumption: the WS replay backlog flushes faster than the fetchSlots
  // HTTP round-trip resolves (gateway pushes buffered events in ms; HTTP
  // response takes tens of ms). If a very large backlog outlasts the
  // round-trip, late catch-up events could still mark slots unread — an
  // acceptable edge case vs. the common-case fix. A server-sent "replay
  // done" marker would make this deterministic but requires gateway changes.
  // Deliberate tradeoff: genuine unreads arriving mid-window are also
  // suppressed (false-negative-over-false-positive for the "just reconnected,
  // user is looking at the screen" scenario).
  socket.reconnectingRef.current = true
  buffers.dropForReconnect()
  dispatch(sseConnected())
  dispatch(fetchSlots()).finally(() => { socket.reconnectingRef.current = false })
  refreshServerStateAfterReconnect(queryClient)
  ctx.seedAutomations()
  dispatch(fetchNotifications()).then(() => ctx.syncPendingApprovals())
  ctx.syncPendingQuestions()
  // Same one-shot problem, different stream: a run that ENDED while the
  // socket was down pushed a terminal `workflow_run_event` nobody received,
  // and a run that STARTED while it was down has no row at all.
  ctx.syncWorkflowRuns()
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
  ws.send(JSON.stringify({ type: 'subscribe_subagents' }))
  if (socket.logCbRef.current) ws.send(JSON.stringify({ type: 'subscribe_logs' }))
  // Re-announce focus: the server lost this socket's focus state with
  // the old connection, and the store subscription only fires on change.
  ws.send(JSON.stringify({ type: 'slot_focused', slot: active || null }))
}
