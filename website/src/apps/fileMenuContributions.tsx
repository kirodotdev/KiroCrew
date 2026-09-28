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
import { MoreHorizontal } from 'lucide-react'
import { api, type FileMenuContext, type FileMenuSurface } from '../api/client'
import AppIcon from '../components/AppIcon'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from '../components/ui/dropdown-menu'
import { i18nT } from '../i18n/t'
import { store } from '../store'
import { HOVER_NONE_ACTIONS_ROW_CLS } from '../utils/touchActions'

/**
 * How a host surface receives a dispatch failure — the same shape `MarkdownPanel`'s
 * `ReportError` has, so a caller passes the reporter it already holds for its other row
 * actions instead of standing up a second error surface for contributed rows.
 */
export type ReportFileMenuError = (message: string) => void

/** The subset of `GET /api/apps` this module reads. */
export interface FileMenuAppRecord {
  name: string
  /** Human name from the manifest; the attribution prefers it over `name`. */
  displayName?: string
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
  /**
   * The attribution RENDERED next to `label`, so a contributed row cannot read as a
   * core one. `label` is app-owned and validated for presence and length only -- it is
   * never checked against the core vocabulary -- so an app may legitimately, or
   * deceptively, call its row "Download". Without a visible owner the reader cannot
   * tell that clicking it hands the file's path to a third party.
   *
   * Mirrors `appLabel` in `apps/command-bar/contributedCommands.ts`, whose own note
   * records why this must never degrade to empty: a row with no attribution renders
   * character-for-character like a builtin. So it steps down -- `displayName`, else
   * `name`, else that name clipped to `MAX_LABEL` -- and is always non-empty for a row
   * that survives validation.
   */
  appLabel: string
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

// Core-owned route segments live in their own module (`coreAppRoutes.ts`): they are
// URL path segments rather than copy, and the i18n lint releases that FILE, which it
// could not do for this one without also releasing the app-actions label and every
// other string here. Imported for `endpointAllowed` below and re-exported so callers
// and tests keep one import site.
import { CORE_APP_ROUTE_SEGMENTS, RESERVED_APP_PATH_SEGMENTS } from './coreAppRoutes'
export { CORE_APP_ROUTE_SEGMENTS }

/**
 * A contributed row's label AND its attribution, so all three surfaces render provenance
 * the same way and none of them can forget it.
 *
 * `label` is app-owned and never checked against the core vocabulary, so an app may name
 * its row "Download" and sit one separator below the real Download. The attribution is
 * what makes that visible: clicking a contributed row POSTs the file's PATH to a third
 * party, which a reader can only consent to if they can see who owns the row.
 *
 * Rendered as a muted trailing span rather than a second line, because all three hosts
 * are single-line menu rows. It carries no connecting copy ("from …") on purpose: that
 * would be a catalog string, while `appLabel` is app-owned text no catalog can translate.
 * Deliberately NOT `aria-hidden` — the provenance is the security-relevant part of the
 * row, so a screen reader must announce it too.
 */
export function FileMenuItemLabel({ item }: { item: ContributedFileMenuItem }) {
  return (
    <>
      {/* `min-w-0` alongside `truncate`: a flex child's default `min-width: auto` refuses
          to shrink below its content, so `truncate` alone never engages inside a flex row
          and the text sets the row's width instead of being clipped by it. */}
      <span className="truncate min-w-0">{item.label}</span>
      {/* A literal space, not just the margin: the accessible NAME is computed from text
          content, so without it a screen reader announces "Send to storedoc-store". */}
      {' '}
      {/* HOST-owned framing around the app's identifier, from the catalog. The bare
          identifier is unspoofable as a host WORD (kebab-case only), but nothing stops an
          app installing as `kiro-crew`, and on its own that still reads as core. The
          word around it is core's, in the reader's language, so the row states who owns
          it rather than leaving the reader to infer it from a slug.
          Shrinkable and truncating rather than `shrink-0`: `KEBAB_RE` bounds the app
          name's alphabet and not its length, so a 120-character one (the `MAX_LABEL`
          clip) would otherwise widen the menu past a 320px viewport and push the rows
          off-screen. Truncated provenance still identifies the app; an unreachable row
          does not. */}
      <span className="ml-2 shrink truncate min-w-0 text-[11px] text-muted">
        {i18nT('components.fileMenuContributions.contributed_by', { app: item.appLabel })}
      </span>
    </>
  )
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

function warnContributionSkipped(appName: string, id: unknown, reason: string): void {
  // eslint-disable-next-line no-console -- a refused contribution is invisible otherwise
  console.warn(
    `[fileMenuContributions] app ${appName}: skipping contributed row ${String(id)} — ${reason}`,
  )
}

/** The row survives; only the LENGTH of its attribution is gone. Distinct from a skip. */
function warnAttributionClipped(appName: string, id: unknown, reason: string): void {
  // eslint-disable-next-line no-console -- otherwise the app author has no way to notice
  console.warn(
    `[fileMenuContributions] app ${appName}: contributed row ${String(id)} renders with a clipped attribution — ${reason}`,
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
  // A reserved segment is never an app's own namespace, whatever the app is called:
  // `/api/apps/registry/install` is a shared literal route mounted before the
  // `/api/apps/{name}` catch-all, and `install` is not a per-app lifecycle segment, so
  // the reserved-segment set below would not catch it. Mirrors the same refusal in
  // `app_endpoint_allowed`.
  if (RESERVED_APP_PATH_SEGMENTS.has(appName)) return false
  let decoded = endpoint
  try {
    decoded = decodeURIComponent(endpoint)
  } catch {
    // A malformed percent-escape cannot be reasoned about; refuse rather than guess.
    return false
  }
  // Character ALLOWLIST, mirroring `_ENDPOINT_ALLOWED_RE` in `apps/manifest.py`, which
  // carries the reasoning: a blocklist loses this one character at a time, because the
  // defect class is "a character the URL parser reinterprets AFTER this check accepted
  // it" — U+0009/000A/000D are stripped as `fetch` builds the request, and `\` is a path
  // separator for http(s), so `/api/apps/foo/.\uninstall` normalizes onto core's
  // uninstall route. Unreserved characters plus the delimiters a real endpoint needs;
  // `\`, controls, DEL and space are outside it without being named. Applied after
  // `decodeURIComponent` so `%5c` is judged as the character it becomes. The Python
  // mirror anchors with `\Z` rather than `$` because Python's `$` also matches before a
  // trailing newline; JavaScript's `$` without `m` is a true end anchor, so `$` is
  // correct HERE and the two patterns are deliberately not character-identical.
  if (!/^[A-Za-z0-9\-._~/?#=&+,:@!$'()*;]+$/.test(decoded)) return false
  if (decoded.includes('..')) return false
  const path = decoded.replace(/\/+$/, '')
  // Stands in for `posixpath.normpath` plus the equality test the Python side runs: a `.`
  // segment or a doubled slash is resolved away by the URL parser BEFORE the request is
  // routed, so `/api/apps/foo/./uninstall` reads as an app route here and still arrives
  // at core's uninstall handler. The leading empty segment is the path's own root slash.
  if (path.split('/').slice(1).some(seg => seg === '' || seg === '.')) return false
  // BOTH documented app namespaces, mirroring `app_endpoint_allowed` in
  // `apps/manifest.py` AS THE FILE-MENU CALLER INVOKES IT
  // (`allow_proxy_namespace=True`). This mirror has exactly one consumer, so the
  // per-caller flag the Python side carries would be a parameter with a single value
  // here; the flag exists there because that check is shared with the publish-provider
  // registry, which keeps the narrower in-gateway shape.
  //
  // Which namespace an app owns is decided by whether it declares
  // `backend.entryPoint` (its own process, reverse-proxied at `/apps/<app>/api/`) or only
  // `backend.hooks.routes` (in-gateway at `/api/apps/<app>/`), never by the app. The
  // reserved core segments apply to the in-gateway prefix ALONE — the proxy namespace
  // forwards wholesale into the app's process, so core serves nothing under it.
  for (const [prefix, reserved] of [
    [`/api/apps/${appName}/`, CORE_APP_ROUTE_SEGMENTS],
    [`/apps/${appName}/api/`, new Set<string>()],
  ] as const) {
    if (!(path + '/').startsWith(prefix)) continue
    // First segment below the app's prefix, cut at a query or fragment for the reason the
    // Python mirror gives: the router matches on the path alone, so `/uninstall?x=1` still
    // reaches core's handler. The bare namespace root yields '', which no core route claims.
    const segment = path.slice(prefix.length).split(/[/?#]/, 1)[0]
    return !reserved.has(segment)
  }
  return false
}

function readItem(app: FileMenuAppRecord, raw: unknown): ContributedFileMenuItem | null {
  if (typeof raw !== 'object' || raw === null) {
    warnContributionSkipped(app.name, raw, 'entry is not an object')
    return null
  }
  const obj = raw as Record<string, unknown>
  const id = str(obj.id)
  if (!ITEM_ID_RE.test(id)) {
    warnContributionSkipped(app.name, obj.id, 'id must be lowercase alphanumeric with dashes')
    return null
  }
  const label = str(obj.label)
  if (!label) {
    warnContributionSkipped(app.name, id, 'missing label')
    return null
  }
  if (label.length > MAX_LABEL) {
    warnContributionSkipped(app.name, id, `label exceeds ${MAX_LABEL} characters`)
    return null
  }
  const endpoint = str(obj.endpoint)
  if (!endpointAllowed(app.name, endpoint)) {
    warnContributionSkipped(app.name, id, `endpoint must route under /api/apps/${app.name}/`)
    return null
  }
  const rawSurfaces = obj.surfaces
  if (!Array.isArray(rawSurfaces)) {
    warnContributionSkipped(app.name, id, 'surfaces must be an array')
    return null
  }
  const surfaces = rawSurfaces.filter(
    (s): s is FileMenuSurface => typeof s === 'string' && SURFACES.has(s as FileMenuSurface),
  )
  if (surfaces.length === 0) {
    warnContributionSkipped(app.name, id, 'names no known surface')
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
  // The attribution, and it comes from `name` -- NEVER `displayName`.
  //
  // `displayName` is free text the app chooses, so as provenance it can claim to be
  // anything, the host included: a manifest saying `displayName: "Kiro Crew"` rendered a
  // row that read as native, and the reader clicked it believing core was handling their
  // file. `name` is the install identity and `KEBAB_RE` constrains it to
  // `[a-z0-9]` plus single hyphens, so it cannot spell a host word with a space or a
  // capital. It is also unique across installed apps, which `displayName` is not.
  //
  // Clipped rather than refused: `KEBAB_RE` bounds the alphabet and not the length, and
  // dropping the row instead would hide every action of an app whose own fields were all
  // fine.
  const rawAppLabel = app.name
  const appLabel = rawAppLabel.length > MAX_LABEL ? app.name.slice(0, MAX_LABEL) : rawAppLabel
  if (appLabel !== rawAppLabel) {
    warnAttributionClipped(app.name, id, `app label exceeds ${MAX_LABEL} characters`)
  }
  return {
    id,
    app: app.name,
    label,
    appLabel,
    icon: str(obj.icon),
    endpoint,
    surfaces,
    when: { extensions, kinds },
  }
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
  // Array-checked, not just nullish-checked. This reads the SHARED `['apps']` cache, so
  // the value is whatever another observer's fetch put there, and `normalizeInstalledApps`
  // returns a non-array payload untouched. Both sibling resolvers on that same key guard
  // here for the same reason: this runs inside a `useMemo` during render, so iterating a
  // non-array throws `apps is not iterable` and takes the whole host page down -- on a
  // menu the reader merely opened.
  for (const app of Array.isArray(apps) ? apps : []) {
    if (!app.enabled) continue
    if (!app.name) {
      warnContributionSkipped('(unnamed)', '(all)', 'app record has no name')
      continue
    }
    const raw = app.manifest?.contributes?.fileMenuItems
    if (raw === undefined || raw === null) continue
    if (!Array.isArray(raw)) {
      warnContributionSkipped(app.name, '(all)', 'contributes.fileMenuItems is not an array')
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
        warnContributionSkipped(app.name, item.id, 'duplicate id')
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
 *
 * A rejection is handed to `onError` rather than logged: every surface closes its menu on
 * activation, so a console-only failure leaves the reader looking at a row that did
 * nothing and no way to tell an endpoint refusal from a slow app. The host renders the
 * message through the shared `ErrorNotice` it already owns for its other row actions.
 */
export function invokeFileMenuItem(
  item: ContributedFileMenuItem,
  ctx: FileMenuContext,
  onError: ReportFileMenuError,
): void {
  // Read the owning slot at dispatch time and send it as the X-Session-Key, the same
  // way MarkdownPanel's own promote and pin actions in this very menu do.
  //
  // Without it `post` falls back to the shared `dashboard:ui` placeholder, which
  // satisfies the server's `if sk:` gate but names no actual session — so a restricted
  // (incognito) slot is never recognised as restricted and the row's write is allowed
  // through. Read HERE, in the one dispatcher all three surfaces call, rather than
  // threaded as a prop through each of them: a surface that forgot the prop would fail
  // OPEN, and silently.
  const slot = store.getState().chat.activeSlot
  void api.invokeFileMenuItem(
    item,
    ctx,
    slot ? `dashboard:${slot}` : undefined,
  ).catch((err: unknown) => {
    // The thrown value is an `ApiError` carrying the endpoint's own response text (or
    // `HTTP <status>`), so it is already the most specific thing there is to say; the
    // `String` arm covers a non-Error rejection rather than reporting an empty banner.
    onError((err as Error)?.message || String(err))
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
 * Hover-revealed actions for the `folder-row` surface, behind ONE overflow trigger.
 *
 * A row of peer buttons is capped at two (`max-two-buttons-per-row`), and the count here
 * is an app's to choose — three declaring apps would put three unranked icon buttons in a
 * listing row that a sidebar or split pane clips before it degrades. So the rows always
 * collapse into a single kebab, even for one contribution: the trigger is one control
 * whatever it holds, and a row whose control count changes when an app is installed is
 * worse than one that never does.
 *
 * `DropdownMenu` is the row-action overflow the dashboard already uses
 * (`CronRowActions`), which is what supplies the roving focus, Escape-to-close and
 * focus-restore rather than this file re-deriving them.
 */
export function FolderRowActions({
  items,
  node,
  onError,
}: {
  items: readonly ContributedFileMenuItem[]
  node: FileMenuNode
  /** Where a dispatch failure goes; the panel renders it through its own ErrorNotice. */
  onError: ReportFileMenuError
}) {
  if (items.length === 0) return null
  const label = i18nT('components.fileMenuContributions.app_actions')
  return (
    // role="presentation" keeps the wrapper out of the accessibility tree while it stops
    // the interaction from reaching the enclosing row: the row is a `role="button"` with
    // its own click and Enter/Space handlers, so opening this menu would otherwise also
    // navigate into the folder or open the file.
    <span
      role="presentation"
      onClick={e => e.stopPropagation()}
      onKeyDown={e => e.stopPropagation()}
      // Hover-reveal keeps the row quiet on a pointer device, but on a touch screen
      // there is no hover and nothing here has been focused yet, so `opacity-0` alone
      // left the ONLY trigger for these actions permanently invisible. Keyed on hover
      // CAPABILITY rather than a width breakpoint (`HOVER_NONE_ACTIONS_ROW_CLS`, the
      // repo's owned constant): a narrow desktop window still hovers, and a wide tablet
      // still does not.
      className={`flex items-center opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity ${HOVER_NONE_ACTIONS_ROW_CLS}`}
    >
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            aria-label={label}
            title={label}
            // The row constant above carries `[&_button]:p-3` for the tap target, but
            // padding cannot grow a box whose width and height are fixed — so the SIZE
            // is overridden here instead, or the trigger would become visible on touch
            // and still be a 22px target.
            className="flex items-center justify-center w-[22px] h-[22px] [@media(hover:none)]:w-9 [@media(hover:none)]:h-9 rounded text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none cursor-pointer"
          >
            <MoreHorizontal size={14} className="lucide-inline" />
          </button>
        </DropdownMenuTrigger>
        {/* Capped like `RejectDropdown`: `KEBAB_RE` bounds an app name's alphabet and not
            its length, so an unconstrained menu widened past a 320px viewport and pushed
            its own rows off-screen. `calc(100vw-2rem)` is what keeps it inside the
            narrowest one. */}
        <DropdownMenuContent align="end" className="min-w-[180px] max-w-[min(420px,calc(100vw-2rem))]">
          {items.map(item => (
            <DropdownMenuItem
              key={`${item.app}:${item.id}`}
              onSelect={() =>
                invokeFileMenuItem(
                  item,
                  { surface: 'folder-row', path: node.path, kind: node.kind },
                  onError,
                )
              }
            >
              <FileMenuItemIcon name={item.icon} />
              <FileMenuItemLabel item={item} />
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
    </span>
  )
}
