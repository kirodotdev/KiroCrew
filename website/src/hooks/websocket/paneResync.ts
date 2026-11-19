/** Re-reading the transcripts on screen when the live stream may have lost
 *  frames that no reconnect will replay.
 *
 *  Three moments, each one a sign the tab can be behind the gateway while its
 *  socket still reads OPEN:
 *
 *  - The page comes back after being hidden (`visibilitychange`), or is
 *    restored from the back-forward cache (`pageshow` with `persisted`). A
 *    phone that locks or backgrounds the browser freezes the page, and the
 *    socket it held can be dead without an `onclose`. The silence watchdog
 *    replaces such a socket only after its window (20s or more of visible
 *    silence), so for that long the pane showed the rows it held when the page
 *    froze: the user's message with no reply under it. An HTTP read does not
 *    wait on the socket, so the reply lands one round trip after the return.
 *  - A `slots` row says a pane's slot is idle and not interrupted, while the
 *    pane's transcript still ends on the user's row. The gateway's reading is
 *    the server transcript's, so the pane missed the rows that answered it.
 *  - A `slots` row says a slot is idle while its live status still names work
 *    in progress. `_done` resets that status; a lost `_done` left it saying
 *    "Thinking…" over a finished turn.
 *
 *  Every read here goes through `refreshSlot` (the active slot) or
 *  `warmSlotCache` (any other pane on screen), the same reads the reconnect
 *  catch-up runs, so a read that finds nothing new changes nothing. */
import type { QueryClient } from '@tanstack/react-query'
import { autoBatched } from '../../lib/streamBatch'
import { drainPendingChunks } from '../../lib/pendingChunkDrain'
import { store, type AppDispatch } from '../../store'
import {
  refreshSlot,
  selectPendingTurn,
  selectTurnInterrupted,
  setSlotStatusDetail,
  warmSlotCache,
} from '../../store/chatSlice'
import { observedPaneSlots } from '../../api/slotMessagesQuery'
import type { ChatSlot } from '../../types'

/** A hide shorter than this re-reads nothing on the return: the gateway's
 *  status frame comes every 5s, so a shorter hide cannot have outlived a frame
 *  the silence watchdog would have missed. */
export const RETURN_RESYNC_MIN_HIDDEN_MS = 5_000

/** Re-read `slot`: the active slot's transcript or another pane's cache.
 *  Resolves true when the page was applied, false when the read failed or was
 *  declined (the slot switched, or a live frame raced a long walk). The
 *  active slot takes the plain refresh the reconnect catch-up takes: an
 *  `onlyIfUnchanged` read would also be dropped when older history pages in
 *  meanwhile, which says nothing about whether the socket delivers. */
async function reRead(dispatch: AppDispatch, slot: string): Promise<boolean> {
  // Land chunks held while hidden before a finished page can replace them.
  drainPendingChunks()
  if (slot === store.getState().chat.activeSlot) {
    const result = await dispatch(refreshSlot(slot))
    return refreshSlot.fulfilled.match(result) && result.payload !== null
  }
  const result = await dispatch(warmSlotCache(slot))
  return warmSlotCache.fulfilled.match(result) && result.payload !== null
}

/** Re-read every transcript on screen: the active slot and each mounted pane. */
export function resyncOnScreenPanes(dispatch: AppDispatch, queryClient: QueryClient): void {
  const active = store.getState().chat.activeSlot
  const slots = new Set<string>(observedPaneSlots(queryClient))
  if (active) slots.add(active)
  for (const slot of slots) void reRead(dispatch, slot)
}

/** Listen for the page coming back and re-read what is on screen then.
 *  Returns the detach. `now` is injectable for tests. */
export function attachReturnResync(
  dispatch: AppDispatch,
  queryClient: QueryClient,
  now: () => number = Date.now,
): () => void {
  let hiddenAt = document.hidden ? now() : 0
  const onVisibility = (): void => {
    if (document.hidden) {
      hiddenAt = now()
      return
    }
    const since = hiddenAt
    hiddenAt = 0
    if (since && now() - since >= RETURN_RESYNC_MIN_HIDDEN_MS) resyncOnScreenPanes(dispatch, queryClient)
  }
  const onPageShow = (event: PageTransitionEvent): void => {
    if (!event.persisted) return
    hiddenAt = 0
    resyncOnScreenPanes(dispatch, queryClient)
  }
  document.addEventListener('visibilitychange', onVisibility)
  window.addEventListener('pageshow', onPageShow)
  return () => {
    document.removeEventListener('visibilitychange', onVisibility)
    window.removeEventListener('pageshow', onPageShow)
  }
}

/** Per slot, the row reading a behind-the-server re-read was sent for, so a
 *  re-read that still leaves the two readings apart is not repeated on every
 *  frame of the same row. */
const behindReads = new Map<string, string>()

/** A new connection: its rows may need the re-reads again. */
export function clearBehindReads(): void {
  behindReads.clear()
}

/** What a row says about its slot's newest turn, for `behindReads`. */
function rowReading(row: ChatSlot): string {
  return JSON.stringify([row.turn_gen ?? null, row.turn ?? null, row.messages ?? null, row.last_message ?? null])
}

/**
 * Re-read each pane in `panes` whose transcript ends on an interruption the
 * gateway's idle row does not report (`interrupted: false`). Panes in
 * `alreadyRead` were re-read by this same frame and are skipped. A pane with a
 * send of its own still in flight is skipped too: its user row is newer than
 * the row's reading. Returns the slots re-read.
 */
export function reReadPanesBehindServer(
  dispatch: AppDispatch,
  panes: readonly string[],
  rows: readonly ChatSlot[],
  alreadyRead: ReadonlySet<string>,
): string[] {
  const state = store.getState()
  const sent: string[] = []
  for (const slot of panes) {
    if (alreadyRead.has(slot)) continue
    const row = rows.find(r => r.key === slot)
    if (!row || row.running !== false || row.interrupted !== false) continue
    if (selectPendingTurn(state, slot) || !selectTurnInterrupted(state, slot)) continue
    const reading = rowReading(row)
    if (behindReads.get(slot) === reading) continue
    behindReads.set(slot, reading)
    // A read that failed or was declined applied nothing: drop its record so
    // the next frame naming the same row tries again.
    void reRead(dispatch, slot).then((applied) => {
      if (!applied && behindReads.get(slot) === reading) behindReads.delete(slot)
    })
    sent.push(slot)
  }
  return sent
}

/**
 * For each idle row, reset the slot's live status as that turn's `_done` would,
 * when it still names work in progress. A row is built from the slot's state
 * when it is sent, so an idle row means no turn runs there now; a status left
 * at "Thinking…" or a tool name is one whose `_done` never reached this tab.
 * A `_done` that does arrive later sets the same value again.
 */
export function settleStatusOfIdleRows(dispatch: AppDispatch, rows: readonly ChatSlot[]): void {
  const state = store.getState()
  const details = state.chat.slotStatusDetail
  for (const row of rows) {
    if (row.running !== false) continue
    const detail = details[row.key]
    if (!detail || detail.kind === 'idle') continue
    // This tab's own send is still waiting for its turn: a row built before
    // the gateway started that turn is older than the status it would clear.
    if (selectPendingTurn(state, row.key)) continue
    dispatch(autoBatched(setSlotStatusDetail({ slot: row.key, kind: 'idle' })))
  }
}
