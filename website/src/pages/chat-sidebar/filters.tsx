/** The session filter chips: the `SESSION_FILTERS` table, the persisted chip, folder
 *  and tag filter state, the Recent window, the status sets the chips count, and the
 *  unread auto-drain. */
import { Circle, Zap, Pin, Clock } from 'lucide-react'
import { useState, useCallback, useMemo, useEffect, useRef } from 'react'
import { shallowEqual } from 'react-redux'
import type { SessionFilterKey, Slot } from './types'
import { safeSetItem } from '../../utils/safeStorage'
import { readStoredHiddenFolders, HIDDEN_FOLDERS_LS_KEY, readStoredTagFilter, TAG_FILTER_LS_KEY, FOLDERS_SHELVED_LS_KEY, readStoredRecentWindow, RECENT_WINDOW_LS_KEY } from './persistence'
import { useAppSelector } from '../../store'
import { selectSidebarWorkflowActiveKeys, selectSidebarAutomationRunningKeys, selectSidebarSubagentCounts, selectSidebarApprovalCounts } from '../../store/chatSlice'
import { decomposeRecentWindow, type RecentUnit, clampRecentAmount, customRecentWindowMs, recentTickIntervalMs, isWithinRecentWindow } from '../recentWindow'
import { normalizeRunSessionKey } from '../../apps/workflows/runModel'
import { slotActivityTs } from '../chat/sessionOrder'
import { decideUnreadDrain } from '../unreadDrain'
import { isPeerRow } from './rowIdentity'

interface SessionFilterDef {
  key: SessionFilterKey
  storageKey: string
  color: string
  icon: (active: boolean) => React.ReactNode
}

export const SESSION_FILTERS: SessionFilterDef[] = [
  {
    key: 'unread', storageKey: 'mc-session-unread-only',
    // Status token, not brand accent: this chip is the legend/toggle for the
    // same unread state whose row dot reads `var(--ok)` in SessionRow (#10479).
    color: 'var(--ok)',
    icon: (active) => <Circle size={12} className={active ? 'text-[var(--ok)]' : 'text-muted'} {...(active ? { strokeWidth: 0, fill: 'var(--ok)' } : {})} />,
  },
  {
    key: 'running', storageKey: 'mc-session-running-only',
    color: 'var(--warn)',
    icon: (active) => <Zap size={12} className={active ? 'text-[var(--warn)]' : 'text-muted'} {...(active ? { fill: 'var(--warn)', stroke: 'none' } : {})} />,
  },
  {
    key: 'pinned', storageKey: 'mc-session-pinned-only',
    color: 'var(--accent)',
    icon: (active) => <Pin size={12} className={active ? 'text-accent' : 'text-muted'} {...(active ? { fill: 'var(--accent)', stroke: 'none' } : {})} />,
  },
  {
    key: 'recent', storageKey: 'mc-session-recent-only',
    color: 'var(--ok)',
    icon: (active) => <Clock size={12} className={active ? 'text-[var(--ok)]' : 'text-muted'} />,
  },
]

/** The status chips, folder hides, tag selection and shelved flag, each persisted. */
export function useSessionFilterState() {
  const [activeFilters, setActiveFilters] = useState<Set<SessionFilterKey>>(() => {
    const initialFilters = new Set<SessionFilterKey>()
    for (const filterDef of SESSION_FILTERS) { if (localStorage.getItem(filterDef.storageKey) === '1') initialFilters.add(filterDef.key) }
    return initialFilters
  })
  // Which folders are excluded from the flat lane, chosen from the filter
  // menu's folder checkboxes. We persist the HIDDEN ids (not the visible ones)
  // so a folder created later defaults to visible instead of silently
  // vanishing. Purely a view preference — folder membership and the folder
  // tree's own collapse state are untouched.
  const [filterHiddenFolders, setFilterHiddenFolders] = useState<Set<string>>(() => readStoredHiddenFolders())
  const toggleFolderFilter = useCallback((id: string) => {
    setFilterHiddenFolders(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      safeSetItem(HIDDEN_FOLDERS_LS_KEY, JSON.stringify([...next]))
      return next
    })
  }, [])
  const showAllFolders = useCallback(() => {
    setFilterHiddenFolders(new Set())
    safeSetItem(HIDDEN_FOLDERS_LS_KEY, '[]')
  }, [])
  /** Tag ids the list is narrowed to. Selecting several is a UNION ("Blocked or
   *  Waiting"), matching how a board column with several tags already behaves, so
   *  the two surfaces cannot disagree about what a multi-tag selection means. */
  const [filterTagIds, setFilterTagIds] = useState<Set<string>>(() => readStoredTagFilter())
  const toggleTagFilter = useCallback((id: string) => {
    setFilterTagIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      safeSetItem(TAG_FILTER_LS_KEY, JSON.stringify([...next]))
      return next
    })
  }, [])
  const clearTagFilter = useCallback(() => {
    setFilterTagIds(new Set())
    safeSetItem(TAG_FILTER_LS_KEY, '[]')
  }, [])
  // Shelved = the Folders section is rolled up to its heading, so a long folder
  // list stops crowding the Filter and Sort rows. Purely cosmetic: shelving
  // changes nothing about which folders are hidden, and the heading keeps
  // showing the hidden count so the state stays visible while rolled up.
  const [foldersShelved, setFoldersShelved] = useState(() => {
    try { return localStorage.getItem(FOLDERS_SHELVED_LS_KEY) === '1' } catch { return false }
  })
  const toggleFoldersShelved = useCallback(() => {
    setFoldersShelved(v => { const next = !v; safeSetItem(FOLDERS_SHELVED_LS_KEY, next ? '1' : '0'); return next })
  }, [])
  const toggleFilter = useCallback((key: SessionFilterKey) => {
    setActiveFilters(prev => {
      const next = new Set(prev)
      const filterDef = SESSION_FILTERS.find(sf => sf.key === key)!
      if (next.has(key)) { next.delete(key); safeSetItem(filterDef.storageKey, '0') }
      else { next.add(key); safeSetItem(filterDef.storageKey, '1') }
      return next
    })
  }, [])
  const disableFilter = useCallback((key: SessionFilterKey) => {
    setActiveFilters(prev => {
      if (!prev.has(key)) return prev
      const next = new Set(prev)
      next.delete(key)
      const filterDef = SESSION_FILTERS.find(sf => sf.key === key)!
      safeSetItem(filterDef.storageKey, '0')
      return next
    })
  }, [])
  const enableFilter = useCallback((key: SessionFilterKey) => {
    setActiveFilters(prev => {
      if (prev.has(key)) return prev
      const next = new Set(prev)
      next.add(key)
      const filterDef = SESSION_FILTERS.find(sf => sf.key === key)!
      safeSetItem(filterDef.storageKey, '1')
      return next
    })
  }, [])
  return {
    activeFilters, setActiveFilters, filterHiddenFolders, setFilterHiddenFolders, toggleFolderFilter,
    showAllFolders, filterTagIds, toggleTagFilter, clearTagFilter, foldersShelved, setFoldersShelved,
    toggleFoldersShelved, toggleFilter, disableFilter, enableFilter,
  }
}

/** Which rows are running, recent or unread, the chip counts, the Recent window and the unread auto-drain. */
export function useSessionStatusFilters({ unreadSlots, activeFilters, enableFilter, localSlots, allRows, disableFilter }: {
  unreadSlots: string[]
  activeFilters: Set<SessionFilterKey>
  enableFilter: (key: SessionFilterKey) => void
  localSlots: Slot[]
  allRows: Slot[]
  disableFilter: (key: SessionFilterKey) => void
}) {
  // Signal from the SSE/data-fetch layer indicating the initial slot list
  // has arrived. Used by the auto-drain effect to distinguish "data not yet
  // loaded" from "data loaded and genuinely empty".
  const slotsLoaded = useAppSelector(s => s.dashboard.slotsLoaded)
  // ── Per-slot live signals live in the rows, not here ─────────────────────
  // Each SessionRow subscribes slot-scoped to its own status line, goal loop,
  // queued sub-agents and workflow runs. The shell needs only PRESENCE — which
  // sessions have background work — for the In-progress filter and the board's
  // state lanes, so it subscribes at key granularity (shallowEqual on key
  // arrays): a mid-loop cycle-count bump or a workflow phase update re-renders
  // one row, never the whole sidebar.
  // Keys are NORMALIZED session keys (normalizeRunSessionKey) — membership
  // tests must normalize the slot key the same way.
  const workflowActiveKeys = useAppSelector(selectSidebarWorkflowActiveKeys, shallowEqual)
  const workflowActiveSet = useMemo(() => new Set(workflowActiveKeys), [workflowActiveKeys])
  // As above, the shell needs only active automation membership. A probe count
  // or terminal detail update re-renders its row without repainting the list.
  const automationRunningKeys = useAppSelector(selectSidebarAutomationRunningKeys, shallowEqual)
  const automationRunningSet = useMemo(
    () => new Set(automationRunningKeys),
    [automationRunningKeys],
  )
  // NOT dashboardSlice.subagentRunning — that only broadcasts on "done", not spawn.
  const subagentCounts = useAppSelector(selectSidebarSubagentCounts, shallowEqual)
  // Spawn approvals (pending + approval_id) — surfaced here since background chats have no inline prompt.
  const subagentApprovalCounts = useAppSelector(selectSidebarApprovalCounts, shallowEqual)
  // O(1) lookup set for the filter predicate (mirrors the `pinned` and
  // `slotSearchRanks` patterns elsewhere in the sidebar).
  const unreadSet = useMemo(() => new Set(unreadSlots), [unreadSlots])
  // Heartbeat that re-evaluates recency even when nothing else re-renders.
  // Sidebar interactions (new messages, status changes, opening the menu) all
  // recompute the recency lookup for free, so this only matters when the sidebar
  // sits idle with the Recent filter on — without it a stale session would
  // never age out of the list. Gated on the filter being active so we don't
  // wake an idle tab needlessly, mirroring the `staleTick` pattern in App.tsx.
  const recentFilterActive = activeFilters.has('recent')
  // User-selectable recency window (ms), persisted. Presets + custom value live
  // in the filter submenu; the chip and menu row show the current window.
  const [recentWindowMs, setRecentWindowMs] = useState(readStoredRecentWindow)
  const setRecentWindow = useCallback((ms: number) => {
    setRecentWindowMs(ms)
    safeSetItem(RECENT_WINDOW_LS_KEY, String(ms))
  }, [])
  /**
   * Commit a window the user explicitly PICKED, which also turns the filter on.
   *
   * Intent is decided here rather than by comparing the new window to the stored
   * one, because an identical value does not mean the user did nothing: the
   * default window IS the first preset (`DEFAULT_RECENT_WINDOW_MS` === the
   * `1 hour` chip), so the chip a fresh user is most likely to click is exactly
   * the one a value-equality gate would swallow — leaving the "chip goes green,
   * list does not change" defect alive for the most common pick.
   */
  const chooseRecentWindow = useCallback((ms: number) => {
    setRecentWindow(ms)
    enableFilter('recent')
  }, [setRecentWindow, enableFilter])
  // Custom-picker draft state. The amount is a raw string (not derived from the
  // committed window) so the field can be cleared / partially edited without
  // snapping to 1 on every keystroke, and the unit stays exactly as the user
  // picked it rather than being re-derived (24 "hours" must not flip to 1 "day").
  // We commit + clamp to `recentWindowMs` only on blur / Enter / unit change; a
  // preset click re-seeds both drafts so the boxes track the chosen preset.
  const [recentAmountDraft, setRecentAmountDraft] = useState(() => String(decomposeRecentWindow(recentWindowMs).value))
  const [recentUnitDraft, setRecentUnitDraft] = useState<RecentUnit>(() => decomposeRecentWindow(recentWindowMs).unit)
  const selectRecentPreset = useCallback((ms: number) => {
    chooseRecentWindow(ms)
    const { value, unit } = decomposeRecentWindow(ms)
    setRecentAmountDraft(String(value))
    setRecentUnitDraft(unit)
  }, [chooseRecentWindow])
  const commitRecentAmount = useCallback(() => {
    const clamped = clampRecentAmount(recentAmountDraft)
    setRecentAmountDraft(String(clamped))
    const next = customRecentWindowMs(clamped, recentUnitDraft)
    setRecentWindow(next)
    // The amount field commits on BLUR as well as Enter, so leaving the field
    // untouched re-commits the window it already held. A changed amount is the
    // intent signal here; a bare blur must not toggle the filter behind the
    // user's back. The picked paths above need no such test — a click on a chip
    // or a unit is unambiguous even when the value repeats.
    if (next !== recentWindowMs) enableFilter('recent')
  }, [recentAmountDraft, recentUnitDraft, recentWindowMs, setRecentWindow, enableFilter])
  const changeRecentUnit = useCallback((unit: RecentUnit) => {
    setRecentUnitDraft(unit)
    chooseRecentWindow(customRecentWindowMs(recentAmountDraft, unit))
  }, [recentAmountDraft, chooseRecentWindow])
  const [recentTick, setRecentTick] = useState(0)
  useEffect(() => {
    if (!recentFilterActive) return
    // Tick often enough that a slot ages out promptly relative to its window
    // (~1/10th the window), but never faster than every 30s and never slower
    // than RECENT_TICK_MS — a short custom window shouldn't wake the tab every
    // few seconds, and a long one shouldn't lag by more than ~10 minutes.
    const id = setInterval(() => setRecentTick(t => t + 1), recentTickIntervalMs(recentWindowMs))
    return () => clearInterval(id)
  }, [recentFilterActive, recentWindowMs])
  // Wider than the payload's `s.running`: a live workflow run or an active goal
  // loop counts as in progress, so neither drops out of the filter or its count.
  const runningSet = useMemo<Set<string>>(() => {
    const out = new Set<string>()
    for (const s of localSlots) {
      // Set membership over selector-produced keys is own-property by
      // construction (Object.keys), so no safeKey guard is needed here.
      const automationRunning = automationRunningSet.has(s.key)
      if (s.running
        || workflowActiveSet.has(normalizeRunSessionKey(s.key))
        || automationRunning) out.add(s.key)
    }
    return out
  }, [localSlots, workflowActiveSet, automationRunningSet])
  // A running turn is recent BY DEFINITION: the ordering key stops advancing
  // mid-turn, so a long turn would age out while it is the busiest row on screen.
  const recentSet = useMemo<Set<string>>(() => {
    // One `now` per recompute, so every slot is measured against the same instant.
    // The last-activity timestamp mirrors the date-sort comparator.
    const now = Date.now()
    const out = new Set<string>()
    for (const s of localSlots) {
      if (runningSet.has(s.key) || isWithinRecentWindow(slotActivityTs(s), now, recentWindowMs)) out.add(s.key)
    }
    return out
    // `recentTick` is an intentional dep: it forces recency to re-evaluate on
    // the heartbeat above so idle sessions age out of the Recent filter.
  }, [localSlots, runningSet, recentWindowMs, recentTick]) // eslint-disable-line react-hooks/exhaustive-deps
  // Exhaustive over `SessionFilterKey` on purpose: a new filter key becomes a
  // type error here instead of a predicate that silently matches nothing.
  const _derivedLookup = useMemo<Record<SessionFilterKey, (slot: Slot) => boolean>>(() => ({
    unread: slot => !isPeerRow(slot) && unreadSet.has(slot.key),
    running: slot => isPeerRow(slot) ? slot.running === true : runningSet.has(slot.key),
    pinned: slot => !isPeerRow(slot) && !!slot.pinned,
    recent: slot => isPeerRow(slot)
      ? isWithinRecentWindow(slotActivityTs(slot), Date.now(), recentWindowMs)
      : recentSet.has(slot.key),
  }), [unreadSet, runningSet, recentSet, recentWindowMs])
  const filterCounts = useMemo(() => {
    const counts = {} as Record<SessionFilterKey, number>
    // Counted over `allRows` — the collection the filter RENDERS — not over
    // `localSlots`. The two diverge for `running` and `recent`, whose predicates
    // are origin-aware and so match peer rows: counting locals while rendering
    // the merged set made those two badges under-report by exactly the remote
    // rows the filter goes on to show. `unread` and `pinned` are unaffected
    // either way because their predicates are themselves local-only
    // (`!isPeerRow(slot) && …`), so widening the collection cannot add a match —
    // which is why the badge must follow the RENDERED set rather than each
    // predicate's notion of scope.
    for (const filterDef of SESSION_FILTERS) counts[filterDef.key] = allRows.filter(_derivedLookup[filterDef.key]).length
    return counts
  }, [allRows, _derivedLookup])
  // Ref mirror of `activeFilters` so the auto-drain effect can read the
  // current toggle state without depending on it. Keeps the effect from
  // re-firing on its own setState output.
  const activeFiltersRef = useRef(activeFilters)
  activeFiltersRef.current = activeFilters
  // Auto-disable the unread filter when the inbox drains, so the user doesn't
  // end up staring at an empty list. Decision logic lives in the pure helper
  // `decideUnreadDrain` so it can be unit-tested in isolation — see
  // `src/test/unreadDrain.test.ts`. The null-sentinel on `prevUnreadCount`
  // distinguishes "data not yet loaded" from "data loaded and genuinely empty"
  // so the persisted=true + loads-empty case fires on the first post-load
  // tick. See the helper's docstring for the known accepted batched-update
  // edge case.
  const prevUnreadCount = useRef<number | null>(null)
  useEffect(() => {
    // Guard the ENTIRE body on slotsLoaded: without this, the unconditional
    // `prevUnreadCount.current = unreadSlots.length` assignment below would
    // destroy the null sentinel on the pre-load effect run, breaking the
    // case-2 "loadedEmpty" branch in `decideUnreadDrain`. The helper's own
    // !slotsLoaded check stays as defense-in-depth.
    if (!slotsLoaded) return
    const action = decideUnreadDrain({
      prev: prevUnreadCount.current,
      current: unreadSlots.length,
      slotsLoaded,
      showUnreadOnly: activeFiltersRef.current.has('unread'),
    })
    if (action === 'disable') disableFilter('unread')
    prevUnreadCount.current = unreadSlots.length
  }, [unreadSlots.length, slotsLoaded, disableFilter])
  return {
    slotsLoaded, workflowActiveSet, automationRunningSet, subagentCounts, subagentApprovalCounts, unreadSet,
    recentWindowMs, recentAmountDraft, setRecentAmountDraft, recentUnitDraft, selectRecentPreset,
    commitRecentAmount, changeRecentUnit, runningSet, _derivedLookup, filterCounts,
  }
}
