/** "Group by Codebase": resolve each session's codebase (its project directory)
 *  and bucket the sessions by it.
 *
 *  Pure, no React and no storage. A session has no durable workdir field, so the
 *  codebase is DERIVED: the slot's own `project` when the backend set one, else
 *  the `project_dir` resolved from the folder it is filed in (nearest-ancestor
 *  wins, exactly as `resolveFolderProjectDir` does). A session that resolves to
 *  neither falls into one "No codebase" bucket, rendered last.
 *
 *  The map from slot key to codebase path is built in the sidebar shell, where
 *  both the folder tree and the slot-to-folder index are in hand, and threaded
 *  into the row model as a local key-indexed read (like `folderOf`). This module
 *  owns only the resolution rule and the bucketing, so both can be unit-tested
 *  without a render.
 */
import type { ChatFolder } from '../../types'
import { resolveFolderProjectDir } from '../../utils/folderAgent'
import { compareText } from '../../i18n/format'
import type { Slot } from './types'

/** The gateway-assigned defaults that do NOT count as a codebase. The gateway
 *  fills `slot.project` on every new slot (`slot_create.py`): with a
 *  `dashboard.default_project` set it uses that; with `dashboard.session_folder_root`
 *  set it mints a per-session directory UNDER that root; otherwise it uses the
 *  workspace default directory. None of those is a codebase the person chose, so
 *  a `slot.project` equal to the configured default, or sitting under the
 *  session-folder root, is treated as "no codebase" (and the folder fallback
 *  still applies).
 *
 *  The gateway stores `slot.project` as a realpath, but the raw `dashboard.*`
 *  settings may be `~/...` or symlinked, so comparing against them alone misses
 *  the match (and the common case: an UNSET `default_project`, where the gateway
 *  uses the realpath'd workspace dir the raw string cannot name). The config GET
 *  exposes the gateway-RESOLVED anchors (`default_project_resolved`,
 *  `session_folder_root_resolved`); the shell passes both the raw and the resolved
 *  forms, and the resolved ones are preferred when present. Everything stays a
 *  pure, testable rule. */
export interface CodebaseContext {
  /** `dashboard.default_project` (raw config string, or empty/undefined). */
  defaultProject?: string | null
  /** `dashboard.session_folder_root` (raw config string, or empty/undefined). */
  sessionFolderRoot?: string | null
  /** The realpath'd default project the gateway assigns (config GET
   *  `default_project_resolved`); the workspace dir when `default_project` is
   *  unset. Preferred over `defaultProject` when present. */
  defaultProjectResolved?: string | null
  /** The realpath'd session-folder root (config GET
   *  `session_folder_root_resolved`). Preferred over `sessionFolderRoot`. */
  sessionFolderRootResolved?: string | null
}

/** Is this project path a gateway-assigned default rather than a chosen codebase?
 *  True when it equals the configured `default_project`, or is the session-folder
 *  root itself or a path under it. Compared on normalized paths so a trailing
 *  slash does not defeat the match. */
export function isGatewayDefaultProject(path: string, ctx: CodebaseContext | undefined): boolean {
  if (!ctx) return false
  const p = normalizeCodebasePath(path)
  if (!p) return true
  // Prefer the gateway-resolved anchors (realpath'd, matching how slot.project is
  // stored); fall back to the raw config string when the resolved form is absent.
  const def = normalizeCodebasePath(ctx.defaultProjectResolved ?? ctx.defaultProject)
  if (def && p === def) return true
  const root = normalizeCodebasePath(ctx.sessionFolderRootResolved ?? ctx.sessionFolderRoot)
  if (root && (p === root || p.startsWith(root + '/') || p.startsWith(root + '\\'))) return true
  return false
}

/** The codebase a session belongs to, or `undefined` when it resolves to none.
 *
 *  `slot.project` wins ONLY when it is a codebase the person chose: the gateway
 *  fills `slot.project` on every new slot (the workspace default, or a per-session
 *  directory under `session_folder_root`), and those are not codebases, so a
 *  `slot.project` that `isGatewayDefaultProject` matches is skipped. When
 *  `slot.project` is absent or a gateway default, the folder's `project_dir`
 *  (nearest ancestor) is the fallback, so a session filed in a repo folder still
 *  groups correctly. A peer/remote row (no local `folder_id`) and a session that
 *  resolves to neither fall into the "No codebase" bucket. */
export function sessionCodebase(
  slot: Pick<Slot, 'project' | 'folder_id'>,
  folders: ChatFolder[],
  ctx?: CodebaseContext,
): string | undefined {
  const explicit = normalizeCodebasePath(slot.project)
  if (explicit && !isGatewayDefaultProject(explicit, ctx)) return explicit
  if (!slot.folder_id) return undefined
  return normalizeCodebasePath(resolveFolderProjectDir(folders, slot.folder_id))
}

/** Trim a codebase path to its grouping-stable form: strip one trailing slash so
 *  `/repo` and `/repo/` bucket together, and treat an empty / whitespace-only
 *  value as "no codebase". Case is NOT folded and symlinks are NOT resolved — the
 *  frontend cannot know the host filesystem's case rule, and folding could merge
 *  two genuinely distinct directories. */
export function normalizeCodebasePath(path: string | null | undefined): string | undefined {
  if (!path) return undefined
  const trimmed = path.trim()
  if (!trimmed) return undefined
  // Strip a single trailing separator, but never reduce a root ("/" or "C:\\")
  // to the empty string.
  const stripped = trimmed.replace(/([^/\\])[/\\]$/, '$1')
  return stripped || undefined
}

/** The display label for a codebase group header: the directory basename, with
 *  the full path available for a title attribute at the call site. A root path
 *  with no basename shows the path itself. */
export function codebaseLabel(path: string): string {
  const segments = path.replace(/[/\\]+$/, '').split(/[/\\]/)
  return segments[segments.length - 1] || path
}

/** One codebase group: the resolved path (the stable key), its display label,
 *  and the sessions in it — already in the caller's lane order, because the input
 *  list is. */
export interface CodebaseGroup {
  /** The resolved codebase path, or `null` for the "No codebase" bucket. */
  path: string | null
  label: string
  slots: Slot[]
}

/**
 * Bucket `slots` by codebase, preserving the input order inside each group.
 *
 * `codebaseOf` is the slot-key -> path map built in the shell (the same
 * resolution as `sessionCodebase`, memoized once). Groups are sorted by path
 * with the locale-aware `compareText`, so the headers collate the way every
 * other list on the page does; the single "No codebase" bucket, if any, is
 * always last. `noCodebaseLabel` is passed in (an i18n string), never hardcoded.
 */
export function groupByCodebase(
  slots: readonly Slot[],
  codebaseOf: (slot: Slot) => string | undefined,
  noCodebaseLabel: string,
): CodebaseGroup[] {
  const byPath = new Map<string, Slot[]>()
  const none: Slot[] = []
  for (const slot of slots) {
    const path = codebaseOf(slot)
    if (path === undefined) { none.push(slot); continue }
    const list = byPath.get(path)
    if (list) list.push(slot); else byPath.set(path, [slot])
  }
  const groups: CodebaseGroup[] = [...byPath.keys()]
    .sort(compareText)
    .map(path => ({ path, label: codebaseLabel(path), slots: byPath.get(path)! }))
  if (none.length > 0) groups.push({ path: null, label: noCodebaseLabel, slots: none })
  return groups
}
