/**
 * The session-tab WORKING SET: which sessions are open as tabs, in what order,
 * and how the set moves when one is opened, replaced or closed.
 *
 * Deliberately store-free and DOM-free so the two surfaces that draw a tab
 * strip share one copy of this arithmetic instead of drifting: the dashboard
 * chat surface (`SessionTabStrip`, keyed by slot) and the embed shell
 * (`EmbedTabStrip`, keyed by index). The index math behind "close the tab you
 * are looking at" is the part that silently rots when copied — it has four
 * cases (before / at / after the active tab, and emptied) and only one of them
 * is exercised by casual use.
 *
 * WHY A WORKING SET AT ALL, given the sidebar already lists every session and
 * `useKeyboardShortcuts` already jumps to the Nth of them: those digits index
 * the sidebar's DISPLAYED order, so which session Alt+3 hits changes the moment
 * the list is filtered, sorted or re-titled. A tab's position is chosen by the
 * user and only the user moves it (#4477).
 */

/** Longest tab title before it is ellipsized. Matches the embed strip. */
export const TAB_TITLE_MAX = 24

/**
 * Session tab geometry, Chrome's model: every tab takes the same width whatever
 * its title, starting at `TAB_MAX_WIDTH` and shrinking uniformly as the strip
 * fills. Past `TAB_MIN_WIDTH` they stop shrinking and the strip scrolls, so a
 * tab never gets too narrow to read or hit.
 */
export const TAB_MAX_WIDTH = 200
export const TAB_MIN_WIDTH = 96
/** Space between tabs. One constant, because the close arithmetic needs it. */
export const TAB_GAP = 4

/**
 * The pointer-close freeze. While the pointer stays in the strip after a
 * close, every tab keeps one fixed width and a trailing spacer keeps the
 * strip's content width, so the tab after the closed one slides left by exactly
 * one slot and its close button lands where the pointer already is. Leaving
 * the strip drops the freeze and the tabs reflow.
 */
export interface TabFreeze {
  /** Width every tab is held at, in px. */
  width: number
  /** Width of the tabs closed so far in this sequence, gaps included, in px. */
  spacer: number
}

/**
 * The freeze after a pointer close.
 *
 * Closing any tab except the last one keeps the width and adds the closed
 * slot to the spacer. Closing the LAST tab has no next tab to slide in, so the
 * remaining tabs widen until the new last tab ends where the closed one ended
 * and its close button lands under the pointer. That widening is capped at
 * `TAB_MAX_WIDTH`, so a strip with only a few tabs left cannot keep it up.
 *
 * `firstLeft` and `closedRight` are the first tab's left edge and the closed
 * tab's right edge in one coordinate space. `count` is the tab count BEFORE
 * the close.
 */
export function freezeAfterPointerClose(
  prev: TabFreeze | null,
  closed: { width: number; isLast: boolean },
  geometry: { firstLeft: number; closedRight: number; count: number },
): TabFreeze {
  const base = prev ?? { width: closed.width, spacer: 0 }
  if (!closed.isLast) return { width: base.width, spacer: base.spacer + closed.width + TAB_GAP }
  const remaining = geometry.count - 1
  if (remaining < 1) return base
  const fill = (geometry.closedRight - geometry.firstLeft - TAB_GAP * (remaining - 1)) / remaining
  return { width: Math.max(base.width, Math.min(TAB_MAX_WIDTH, fill)), spacer: base.spacer }
}

/**
 * There is deliberately NO ceiling on the working set.
 *
 * An earlier revision capped it at 12 and dropped the oldest tab past that, on
 * the stated grounds that more tabs make each one too narrow to read. That harm
 * cannot occur here: tabs stop shrinking at `TAB_MIN_WIDTH` and the
 * `overflow-x-auto` scroller with edge gradients takes over, so a tab never
 * becomes too narrow to read.
 * The cap was therefore paying a real cost — silently evicting a tab the user
 * placed, which is the exact promise this feature exists to make — to prevent
 * nothing. The set is still bounded, by pruning to sessions that actually
 * exist on the surface.
 */

/** Per-surface storage key, mirroring `mc-active-slot-<mode>` in ChatPage. */
export function sessionTabsStorageKey(mode?: string): string {
  return `mc-session-tabs-${mode || 'chat'}`
}

/**
 * The persisted set, or a single-tab set seeded from `fallback`.
 *
 * A parse failure resolves to the fallback rather than throwing: the strip is
 * an aid to navigation, so corrupt storage must degrade to today's behaviour
 * (one session, no strip) and never block the chat surface from mounting.
 */
export function loadSessionTabs(mode: string | undefined, fallback: string | null): string[] {
  let stored: unknown
  try {
    const raw = localStorage.getItem(sessionTabsStorageKey(mode))
    stored = raw ? JSON.parse(raw) : null
  } catch { stored = null }
  const keys = Array.isArray(stored) ? stored.filter((k): k is string => typeof k === 'string' && k.length > 0) : []
  const deduped = [...new Set(keys)]
  if (deduped.length) return deduped
  return fallback ? [fallback] : []
}

/**
 * Persist the set. A single tab is stored as EMPTY, not as one key: the strip
 * does not render below two tabs, so writing one would leave a user who never
 * touched the feature carrying tab state they cannot see, and a stale
 * single-key set would then decide which session a later reload restores —
 * which is `mc-active-slot-<mode>`'s job, not this key's.
 */
export function saveSessionTabs(
  mode: string | undefined,
  tabs: readonly string[],
  write: (key: string, value: string) => void,
): void {
  write(sessionTabsStorageKey(mode), JSON.stringify(tabs.length > 1 ? tabs : []))
}

/** Drop tabs whose session no longer exists on this surface, order preserved. */
export function pruneSessionTabs(tabs: readonly string[], liveKeys: Iterable<string>): string[] {
  const live = liveKeys instanceof Set ? liveKeys : new Set(liveKeys)
  return tabs.filter(k => live.has(k))
}

/**
 * Open `key` as a NEW tab immediately after `afterKey` (the tab the user is
 * looking at), the way an editor inserts beside the current tab rather than at
 * the end — a tab opened from the session you were reading belongs next to it.
 *
 * Already-open is not an error: it returns the set unchanged so the caller's
 * "then activate it" step is the whole behaviour, matching every tabbed UI.
 */
export function openSessionTab(tabs: readonly string[], key: string, afterKey: string | null): string[] {
  if (!key) return [...tabs]
  if (tabs.includes(key)) return [...tabs]
  const next = [...tabs]
  const at = afterKey ? next.indexOf(afterKey) : -1
  if (at === -1) next.push(key)
  else next.splice(at + 1, 0, key)
  return next
}

/**
 * Point the tab currently holding `oldKey` at `newKey` — the "clicking a
 * session in the sidebar replaces the active tab's content" path. The tab
 * keeps its POSITION, which is what makes the strip a stable working set
 * rather than a most-recent list.
 *
 * `newKey` already being open elsewhere means the caller should activate that
 * tab instead of duplicating it, so the set comes back untouched.
 */
export function replaceSessionTab(tabs: readonly string[], oldKey: string | null, newKey: string): string[] {
  if (!newKey || tabs.includes(newKey)) return [...tabs]
  const at = oldKey ? tabs.indexOf(oldKey) : -1
  if (at === -1) return [...tabs, newKey]
  const next = [...tabs]
  next[at] = newKey
  return next
}

/** The set without `key`. */
export function closeSessionTab(tabs: readonly string[], key: string): string[] {
  return tabs.filter(k => k !== key)
}

/**
 * Which session to show after `key` is closed, or null when nothing is left.
 *
 * Closing a tab the user is NOT looking at must not move them, so this returns
 * the active tab untouched in that case. Closing the active one delegates to
 * `removeTabAt` — the landing rule (right neighbour, falling back to the left at
 * the end of the strip) is defined THERE and nowhere else. Two spellings of one
 * rule, even adjacent in this file, is exactly the drift the shared module
 * exists to prevent; this function only translates keys into the index model.
 */
export function nextActiveAfterClose(
  tabs: readonly string[],
  key: string,
  activeKey: string | null,
): string | null {
  if (activeKey !== key) return activeKey
  const at = tabs.indexOf(key)
  if (at === -1) return activeKey
  const { tabs: remaining, activeIndex } = removeTabAt(tabs, at, at)
  return remaining.length ? remaining[activeIndex] : null
}

/**
 * Splice `index` out and say where the selection lands. THE landing rule for
 * both strips: the selection moves to the closed tab's RIGHT neighbour, falling
 * back to the left at the end of the strip. Landing on "the first tab" instead
 * would teleport a user who closes the last of eight tabs to the other end.
 *
 * Expressed over INDICES because the embed strip identifies a tab by position
 * (it can hold a slug-less "sessions" placeholder, so keys are not unique
 * there). The dashboard's key-model `nextActiveAfterClose` translates into this
 * rather than restating the rule.
 *
 * An emptied list reports index 0 so the caller can seed its placeholder and
 * select it without a second clamp.
 */
export function removeTabAt<T>(
  tabs: readonly T[],
  index: number,
  activeIndex: number,
): { tabs: T[]; activeIndex: number } {
  const next = [...tabs]
  if (index < 0 || index >= next.length) return { tabs: next, activeIndex }
  next.splice(index, 1)
  if (!next.length) return { tabs: next, activeIndex: 0 }
  let active = activeIndex
  if (activeIndex > index) active -= 1
  else if (activeIndex >= next.length) active = next.length - 1
  return { tabs: next, activeIndex: active }
}

/** What a tab's status dot reports. Ordered by which state a user must see first. */
export type TabStatus = 'idle' | 'running' | 'unread' | 'permission' | 'question'

/**
 * The one status mapping both strips draw from.
 *
 * The ORDER is the contract, not an implementation detail. `permission` and
 * `question` outrank `running` because both mean the turn is parked on the
 * user: a tab pulsing "working" is exactly what makes someone leave it alone,
 * so a blocked session that reads as busy is never looked at. `unread` is last
 * because it is true of anything that merely finished.
 */
export function tabStatus(
  slot: { running?: boolean; pending_approval?: boolean; needs_input?: boolean } | undefined,
  unreadKeys: readonly string[] | ReadonlySet<string>,
  key: string,
): TabStatus {
  if (!slot) return 'idle'
  if (slot.pending_approval) return 'permission'
  if (slot.needs_input) return 'question'
  if (slot.running) return 'running'
  const unread = unreadKeys instanceof Set ? unreadKeys.has(key) : (unreadKeys as readonly string[]).includes(key)
  return unread ? 'unread' : 'idle'
}

/**
 * The status dot's colour vocabulary, as theme variables.
 *
 * Shared rather than inlined per strip so the same state cannot read as two
 * different colours on two surfaces — a user who learns "amber means it wants
 * something from me" in the embed shell must not have to relearn it here.
 */
export const TAB_STATUS_COLOR: Record<TabStatus, string> = {
  idle: 'var(--muted)',
  running: 'var(--accent)',
  unread: 'var(--ok)',
  permission: 'var(--warn)',
  question: 'var(--info)',
}

/**
 * Whether the dot animates. Only the two states that are WAITING ON SOMETHING
 * pulse — work in flight, and a gate the user must clear. A steady dot means
 * nothing will change unless the user acts, so pulsing an idle or merely
 * unread tab would spend the one attention-grabbing cue on the states that
 * least need it.
 */
export function tabStatusPulses(status: TabStatus): boolean {
  return status === 'running' || status === 'permission'
}

/** Ellipsize a tab label. Returns the input untouched when it already fits. */
export function truncateTabTitle(title: string, max: number = TAB_TITLE_MAX): string {
  return title.length > max ? title.slice(0, max) + '…' : title
}
