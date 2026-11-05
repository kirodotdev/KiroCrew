/**
 * The left rail's user-owned layout: ordered groups of pinned nav ids.
 *
 * Persistence follows the same "absent means shipped default" contract as
 * `appNavHidden.ts` and `navPinned.ts`: with no stored value the rail renders
 * `DEFAULT_RAIL_LAYOUT`, the way a fresh macOS Dock starts from a shipped
 * arrangement. The stored value is a versioned envelope parsed by a pure,
 * validating `parseRailLayout` (the same shape as the composable layout's
 * `parseLayout`): anything malformed or of an unknown version falls back to
 * the default instead of throwing.
 *
 * Resolution (`resolveRailLayout`) is what keeps the stored layout from
 * needing migrations:
 *   - an id with no current surface (preview-gated, uninstalled) is skipped,
 *     but stays in storage so it returns to its slot when it comes back;
 *   - an available id the layout never listed, and that the user has not
 *     unpinned, lands in its default group, so a built-in added later still
 *     appears for someone who customized their rail before it shipped.
 *
 * Writers MUST merge into a fresh `readRailLayout()`, never a React copy: a
 * same-tab write fires no `storage` event, and another tab may have written
 * since this one last read.
 */
import { useEffect, useState } from 'react'

import { safeGetItem, safeRemoveItem, safeSetItem } from '../utils/safeStorage'

export const RAIL_LAYOUT_KEY = 'mc-rail-layout'
export const RAIL_LAYOUT_CHANGED_EVENT = 'mc:rail-layout-changed'
export const RAIL_LAYOUT_VERSION = 1

export interface RailGroup {
  id: string
  /** A user-chosen title. Absent: the group's shipped title (by id) is shown. */
  title?: string
  items: string[]
}

export interface RailLayout {
  version: number
  groups: RailGroup[]
  /** Ids the user unpinned. Kept so a removed default does not reappear. */
  hidden: string[]
  /** Rail-only display names, by id. Absent: the row's live name is shown. */
  labels?: Record<string, string>
}

/* ── Entity pins: a session or a Mate the user dragged onto the rail. ── */

export const SESSION_PIN_PREFIX = 'session:'
export const MATE_PIN_PREFIX = 'mate:'
export const sessionPinId = (slotKey: string) => SESSION_PIN_PREFIX + slotKey
export const matePinId = (slug: string) => MATE_PIN_PREFIX + slug
/** True for a pinned session or Mate: listed by the user, never by a shipped default. */
export const isEntityPin = (id: string) => id.startsWith(SESSION_PIN_PREFIX) || id.startsWith(MATE_PIN_PREFIX)
/** Native-drag MIME type a drag source sets to offer a rail pin id. */
export const RAIL_PIN_MIME = 'application/x-kirocrew-rail-pin'

/** The shipped arrangement. Group ids double as the i18n lookup for titles. */
export const DEFAULT_RAIL_LAYOUT: RailLayout = {
  version: RAIL_LAYOUT_VERSION,
  groups: [
    { id: 'workspace', items: ['chat', 'members', 'schedule', 'artifacts'] },
    { id: 'apps', items: ['apps', 'apps-library'] },
  ],
  hidden: [],
}

/** The group an id belongs to when the layout has never listed it. */
export const FALLBACK_GROUP_ID = 'workspace'

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

const strings = (v: unknown): string[] =>
  Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []

/**
 * Parse a stored layout. Returns null for anything that is not a current-version
 * layout with at least one well-formed group, so the caller falls back to the
 * default. Duplicate group ids and an id listed twice keep their first
 * occurrence: a tampered value can never render one row in two places.
 */
export function parseRailLayout(raw: string | null): RailLayout | null {
  if (raw === null) return null
  let parsed: unknown
  try { parsed = JSON.parse(raw) } catch { return null }
  if (!isPlainObject(parsed) || parsed.version !== RAIL_LAYOUT_VERSION) return null
  if (!Array.isArray(parsed.groups)) return null
  const seenGroups = new Set<string>()
  const seenItems = new Set<string>()
  const groups: RailGroup[] = []
  for (const g of parsed.groups) {
    if (!isPlainObject(g) || typeof g.id !== 'string' || !g.id || seenGroups.has(g.id)) continue
    seenGroups.add(g.id)
    const items = strings(g.items).filter(id => !seenItems.has(id) && (seenItems.add(id), true))
    groups.push({ id: g.id, ...(typeof g.title === 'string' && g.title ? { title: g.title } : {}), items })
  }
  if (groups.length === 0) return null
  const labels: Record<string, string> = {}
  if (isPlainObject(parsed.labels)) {
    for (const [id, v] of Object.entries(parsed.labels)) if (typeof v === 'string' && v.trim()) labels[id] = v
  }
  return {
    version: RAIL_LAYOUT_VERSION, groups, hidden: strings(parsed.hidden),
    ...(Object.keys(labels).length ? { labels } : {}),
  }
}

export function readRailLayout(): RailLayout {
  return parseRailLayout(safeGetItem(RAIL_LAYOUT_KEY)) ?? DEFAULT_RAIL_LAYOUT
}

export function writeRailLayout(layout: RailLayout): boolean {
  const ok = safeSetItem(RAIL_LAYOUT_KEY, JSON.stringify(layout))
  if (ok && typeof window !== 'undefined') window.dispatchEvent(new Event(RAIL_LAYOUT_CHANGED_EVENT))
  return ok
}

/** The default group of an id: where `DEFAULT_RAIL_LAYOUT` puts it, else the fallback. */
export function defaultGroupOf(id: string): string {
  return DEFAULT_RAIL_LAYOUT.groups.find(g => g.items.includes(id))?.id ?? FALLBACK_GROUP_ID
}

/**
 * The groups the rail draws: stored order, filtered to `available` ids, with
 * every available-but-unlisted, not-hidden id appended to its default group
 * (or the last group when that group was removed). Empty groups are kept so a
 * drop target survives the user dragging its last row out.
 */
export function resolveRailLayout(
  layout: RailLayout,
  available: readonly string[],
  groupOf: (id: string) => string = defaultGroupOf,
): RailGroup[] {
  const avail = new Set(available)
  const hidden = new Set(layout.hidden)
  const listed = new Set(layout.groups.flatMap(g => g.items))
  const groups = layout.groups.map(g => ({ ...g, items: g.items.filter(id => avail.has(id) && !hidden.has(id)) }))
  for (const id of available) {
    if (listed.has(id) || hidden.has(id)) continue
    const target = groups.find(g => g.id === groupOf(id)) ?? groups[groups.length - 1]
    target.items.push(id)
  }
  return groups
}

/**
 * Apply a reorder of the VISIBLE rows back onto the stored layout.
 *
 * `visible` is the resolved groups after the move. Each stored group keeps the
 * ids it lists that are NOT currently visible (unavailable ones), in place
 * relative to the visible ids that preceded them, so a preview-gated row keeps
 * its slot through an unrelated drag. Groups are matched by id; a visible
 * group the store lacks is appended.
 */
export function applyVisibleOrder(stored: RailLayout, visible: readonly RailGroup[]): RailLayout {
  const visibleIds = new Set(visible.flatMap(g => g.items))
  const byId = new Map(stored.groups.map(g => [g.id, g]))
  const groups = visible.map(vg => {
    const old = byId.get(vg.id)
    const out: string[] = []
    const queue = [...vg.items]
    for (const id of old?.items ?? []) {
      if (visibleIds.has(id)) { const next = queue.shift(); if (next) out.push(next) }
      else out.push(id)
    }
    out.push(...queue)
    return { ...(old ?? {}), ...vg, items: out }
  })
  return { ...stored, version: RAIL_LAYOUT_VERSION, groups }
}

/* ── Edits on the VISIBLE groups (what the rail draws). Pure; commit with `commitRail`. ── */

/** Move one row to the end of another group. A no-op for an unknown id or group. */
export function moveToGroup(groups: readonly RailGroup[], id: string, groupId: string): RailGroup[] {
  if (!groups.some(g => g.id === groupId) || !groups.some(g => g.items.includes(id))) return [...groups]
  return groups.map(g => {
    const items = g.items.filter(x => x !== id)
    return g.id === groupId ? { ...g, items: [...items, id] } : { ...g, items }
  })
}

/** A group id no current group uses. */
export function newGroupId(groups: readonly RailGroup[]): string {
  const used = new Set(groups.map(g => g.id))
  let n = groups.length + 1
  while (used.has(`group-${n}`)) n++
  return `group-${n}`
}

/** Append an empty, titled group. */
export function addGroup(groups: readonly RailGroup[], title: string): { groups: RailGroup[]; groupId: string } {
  const groupId = newGroupId(groups)
  return { groups: [...groups, { id: groupId, title, items: [] }], groupId }
}

export function renameGroup(groups: readonly RailGroup[], groupId: string, title: string): RailGroup[] {
  const t = title.trim()
  return groups.map(g => (g.id === groupId ? { ...g, title: t || undefined } : g))
}

/** Remove a group; its rows join the group above it (the first group, for the first). */
export function removeGroup(groups: readonly RailGroup[], groupId: string): RailGroup[] {
  const i = groups.findIndex(g => g.id === groupId)
  if (i < 0 || groups.length < 2) return [...groups]
  const into = i === 0 ? 1 : i - 1
  return groups
    .map((g, j) => (j === into ? { ...g, items: into < i ? [...g.items, ...groups[i].items] : [...groups[i].items, ...g.items] } : g))
    .filter((_, j) => j !== i)
}

export function moveGroup(groups: readonly RailGroup[], groupId: string, delta: -1 | 1): RailGroup[] {
  const i = groups.findIndex(g => g.id === groupId)
  const j = i + delta
  if (i < 0 || j < 0 || j >= groups.length) return [...groups]
  const next = [...groups]
  ;[next[i], next[j]] = [next[j], next[i]]
  return next
}

/**
 * Persist an edit of the visible groups, optionally hiding or un-hiding one
 * id. Merges into a FRESH read, so another tab's write is not overwritten and
 * unavailable ids keep their stored slots (see `applyVisibleOrder`).
 */
export function commitRail(next: readonly RailGroup[], hidden?: { add?: string; remove?: string }): boolean {
  const stored = applyVisibleOrder(readRailLayout(), next)
  const set = new Set(stored.hidden)
  if (hidden?.add) set.add(hidden.add)
  if (hidden?.remove) set.delete(hidden.remove)
  return writeRailLayout({ ...stored, hidden: [...set].sort() })
}

/** Without `id`'s own label; drops the map when it empties. */
function withoutLabel(layout: RailLayout, id: string): RailLayout {
  if (!layout.labels || !(id in layout.labels)) return layout
  const { [id]: _gone, ...rest } = layout.labels
  const { labels: _old, ...base } = layout
  return Object.keys(rest).length ? { ...base, labels: rest } : base
}

/**
 * Place `id` in `groupId` before `beforeId` (at the end when `beforeId` is null
 * or not in that group), removing it from wherever it was. An unknown group
 * falls back to the first. Pure, on the STORED layout, so unavailable ids keep
 * their slots. Un-hides the id, so pinning a once-unpinned row brings it back.
 */
export function insertPin(layout: RailLayout, id: string, groupId: string, beforeId: string | null): RailLayout {
  const target = layout.groups.some(g => g.id === groupId) ? groupId : layout.groups[0]?.id
  const groups = layout.groups.map(g => {
    const items = g.items.filter(x => x !== id)
    if (g.id !== target) return { ...g, items }
    const at = beforeId ? items.indexOf(beforeId) : -1
    return { ...g, items: at < 0 ? [...items, id] : [...items.slice(0, at), id, ...items.slice(at)] }
  })
  return { ...layout, version: RAIL_LAYOUT_VERSION, groups, hidden: layout.hidden.filter(x => x !== id) }
}

/** Remove an entity pin outright: it leaves every group and loses its label. */
export function removePin(layout: RailLayout, id: string): RailLayout {
  return withoutLabel({ ...layout, groups: layout.groups.map(g => ({ ...g, items: g.items.filter(x => x !== id) })) }, id)
}

/** Set or clear (empty / null) a row's rail-only name. */
export function setRailLabel(layout: RailLayout, id: string, label: string | null): RailLayout {
  const t = (label ?? '').trim()
  if (!t) return withoutLabel(layout, id)
  return { ...layout, labels: { ...(layout.labels ?? {}), [id]: t } }
}

/** Read-modify-write the stored layout with a pure edit. */
export function updateRail(edit: (layout: RailLayout) => RailLayout): boolean {
  return writeRailLayout(edit(readRailLayout()))
}

/** Drop every customization: the rail returns to `DEFAULT_RAIL_LAYOUT`. */
export function resetRailLayout(): void {
  safeRemoveItem(RAIL_LAYOUT_KEY)
  if (typeof window !== 'undefined') window.dispatchEvent(new Event(RAIL_LAYOUT_CHANGED_EVENT))
}

/** React view of the stored layout, live for same-tab and cross-tab writes. */
export function useRailLayout(): RailLayout {
  const [layout, setLayout] = useState<RailLayout>(() => readRailLayout())
  useEffect(() => {
    const reread = () => setLayout(readRailLayout())
    const onStorage = (e: StorageEvent) => { if (e.key === RAIL_LAYOUT_KEY) reread() }
    window.addEventListener(RAIL_LAYOUT_CHANGED_EVENT, reread)
    window.addEventListener('storage', onStorage)
    return () => {
      window.removeEventListener(RAIL_LAYOUT_CHANGED_EVENT, reread)
      window.removeEventListener('storage', onStorage)
    }
  }, [])
  return layout
}
