/**
 * Lazy boundary for the `@pierre/trees` workspace tree — keeps the trees
 * runtime out of the eager bundle, mirroring `./index` for `@pierre/diffs`.
 */
import { Suspense, lazy } from 'react'
import { i18nT } from '../i18n/t'

const TreeImpl = lazy(() =>
  import('./PierreWorkspaceTreeImpl').then(m => ({ default: m.PierreWorkspaceTreeImpl })),
)

/** Tree-shaped shimmer placeholder: [indent px, row width]. Varied depths and
 *  widths so the skeleton reads as a file tree, not a generic list. Shared by
 *  the lazy-chunk fallback here and the impl's data-loading state, so both
 *  waits look identical. */
const SKELETON_ROWS: Array<[number, string]> = [
  [0, '52%'], [14, '68%'], [28, '46%'], [28, '58%'], [14, '40%'], [28, '62%'], [0, '44%'], [14, '55%'],
]

export function TreeSkeleton() {
  return (
    <div
      className="px-2 py-2 flex flex-col gap-[7px]"
      role="status"
      aria-label={i18nT('pages.chat.activityViewer.loading_workspace')}
    >
      {SKELETON_ROWS.map(([indent, width], i) => (
        <div
          key={i}
          className="h-[13px] rounded bg-bg-hover animate-pulse"
          style={{ marginLeft: indent, width, animationDelay: `${i * 90}ms` }}
        />
      ))}
    </div>
  )
}

export function PierreWorkspaceTree({ projectDir, onFileOpen, onAddToContext, searchQuery, mode, selectedPath }: {
  projectDir: string
  onFileOpen?: (absPath: string) => void
  /** Right-click "Add to context" on a row — forwards the ABSOLUTE path and
   *  whether it is a file or a directory to the host composer. */
  onAddToContext?: (absPath: string, kind: 'file' | 'dir') => void
  /** Forwarded into the tree's search session (null clears it). */
  searchQuery?: string | null
  /** 'all' (default) = full workspace; 'changed' = only working-tree changes. */
  mode?: 'all' | 'changed'
  /** Absolute path of the host's open file — echoed as the tree selection. */
  selectedPath?: string | null
}) {
  return (
    <Suspense fallback={<TreeSkeleton />}>
      <TreeImpl key={mode ?? 'all'} projectDir={projectDir} onFileOpen={onFileOpen} onAddToContext={onAddToContext} searchQuery={searchQuery} mode={mode} selectedPath={selectedPath} />
    </Suspense>
  )
}
