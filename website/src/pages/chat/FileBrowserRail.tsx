import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Files, Diff, Search, X, RefreshCw } from 'lucide-react'
import { api } from '../../api/client'
import { cn } from '../../lib/utils'
import { useColumnResize } from '../../hooks/useColumnResize'
import { PierreWorkspaceTree } from '../../pierre/tree'

/** Rail width bounds; the grip clamps between them. */
const RAIL_MIN_W = 300
const RAIL_MAX_W = 520
const RAIL_W_KEY = 'mc-files-rail-w'

/** All/Changed mode for the current page session. Module-level (not
 *  persisted): in-place tab navigation remounts the rail — the tab id
 *  changes — and the mode must survive that, while a fresh page load still
 *  defaults to All files. */
let sessionChangedMode = false

/** Whether the tree APIs answer for this directory. Shares the tree
 *  component's query key, so the probe costs no extra request. */
/**
 * Why the tree is or is not usable, which is NOT a boolean: a fetch that failed
 * and a chat with no project directory need different words and different
 * remedies. Collapsing them sends the user to fix a setting that is already
 * correct — the header is naming the directory while the body denies it exists.
 *
 * `ready` covers the in-flight case on purpose: the tree renders its own loading
 * state, so the rail should mount rather than flashing an error first.
 */
export type TreeState = 'no-dir' | 'error' | 'ready'

export function useTreeState(projectDir: string | null | undefined): TreeState {
  const q = useQuery({
    queryKey: ['project-tree', projectDir ?? ''],
    queryFn: () => api.projectTree(projectDir ?? ''),
    enabled: !!projectDir,
    retry: false,
    staleTime: 10_000,
  })
  if (!projectDir) return 'no-dir'
  return q.isError ? 'error' : 'ready'
}

export function useTreeAvailable(projectDir: string | null | undefined): boolean {
  return useTreeState(projectDir) === 'ready'
}

/**
 * The file-browser rail: resize grip + tree column, headed by ONE row — an
 * icons-only All/Changed segment (tooltips carry the labels, Changed shows a
 * live count) with an always-open search field filling the rest. The query
 * feeds the tree's search session (the tree's own built-in bar is disabled).
 *
 * Both modes render the SAME Pierre tree; Changed feeds it the git-status
 * path set and its opens land in diff mode (`onFileOpen`'s second argument).
 */
export default function FileBrowserRail({ projectDir, onFileOpen, onAddToContext, selectedPath }: {
  projectDir: string
  onFileOpen: (absPath: string, diff: boolean) => void
  /** Right-click "Add to context" on a tree row: forwards the ABSOLUTE path
   *  and whether it is a file or a directory up to the composer host. */
  onAddToContext?: (absPath: string, kind: 'file' | 'dir') => void
  /** Currently-open file, echoed as the tree selection. */
  selectedPath?: string | null
}) {
  const { t } = useTranslation()
  const [changedMode, _setChangedMode] = useState(() => sessionChangedMode)
  const setChangedMode = (v: boolean) => {
    sessionChangedMode = v
    _setChangedMode(v)
  }
  const [query, setQuery] = useState('')

  const { data: status } = useQuery({
    queryKey: ['git-status', projectDir],
    queryFn: () => api.projectGitStatus(projectDir),
    enabled: !!projectDir,
    refetchInterval: 5_000,
    refetchOnWindowFocus: true,
  })
  const changedCount = status?.files?.length ?? 0

  // Both queries poll (10s tree / 5s status); this is the "I changed something
  // outside the app, show me now" escape hatch. `refetchQueries` (not
  // `invalidateQueries`) so `refreshing` tracks the actual network round trip
  // and the spinner reflects real work.
  const qc = useQueryClient()
  const [refreshing, setRefreshing] = useState(false)
  const refresh = async () => {
    setRefreshing(true)
    try {
      await Promise.all([
        qc.refetchQueries({ queryKey: ['project-tree', projectDir] }),
        qc.refetchQueries({ queryKey: ['git-status', projectDir] }),
      ])
    } finally {
      setRefreshing(false)
    }
  }

  // The grip sits on the rail's LEFT edge, so the hook negates the drag delta
  // (edge: 'left'): dragging left grows the rail. Clamping and the persisted
  // width key are unchanged from the hand-rolled block this replaces.
  const rail = useColumnResize(
    RAIL_W_KEY,
    () => {
      const v = parseInt(localStorage.getItem(RAIL_W_KEY) || '', 10)
      return Number.isFinite(v) ? Math.min(RAIL_MAX_W, Math.max(RAIL_MIN_W, v)) : RAIL_MIN_W
    },
    RAIL_MIN_W,
    RAIL_MAX_W,
    undefined,
    undefined,
    'left',
  )

  const segBtn = (on: boolean) =>
    cn('flex items-center justify-center gap-1.5 h-[22px] px-2 rounded-[5px] text-[11.5px] font-medium cursor-pointer border-none transition-colors',
       on ? 'bg-bg text-text shadow-[0_0_0_1px_var(--border)]' : 'bg-transparent text-muted hover:text-text')

  return (
    <>
      <div
        {...rail.handleProps}
        role="separator"
        aria-orientation="vertical"
        aria-label={t('pages.chat.fileBrowserRail.resize')}
        className="w-1 shrink-0 cursor-col-resize bg-transparent hover:bg-accent/40 active:bg-accent/60 transition-colors"
        style={{ touchAction: 'none' }}
      />
      <div style={{ width: rail.width }} className="shrink-0 min-h-0 border-l border-border flex flex-col">
        <div className="flex items-center gap-1.5 px-2 h-[40px] shrink-0 border-b border-border">
          <div
            className="flex flex-none bg-bg-elevated border border-border rounded-[7px] p-[2px] gap-[2px]"
            role="group"
            aria-label={t('pages.chat.fileBrowserRail.tree_mode')}
          >
            <button
              onClick={() => setChangedMode(false)}
              aria-pressed={!changedMode}
              className={segBtn(!changedMode)}
              title={t('pages.chat.fileBrowserRail.all_files')}
              aria-label={t('pages.chat.fileBrowserRail.all_files')}
            >
              <Files size={12} className="shrink-0" />
            </button>
            <button
              onClick={() => setChangedMode(true)}
              aria-pressed={changedMode}
              className={segBtn(changedMode)}
              title={t('pages.chat.fileBrowserRail.changed')}
              aria-label={t('pages.chat.fileBrowserRail.changed')}
            >
              <Diff size={12} className="shrink-0" />
              {changedCount > 0 && <span className="opacity-60 text-[10px] tabular-nums">{changedCount}</span>}
            </button>
          </div>
          <div className="flex flex-1 min-w-0 items-center gap-1.5 h-[26px] px-2 bg-bg-elevated border border-border focus-within:border-accent rounded-[7px] transition-colors">
            <Search size={12} className="text-muted shrink-0" />
            <input
              value={query}
              onChange={e => setQuery(e.target.value)}
              onKeyDown={e => { if (e.key === 'Escape') setQuery('') }}
              placeholder={t('pages.chat.fileBrowserRail.filter_placeholder')}
              aria-label={t('pages.chat.fileBrowserRail.filter_placeholder')}
              className="flex-1 min-w-0 bg-transparent border-none outline-none text-[12px] text-text"
            />
            {query && (
              <button
                onClick={() => setQuery('')}
                className="flex items-center justify-center w-[18px] h-[18px] rounded cursor-pointer text-muted hover:text-text bg-transparent border-none shrink-0"
                aria-label={t('pages.chat.fileBrowserRail.close_search')}
              >
                <X size={11} />
              </button>
            )}
          </div>
          <button
            onClick={refresh}
            disabled={refreshing}
            className="flex flex-none items-center justify-center w-[26px] h-[26px] rounded-[7px] bg-bg-elevated border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-colors disabled:opacity-40 disabled:cursor-default"
            title={t('pages.chat.fileBrowserRail.refresh')}
            aria-label={t('pages.chat.fileBrowserRail.refresh')}
          >
            <RefreshCw size={12} className={refreshing ? 'animate-spin' : ''} />
          </button>
        </div>
        <div className="flex-1 min-h-0 flex flex-col py-1.5 pl-1">
          <PierreWorkspaceTree
            mode={changedMode ? 'changed' : 'all'}
            projectDir={projectDir}
            onFileOpen={(abs) => {
              setQuery('')
              onFileOpen(abs, changedMode)
            }}
            onAddToContext={onAddToContext}
            searchQuery={query || null}
            selectedPath={selectedPath ?? null}
          />
        </div>
      </div>
    </>
  )
}
