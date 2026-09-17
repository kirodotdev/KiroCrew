import { useTranslation } from 'react-i18next'
import { useQueryClient } from '@tanstack/react-query'
import { FileText, RotateCw, ExternalLink } from 'lucide-react'
import { useBranding } from '../../hooks/useBranding'
import { revealOrOpen, useRevealFailure, useRevealLabel } from '../../components/FilePathMenu'
import ErrorNotice from '../../components/ErrorNotice'
import FileBrowserRail, { useTreeState, useTreeAvailable, useTreeNotice } from './FileBrowserRail'

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
  /** `opts.line` opens the file at that line — a rail content-search hit. */
  onFileOpen: (absPath: string, diff: boolean, opts?: { line?: number }) => void
  /** Right-click "Add to context" on a tree row — forwarded to the composer
   *  host so a file/folder becomes an `@`-mention. */
  onAddToContext?: (absPath: string, kind: 'file' | 'dir') => void
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  // Reveal shells out on the gateway host, so it only makes sense when the
  // browser is on that same machine. On a remote/tunneled session the backend
  // degrades reveal to a clipboard copy, so hide the affordance to match every
  // other gated file-location surface (FilePathMenu, ReportProblemModal, …).
  const isLocal = useBranding().directLocal
  // The platform-aware wording every other file-location surface uses ("Open in
  // Finder" / "Open in File Explorer" / "Show in file manager"), read from the
  // gateway host that `/api/reveal` shells out on — not a static "file manager".
  const revealLabel = useRevealLabel()
  // A failed reveal (policy-blocked path, no file manager) renders under the
  // header; askAgent on — the Files panel holds no draft.
  const reveal = useRevealFailure(projectDir ?? undefined)
  const treeState = useTreeState(projectDir)
  const treeNotice = useTreeNotice(projectDir, t)
  const railMounts = useTreeAvailable(projectDir)
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
            {/* Covers every state the rail does not, including a cause a Refresh cannot answer —
                unlabelled there, so the control exists without the copy promising a remedy. */}
            {!railMounts && (
              <button onClick={refresh} className={iconBtn} title={t('pages.chat.filesHome.refresh')} aria-label={t('pages.chat.filesHome.refresh')}>
                <RotateCw size={14} />
              </button>
            )}
            {isLocal && (
              <button onClick={() => { void revealOrOpen(projectDir, 'reveal', reveal) }} className={iconBtn} title={revealLabel} aria-label={revealLabel}>
                <ExternalLink size={14} />
              </button>
            )}
          </>
        )}
      </div>
      {reveal.error && (
        <div className="px-3 py-2 border-b border-border">
          <ErrorNotice variant="inline" className="whitespace-normal" message={reveal.error} askAgent onDismiss={reveal.clear} testId="files-home-reveal-error" />
        </div>
      )}
      <div className="flex-1 min-h-0 flex">
        <div className="flex-1 min-w-0 flex flex-col items-center justify-center gap-2 text-muted px-6 text-center">
          <FileText size={22} className="opacity-40" />
          {treeState === 'error' ? (
            /* Only NON-recoverable causes reach here, so the composed line carries no remedy: a
               refusal answers the same however often it is re-asked. It still names WHICH refusal,
               because "couldn't load" left the reason to be read off an absent clause. */
            <ErrorNotice message={treeNotice ?? ''} askAgent />
          ) : (
            <span className="text-[12.5px]">
              {/* Silent on a recoverable failure: the rail beside this is already naming it,
                  and promising a tree to pick from would contradict that notice. */}
              {treeState === 'ready' ? t('pages.chat.filesHome.select_file_hint')
                : treeState === 'no-dir' ? t('pages.chat.filesHome.no_project_dir')
                  : null}
            </span>
          )}
        </div>
        {railMounts && (
          <FileBrowserRail projectDir={projectDir} onFileOpen={onFileOpen} onAddToContext={onAddToContext} />
        )}
      </div>
    </div>
  )
}
