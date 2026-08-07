import { appPageLabel } from './components/appstore/appManifest'

/**
 * Where an installed app lives in the dashboard — one definition, shared by every
 * surface that offers a way to open one.
 *
 * The left rail and the Search Everywhere palette both have to turn an app record
 * from `GET /api/apps` into a destination. Deriving that twice is how they drift:
 * a palette copy that missed the orphaned branch would send the user to a page the
 * app no longer has, and one that missed the `enabled` filter would offer to open
 * an app that is switched off. Route and eligibility therefore live here; only
 * ICON RENDERING stays per-surface, because the rail tints orphaned apps and sizes
 * its glyph for a 16px row while the palette does not.
 */

/**
 * The subset of `GET /api/apps` this module reads. Pinned locally (rather than
 * imported from a consumer) so a field this derivation depends on cannot quietly
 * change shape — the same reason `sessionsProvider` pins its own response type.
 */
export interface AppNavRecord {
  name: string
  displayName?: string
  enabled?: boolean
  origin?: string
  orphaned?: boolean
  manifest?: {
    iconUrl?: string
    ui?: {
      entry?: string
      pages?: Array<{ route: string; icon?: string; iconUrl?: string; label?: string }>
    }
  }
}

/** An app that has a place in the dashboard, with its destination resolved. */
export interface AppNavTarget {
  /** App name (the `/api/apps` key, and the `/apps/<name>` path segment). */
  name: string
  /** Route to navigate to. */
  route: string
  /** Nav id — `app-<name>` for AppHost-routed apps, the bare name for native builtins. */
  id: string
  /** Display label, localised. */
  label: string
  /** True when the app needs migration; its route points at the migrate page. */
  orphaned: boolean
  /**
   * True for a builtin app (`origin === 'builtin'`).
   *
   * Exposed because the lucide-glyph icon fallback is builtin-only: `iconName`
   * comes from the manifest, so looking it up for an INSTALLED app would render a
   * builtin glyph for any app whose `page.icon` happens to collide with one.
   */
  builtin: boolean
  /** Custom top-level icon (an absolute `/app-assets/...` path), when the manifest has one. */
  iconUrl: string
  /** Lucide glyph name from the app's first UI page, for the builtin icon lookup. */
  iconName: string
  /** Page-relative icon file (installed apps), resolved against `/apps/<name>/ui/`. */
  pageIconUrl: string
}

/**
 * Whether *app* contributes a dashboard destination at all.
 *
 * Disabled apps and apps with no UI page have nowhere to go, so no surface should
 * offer to open them.
 */
export function isAppNavigable(app: AppNavRecord): boolean {
  return !!app.enabled && (app.manifest?.ui?.pages?.length ?? 0) > 0
}

/**
 * Resolve *app* to its dashboard destination, or `null` when it has none.
 *
 * Three routing cases, in precedence order:
 *  1. **Orphaned** — the app predates a manifest migration, so it goes to the
 *     migration page rather than to a page it may no longer serve.
 *  2. **AppHost-routed** — installed apps, and builtins that ship a dynamic UI
 *     bundle (`manifest.ui.entry`) and therefore have no natively compiled
 *     surface, are served under `/apps/<name>`.
 *  3. **Native builtin** — no `ui.entry`, so its compiled surface is already
 *     registered at the page's own route.
 */
export function appNavTarget(app: AppNavRecord): AppNavTarget | null {
  if (!isAppNavigable(app)) return null
  const page = app.manifest!.ui!.pages![0]
  const isBuiltin = app.origin === 'builtin'
  const orphaned = !!app.orphaned
  const appHostRouted = !isBuiltin || !!app.manifest?.ui?.entry
  const route = orphaned
    ? `/apps/migrate/${app.name}`
    : appHostRouted
      ? `/apps/${app.name}`
      : page.route
  return {
    name: app.name,
    route,
    id: appHostRouted ? `app-${app.name}` : app.name,
    label: appPageLabel(app.name, page.label, app.displayName),
    orphaned,
    builtin: isBuiltin,
    iconUrl: app.manifest?.iconUrl || '',
    iconName: page.icon || '',
    pageIconUrl: page.iconUrl || '',
  }
}

/** Every navigable app in *apps*, in the order the API returned them. */
export function appNavTargets(apps: readonly AppNavRecord[]): AppNavTarget[] {
  const out: AppNavTarget[] = []
  for (const app of apps) {
    const target = appNavTarget(app)
    if (target) out.push(target)
  }
  return out
}
