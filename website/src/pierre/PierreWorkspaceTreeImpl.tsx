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
import { useEffect, useLayoutEffect, useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { GitStatus, GitStatusEntry } from '@pierre/trees'
import { FileTree, useFileTree } from '@pierre/trees/react'
import { FileDiff, FolderOpen } from 'lucide-react'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { TreeSkeleton } from './tree'

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

export function PierreWorkspaceTreeImpl({ projectDir, onFileOpen, searchQuery, mode = 'all', selectedPath }: {
  projectDir: string
  onFileOpen?: (absPath: string) => void
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
    () => (mode === 'changed' ? statusEntries.map(e => e.path) : tree?.paths ?? []),
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
      <FileTree model={model} className="pierre-tree" style={{ height: '100%', flex: 1, minHeight: 0 }} />
    </div>
  )
}
