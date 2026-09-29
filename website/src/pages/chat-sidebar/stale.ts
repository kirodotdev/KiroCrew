/** Dormant-session collapse: the persisted threshold, per-container expansion, the
 *  exemption for rows that just moved, and the bridge that pre-expands when a narrow
 *  clears. */
import { useState, useCallback, useEffect, useRef, type Dispatch, type SetStateAction } from 'react'
import { safeSetItem } from '../../utils/safeStorage'
import { STALE_COLLAPSE_TICK_MS, splitStaleSlots } from '../staleCollapse'
import { readStoredStaleCollapse, STALE_COLLAPSE_LS_KEY } from './persistence'
import type { Slot } from './types'
import { isPeerRow, localSlotFolder } from './rowIdentity'
import { lastActivityEpoch, type SortKey } from '../chat/sessionOrder'

/** The dormant-collapse threshold, expanded containers, move exemptions and exempt rows. */
export function useStaleCollapse({ pinned, activeSlot, runningSet, subagentCounts, unreadSet }: {
  pinned: Set<string>
  activeSlot: string | null
  runningSet: Set<string>
  subagentCounts: Record<string, number>
  unreadSet: Set<string>
}) {
  // ── Stale-session collapse ─────────────────────────────────────────────────
  // Sessions idle past the threshold collapse behind a per-container
  // "N dormant sessions hidden" expander row, independently at every tree level (each
  // folder body + the ungrouped root). Pinned, focused, running and
  // needs-input sessions are exempt: collapsing de-noises settled work, it is
  // never a place where live or deliberately-kept rows can disappear.
  const [staleCollapseMs, setStaleCollapseMsState] = useState(readStoredStaleCollapse)
  const setStaleCollapseMs = useCallback((ms: number) => {
    setStaleCollapseMsState(ms)
    safeSetItem(STALE_COLLAPSE_LS_KEY, String(ms))
    // Off also stops the heartbeat (the map's only GC), so drop the move
    // exemptions now rather than letting them accumulate unpruned. Read-time
    // expiry keeps them harmless meanwhile; this is hygiene, not correctness.
    if (ms <= 0) setStaleRecentlyMoved(prev => (prev.size ? new Map() : prev))
  }, [])
  // Manually-expanded containers ('root' or a folder id). Deliberately NOT
  // persisted: expanding is a "let me peek" gesture, and the collapse is the
  // steady state the user chose via the threshold — a reload restores it.
  const [staleExpanded, setStaleExpanded] = useState<Set<string>>(new Set())
  // Rows the user just MOVED between containers, keyed to WHEN they moved.
  // Exempt from collapsing so a drag or menu move never lands its row behind
  // a closed expander (which reads as data loss). Timestamped so the
  // heartbeat prunes only entries a full interval old — a bare clear could
  // strip a move made milliseconds before the tick fired.
  const [staleRecentlyMoved, setStaleRecentlyMoved] = useState<ReadonlyMap<string, number>>(new Map())
  // Slow heartbeat so rows age INTO the collapsed set while the tab stays
  // open. Staleness moves on a scale of days, so ten minutes is plenty.
  const [, setStaleCollapseTick] = useState(0)
  useEffect(() => {
    if (staleCollapseMs <= 0) return
    const id = setInterval(() => {
      setStaleCollapseTick(t => t + 1)
      setStaleRecentlyMoved(prev => {
        if (prev.size === 0) return prev
        const cutoff = Date.now() - STALE_COLLAPSE_TICK_MS
        const kept = new Map([...prev].filter(([, at]) => at > cutoff))
        return kept.size === prev.size ? prev : kept
      })
    }, STALE_COLLAPSE_TICK_MS)
    return () => clearInterval(id)
  }, [staleCollapseMs])
  // Exempt everything live or owed to the user: pinned, focused, running
  // (incl. workflows/goal loops), live or queued subagents, an approval gate,
  // an unanswered question, unread output — and a row the user JUST moved,
  // which must stay visible at its destination whatever its age. The collapse
  // de-noises settled work; a row that needs the user is not settled.
  // Memoized (not just for render cost): the reveal-in-sidebar effect (./reveal)
  // consults it to decide whether the target row needs its dormant section
  // pre-expanded, so it must be a listable effect dependency.
  const isStaleExempt = useCallback((s: Slot): boolean => {
    // A peer row has no local pin, focus, unread or subagent state to exempt it
    // — every clause below is a lookup in a LOCAL map keyed by slot key, and a
    // peer key can collide with a local one. Its own `running` flag, read off
    // the proxied payload, is the only signal that travels with it.
    if (isPeerRow(s)) return s.running === true
    return pinned.has(s.key) || s.key === activeSlot || runningSet.has(s.key)
      || (subagentCounts[s.key] ?? 0) > 0 || !!s.pending_approval
      || !!s.needs_input || unreadSet.has(s.key)
      // Read-time expiry: an entry only counts while younger than one heartbeat
      // interval, so correctness never depends on the prune timer having fired
      // (the timer is gated on the feature being on; the writer is not).
      || (staleRecentlyMoved.get(s.key) ?? 0) > Date.now() - STALE_COLLAPSE_TICK_MS
  },
  [pinned, activeSlot, runningSet, subagentCounts, unreadSet, staleRecentlyMoved])
  return {
    staleCollapseMs, setStaleCollapseMs, staleExpanded, setStaleExpanded, setStaleRecentlyMoved,
    isStaleExempt,
  }
}

/** Exempts a session that changed container from the dormant collapse. */
export function useStaleMoveWatcher({ foldersLoaded, localSlots, slotFolders, setStaleRecentlyMoved }: {
  foldersLoaded: boolean
  localSlots: Slot[]
  slotFolders: Record<string, string>
  setStaleRecentlyMoved: Dispatch<SetStateAction<ReadonlyMap<string, number>>>
}) {
  // Watch for sessions changing container and exempt them from the stale
  // collapse until they age out (see the timestamped prune on the heartbeat).
  // Derived from the store rather than wrapped around a move call site, so
  // EVERY path that moves a session — drag, the row menu, the chat-header
  // menu, and the move-undo bar — gets the exemption, including moves
  // initiated outside this component. Gated on `foldersLoaded`: until folder
  // data has arrived every filed slot maps to undefined, and treating that
  // hydration as movement would exempt the whole tree on a cold load.
  const prevSlotFoldersRef = useRef<Map<string, string | undefined> | null>(null)
  useEffect(() => {
    if (!foldersLoaded) return
    const prev = prevSlotFoldersRef.current
    const next = new Map<string, string | undefined>()
    for (const s of localSlots) next.set(s.key, slotFolders[s.key])
    prevSlotFoldersRef.current = next
    if (!prev) return
    const moved: string[] = []
    for (const [key, fid] of next) {
      if (prev.has(key) && prev.get(key) !== fid) moved.push(key)
    }
    if (moved.length) {
      const now = Date.now()
      setStaleRecentlyMoved(prevMap => {
        const merged = new Map(prevMap)
        for (const key of moved) merged.set(key, now)
        return merged
      })
    }
  }, [localSlots, slotFolders, foldersLoaded, setStaleRecentlyMoved])
}

/** Pre-expands dormant sections when a narrowed list clears. */
export function useStaleNarrowBridge({ listNarrowed, filteredSlots, staleCollapseMs, sortKey, isStaleExempt, setStaleExpanded, slotFolders }: {
  listNarrowed: boolean
  filteredSlots: Slot[]
  staleCollapseMs: number
  sortKey: SortKey
  isStaleExempt: (s: Slot) => boolean
  setStaleExpanded: Dispatch<SetStateAction<Set<string>>>
  slotFolders: Record<string, string>
}) {
  // Bridge a clearing narrow for the stale collapse: while narrowed the
  // collapse is inert, so a 10-day-old search match renders as an ordinary
  // row. Clearing the search must not swallow the row the user was just
  // reading behind an expander they have never seen — so when the narrow
  // ends, pre-expand every container whose narrowed-visible rows would now
  // collapse. Captured in an EFFECT (committed renders only — a ref written
  // during render could hold a speculative list an abandoned render never
  // showed), consumed on the committed narrowed→clear transition. Effect
  // order matters and matches declaration order: the capture effect sees
  // `listNarrowed === false` on the clearing commit and leaves the ref for
  // the consumer below. Pre-expanding a container the narrow never scrolled
  // into view is accepted: an expanded section inside a collapsed folder is
  // invisible, and over-expansion never hides anything.
  const staleNarrowBridgeRef = useRef<Slot[] | null>(null)
  useEffect(() => {
    if (listNarrowed) staleNarrowBridgeRef.current = filteredSlots
  }, [listNarrowed, filteredSlots])
  useEffect(() => {
    if (listNarrowed) return
    const shown = staleNarrowBridgeRef.current
    // Consumed (and discarded) on the clearing transition even when the
    // bridge cannot act — under a non-date sort or with the feature off the
    // collapse is inert anyway, and holding the capture for a LATER sort
    // switch would mean expanding containers from an arbitrarily old list.
    staleNarrowBridgeRef.current = null
    if (!shown?.length || staleCollapseMs <= 0 || sortKey !== 'date-desc') return
    const { stale } = splitStaleSlots(
      shown, staleCollapseMs, Date.now(),
      s => lastActivityEpoch(s) * 1000, isStaleExempt,
    )
    if (!stale.length) return
    setStaleExpanded(prev => {
      const next = new Set(prev)
      for (const s of stale) next.add(localSlotFolder(s, slotFolders) || 'root')
      return next
    })
    // Deliberately keyed on the narrowed→clear transition alone: the bridge
    // must fire exactly when the narrow ends, not whenever the collapse
    // inputs it reads happen to change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [listNarrowed])
}
