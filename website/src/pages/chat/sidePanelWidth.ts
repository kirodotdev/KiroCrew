/**
 * Which stored size the side panel is wearing.
 *
 * The panel's width is remembered PER TAB GROUP rather than once for the whole
 * panel. A browser page, a document and a list view have genuinely different
 * comfortable widths, so one shared number re-imposes the last drag on every
 * other tab: a user moving between a chat parked on Git and a chat parked on
 * Browser re-drags the handle on every switch.
 *
 * The group is derived from the active tab's `TabKind`, so no tab kind has to
 * opt in: a kind added later gets its own bucket for free. Kinds are grouped
 * only where they are the same READING SHAPE to a person — a file, a diff and
 * an artifact preview are all "a document open in the panel", and a width
 * dragged for one is the width wanted for the next.
 *
 * The bottom dock's height uses the same grouping (see `SIDE_PANEL_HEIGHT_KEY`):
 * a docked terminal and a docked diff want different heights for the same
 * reason.
 */
import { isPanelTabKind } from '../../hooks/panelTabRegistry'
import type { TabKind } from '../../hooks/usePanelTabs'

/** Base key for the right-dock width. A group suffix is appended by
 *  `sidePanelDimKey`; the bare key is the pre-grouping value, still read as the
 *  seed for a group that has none of its own (see `loadSidePanelDim`). */
export const SIDE_PANEL_WIDTH_KEY = 'mc-side-panel-width'
/** Base key for the bottom-dock height. Separate from width so flipping dock
 *  orientation restores each orientation's own last size. */
export const SIDE_PANEL_HEIGHT_KEY = 'mc-side-panel-height'

/** Bucket for a tab that has no kind of its own — a host's leading tab (the
 *  Members page's crew summary) is an identity chip, not a `TabKind`. */
export const SIDE_PANEL_DEFAULT_GROUP = 'default'

/**
 * The width bucket a tab kind draws from.
 *
 * Returns the kind itself for anything with a shape of its own, so the mapping
 * stays total over `TabKind` without enumerating it.
 */
export function sidePanelWidthGroup(kind: TabKind | null | undefined): string {
  if (!kind) return SIDE_PANEL_DEFAULT_GROUP
  // One "document open in the panel" bucket: all three are a body of content
  // read at the same comfortable measure.
  if (kind === 'file' || kind === 'diff' || kind === 'artifact') return 'doc'
  // Every app-contributed tab shares one bucket. Per-app widths would key on an
  // installed app's name, so uninstalling one would leak its key forever.
  if (kind === 'app' || isPanelTabKind(kind)) return 'app'
  return kind
}

/** Storage key for one group's size. */
export function sidePanelDimKey(base: string, group: string): string {
  return `${base}:${group}`
}

/**
 * Stored size for a group, or `fallback`.
 *
 * Falls back to the ungrouped `base` key before the default, which is what
 * migrates an existing install: the width the user had already dragged becomes
 * the starting width of every group instead of every group snapping to the
 * built-in default on upgrade.
 *
 * A value below `min` is ignored rather than clamped up — it can only come from
 * a hand-edited or stale entry, and the caller's floor is the real minimum.
 */
export function loadSidePanelDim(
  { base, group, min, fallback }: { base: string; group: string; min: number; fallback: number },
): number {
  for (const key of [sidePanelDimKey(base, group), base]) {
    const v = parseInt(localStorage.getItem(key) || '', 10)
    if (!isNaN(v) && v >= min) return v
  }
  return fallback
}
