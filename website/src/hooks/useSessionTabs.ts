import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { safeSetItem } from '../utils/safeStorage'
import {
  closeSessionTab,
  loadSessionTabs,
  nextActiveAfterClose,
  openSessionTab,
  pruneSessionTabs,
  replaceSessionTab,
  saveSessionTabs,
} from '../lib/sessionTabs'

export interface SessionTabsApi {
  /** Open sessions in strip order. Length < 2 means the strip does not render. */
  tabs: string[]
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
  const [cue, setCue] = useState<{ key: string; at: number } | null>(null)
  const tabsRef = useRef(tabs)
  tabsRef.current = tabs
  const activeRef = useRef(activeSlot)
  activeRef.current = activeSlot
  const enabledRef = useRef(enabled)
  enabledRef.current = enabled
  // The session the user was on BEFORE this change — the tab whose content a
  // plain sidebar click replaces. Seeded from the mount value so the first
  // switch of a visit replaces rather than appends.
  const prevActiveRef = useRef<string | null>(activeSlot)

  useEffect(() => { if (enabled) saveSessionTabs(mode, tabs, safeSetItem) }, [enabled, mode, tabs])

  const liveKeys = useMemo(() => new Set(liveSlots.map(s => s.key)), [liveSlots])

  // A session that was deleted, or moved to another surface, cannot stay a tab.
  // Skipped while the list is empty: on a cold load the slots arrive after the
  // first paint, and pruning against nothing would discard the restored set
  // before it could ever be shown.
  useEffect(() => {
    if (!enabled || liveKeys.size === 0) return
    setTabs(prev => {
      const next = pruneSessionTabs(prev, liveKeys)
      return next.length === prev.length ? prev : next
    })
  }, [enabled, liveKeys])

  // Keep the invariant. An activation that is already a tab is just a
  // selection (the strip derives "active" from activeSlot, so there is nothing
  // to store); one that is not takes over the tab the user was looking at.
  //
  // FUNCTIONAL update, deliberately. Computing the next set from a snapshot read
  // outside (`tabsRef`) loses any update already queued in the same flush: the
  // prune effect above can have queued a removal, and writing back a pre-prune
  // array resurrects the deleted tab and persists it.
  useEffect(() => {
    const prev = prevActiveRef.current
    prevActiveRef.current = activeSlot
    if (!enabled || !activeSlot) return
    setTabs(latest => {
      if (latest.includes(activeSlot)) return latest
      if (latest.length === 0) return [activeSlot]
      return replaceSessionTab(latest, prev, activeSlot)
    })
  }, [enabled, activeSlot])

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
    setTabs(current => openSessionTab(current, key, activeRef.current))
  }, [])

  const closeTab = useCallback((key: string) => {
    if (!enabledRef.current) return null
    const current = tabsRef.current
    const nextActive = nextActiveAfterClose(current, key, activeRef.current)
    setTabs(closeSessionTab(current, key))
    return nextActive
  }, [])

  return { tabs: enabled ? tabs : EMPTY_TABS, cue: enabled ? cue : null, openInNewTab, closeTab }
}
