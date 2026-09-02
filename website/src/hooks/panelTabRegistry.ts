/**
 * Side-panel tab contributions.
 *
 * Turns the `contributes.panelTabs` declarations of the installed apps into the
 * body-owning tabs the chat side panel can open. The host reads the answer as
 * data — it never names a specific app — and mounts each tab's `entry` through
 * the same in-process ESM app host `ui.pages` use (an `AppHost`), so a second
 * tab-contributing app costs no host change and no app code crosses the boundary.
 *
 * Kept as a pure function over a locally pinned subset of `GET /api/apps` (same
 * reason `overlaySlots.ts` and `appNav.ts` pin their own records): a field this
 * derivation depends on cannot quietly change shape underneath it. The manifest
 * is the source of truth, so an installed app declares a tab — the edition (and
 * the core) register nothing.
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

/** One `contributes.panelTabs[]` entry as it arrives from the manifest. */
export interface AppPanelTabDecl {
  id?: string
  title?: string
  menuLabel?: string
  menuDescription?: string
  icon?: string
  entry?: string
}

/** The subset of `GET /api/apps` this module reads. */
export interface PanelTabAppRecord {
  name: string
  enabled?: boolean
  manifest?: {
    contributes?: { panelTabs?: AppPanelTabDecl[] }
  }
}

/**
 * A resolved, ready-to-render side-panel tab. `kind` is the persisted tab kind
 * `app:<appName>:<id>`. `title`/`icon`/`entry` are re-read from the manifest on
 * every resolve, so a manifest change is reflected without rewriting a persisted
 * tab (only `{ kind, appName, tabId }` is persisted).
 */
export interface PanelTabDescriptor {
  kind: `app:${string}`
  appName: string
  tabId: string
  title: string
  menuLabel: string
  menuDescription?: string
  /** Lucide icon NAME (resolved to a glyph by the consumer, not a ReactNode —
   *  the descriptor crosses no React boundary). */
  icon: string
  /** ESM entry relative to the app root that the AppHost mounts as the body. */
  entry: string
}

const KIND_PREFIX = 'app:'

/**
 * Most tabs one app may contribute, mirroring `_MAX_PANEL_TABS_PER_APP` in
 * `apps/manifest.py`. Enforced on this side too: a cap only the manifest checks
 * would let a hand-edited `app.json` render an unbounded strip, and one only the
 * reader checks silently truncates without telling the app author.
 */
export const MAX_PANEL_TABS_PER_APP = 8

/** The persisted tab kind for a contributed tab. Exported so the reader and the
 *  persistence layer derive it the same way. */
export function panelTabKind(appName: string, tabId: string): `app:${string}` {
  return `${KIND_PREFIX}${appName}:${tabId}`
}

/** True for a kind produced by {@link panelTabKind} — an app-contributed,
 *  re-mountable tab, as opposed to a core kind or the ephemeral `app` MCP tab.
 *  A type predicate, so a caller that narrows on it can index a `Record` keyed by
 *  the built-in kinds without a cast. */
export function isPanelTabKind(kind: string): kind is `app:${string}` {
  return kind.startsWith(KIND_PREFIX)
}

/**
 * Resolve the contributed side-panel tabs across the installed apps.
 *
 * Only ENABLED apps contribute: an app's enable state is the user's opt-in. Apps
 * are considered in name order so the strip order is stable across gateway
 * restarts rather than depending on a directory scan.
 *
 * A declaration missing id/title/menuLabel/entry, or a duplicate kind, is warned
 * and skipped rather than applied — each would otherwise fail as a silent absence
 * or an unrenderable tab. Tabs past {@link MAX_PANEL_TABS_PER_APP} are warned and
 * dropped for the same reason. These declarations arrive from installed app
 * manifests (a third party, or a hand-edited app.json), so they warn rather than
 * throw: a bad manifest must never take the dashboard down.
 */
export function resolvePanelTabs(apps: readonly PanelTabAppRecord[]): PanelTabDescriptor[] {
  const out: PanelTabDescriptor[] = []
  const seen = new Set<string>()
  // `api.listApps` is not guaranteed to hand back an array — `normalizeInstalledApps`
  // returns its input untouched when it is not one — and this runs inside a `useMemo`
  // during render, so spreading a non-array would throw `apps is not iterable` and
  // take the whole chat page down. Same guard the sibling contributed-commands
  // resolver makes, and the reason this module warns rather than throws throughout.
  const list = Array.isArray(apps) ? apps : []
  // Byte order, not a locale comparison: `name` is the `/api/apps` identifier, and
  // the strip order must be the same answer for every viewer.
  const sorted = [...list].sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
  for (const app of sorted) {
    if (!app.enabled) continue
    // A hand-edited app.json can carry `"panelTabs": {}` — an object satisfies the
    // optional-chain and the `?? []` default, then `.slice` is not a function. The
    // manifest normalizer does not coerce `contributes`, so the shape is checked here.
    const rawDeclared = app.manifest?.contributes?.panelTabs
    const declared = Array.isArray(rawDeclared) ? rawDeclared : []
    if (declared.length > MAX_PANEL_TABS_PER_APP) {
      // eslint-disable-next-line no-console -- a dropped contribution is invisible otherwise
      console.warn(`[panelTabs] app ${app.name} declares ${declared.length} tabs; only the first ${MAX_PANEL_TABS_PER_APP} are used`)
    }
    for (const decl of declared.slice(0, MAX_PANEL_TABS_PER_APP)) {
      const tabId = decl.id
      if (!tabId || !decl.title || !decl.menuLabel || !decl.entry) {
        // eslint-disable-next-line no-console -- a refused contribution is invisible otherwise
        console.warn(`[panelTabs] app ${app.name} tab ${tabId ?? '(no id)'} missing id/title/menuLabel/entry; ignoring`)
        continue
      }
      const kind = panelTabKind(app.name, tabId)
      if (seen.has(kind)) {
        // eslint-disable-next-line no-console -- a refused contribution is invisible otherwise
        console.warn(`[panelTabs] app ${app.name} declares duplicate tab ${tabId}; ignoring`)
        continue
      }
      seen.add(kind)
      out.push({
        kind,
        appName: app.name,
        tabId,
        title: decl.title,
        menuLabel: decl.menuLabel,
        menuDescription: decl.menuDescription,
        icon: decl.icon ?? '',
        entry: decl.entry,
      })
    }
  }
  return out
}

/** Pure lookup over an already-resolved list. */
export function panelTabDescriptor(
  kind: string,
  tabs: readonly PanelTabDescriptor[],
): PanelTabDescriptor | undefined {
  return tabs.find(d => d.kind === kind)
}

/**
 * The contributed side-panel tabs from the installed apps. Reuses the shared
 * `['apps']` query so it costs no extra fetch and reflects enable/disable and
 * install/uninstall the same moment every other apps consumer does.
 */
export function usePanelTabDescriptors(): PanelTabDescriptor[] {
  const { data } = useQuery({ queryKey: ['apps'], queryFn: api.listApps })
  return useMemo(() => resolvePanelTabs((data ?? []) as unknown as PanelTabAppRecord[]), [data])
}
