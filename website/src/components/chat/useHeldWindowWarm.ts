/** Re-reads a background slot's transcript once when a pane mounts onto a window
 *  the store already holds.
 *
 *  The pane's own hydrate returns early on a held window (`hydrateSlotMessages`),
 *  so the pane resumes from the rows it had when it was last on screen. Those rows
 *  can be behind the server: a slot that was off screen while the socket was down
 *  missed every frame of that gap, and the reconnect warms only the panes that
 *  are mounted then. Its cached copy then ends in a frozen partial reply that only
 *  a page reload replaced. `warmSlotCache` is the merge that reconciles a held
 *  window with the server's page, so a mount onto a held window runs it once.
 *
 *  The pane then skips its own hydrate read, whose page the held window would
 *  discard, so the mount still costs one read. That read also set the pane's
 *  hydrated latch and drove its load-failure notice, so this hook reports the
 *  same two outcomes: `settled` once the warm's page has landed, `failed` when
 *  it was refused, and `retry` to run it again from that notice. */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useAppDispatch, useAppSelector } from '../../store'
import { warmSlotCache } from '../../store/chatSlice'

export interface HeldWindowWarm {
  /** This pane re-reads the slot by a warm instead of its own hydrate read. */
  warm: boolean
  /** The warm's page has landed in the store. */
  settled: boolean
  /** The warm's read failed. */
  failed: boolean
  retry: () => void
}

type WarmState = { slot: string; run: number; settled: boolean; failed: boolean }

export function useHeldWindowWarm(slotKey: string, paneIsActive: boolean): HeldWindowWarm {
  const dispatch = useAppDispatch()
  const held = useAppSelector((s) => s.chat.slotPaneHasMore?.[slotKey] !== undefined)
  // Decided when the pane first shows this slot. A hydrate that lands later
  // writes the marker too, and that page is already current. The active slot's
  // transcript is owned by `switchSlot` / `refreshSlot`.
  const mount = useRef<{ slot: string; warm: boolean; started: boolean } | null>(null)
  if (mount.current?.slot !== slotKey) mount.current = { slot: slotKey, warm: held && !paneIsActive, started: false }
  const warm = mount.current.warm
  const [state, setState] = useState<WarmState>({ slot: slotKey, run: 0, settled: false, failed: false })
  // Only the latest warm reports: one still in flight from an earlier visit to
  // this slot, or from a Retry clicked again, would otherwise settle or fail
  // the pane ahead of the newer read.
  const runs = useRef(0)

  const run = useCallback(() => {
    const slot = slotKey
    const id = ++runs.current
    setState({ slot, run: id, settled: false, failed: false })
    dispatch(warmSlotCache(slot)).then((action) => {
      const failed = warmSlotCache.rejected.match(action)
      setState((cur) => (cur.run === id ? { ...cur, settled: !failed, failed } : cur))
    })
  }, [slotKey, dispatch])

  useEffect(() => {
    const m = mount.current
    if (!m || m.slot !== slotKey || !m.warm || m.started) return
    m.started = true
    run()
  }, [slotKey, run])

  const current = state.slot === slotKey
  return { warm, settled: warm && current && state.settled, failed: warm && current && state.failed, retry: run }
}
