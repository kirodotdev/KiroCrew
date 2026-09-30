// Side-effect: registers every built-in surface in the registry. MUST run
// before `getBuiltinSurfaces()` is invoked below to compute `NAV_ITEMS`.
import '../../surfaces/builtins'
import { getBuiltinSurfaces } from '../../surfaces/registry'

/**
 * Built-in nav items. Sourced from the surface registry (see
 * `src/surfaces/builtins.tsx`) so each item is registered exactly once and
 * its badge wiring lives next to its registration. Adding a new built-in
 * destination is a single registry entry — no code change needed here.
 *
 * Shape and order are preserved for back-compat with the shell's rail
 * (group filtering, sortedAppGroup merge with dynamic apps, settings lookup).
 */
/**
 * Static nav descriptors. `label` is intentionally NOT resolved here — this is a
 * module-level constant, so a translated string baked in at import time would be
 * frozen in whatever language happened to be active then (and the rail would
 * stay English while the rest of the dashboard switched). `labelKey` is carried
 * through and resolved per render via `surfaceLabel()`.
 */
export const NAV_ITEMS = getBuiltinSurfaces().map(s => ({
  path: s.route,
  id: s.navId,
  label: s.label,
  labelKey: s.labelKey,
  group: s.group,
  icon: s.icon,
  // Carried through so the rail can drop a preview-gated surface at RENDER
  // time. It cannot be filtered out here: this constant is evaluated once at
  // module load, so a flag flipped later would not take effect until a reload.
  previewFlag: s.previewFlag,
  // Same reason, for the same reason: whether a promotable sub-item occupies a
  // rail row is a localStorage read that changes without a reload.
  pinnable: s.pinnable,
}))
