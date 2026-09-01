/**
 * File-menu rows contributed by installed apps.
 *
 * This is what lets a row in the file-editor overflow menu, the workspace-tree
 * context menu, or the folder panel live OUTSIDE this repository. An app declares
 * `contributes.fileMenuItems` in its manifest; this module turns that declaration
 * into rows the host renders, and activating one POSTs the file's PATH to the app's
 * own endpoint. Nothing here executes app code — a contribution is data, and the
 * host is the only thing that acts on it.
 *
 * **Everything below re-validates what the backend already checks**, for the reason
 * `contributedCommands.ts` gives: an unknown top-level manifest key reaches this
 * dashboard through the manifest's `extra` bucket without passing any schema, so an
 * app installed by an older gateway — or one whose `app.json` was edited in place —
 * can put an arbitrary object on this path. Manifest data from a third party is
 * untrusted input.
 *
 * A bad declaration is SKIPPED with a warning, never thrown: a malformed app must not
 * be able to take a file menu down for every other app on the instance.
 *
 * The rows are read off the SHARED `['apps']` query rather than an endpoint of their
 * own. `contributes` already reaches the dashboard on that response, so a second
 * request would buy nothing and cost a per-session round trip plus a second
 * `list_apps()` disk walk.
 *
 * Kept pure and dependency-light so the rules are pinned by unit test rather than by
 * reading a component. Icons stay STRINGS here; resolving one to a glyph is the
 * renderer's job.
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type FileMenuContext, type FileMenuSurface } from '../api/client'
import AppIcon from '../components/AppIcon'

/** The subset of `GET /api/apps` this module reads. */
export interface FileMenuAppRecord {
  name: string
  enabled?: boolean
  manifest?: {
    contributes?: {
      fileMenuItems?: unknown
    }
  }
}

/** A validated row, ready to render. */
export interface ContributedFileMenuItem {
  /** Row id, namespaced by the contributing app so two apps may use one id. */
  id: string
  /** The app that contributed it — namespaces the id and scopes the endpoint. */
  app: string
  /** App-owned literal; the host has no catalog key for a row it does not know. */
  label: string
  /** Host glyph name; the renderer maps it, and an unknown name falls back. */
  icon: string
  endpoint: string
  surfaces: FileMenuSurface[]
  when: { extensions: string[]; kinds: ('file' | 'dir')[] }
}

/** The node a row is being considered for. */
export interface FileMenuNode {
  path: string
  kind: 'file' | 'dir'
}

/**
 * Mirrors `_MAX_FILE_MENU_ITEMS_PER_APP` in `apps/manifest.py`. A cap only the
 * manifest enforces is not a cap: the app would install clean and the menu would then
 * drop the overflow with no error its author can see.
 */
const MAX_FILE_MENU_ITEMS_PER_APP = 10
/** Mirrors `_MAX_TITLE` in `apps/manifest.py`. */
const MAX_LABEL = 120
/** Mirrors `_COMMAND_SLUG_RE` in `apps/manifest.py`, which contributed ids share. */
const ITEM_ID_RE = /^[a-z0-9][a-z0-9-]*$/
/** Mirrors `FILE_MENU_SURFACES` in `apps/manifest.py`. */
const SURFACES = new Set<FileMenuSurface>(['file-overflow', 'tree-context', 'folder-row'])
/** Mirrors `_FILE_MENU_KINDS` in `apps/manifest.py`. */
const KINDS = new Set(['file', 'dir'])

function str(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

function warnSkipped(appName: string, id: unknown, reason: string): void {
  // eslint-disable-next-line no-console -- a refused contribution is invisible otherwise
  console.warn(
    `[fileMenuContributions] app ${appName}: skipping contributed row ${String(id)} — ${reason}`,
  )
}

/**
 * Whether an app-declared endpoint routes inside that app's own namespace.
 *
 * Mirrors `app_endpoint_allowed` in `apps/manifest.py`, which refuses a bad endpoint at
 * INSTALL — this copy is the dispatch-time floor, because the row that gets POSTed is
 * the one in this list and a manifest that reached the dashboard through `extra` never
 * met the install check. The trailing slash on the prefix is what stops a sibling app
 * (`/api/apps/foobar/x`) from passing `foo`'s allowlist.
 */
function endpointAllowed(appName: string, endpoint: string): boolean {
  if (!appName || !endpoint) return false
  let decoded = endpoint
  try {
    decoded = decodeURIComponent(endpoint)
  } catch {
    // A malformed percent-escape cannot be reasoned about; refuse rather than guess.
    return false
  }
  if (decoded.includes('..')) return false
  return (decoded.replace(/\/+$/, '') + '/').startsWith(`/api/apps/${appName}/`)
}

function readItem(app: FileMenuAppRecord, raw: unknown): ContributedFileMenuItem | null {
  if (typeof raw !== 'object' || raw === null) {
    warnSkipped(app.name, raw, 'entry is not an object')
    return null
  }
  const obj = raw as Record<string, unknown>
  const id = str(obj.id)
  if (!ITEM_ID_RE.test(id)) {
    warnSkipped(app.name, obj.id, 'id must be lowercase alphanumeric with dashes')
    return null
  }
  const label = str(obj.label)
  if (!label) {
    warnSkipped(app.name, id, 'missing label')
    return null
  }
  if (label.length > MAX_LABEL) {
    warnSkipped(app.name, id, `label exceeds ${MAX_LABEL} characters`)
    return null
  }
  const endpoint = str(obj.endpoint)
  if (!endpointAllowed(app.name, endpoint)) {
    warnSkipped(app.name, id, `endpoint must route under /api/apps/${app.name}/`)
    return null
  }
  const rawSurfaces = obj.surfaces
  if (!Array.isArray(rawSurfaces)) {
    warnSkipped(app.name, id, 'surfaces must be an array')
    return null
  }
  const surfaces = rawSurfaces.filter(
    (s): s is FileMenuSurface => typeof s === 'string' && SURFACES.has(s as FileMenuSurface),
  )
  if (surfaces.length === 0) {
    warnSkipped(app.name, id, 'names no known surface')
    return null
  }
  // `when` is advisory rather than load-bearing: a malformed filter narrows nothing, so
  // it degrades to "no constraint" instead of dropping the row. The manifest reports the
  // same input as an error, which is where an author finds out.
  const rawWhen = typeof obj.when === 'object' && obj.when !== null ? (obj.when as Record<string, unknown>) : {}
  const extensions = Array.isArray(rawWhen.extensions)
    ? rawWhen.extensions.filter((e): e is string => typeof e === 'string' && e.length > 0)
        .map(e => e.toLowerCase().replace(/^\.+/, ''))
    : []
  const kinds = Array.isArray(rawWhen.kinds)
    ? rawWhen.kinds.filter((k): k is 'file' | 'dir' => typeof k === 'string' && KINDS.has(k))
    : []
  return { id, app: app.name, label, icon: str(obj.icon), endpoint, surfaces, when: { extensions, kinds } }
}

/**
 * Every valid row contributed by the ENABLED installed apps.
 *
 * Disabled apps contribute nothing: the enable state is the reader's switch for the
 * whole app, and a row that still POSTed from a disabled app would make that switch a
 * lie.
 */
export function contributedFileMenuItems(
  apps: readonly FileMenuAppRecord[],
): ContributedFileMenuItem[] {
  const out: ContributedFileMenuItem[] = []
  for (const app of apps) {
    if (!app.enabled) continue
    if (!app.name) {
      warnSkipped('(unnamed)', '(all)', 'app record has no name')
      continue
    }
    const raw = app.manifest?.contributes?.fileMenuItems
    if (raw === undefined || raw === null) continue
    if (!Array.isArray(raw)) {
      warnSkipped(app.name, '(all)', 'contributes.fileMenuItems is not an array')
      continue
    }
    // Sliced BEFORE the loop so the cap bounds the WORK, not just the output: a manifest
    // with fifty thousand malformed entries would otherwise run that many validations
    // and `console.warn` calls synchronously on the thread drawing the menu.
    const seen = new Set<string>()
    for (const entry of raw.slice(0, MAX_FILE_MENU_ITEMS_PER_APP)) {
      const item = readItem(app, entry)
      if (!item) continue
      if (seen.has(item.id)) {
        warnSkipped(app.name, item.id, 'duplicate id')
        continue
      }
      seen.add(item.id)
      out.push(item)
    }
  }
  return out
}

/**
 * The rows enabled apps contribute to one surface.
 *
 * A pure cache subscriber (`enabled: false`), like the Command Bar's use of the same
 * query: it re-renders when the shell's own `['apps']` fetch lands and never issues a
 * request of its own, so mounting this from three menus costs nothing. With no
 * contributing app it returns `[]`, so a stock build renders nothing and is inert.
 */
export function useFileMenuItems(surface: FileMenuSurface): ContributedFileMenuItem[] {
  const { data: apps } = useQuery({
    queryKey: ['apps'],
    queryFn: () => api.listApps(),
    enabled: false,
  })
  return useMemo(
    () =>
      contributedFileMenuItems((apps ?? []) as FileMenuAppRecord[]).filter(it =>
        it.surfaces.includes(surface),
      ),
    [apps, surface],
  )
}

/**
 * Whether a row's declarative `when` predicate admits this node. An empty field is "no
 * constraint on that axis"; present fields AND together. A path with no dot has no
 * extension, so an extension filter excludes it.
 */
export function fileMenuItemMatches(
  item: ContributedFileMenuItem,
  node: FileMenuNode,
): boolean {
  const { extensions, kinds } = item.when
  if (kinds.length && !kinds.includes(node.kind)) return false
  if (extensions.length) {
    const base = node.path.split('/').pop() ?? ''
    // A leading-dot name (`.gitignore`) is a name, not an extension.
    const dot = base.lastIndexOf('.')
    if (dot <= 0) return false
    if (!extensions.includes(base.slice(dot + 1).toLowerCase())) return false
  }
  return true
}

/** Surface rows already filtered against a node's `when` predicate. */
export function visibleFileMenuItems(
  items: readonly ContributedFileMenuItem[],
  node: FileMenuNode,
): ContributedFileMenuItem[] {
  return items.filter(it => fileMenuItemMatches(it, node))
}

/**
 * POST a row's activation to the app that declared it.
 *
 * Only the PATH crosses the boundary — never file CONTENT. An app that needs the bytes
 * reads them through a route its own `permissions` cover, which is where the reader's
 * consent for that access is recorded; handing content to every contributed row would
 * grant it silently to any app that declares one.
 */
export function invokeFileMenuItem(
  item: ContributedFileMenuItem,
  ctx: FileMenuContext,
): void {
  void api.invokeFileMenuItem(item, ctx).catch((err: unknown) => {
    // eslint-disable-next-line no-console -- a failed dispatch is invisible otherwise
    console.warn(`[fileMenuContributions] ${item.app}: row ${item.id} failed —`, err)
  })
}

/**
 * Render a contributed row's icon. `icon` is a manifest string, resolved through the
 * same `AppIcon` allowlist app icons use (an unknown name falls back to a generic
 * glyph); an empty icon renders nothing.
 */
export function FileMenuItemIcon({ name }: { name?: string }) {
  if (!name) return null
  return <AppIcon icon={name} size={14} />
}

/**
 * Hover-revealed action buttons for the `folder-row` surface — one icon button per
 * already-`when`-filtered row. Each POSTs the node's path to the app's endpoint; the
 * click is stopped so it does not also activate the row. Renders nothing when no row
 * matches, so a stock build's rows are unchanged.
 */
export function FolderRowActions({
  items,
  node,
}: {
  items: readonly ContributedFileMenuItem[]
  node: FileMenuNode
}) {
  if (items.length === 0) return null
  return (
    <span className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
      {items.map(item => (
        <button
          key={`${item.app}:${item.id}`}
          type="button"
          aria-label={item.label}
          title={item.label}
          className="flex items-center justify-center w-[22px] h-[22px] rounded text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none cursor-pointer"
          onClick={e => {
            e.stopPropagation()
            invokeFileMenuItem(item, { surface: 'folder-row', path: node.path, kind: node.kind })
          }}
        >
          <FileMenuItemIcon name={item.icon} />
        </button>
      ))}
    </span>
  )
}
