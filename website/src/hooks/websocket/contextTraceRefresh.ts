/** When the Context tab re-reads its trace. Nothing polls it.
 *
 *  The tab reads `GET /api/telemetry/context-trace` once on mount. After that it
 *  re-reads only when the gateway says the trace moved: a `session_projection`
 *  frame for the `usage` fold, which the gateway pushes when a turn's context is
 *  composed or a turn closes.
 *
 *  THE FRAME IS A SIGNAL, NOT THE VALUE. It carries ONE crew-log unit's usage fold,
 *  while the trace joins every unit the slot ran under and cuts it to a day window.
 *  Applying the frame would drop older units' turns, so it prompts one re-read.
 *
 *  THREE RULES keep the reads few and ordered:
 *  - A frame at or below the newest revision this tab saw for its unit is a
 *    duplicate delivery and is dropped.
 *  - Frames for one slot inside `COALESCE_MS` become one read.
 *  - A read in flight is never cancelled or doubled. A frame that lands during it
 *    owes exactly one more read once it settles, so the newer read always lands
 *    last and a late response can never overwrite a newer one. */
import type { QueryClient } from '@tanstack/react-query'

import { slotKey } from '../../pages/chat/command-center/model'
import type { SessionProjectionFrame } from './sessionProjection'

/** The window frames for one slot are merged in. The gateway sends session frames
 *  on the same window (`COALESCE_SECONDS` in `dashboard/handlers/crew_log.py`), so
 *  a turn's burst from several units folds into one read here. */
export const COALESCE_MS = 250

/** The Context tab's query key for *slot*. One spelling, shared with the reader.
 *
 *  In the dashboard's own key form: the tab holds the bare slot, while a frame
 *  names it scope-qualified (`dashboard:<slot>`). */
export function contextTraceKey(slot: string): readonly unknown[] {
  return ['context-trace', slotKey(slot)]
}

/** The newest usage revision seen, per crew-log unit. A revision is minted per
 *  gateway process, so the map is cleared whenever a socket opens. */
const seen = new Map<string, number>()
/** Slots with a coalesce timer armed. */
const armed = new Map<string, ReturnType<typeof setTimeout>>()
/** Slots that already owe one read after the read in flight. */
const owed = new Set<string>()

/** Forget every revision and pending read. Called when a socket opens. */
export function resetContextTraceRefresh(): void {
  seen.clear()
  for (const timer of armed.values()) clearTimeout(timer)
  armed.clear()
  owed.clear()
}

export type UsageFrameOutcome = 'requested' | 'stale' | 'ignored'

/** Take one `session_projection` frame; whether it asked the tab to re-read. */
export function onUsageFrame(queryClient: QueryClient, frame: SessionProjectionFrame): UsageFrameOutcome {
  if (frame.name !== 'usage') return 'ignored'
  const held = seen.get(frame.unit)
  if (held !== undefined && frame.revision <= held) return 'stale'
  seen.set(frame.unit, frame.revision)
  requestContextTraceRead(queryClient, frame.slot)
  return 'requested'
}

/** Ask for one re-read of *slot*'s trace, merged with any asked for within
 *  `COALESCE_MS`. */
export function requestContextTraceRead(queryClient: QueryClient, slot: string): void {
  const own = slotKey(slot)
  if (armed.has(own)) return
  armed.set(own, setTimeout(() => {
    armed.delete(own)
    readNow(queryClient, own)
  }, COALESCE_MS))
}

/** Re-read every trace this tab holds, now. Called on a reconnect, which is also
 *  how a gateway restart reaches the tab: the trace may have moved while no frame
 *  could arrive. */
export function rereadAllContextTraces(queryClient: QueryClient): void {
  for (const query of queryClient.getQueryCache().findAll({ queryKey: ['context-trace'] })) {
    const [, slot] = query.queryKey as unknown[]
    if (typeof slot === 'string' && slot) readNow(queryClient, slot)
  }
}

function readNow(queryClient: QueryClient, slot: string): void {
  const key = contextTraceKey(slot)
  const query = queryClient.getQueryCache().find({ queryKey: key, exact: true })
  // No tab has read this slot: it reads on mount, so there is nothing to refresh.
  if (!query) return
  if (query.state.fetchStatus === 'fetching' && query.promise) {
    if (owed.has(slot)) return
    owed.add(slot)
    void query.promise
      .catch(() => undefined)
      .finally(() => {
        owed.delete(slot)
        void queryClient.invalidateQueries({ queryKey: key, exact: true }, { cancelRefetch: false })
      })
    return
  }
  void queryClient.invalidateQueries({ queryKey: key, exact: true }, { cancelRefetch: false })
}
