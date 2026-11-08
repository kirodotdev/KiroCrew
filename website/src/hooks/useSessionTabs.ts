import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { safeSetItem } from '../utils/safeStorage'
import { resolveSlotSuccession } from '../utils/slotSuccession'
import {
  closeSessionTab,
  loadPinnedSessionTabs,
  loadSessionTabs,
  nextActiveAfterClose,
  openSessionTab,
  pinnedFirst,
  pruneSessionTabs,
  replaceSessionTab,
  savePinnedSessionTabs,
  saveSessionTabs,
} from '../lib/sessionTabs'

export interface SessionTabsApi {
  /** Open sessions in strip order. Length < 2 means the strip does not render. */
  tabs: string[]
  /**
   * The pinned tabs (#22775), always at the front of `tabs`. Nothing replaces
   * a pinned tab's session: an activation that would have, opens beside it.
   */
  pinned: string[]
  /**
   * "Look at this tab" — the strip turns it into a brief outline. Carries the
   * time so a repeat on the same key still re-fires.
   *
   * TWO triggers, one mechanism, because the user's question is the same both
   * times ("where did my session go?"):
   *  - a plain sidebar click swapped a tab's session IN PLACE, which consumes a
   *    tab the user may have opened deliberately and is otherwise invisible: the
   *    position is unchanged and only the label differs;
   *  - a gesture asked to open a session that is ALREADY a tab, which changes
   *    nothing and — in background mode, which does not even switch — would
   *    otherwise produce no response at all, indistinguishable from a misfire
   *    during exactly the triage flow the gesture exists for.
   */
  cue: { key: string; at: number } | null
  /** Open `key` beside the active tab. The caller still activates it. */
  openInNewTab: (key: string) => void
  /** Close `key`; returns the session to activate, or null when none is left. */
  closeTab: (key: string) => string | null
  /**
   * Replace the order with `keys`. Ignored unless `keys` holds exactly the
   * open tabs: the strip reports an order computed from what it last
   * rendered, and a tab opened or closed since then must not be dropped or
   * resurrected by it. Activation is unchanged.
   */
  reorderTabs: (keys: string[]) => void
  /** Pin `key`, or unpin it if pinned. */
  togglePin: (key: string) => void
}

/** Stable identity so a disabled surface never re-renders on a new empty array. */
const EMPTY_TABS: string[] = []

/**
 * Owns the session-tab working set for one chat surface: restores it, keeps it
 * consistent with the active session and the live slot list, and persists it.
 *
 * The set is NOT in Redux. It is per-surface view state with a single consumer
 * tree (the chat page renders both the strip and the sidebar that feeds it), so
 * a slice would add a global mutation seam for something no other surface may
 * read — and `useSessionGrid`/`splitLayoutStore` already establish local state
 * plus a localStorage key as the pattern for chat-surface layout.
 *
 * INVARIANT: the active session is always in `tabs`. That is what makes the
 * feature invisible until it is used — a user who never opens a second tab has
 * a one-element set, and the strip renders nothing at all.
 *
 * `enabled=false` makes the whole hook INERT: no restore, no reconcile, no
 * write, and the mutators are no-ops. That is not an optimisation. ChatPage is
 * mounted by several EMBEDDED hosts too — a popped-out window, the artifact
 * companion panel, Papyrus's co-author panel, the app-SDK chat panel — and they
 * share one origin, therefore one `localStorage`. Left live, each of those
 * instances would reconcile the SAME key against its own active session and
 * overwrite the dashboard's working set with a session the dashboard never
 * opened (or, for an app-owned slot, one its sidebar cannot even show). One
 * predicate has to decide both who draws the strip and who owns the set, or the
 * two drift apart; see `ownsSessionTabs` in ChatPage.
 */
export function useSessionTabs(
  mode: string | undefined,
  activeSlot: string | null,
  /** Sessions living on this surface. Must be referentially stable (memoized). */
  liveSlots: readonly { key: string }[],
  /** Whether this surface owns the working set. False on every embedded host. */
  enabled: boolean,
): SessionTabsApi {
  const [tabs, setTabs] = useState<string[]>(() => (enabled ? loadSessionTabs(mode, activeSlot) : []))
  const [pinned, setPinned] = useState<string[]>(() => (enabled ? loadPinnedSessionTabs(mode) : []))
  const [cue, setCue] = useState<{ key: string; at: number } | null>(null)
  const tabsRef = useRef(tabs)
  tabsRef.current = tabs
  const pinnedRef = useRef(pinned)
  pinnedRef.current = pinned
  const activeRef = useRef(activeSlot)
  activeRef.current = activeSlot
  const enabledRef = useRef(enabled)
  enabledRef.current = enabled
  // The session the user was on BEFORE this change — the tab whose content a
  // plain sidebar click replaces. Seeded from the mount value so the first
  // switch of a visit replaces rather than appends.
  const prevActiveRef = useRef<string | null>(activeSlot)

  useEffect(() => { if (enabled) saveSessionTabs(mode, tabs, safeSetItem) }, [enabled, mode, tabs])
  useEffect(() => { if (enabled) savePinnedSessionTabs(mode, pinned, safeSetItem) }, [enabled, mode, pinned])

  // A pin outlives its tab only until the set settles: a tab pruned away (or a
  // stored set that restored without it) must not come back pinned later.
  // Skipped while the set is empty, which is a cold load before the first
  // activation, not a set that lost its tabs.
  useEffect(() => {
    if (!enabled || tabs.length === 0) return
    setPinned(prev => {
      const next = prev.filter(k => tabs.includes(k))
      return next.length === prev.length ? prev : next
    })
  }, [enabled, tabs])

  const liveKeys = useMemo(() => new Set(liveSlots.map(s => s.key)), [liveSlots])

  // A pinned tab handed to the session that succeeds it (a memory-mode switch,
  // below) stays provisional until the switch settles. The old session being
  // deleted is the commit. Anything that leaves the old session alive (the user
  // goes back to it, the switch fails or times out, the delete fails) gives the
  // old session its tab, in place, and its pin back. After a commit the record
  // stays, inert: both checks below need the old session alive.
  const handoffRef = useRef<{ from: string; to: string } | null>(null)
  const giveBack = useCallback((h: { from: string; to: string }, keepTo: boolean) => {
    handoffRef.current = null
    const pins = pinnedRef.current.map(k => (k === h.to ? h.from : k))
    setPinned(p => p.map(k => (k === h.to ? h.from : k)))
    setTabs(latest => {
      if (!latest.includes(h.to) || latest.includes(h.from)) return latest
      const back = latest.map(k => (k === h.to ? h.from : k))
      return keepTo ? pinnedFirst(openSessionTab(back, h.to, h.from), pins) : back
    })
  }, [])

  // A session that was deleted, or moved to another surface, cannot stay a tab.
  // Skipped while the list is empty: on a cold load the slots arrive after the
  // first paint, and pruning against nothing would discard the restored set
  // before it could ever be shown. A deleted successor gives its tab back first,
  // so the old session keeps its place.
  useEffect(() => {
    if (!enabled || liveKeys.size === 0) return
    // The old session leaving the list is NOT the commit: a delete removes the
    // row before the server answers, and a failed delete brings it back.
    const h = handoffRef.current
    if (h && liveKeys.has(h.from) && !liveKeys.has(h.to)) giveBack(h, false)
    setTabs(prev => {
      const next = pruneSessionTabs(prev, liveKeys)
      return next.length === prev.length ? prev : next
    })
  }, [enabled, liveKeys, giveBack])

  // A switch that gave up with both sessions alive forgets the succession
  // (memoryModeSwitch.ts, paneLayout.tsx). Nothing else marks that moment, so
  // every render checks for it; the check is a map lookup.
  useEffect(() => {
    const h = handoffRef.current
    if (!enabled || !h || !liveKeys.has(h.from) || !liveKeys.has(h.to)) return
    if (resolveSlotSuccession(h.from) !== h.to) giveBack(h, true)
  })

  // Keep the invariant. An activation that is already a tab is just a
  // selection (the strip derives "active" from activeSlot, so there is nothing
  // to store); one that is not takes over the tab the user was looking at,
  // unless that tab is pinned, when it opens in a new tab beside it instead.
  // A memory-mode switch is the exception: its new session SUCCEEDS the old
  // one (slotSuccession), so it takes over the tab and its pin, provisionally.
  //
  // FUNCTIONAL update, deliberately. Computing the next set from a snapshot read
  // outside (`tabsRef`) loses any update already queued in the same flush: the
  // prune effect above can have queued a removal, and writing back a pre-prune
  // array resurrects the deleted tab and persists it.
  useEffect(() => {
    const prev = prevActiveRef.current
    prevActiveRef.current = activeSlot
    if (!enabled || !activeSlot) return
    const h = handoffRef.current
    if (h?.from === activeSlot) giveBack(h, false)
    const prevPinned = !!prev && pinnedRef.current.includes(prev)
    const succeeds = !!prev && prevPinned && resolveSlotSuccession(prev) === activeSlot
    if (succeeds) {
      handoffRef.current = { from: prev, to: activeSlot }
      setPinned(p => p.map(k => (k === prev ? activeSlot : k)))
    }
    setTabs(latest => {
      if (latest.includes(activeSlot)) return latest
      if (latest.length === 0) return [activeSlot]
      if (prevPinned && !succeeds) return pinnedFirst(openSessionTab(latest, activeSlot, prev), pinnedRef.current)
      return replaceSessionTab(latest, prev, activeSlot)
    })
  }, [enabled, activeSlot, giveBack])

  // Report an in-place swap by OBSERVING the committed transition rather than
  // predicting it from the branch above. The prediction needed a pre-read
  // snapshot, which is exactly the stale read the functional update exists to
  // avoid; a diff of two committed states cannot be stale, and it also catches
  // any future path that replaces a tab.
  //
  // The signature of a swap is precise: same length, exactly ONE index changed,
  // and that index now holds the active session. Pruning and opening both change
  // the length, so neither can be mistaken for one.
  const prevTabsRef = useRef(tabs)
  useEffect(() => {
    const before = prevTabsRef.current
    prevTabsRef.current = tabs
    if (!enabled || !activeSlot || before.length !== tabs.length) return
    let changed = -1
    for (let i = 0; i < tabs.length; i++) {
      if (tabs[i] === before[i]) continue
      if (changed !== -1) return
      changed = i
    }
    if (changed === -1 || tabs[changed] !== activeSlot) return
    setCue({ key: activeSlot, at: Date.now() })
  }, [enabled, tabs, activeSlot])

  const openInNewTab = useCallback((key: string) => {
    if (!enabledRef.current) return
    // Already a tab: the set does not change, so point at the tab that already
    // holds it. Without this the gesture is a no-op the user cannot tell from a
    // misfire — background mode does not even switch sessions.
    if (tabsRef.current.includes(key)) { setCue({ key, at: Date.now() }); return }
    // Read the active session NOW, not in the updater: the updater can run
    // after a render that already moved `activeRef` to a session the caller
    // just switched to, which puts the tab at the end instead of beside the
    // one the user was on.
    const besideKey = activeRef.current
    setTabs(current => pinnedFirst(openSessionTab(current, key, besideKey), pinnedRef.current))
  }, [])

  const closeTab = useCallback((key: string) => {
    if (!enabledRef.current) return null
    const current = tabsRef.current
    const nextActive = nextActiveAfterClose(current, key, activeRef.current)
    setTabs(closeSessionTab(current, key))
    return nextActive
  }, [])

  // Functional for the same reason as the activation effect: a prune queued in
  // the same flush must not be undone by writing back a stale array.
  const reorderTabs = useCallback((keys: string[]) => {
    if (!enabledRef.current) return
    setTabs(latest => {
      if (keys.length !== latest.length) return latest
      const members = new Set(latest)
      if (new Set(keys).size !== keys.length || !keys.every(k => members.has(k))) return latest
      // A drag cannot carry an unpinned tab ahead of a pinned one.
      const next = pinnedFirst(keys, pinnedRef.current)
      return next.every((k, i) => k === latest[i]) ? latest : next
    })
  }, [])

  const togglePin = useCallback((key: string) => {
    if (!enabledRef.current) return
    const current = pinnedRef.current
    const next = current.includes(key) ? current.filter(k => k !== key) : [...current, key]
    setPinned(next)
    setTabs(latest => pinnedFirst(latest, next))
  }, [])

  return {
    tabs: enabled ? tabs : EMPTY_TABS,
    pinned: enabled ? pinned : EMPTY_TABS,
    cue: enabled ? cue : null,
    openInNewTab,
    closeTab,
    reorderTabs,
    togglePin,
  }
}
