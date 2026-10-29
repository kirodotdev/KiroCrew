import { useRef, useCallback, useEffect } from 'react'
import { useAppSelector, type AppDispatch, type RootState } from '../store'
import { selectSlotRunEpoch, requestStop, syncSlotRunningFromServer } from '../store/chatSlice'
import { useStopPress, type StopOutcome } from '../chat-core/stop/useStopPress'

/**
 * `ChatPane`'s hold on the running turn. `useChatPaneStop` is the Stop press
 * protocol for the pane's slot: the cooperative cancel, then the hard kill, the
 * optimistic `soft_pending` until the server speaks, and the in-pane failure
 * notice. `useChatPaneRunSync` settles the slot idle on an OBSERVED running
 * true->false transition of the server snapshot, for a turn whose end this tab
 * missed. Both reset when the pane is re-pointed at another slot.
 */

type PaneSlot = RootState['dashboard']['slots'][number] | undefined

export function useChatPaneStop({ slotKey, busy, paneSlot, dispatch }: {
  slotKey: string
  busy: boolean
  paneSlot: PaneSlot
  dispatch: AppDispatch
}) {
  // The press protocol itself is shared with ChatEmbed
  // (chat-core/stop/useStopPress, #9547 and GPT rounds 8-9): soft first, an
  // optimistic `soft_pending` over the WS lag, a lost press keeps the pending
  // state so the retry is a deliberate force, a reply for a slot the pane no
  // longer shows is dropped. The pane sends through the Redux `requestStop`
  // thunk and uses neither of the embed's turn or decline extensions.
  const sendStop = useCallback(async (force: boolean): Promise<StopOutcome> => {
    const res = await dispatch(requestStop({ slotId: slotKey, force }))
    const failure = requestStop.fulfilled.match(res) ? res.payload : null
    if (!failure) return { kind: 'ok' }
    // A transport failure carries only the browser's own phrase ("Failed to
    // fetch", "NetworkError…", "Load failed"): it is ambiguous, so it is `lost`.
    return /fetch|network|load failed/i.test(failure.error)
      ? { kind: 'lost' }
      : { kind: 'refused', ...failure }
  }, [dispatch, slotKey])
  const { stopState: paneStopState, onStop, stopError, stopErrorDetail, dismissStopError } =
    useStopPress({ slotKey, busy, serverStopState: paneSlot?.stop_state, sendStop })
  return { stopError, stopErrorDetail, dismissStopError, paneStopState, onStop }
}

export function useChatPaneRunSync({ slotKey, paneSlot, dispatch }: {
  slotKey: string
  paneSlot: PaneSlot
  dispatch: AppDispatch
}) {
  // Reconcile this pane's run state from the server's slot snapshot, the way
  // ChatPage does for the active slot. The reducer only takes the idle
  // direction for a background slot; the running direction stays with the
  // live frames.
  //
  // Only on an OBSERVED running true->false transition, never on the value a
  // snapshot happens to hold when the pane mounts: a pane opened mid-turn can
  // hold a snapshot fetched before the turn started (`running: false`) while
  // live frames already mark it busy, and settling on that would idle the
  // composer and finalize an in-flight reply until the next chunk re-promotes
  // it. The `/stop`-reply settlement covers the stuck-pane press; this path
  // exists for the turn that ends while the tab misses its `_done`, and that
  // end is a transition this pane sees.
  const hasPaneSlot = !!paneSlot
  const paneRunning = !!paneSlot?.running
  const paneStopping = !!paneSlot?.stopping
  const paneRunEpoch = useAppSelector((s) => selectSlotRunEpoch(s, slotKey))
  // The observation and the slot it was made on. Reset together, inside this
  // effect, the moment `slotKey` changes: a pane re-pointed A -> B -> A must
  // not carry A's old observation back (it would settle A on a stale snapshot
  // while A's live frames mark it busy), and a separate reset effect would
  // run AFTER this one on mount and erase a fresh observation instead.
  const observedSlotRef = useRef<string | null>(null)
  const sawRunningRef = useRef(false)
  // WHICH turn was observed running: the slot's `runEpoch` as of the last
  // render in which the snapshot said `running`. The idle snapshot answers
  // about that turn only. A newer turn's first live frame bumps the epoch —
  // and it can land in the same render as the lagging idle snapshot, so the
  // epoch current at settle time may already be the new turn's. The reducer
  // compares against the observed one and drops a settlement that would idle
  // (and split the streaming reply of) a turn this effect never saw.
  const observedEpochRef = useRef(0)
  useEffect(() => {
    if (observedSlotRef.current !== slotKey) {
      observedSlotRef.current = slotKey
      sawRunningRef.current = false
      observedEpochRef.current = 0
    }
    if (!hasPaneSlot) return
    if (paneRunning) { sawRunningRef.current = true; observedEpochRef.current = paneRunEpoch; return }
    if (!sawRunningRef.current) return
    sawRunningRef.current = false
    dispatch(syncSlotRunningFromServer({ slot: slotKey, running: false, stopping: paneStopping, epoch: observedEpochRef.current }))
  }, [dispatch, slotKey, hasPaneSlot, paneRunning, paneStopping, paneRunEpoch])
}
