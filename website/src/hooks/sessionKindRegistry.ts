/**
 * Session-kind contributions.
 *
 * Turns the `contributes.sessionKinds` declarations of the installed apps into the
 * kinds of session the sidebar's create menu offers and whose main pane an app
 * draws. The host reads the answer as data — it never names a specific app — and
 * mounts each kind's `entry` through the same in-process ESM `AppHost` that
 * `ui.pages` and `contributes.panelTabs` use, so a second kind-contributing app
 * costs no host change.
 *
 * A pure function over a locally pinned subset of `GET /api/apps`, for the reason
 * `panelTabRegistry.ts` gives: a field this derivation reads cannot quietly change
 * shape underneath it. The `['apps']` query is NOT redeclared here; this module
 * reads it through `useInstalledApps`, the one guarded observer.
 */
import { useMemo } from 'react'
import { useInstalledApps } from './panelTabRegistry'

/** One `contributes.sessionKinds[]` entry as it arrives from the manifest. */
export interface AppSessionKindDecl {
  id?: unknown
  title?: unknown
  menuLabel?: unknown
  menuDescription?: unknown
  icon?: unknown
  entry?: unknown
  defaultAgent?: unknown
}

/** The subset of `GET /api/apps` this module reads. */
export interface SessionKindAppRecord {
  name: string
  enabled?: boolean
  /** Set when the gateway skipped this built-in at boot: it has no routes, so it is not live. */
  unavailable?: { reason: string }
  manifest?: {
    /** Paths of the agent files the app ships (`agents/<name>.json`). */
    agents?: unknown
    contributes?: { sessionKinds?: unknown }
  }
}

/**
 * A resolved, ready-to-render session kind. Only `{ app, kind }` is persisted on
 * the slot (with the server-minted `object`); everything else is re-read from the
 * manifest on every resolve, so a manifest change shows without touching a session.
 */
export interface SessionKindDescriptor {
  appName: string
  kindId: string
  title: string
  menuLabel: string
  menuDescription?: string
  /** Lucide icon NAME, resolved to a glyph by the consumer (`appIcon`). */
  icon: string
  /** ESM entry relative to the app's `ui/` directory, mounted as the main pane. */
  entry: string
  /** Agent a new session of this kind runs as; absent means the dashboard default. */
  defaultAgent?: string
  /** True when `defaultAgent` is one of the agents the app itself ships, so the
   *  create stamps the app as its owner and every turn refuses a same-name spec
   *  from anywhere else. Absent for a core agent, which an owner stamp would
   *  refuse on every turn. */
  appOwnsDefaultAgent?: true
}

/**
 * Most kinds one app may contribute, mirroring the manifest validator's cap
 * (CONTRACT §8). Enforced here too, for the reason `MAX_PANEL_TABS_PER_APP` is:
 * a hand-edited `app.json` must not grow the create menu without bound.
 */
export const MAX_SESSION_KINDS_PER_APP = 4

/** The manifest's id grammar. A kind id rides the slot binding and a test id, so
 *  one the server would refuse is refused here as well rather than offered. */
const KIND_ID_RE = /^[a-z][a-z0-9_-]{0,63}$/

const nonEmpty = (v: unknown): v is string => typeof v === 'string' && v.trim() !== ''

/**
 * The agent names an app ships, read from its manifest's `agents` paths by file
 * stem (`agents/project-conductor.json` → `project-conductor`), the convention a
 * manifest's agent file follows. The dashboard sees only the paths; the gateway
 * holds the parsed names and re-checks the owner on every turn.
 */
function shippedAgentNames(agents: unknown): Set<string> {
  const out = new Set<string>()
  if (!Array.isArray(agents)) return out
  for (const path of agents) {
    if (typeof path !== 'string') continue
    const base = path.split(/[\\/]/).pop() ?? ''
    const stem = base.endsWith('.json') ? base.slice(0, -'.json'.length) : ''
    if (stem) out.add(stem)
  }
  return out
}

/**
 * Resolve the contributed session kinds across the installed apps.
 *
 * Only ENABLED, available apps contribute (enable is the user's opt-in). Apps are
 * read in byte order of their name so the menu order is the same for every viewer
 * and across restarts. A declaration with a bad id or a missing
 * title/menuLabel/entry, or a duplicate id, is warned and skipped; kinds past
 * {@link MAX_SESSION_KINDS_PER_APP} are warned and dropped. These come from
 * third-party manifests, so this warns rather than throws: a bad manifest must
 * never take the dashboard down.
 */
export function resolveSessionKinds(apps: readonly SessionKindAppRecord[]): SessionKindDescriptor[] {
  const out: SessionKindDescriptor[] = []
  // Not guaranteed to be an array (see resolvePanelTabs); this runs inside a render memo.
  const list = Array.isArray(apps) ? apps : []
  const sorted = [...list].sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
  for (const app of sorted) {
    if (!app?.enabled || app.unavailable) continue
    const raw = app.manifest?.contributes?.sessionKinds
    const declared: unknown[] = Array.isArray(raw) ? raw : []
    if (declared.length > MAX_SESSION_KINDS_PER_APP) {
      // eslint-disable-next-line no-console -- a refused contribution is invisible otherwise
      console.warn(`[sessionKinds] app ${app.name} declares ${declared.length} session kinds; only the first ${MAX_SESSION_KINDS_PER_APP} are used`)
    }
    const seen = new Set<string>()
    const shipped = shippedAgentNames(app.manifest?.agents)
    for (const item of declared.slice(0, MAX_SESSION_KINDS_PER_APP)) {
      const decl = (item && typeof item === 'object' ? item : {}) as AppSessionKindDecl
      const id = decl.id
      if (typeof id !== 'string' || !KIND_ID_RE.test(id)) {
        // eslint-disable-next-line no-console -- a refused contribution is invisible otherwise
        console.warn(`[sessionKinds] app ${app.name} session kind ${typeof id === 'string' && id ? id : '(no id)'} has an invalid id; ignoring`)
        continue
      }
      if (!nonEmpty(decl.title) || !nonEmpty(decl.menuLabel) || !nonEmpty(decl.entry)) {
        // eslint-disable-next-line no-console -- a refused contribution is invisible otherwise
        console.warn(`[sessionKinds] app ${app.name} session kind ${id} missing title/menuLabel/entry; ignoring`)
        continue
      }
      if (seen.has(id)) {
        // eslint-disable-next-line no-console -- a refused contribution is invisible otherwise
        console.warn(`[sessionKinds] app ${app.name} declares duplicate session kind ${id}; ignoring`)
        continue
      }
      seen.add(id)
      out.push({
        appName: app.name,
        kindId: id,
        title: decl.title,
        menuLabel: decl.menuLabel,
        ...(nonEmpty(decl.menuDescription) ? { menuDescription: decl.menuDescription } : {}),
        icon: typeof decl.icon === 'string' ? decl.icon : '',
        entry: decl.entry,
        ...(nonEmpty(decl.defaultAgent) ? { defaultAgent: decl.defaultAgent } : {}),
        ...(nonEmpty(decl.defaultAgent) && shipped.has(decl.defaultAgent) ? { appOwnsDefaultAgent: true as const } : {}),
      })
    }
  }
  return out
}

/**
 * The live descriptor for a slot's `{ app, kind }` binding, or undefined when the
 * app is not installed, is disabled or unavailable, or no longer declares the kind.
 * Every one of those is the same answer to the pane: the app cannot draw it.
 */
export function sessionKindDescriptor(
  apps: readonly SessionKindAppRecord[],
  app: string,
  kind: string,
): SessionKindDescriptor | undefined {
  return resolveSessionKinds(apps).find(d => d.appName === app && d.kindId === kind)
}

/** The contributed session kinds, from the shared `['apps']` query. */
export function useSessionKindDescriptors(): SessionKindDescriptor[] {
  const { apps } = useInstalledApps()
  return useMemo(() => resolveSessionKinds(apps as unknown as SessionKindAppRecord[]), [apps])
}
