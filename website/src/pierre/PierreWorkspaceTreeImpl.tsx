/**
 * Workspace file tree for the Files tab, rendered with `@pierre/trees`.
 *
 * Fed by `GET /api/project/tree` (paths, scoped to the chat's project dir)
 * and `GET /api/project/git/status` (edit-status lanes). Clicking a file
 * reports the ABSOLUTE path so it opens through the same flow as every other
 * file affordance in the panel.
 *
 * Like the diff/code surfaces, the heavy `@pierre/trees` runtime loads behind
 * a lazy boundary (see `./tree.tsx`) so the eager bundle stays clean.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { GitStatus, GitStatusEntry } from '@pierre/trees'
// The package root re-exports the tree's context-menu types under shorter
// names; alias them back to the render-signature names for local clarity.
import type {
  ContextMenuItem as FileTreeContextMenuItem,
  ContextMenuOpenContext as FileTreeContextMenuOpenContext,
} from '@pierre/trees'
import { FileTree, useFileTree } from '@pierre/trees/react'
import { AtSign, FileDiff, FolderOpen } from 'lucide-react'
import { api } from '../api/client'
import { useMenuKeyboard } from '../hooks/useMenuKeyboard'
import { i18nT } from '../i18n/t'
import { normalizeWindowsPath } from '../utils/fileTokens'
import { TreeSkeleton } from './tree'

/** The kind vocabulary the composer's `@`-mention plumbing speaks: a file is
 *  staged + tokenized, a folder becomes a bare `@rel/` reference. Narrower than
 *  Pierre's `'directory' | 'file'`, so map at the boundary. */
type TreeEntryKind = 'file' | 'dir'

/** Row-level right-click menu projected into Pierre's `context-menu` slot.
 *  Pierre owns the anchor, the outside-click wash, and open/close; this renders
 *  only the item list — a single "Add to chat" action (row click already opens
 *  a file, so the menu deliberately carries no Open duplicate). The action
 *  closes the menu itself so focus returns to the row. */
function TreeContextMenu({ item, context, root, onAddToContext }: {
  item: FileTreeContextMenuItem
  context: FileTreeContextMenuOpenContext
  root: string
  onAddToContext?: (absPath: string, kind: TreeEntryKind) => void
}) {
  const isDir = item.kind === 'directory'
  // Pierre paths are POSIX (`/`), but on native Windows `root` is
  // backslash-separated, so a raw join yields a mixed-separator path. The
  // mention host's makeRelative cannot relativize that, leaving an absolute
  // `@C:\proj/...` token and a duplicate attachment marker. Normalize the
  // ROOT only, and only when it is Windows-shaped (normalizeWindowsPath):
  // `item.path` must pass through untouched, because on POSIX `\` is a legal
  // filename character and rewriting it would corrupt a real name. Strip any
  // trailing separator the normalized root carries (a drive-root project,
  // `D:\` -> `D:/`) BEFORE appending -- left un-stripped, the join produces
  // `D://item`, and makeRelative's prefix strip then consumes only ONE of the
  // two slashes, leaving a stray leading `/` on the relativized path (a
  // root-relative-looking path instead of project-relative).
  const abs = `${normalizeWindowsPath(root).replace(/\/$/, '')}/${item.path}`
  // role="menuitem" divs (an interactive ARIA role) with a keyboard handler:
  // the correct menu semantics inside the role="menu" container, and the role
  // is what makes an onClick div compliant rather than a static-element one.
  const itemCls =
    'flex items-center gap-2 rounded-md px-2.5 py-1.5 text-[12.5px] text-text ' +
    'cursor-pointer hover:bg-bg-hover focus:bg-bg-hover outline-none'
  const activate = (run: () => void) => (e: React.MouseEvent | React.KeyboardEvent) => {
    if ('key' in e) {
      if (e.key !== 'Enter' && e.key !== ' ') return
      e.preventDefault()
    }
    context.close()
    run()
  }
  // Focus the first item on open. Pierre's own open path calls `item.focus()`
  // on the tree ROW, never on this slotted content, so a keyboard user who
  // opens the menu (Shift+F10 / the ContextMenu key) would otherwise be left
  // with focus on the row: the Enter/Space handler below sits on the
  // `tabIndex={-1}` menuitem and could never fire, and Enter would re-trigger
  // the row instead. Closing restores focus to the row (Pierre's
  // `restoreFocus`), so this does not strand focus inside a dismissed menu.
  const firstItemRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    firstItemRef.current?.focus()
  }, [])
  // The DEGENERATE single-item case of the shared role="menu" keyboard contract
  // (#6231): with exactly one item the arrows have nothing to move between, so
  // the wiring buys no navigation today. It is here because the CONTRACT is
  // what role="menu" advertises to assistive technology, and honouring it
  // per-surface-by-item-count is how surfaces drift: an arrow inside an open
  // menu must be consumed rather than scrolling the tree behind it, Tab must
  // stay contained (#2533), and IME composition keys must not reach the menu at
  // all — all true of a one-item menu. It also means the day this menu grows a
  // second action (an Open, a Reveal), real navigation arrives with it instead
  // of being a second bug to find. `enabled: true` unconditionally because
  // Pierre only mounts this component while the menu is open.
  // focusFirstOnOpen: false — the firstItemRef effect above already owns focus
  // entry (it must, because Pierre focuses the tree ROW, not this slotted
  // content); letting the hook also focus would be a redundant second move.
  const menuRef = useRef<HTMLDivElement>(null)
  useMenuKeyboard({ enabled: true, containerRef: menuRef, focusFirstOnOpen: false })
  return (
    <div
      ref={menuRef}
      role="menu"
      className="min-w-[176px] rounded-lg border border-border bg-bg-elevated p-1 shadow-lg"
    >
      <div
        ref={firstItemRef}
        role="menuitem"
        tabIndex={-1}
        className={itemCls}
        onClick={activate(() => onAddToContext?.(abs, isDir ? 'dir' : 'file'))}
        onKeyDown={activate(() => onAddToContext?.(abs, isDir ? 'dir' : 'file'))}
      >
        <AtSign className="lucide-inline text-muted" />
        {i18nT('pages.chat.fileBrowserRail.ctx_add_to_chat')}
      </div>
    </div>
  )
}

/** Map a porcelain status letter to Pierre's git-status lane vocabulary. */function gitStatusFor(letter: string): GitStatus | null {
  switch (letter) {
    case 'M': return 'modified'
    case 'A': return 'added'
    case 'D': return 'deleted'
    case 'R': return 'renamed'
    case 'C': return 'added'
    case '?': return 'untracked'
    default: return null
  }
}

export function PierreWorkspaceTreeImpl({ projectDir, onFileOpen, onAddToContext, searchQuery, mode = 'all', selectedPath }: {
  projectDir: string
  onFileOpen?: (absPath: string) => void
  /** Right-click "Add to context" on a row: hands the host the ABSOLUTE path
   *  and whether it is a file or a directory, so the composer can insert the
   *  same `@`-mention the file picker does. Absent → the menu item is still
   *  shown but inert (the tree has no host to mention into). */
  onAddToContext?: (absPath: string, kind: TreeEntryKind) => void
  /** Forwarded into the tree's search session (null clears it). */
  searchQuery?: string | null
  /** 'all' renders the full workspace; 'changed' renders only the files with
   *  working-tree changes (the git-status set), fully expanded. Remount
   *  (key) on mode change — initial expansion is fixed at model creation. */
  mode?: 'all' | 'changed'
  /** Absolute path of the file the host surface has open: echoed as the tree
   *  selection (and scrolled into view). Selection changes caused by this
   *  prop never re-fire `onFileOpen`. */
  selectedPath?: string | null
}) {
  const { data: tree } = useQuery({
    queryKey: ['project-tree', projectDir],
    queryFn: () => api.projectTree(projectDir),
    enabled: !!projectDir,
    refetchInterval: 10_000,
    refetchOnWindowFocus: true,
  })
  const { data: status } = useQuery({
    queryKey: ['git-status', projectDir],
    queryFn: () => api.projectGitStatus(projectDir),
    enabled: !!projectDir && (mode === 'changed' || !!tree?.repo),
    refetchInterval: 5_000,
    refetchOnWindowFocus: true,
  })

  const { model } = useFileTree({
    paths: [],
    // Changed mode holds a handful of paths — show them all; the full
    // workspace starts collapsed.
    initialExpansion: mode === 'changed' ? 'open' : 'closed',
    flattenEmptyDirectories: true,
    // The rail renders its own search field and forwards it through
    // `searchQuery` → model.setSearch (which works regardless of this flag);
    // the tree's built-in bar would duplicate it.
    search: false,
    // Baseline composition for the row context menu. `<FileTree>`'s
    // renderContextMenu wiring forces `enabled: true` but preserves this
    // trigger config: a hover ellipsis button plus right-click (and Shift+F10
    // for keyboards), the affordance shown only when the row is hovered/focused
    // so a narrow rail stays uncluttered.
    composition: { contextMenu: { triggerMode: 'both', buttonVisibility: 'when-needed' } },
  })

  // The tree endpoint returns paths relative to the PROJECT dir while git
  // status paths are relative to the REPO root — for a project dir that is a
  // repo subdirectory the two disagree. Anchor both to absolute paths via the
  // respective roots and re-relativize against the project root so lanes land
  // on the right rows.
  const root = tree?.root ?? projectDir
  const statusEntries = useMemo<GitStatusEntry[]>(() => {
    if (!status?.files?.length) return []
    const repoRoot = status.repoRoot
    const entries: GitStatusEntry[] = []
    const seen = new Set<string>()
    for (const f of status.files) {
      const abs = repoRoot ? `${repoRoot}/${f.path}` : `${root}/${f.path}`
      if (!abs.startsWith(root + '/')) continue
      const rel = abs.slice(root.length + 1)
      const mapped = gitStatusFor(f.status)
      // Staged + unstaged rows for one file: first (staged) entry wins; the
      // lane shows one state per row either way.
      if (!mapped || seen.has(rel)) continue
      seen.add(rel)
      entries.push({ path: rel, status: mapped })
    }
    return entries
  }, [status, root])

  // The rendered path set: the whole workspace, or just the changed files —
  // the SAME tree component either way, so both modes share look, keyboard
  // model, search, and git-status lanes.
  const paths = useMemo<string[]>(
    () =>
      mode === 'changed'
        ? statusEntries.map(e => e.path)
        : // The full-workspace list can still carry a duplicate — e.g. two
          // genuinely different paths that collapse to the same string once
          // egress redaction flattens a differing segment. @pierre/trees
          // `appendPresortedPaths` throws 'Duplicate path' on adjacent
          // identical entries, and that throw is uncaught inside the
          // resetPaths useLayoutEffect below, taking down the whole route.
          // De-dup here (preserving order + first occurrence, mirroring the
          // `changed` branch's statusEntries seen-Set) so a duplicate degrades
          // to a single (missing) row instead of a render crash.
          Array.from(new Set(tree?.paths ?? [])),
    [mode, statusEntries, tree],
  )
  const ready = mode === 'changed' ? status != null : tree != null

  // Feed data into the model imperatively (the model is created once; path
  // resets and git-status patches are the supported update API). Layout
  // effects, not plain effects: the rail remounts on in-place tab navigation,
  // and post-paint effects would flash an empty then UNFILTERED tree before
  // the search below re-applies — data, search, and reveal must all land in
  // the same pre-paint pass so the first visible frame is already correct.
  const pathsKey = useMemo(() => paths.join('\n'), [paths])
  const lastPathsKey = useRef<string | null>(null)
  useLayoutEffect(() => {
    if (!ready) return
    if (lastPathsKey.current === pathsKey) return
    lastPathsKey.current = pathsKey
    model.resetPaths(paths)
  }, [ready, paths, pathsKey, model])
  useEffect(() => {
    model.setGitStatus(statusEntries)
  }, [statusEntries, model])

  // Forward the panel's shared search box into the tree's search session.
  useLayoutEffect(() => {
    model.setSearch(searchQuery || null)
  }, [searchQuery, model])

  // Echo the host's open file as the tree selection. The ref lets the
  // open-on-selection subscription below tell this programmatic selection
  // (and a click on the already-open file) apart from a real user open.
  const selectedPathRef = useRef(selectedPath)
  selectedPathRef.current = selectedPath
  useLayoutEffect(() => {
    if (!ready || !selectedPath) return
    const rel = selectedPath.startsWith(`${root}/`) ? selectedPath.slice(root.length + 1) : null
    if (!rel) return
    model.focusPath(rel)
    // Selection (not just focus) renders the persistent row highlight, so the
    // open file stays visibly marked. The render-level FileTree only exposes
    // selection through item handles. The subscription below ignores this
    // programmatic selection via selectedPathRef.
    for (const p of model.getSelectedPaths()) if (p !== rel) model.getItem(p)?.deselect()
    // A nested file is invisible while an ancestor is collapsed — expand the
    // chain root-down so the highlighted row is actually on screen.
    const segments = rel.split('/')
    for (let i = 1; i < segments.length; i++) {
      const dir = model.getItem(segments.slice(0, i).join('/'))
      if (dir && 'expand' in dir) dir.expand()
    }
    model.getItem(rel)?.select()
  }, [ready, selectedPath, root, model])

  // Open on selection: single-click selects a file row; report it as an open.
  const onFileOpenRef = useRef(onFileOpen)
  onFileOpenRef.current = onFileOpen
  useEffect(() => {
    const unsubscribe = model.subscribe(() => {
      const focused = model.getFocusedItem()
      if (!focused || focused.isDirectory()) return
      const selected = model.getSelectedPaths()
      if (selected.length !== 1 || selected[0] !== focused.getPath()) return
      const abs = `${root}/${focused.getPath()}`
      // The host's own open file: this selection is the echo effect above (or
      // a click on the file already open) — not a new open.
      if (abs === selectedPathRef.current) return
      onFileOpenRef.current?.(abs)
    })
    return unsubscribe
  }, [model, root])

  // Row context menu. Ref-backed so the callback identity stays stable across
  // handler/prop changes (Pierre re-reads it only when the menu opens) while
  // still calling the latest handlers. `root` is captured live off the tree
  // query, so it must come through the ref too.
  const onAddToContextRef = useRef(onAddToContext)
  onAddToContextRef.current = onAddToContext
  const rootRef = useRef(root)
  rootRef.current = root
  const renderContextMenu = useCallback(
    (item: FileTreeContextMenuItem, ctx: FileTreeContextMenuOpenContext) => (
      <TreeContextMenu
        item={item}
        context={ctx}
        root={rootRef.current}
        onAddToContext={onAddToContextRef.current}
      />
    ),
    [],
  )

  // Data still in flight: an empty tree is indistinguishable from an empty
  // workspace, so show shimmer rows until the first payload decides which.
  if (!ready) {
    return <TreeSkeleton />
  }

  // `ready` already means "the query that supplies `paths` has answered" — the
  // tree query in `all` mode, the status query in `changed` — so it is the only
  // readiness signal this branch may consult. Gating on the tree query's
  // loading flag would suppress the notice while `changed` mode is already
  // decided, leaving an empty FileTree in its place.
  if (ready && paths.length === 0) {
    const [Icon, message] =
      mode === 'changed'
        ? ([FileDiff, i18nT('pages.chat.folderPanel.no_changes')] as const)
        : ([FolderOpen, i18nT('pages.chat.activityViewer.workspace_empty')] as const)
    return (
      <div className="h-full flex flex-col items-center justify-center gap-2.5 text-muted px-6 text-center">
        <Icon size={20} className="opacity-50" />
        <span className="text-[12.5px]">{message}</span>
      </div>
    )
  }

  return (
    <div className="flex-1 min-h-0 flex flex-col">
      {mode === 'all' && tree?.truncated && (
        <div className="px-3 py-1 text-[11px] text-muted">
          {i18nT('pages.chat.activityViewer.workspace_truncated')}
        </div>
      )}
      <FileTree
        model={model}
        className="pierre-tree"
        style={{ height: '100%', flex: 1, minHeight: 0 }}
        // Wired only when there is a host to hand the row to: `hasContextMenu`
        // (FileTree's own renderContextMenu != null check) forces the menu
        // enabled unconditionally, so passing it regardless of onAddToContext
        // would open a menu whose only action closes itself and does nothing.
        renderContextMenu={onAddToContext ? renderContextMenu : undefined}
      />
    </div>
  )
}
