/**
 * Change-set tree for the pull request panel, rendered with `@pierre/trees`.
 *
 * Unlike `PierreWorkspaceTreeImpl` this fetches nothing: a pull request's file
 * list arrives with the source payload, so the tree is a pure projection of
 * props — the changed paths, each file's change lane, the caller's viewed set
 * (drawn as a per-row check decoration), and the file currently at the top of
 * the diff viewport (echoed as the selection). Clicking a file reports its
 * repo-relative path so the caller can scroll the diff to it.
 *
 * Reached only through the lazy boundary in `./tree`, like the other trees
 * surface, so `@pierre/trees` stays out of the eager bundle.
 */
import { useEffect, useLayoutEffect, useMemo, useRef } from 'react'
import type { GitStatus, GitStatusEntry } from '@pierre/trees'
import { FileTree, useFileTree } from '@pierre/trees/react'
import { i18nT } from '../i18n/t'
import { PR_TREE_VIEWED_ICON, PR_TREE_VIEWED_SPRITE, PR_TREE_VIEWED_CSS, treeSiblingCompare } from './config'

/** Map a provider status token onto Pierre's git-status lane vocabulary. The
 *  tokens are words (`added`, `removed`…), not porcelain letters — the same
 *  spellings `patchChangeForStatus` reads when synthesizing patch headers. */
function laneForStatus(status: string | undefined): GitStatus {
  const token = (status ?? '').toLowerCase()
  if (token === 'added' || token === 'new') return 'added'
  if (token === 'removed' || token === 'deleted') return 'deleted'
  if (token === 'renamed') return 'renamed'
  return 'modified'
}

export function PierrePrTreeImpl({ files, viewedPaths, currentPath, onFileClick, searchQuery }: {
  /** The change set, in the diff's own order. Paths are repo-relative and
   *  double as row identity, matching the CodeView item ids. */
  files: readonly { path: string; status?: string }[]
  /** Files the reader marked viewed — each draws a trailing check decoration. */
  viewedPaths: ReadonlySet<string>
  /** The file at the top of the diff viewport, echoed as the tree selection so
   *  the tree follows the reader's own scrolling. Selection changes caused by
   *  this prop never re-fire `onFileClick`. */
  currentPath?: string | null
  onFileClick?: (path: string) => void
  /** Forwarded into the tree's search session (null clears it), same as the
   *  workspace tree: the rail renders its own filter field. */
  searchQuery?: string | null
}) {
  /* The renderer is captured once at model creation, so it reads the live set
     through a ref; the repaint when the set changes is driven below. */
  const viewedRef = useRef(viewedPaths)
  viewedRef.current = viewedPaths

  const icons = useMemo(() => ({ spriteSheet: PR_TREE_VIEWED_SPRITE, set: 'complete' as const }), [])
  const { model } = useFileTree({
    paths: [],
    // A change set is a working set, not a workspace: every file is relevant,
    // so everything starts visible.
    initialExpansion: 'open',
    flattenEmptyDirectories: true,
    search: false,
    // The same pinned collation the CodeView file order uses
    // (`treeOrderCompare`), so the tree and the diff can never disagree on
    // sibling order across machines.
    sort: (a, b) => treeSiblingCompare(a.basename, a.isDirectory, b.basename, b.isDirectory),
    icons,
    unsafeCSS: PR_TREE_VIEWED_CSS,
    renderRowDecoration: ({ row }) => {
      if (row.kind !== 'file' || !viewedRef.current.has(row.path)) return null
      return {
        icon: { name: PR_TREE_VIEWED_ICON, width: 13, height: 13, viewBox: '0 0 24 24' },
        title: i18nT('components.pullRequestPanel.viewed'),
      }
    },
  })

  // Feed paths + lanes imperatively (the supported update API — the model is
  // created once). Layout effects for the same reason as the workspace tree:
  // the first visible frame must already hold the data.
  const paths = useMemo(() => files.map(f => f.path), [files])
  const pathsKey = useMemo(() => JSON.stringify(paths), [paths])
  const lastPathsKey = useRef<string | null>(null)
  useLayoutEffect(() => {
    if (lastPathsKey.current === pathsKey) return
    lastPathsKey.current = pathsKey
    model.resetPaths(paths)
  }, [paths, pathsKey, model])
  const statusEntries = useMemo<GitStatusEntry[]>(
    () => files.map(f => ({ path: f.path, status: laneForStatus(f.status) })),
    [files],
  )
  useEffect(() => {
    model.setGitStatus(statusEntries)
  }, [statusEntries, model])

  // Forward the rail's filter box into the tree's search session, exactly as
  // the workspace tree does (setSearch works regardless of the `search` flag,
  // which only governs the tree's own built-in field).
  useEffect(() => {
    model.setSearch(searchQuery || null)
  }, [searchQuery, model])

  /* Decoration repaint. The renderer above reads `viewedRef`, but nothing
     re-runs it until the view re-renders — `setIcons` re-renders the tree root
     unconditionally, and re-asserting the same config is otherwise a no-op. */
  useEffect(() => {
    model.setIcons(icons)
  }, [viewedPaths, icons, model])

  // Echo the viewport's file as the tree selection (same pattern as the
  // workspace tree's selectedPath echo, minus the root juggling — both sides
  // of this surface already speak repo-relative paths).
  const currentPathRef = useRef(currentPath)
  currentPathRef.current = currentPath
  useLayoutEffect(() => {
    if (!currentPath) return
    model.focusPath(currentPath)
    for (const p of model.getSelectedPaths()) if (p !== currentPath) model.getItem(p)?.deselect()
    model.getItem(currentPath)?.select()
  }, [currentPath, model])

  // A click that lands a single selected file is an open; the echo effect
  // above is filtered out by comparing against the caller-declared current file.
  const onFileClickRef = useRef(onFileClick)
  onFileClickRef.current = onFileClick
  useEffect(() => {
    const unsubscribe = model.subscribe(() => {
      const focused = model.getFocusedItem()
      if (!focused || focused.isDirectory()) return
      const selected = model.getSelectedPaths()
      if (selected.length !== 1 || selected[0] !== focused.getPath()) return
      if (focused.getPath() === currentPathRef.current) return
      onFileClickRef.current?.(focused.getPath())
    })
    return unsubscribe
  }, [model])

  return <FileTree model={model} className="pierre-tree" style={{ height: '100%', flex: 1, minHeight: 0 }} />
}
