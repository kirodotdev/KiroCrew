/**
 * The desktop shapes an app's manifest `desktop` block asks the desktop shell for,
 * read for the trust prompt (`TrustAppDesktopBox`). Each is a yes or no, never the
 * app's own words about it.
 */

/** The desktop shapes a manifest declares, each a yes or no. */
export interface DesktopRequest {
  /** A see-through window over the display the user is on. */
  overlay: boolean
  /** A see-through window over every display, with the app's avatar on one. */
  spansDisplays: boolean
  /** A small window that stays above the user's other windows. */
  panel: boolean
  /** A page that is never shown and runs while the app is on. */
  background: boolean
  /** At least one global keyboard shortcut. */
  shortcuts: boolean
}

/** The desktop request in a raw manifest, or `undefined` when it declares no window. */
export function desktopRequestOf(manifest: unknown): DesktopRequest | undefined {
  if (!manifest || typeof manifest !== 'object') return undefined
  const desktop = (manifest as { desktop?: unknown }).desktop
  if (!desktop || typeof desktop !== 'object') return undefined
  const { windows, shortcuts } = desktop as { windows?: unknown; shortcuts?: unknown }
  if (!Array.isArray(windows) || windows.length === 0) return undefined
  const shapes = windows.map((w) => (w && typeof w === 'object' ? w as { kind?: unknown; displays?: unknown } : {}))
  return {
    overlay: shapes.some((w) => w.kind === 'overlay' && w.displays !== 'all'),
    spansDisplays: shapes.some((w) => w.kind === 'overlay' && w.displays === 'all'),
    panel: shapes.some((w) => w.kind === 'panel'),
    background: shapes.some((w) => w.kind === 'background'),
    shortcuts: Array.isArray(shortcuts) && shortcuts.length > 0,
  }
}
