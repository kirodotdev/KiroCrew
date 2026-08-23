import { useTranslation } from 'react-i18next'
import { useQueryClient } from '@tanstack/react-query'
import { FileText, RotateCw, ExternalLink } from 'lucide-react'
import { api } from '../../api/client'
import FileBrowserRail, { useTreeState } from './FileBrowserRail'

/** Last path segment, trailing slashes ignored. */
function basename(p: string): string {
  return p.replace(/\/+$/, '').split('/').pop() || p
}

/**
 * The pinned Files tab: an empty preview pane on the left and the permanent
 * file-browser rail on the right, under one full-width header. Clicking a
 * file NEVER opens inline here — every open spawns a file tab (the same
 * primitive every other file-open path lands in), so this tab stays the
 * stable jumping-off point.
 *
 * The rail is deliberately not hideable in this state: without a file, the
 * tree IS the tab.
 */
export default function FilesHomePanel({ projectDir, onFileOpen, onAddToContext }: {
  projectDir: string
  onFileOpen: (absPath: string, diff: boolean) => void
  /** Right-click "Add to context" on a tree row — forwarded to the composer
   *  host so a file/folder becomes an `@`-mention. */
  onAddToContext?: (absPath: string, kind: 'file' | 'dir') => void
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const treeState = useTreeState(projectDir)
  const treeAvailable = treeState === 'ready'
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['project-tree', projectDir] })
    qc.invalidateQueries({ queryKey: ['git-status', projectDir] })
  }
  const iconBtn = 'flex items-center justify-center w-[26px] h-[26px] rounded-md cursor-pointer transition-colors text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none shrink-0'
  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="flex items-center gap-2 h-[38px] px-3 shrink-0 border-b border-border">
        <span className="text-[12px] font-semibold text-text-strong">{t('pages.chat.filesHome.title')}</span>
        {projectDir && <span className="text-[11.5px] text-muted truncate" title={projectDir}>{basename(projectDir)}</span>}
        <span className="flex-1" />
        {projectDir && (
          <>
            {/* The rail's own refresh targets the same two queries and awaits
                them, and the error state below carries a labelled Refresh of its
                own, so mounting this unconditionally would put two
                identically-named controls in one view. It covers only the state
                that has neither: a directory whose tree has not resolved yet. */}
            {!treeAvailable && treeState !== 'error' && (
              <button onClick={refresh} className={iconBtn} title={t('pages.chat.filesHome.refresh')} aria-label={t('pages.chat.filesHome.refresh')}>
                <RotateCw size={14} />
              </button>
            )}
            <button onClick={() => api.revealPath(projectDir)} className={iconBtn} title={t('pages.chat.filesHome.reveal_in_finder')} aria-label={t('pages.chat.filesHome.reveal_in_finder')}>
              <ExternalLink size={14} />
            </button>
          </>
        )}
      </div>
      <div className="flex-1 min-h-0 flex">
        <div className="flex-1 min-w-0 flex flex-col items-center justify-center gap-2 text-muted px-6 text-center">
          <FileText size={22} className="opacity-40" />
          {treeState === 'error' ? (
            <>
              {/* A failed fetch is not a missing setting: the directory is set
                  (the header is naming it), the tree endpoint just would not
                  serve it. Retrying is the remedy, so the affordance sits with
                  the message instead of only as a header icon. */}
              <span className="text-[12.5px]">{t('pages.chat.filesHome.tree_error')}</span>
              <button
                onClick={refresh}
                className="text-[12px] px-2.5 h-[26px] rounded-md cursor-pointer transition-colors text-muted hover:text-text hover:bg-bg-hover bg-transparent border border-border"
              >{t('pages.chat.filesHome.refresh')}</button>
            </>
          ) : (
            <span className="text-[12.5px]">
              {treeAvailable ? t('pages.chat.filesHome.select_file_hint') : t('pages.chat.filesHome.no_project_dir')}
            </span>
          )}
        </div>
        {treeAvailable && (
          <FileBrowserRail projectDir={projectDir} onFileOpen={onFileOpen} onAddToContext={onAddToContext} />
        )}
      </div>
    </div>
  )
}
