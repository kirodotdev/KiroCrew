/** Row-delivery watchdog: re-hydrates the active slot when a turn this client
 *  believes is running stops delivering rows over a socket that is still open,
 *  and escalates to a reconnect when the re-read proves rows were missed. */
import { useEffect, useRef } from 'react'
import { useAppStore, type AppDispatch } from '../../store'
import { hasUnidentifiedDurableRow, refreshSlot } from '../../store/chatSlice'

/** Exported so the threshold is asserted rather than guessed at in the spec. */
export const ROW_STALL_MS = 100_000
export const ROW_STALL_TICK_MS = 15_000

export function useRowDeliveryWatchdog(dispatch: AppDispatch, forceReconnect: () => void): void {
  // Reads the PROVIDER's store, so a host (or a test) that mounts its own store
  // is the one watched.
  const appStore = useAppStore()
  // The escalation target, taken through a ref so a new `forceReconnect`
  // identity does not restart the interval and reset its progress stamp.
  const forceReconnectRef = useRef(forceReconnect)
  forceReconnectRef.current = forceReconnect
  /* Row-delivery watchdog.
   *
   * A dropped socket already has two owners: the reconnect handler
   * re-hydrates the active slot, and `useDashboardHealthProbe` polls
   * /api/status while `dashboard.connected === false`. A socket that stays
   * OPEN but goes wholly silent has a third: the facade's silence check, which
   * replaces it once the gateway's 5s `dashboard` frame stops. None covers the
   * case this guards. The socket stays OPEN and keeps delivering status frames
   * -- so nothing reconnects, the probe never runs and the silence check sees
   * a live socket -- while chat frames for the active slot stop arriving. The
   * transcript then freezes mid-turn with no client-visible sign, and
   * `slotRunning`, set at send and cleared only by a `_done`/error frame, stays
   * true: the composer keeps offering Stop for a turn whose rows have stopped
   * updating, and the only way back is a manual reload -- which works only
   * because it re-fetches over HTTP instead of trusting the live channel.
   *
   * This is that re-fetch, moved inside the running app. While the active slot
   * believes it is running and neither the row count nor the tail row's length
   * has moved for ROW_STALL_MS, dispatch the same `refreshSlot` the reconnect
   * path uses. It is idempotent (a count-matched server page replaces
   * `messages`) and self-healing: `refreshSlot.fulfilled` writes `slotRunning`
   * from the server's own `running`, so a turn that ended while its rows were
   * stranded stops claiming to be running.
   *
   * ROW_STALL_MS sits above the longest legitimate silence inside a working
   * turn (about 100s between tool rows on a slow agent). A false positive costs
   * one GET and one no-op merge -- never a lost row. The steady state costs
   * nothing: a tick reads app state and issues no request unless a slot this
   * client believes is running has gone ROW_STALL_MS without progress.
   *
   * Scope, deliberately: this watches the slot THIS client believes is running.
   * A client whose belief is wrong -- a turn started on another device whose run
   * frame was lost, so the slot looks idle here -- is NOT covered: nothing
   * re-checks a slot this client believes idle. Covering it from here would
   * cost a steady slots GET per visible tab to guard a case no report has
   * produced.
   */
  useEffect(() => {
    let slotKey: string | null = null
    let rows = -1
    let tail = -1
    let seq = -1
    let stampedAt = Date.now()
    const id = setInterval(() => {
      const chat = appStore.getState().chat
      const msgs = chat.messages
      const last = msgs[msgs.length - 1]
      const lastLen = last ? (last.rawText ?? last.content ?? '').length : 0
      const liveSeq = chat.liveFrameSeq ?? 0
      // Any change to the row count, the tail row's text, or the active-slot
      // live-frame counter is progress -- including a chunk reduced into the
      // streaming row that sits ABOVE a queued bubble, which the row count and
      // the tail length both miss (a queued/user row is pushed last, so the
      // growing streaming row is no longer the tail). `liveFrameSeq` is bumped
      // by `countLiveFrame` on every active-slot live frame, the same signal
      // `refreshSlot` already reads for its stale-page guard.
      if (chat.activeSlot !== slotKey || msgs.length !== rows || lastLen !== tail || liveSeq !== seq) {
        slotKey = chat.activeSlot
        rows = msgs.length
        tail = lastLen
        seq = liveSeq
        stampedAt = Date.now()
        return
      }
      const believesRunning = chat.slotRunning || chat.slotState !== 'idle'
      if (!chat.activeSlot || !believesRunning) {
        stampedAt = Date.now()
        return
      }
      if (Date.now() - stampedAt < ROW_STALL_MS) return
      stampedAt = Date.now()
      const slot = chat.activeSlot
      /* What this client held when the stall was declared. A returned page that
       * carries a durable row outside this set is proof the socket missed
       * deliveries rather than the turn being slow: the server produced rows
       * while the transcript sat still, over a socket that never closed.
       * `meta.mid` identifies the server's own rows; client-only rows have none
       * and prove nothing. */
      const held = new Set<string>()
      for (const row of msgs) {
        // `meta.mid` is typed `unknown` on the row, so the string test is what
        // makes it a usable set key -- and a row whose mid is not a string
        // proves nothing about what the socket delivered.
        const mid = row?.meta?.mid
        if (typeof mid === 'string' && mid) held.add(mid)
      }
      /* A row the client already displays WITHOUT a server mid makes the
       * missed-row proof below untrustworthy: a drained queue entry is rebuilt
       * client-side as `{ role: 'user', ... }` (`queue.ts`) whose meta carries
       * no `mid`, is never echoed as `chat_message`, and -- a tail drain going
       * through the next-queued-turn path -- is never refreshed, while the
       * server's own copy of that row carries a mid the client never held AND a
       * different (drain-time vs enqueue-time) `ts`. So neither a mid nor a
       * `ts` match can recognise it, and it would read as a missed delivery on
       * every stall. When the view holds such a row the escalation is gated off
       * (the cheap refresh still runs); this mirrors `slotRefresh`'s own
       * `hasUnidentifiedDurableRow` span-trust check. */
      const proofTrustworthy = !hasUnidentifiedDurableRow(msgs)
      void dispatch(refreshSlot({ key: slot, onlyIfUnchanged: true }))
        .then((result) => {
          const current = appStore.getState().chat
          if (refreshSlot.rejected.match(result)) {
            // eslint-disable-next-line no-console -- only trace of a failed recovery GET; the next stall tick retries
            console.warn('Transcript recovery failed; retrying after the next stall window', result.error)
            return
          }
          if (!refreshSlot.fulfilled.match(result) || current.activeSlot !== slot ||
              current.lastRecoveryRequestId !== result.meta.requestId) return
          const page = result.payload
          /* `null` here also means the stale-page guard discarded the fetch
           * (rows arrived while it was in flight) -- with no page there is no
           * missed-row proof, so the reconnect teardown is skipped too. */
          const rows = page?.messages ?? []
          const missed = proofTrustworthy && rows.some((row) => {
            const mid = row?.meta?.mid
            return typeof mid === 'string' && !!mid && !held.has(mid)
          })
          if (!missed) return
          /* Escalate to a reconnect: its catch-up re-reads every frame family
           * this socket carries (slots, notifications, approvals, questions,
           * workflow runs, artifacts, member threads), which is what the frozen
           * sidebar and badges need -- this slot's rows were only the visible
           * symptom. Gated on the proof above, so a turn that is merely slow
           * -- the likelier reading of 100s of silence -- never pays for a
           * socket teardown, which discards buffered partial chunks. */
          forceReconnectRef.current()
        })
        .catch((error: unknown) => {
          // eslint-disable-next-line no-console -- only trace of a failed escalation; the transcript stays stalled until a reload
          console.error('Transcript recovery could not reconnect; reload if the transcript remains stalled', error)
        })
    }, ROW_STALL_TICK_MS)
    return () => clearInterval(id)
  }, [dispatch, appStore])
}
