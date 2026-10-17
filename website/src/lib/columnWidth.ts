// Persisted width for a drag-resizable layout column. Pure and React-free, so
// both the hook (hooks/useColumnResize) and the per-surface layout constant
// modules that feed it can import this without dragging React into them.

/** Read a persisted column width, falling back when the stored value is unusable.
 *
 * Anything outside [min, max] is discarded rather than clamped: an out-of-range
 * value means the number was written under different bounds (a stale value from
 * an older min/max, or a hand-edited key), so the caller's default is a better
 * guess than the nearest legal width. A blocked or throwing `localStorage` (private
 * mode, storage disabled) also falls back rather than propagating. */
export function loadColumnWidth(
  key: string, min: number, max: number, fallback: number,
): number {
  try {
    const raw = Number(localStorage.getItem(key))
    if (raw >= min && raw <= max) return raw
  } catch {
    /* storage unavailable — the default is still a usable layout */
  }
  return fallback
}

/** Read a persisted "column is collapsed" flag. Stored apart from the width so
 * collapsing and re-expanding returns the column to the width the user chose,
 * not the default. */
export function loadColumnCollapsed(key: string): boolean {
  try {
    return localStorage.getItem(key) === '1'
  } catch {
    return false
  }
}
