import { useTranslation } from 'react-i18next'
import { useQueryClient } from '@tanstack/react-query'
import { FileText, ExternalLink, TerminalSquare, MoreHorizontal } from 'lucide-react'
import { useBranding } from '../../hooks/useBranding'
import { revealOrOpen, useRevealFailure, useRevealLabel } from '../../components/FilePathMenu'
import ErrorNotice from '../../components/ErrorNotice'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from '../../components/ui/dropdown-menu'
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
 *
 * The header is also where a PER-PROJECT action lives (issue #1142): it is the
 * one always-present place that names the project directory, so "open a terminal
 * already `cd`'d into it" is offered here instead of requiring the user to know
 * that a side-panel tab kind spawns a shell in the right directory.
 */
export default function FilesHomePanel({ projectDir, onFileOpen, onAddToContext, onOpenTerminal }: {
  projectDir: string
  /** `opts.line` opens the file at that line — a rail content-search hit. */
  onFileOpen: (absPath: string, diff: boolean, opts?: { line?: number }) => void
  /** Right-click "Add to context" on a tree row — forwarded to the composer
   *  host so a file/folder becomes an `@`-mention. */
  onAddToContext?: (absPath: string, kind: 'file' | 'dir') => void
  /** Spawn a terminal tab whose cwd is this project directory. Omitted when the
   *  host cannot serve one (the terminal feature is off, or the host withdraws
   *  the terminal view), which withdraws the action rather than offering a shell
   *  that will not start. */
  onOpenTerminal?: () => void
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
  const treeAvailable = treeState === 'ready'
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['project-tree', projectDir] })
    qc.invalidateQueries({ queryKey: ['git-status', projectDir] })
  }
  // WHY THE ACTION IS A MENU ROW AND NOT A SECOND HEADER BUTTON. The row has to
  // say what a terminal IS, not just name it: a first-time reader identified the
  // control correctly and still would not click it ("a terminal feels like
  // something for developers"), so the label carries a one-line explanation
  // underneath. A 26px icon button in this header has nowhere to put that line —
  // only a tooltip, which is exactly the placement that failed. So the row lives
  // in a `Project actions` menu, whose trigger counts as one control against
  // `max-two-buttons-per-row` however many rows it holds.
  //
  // The row had space for the trigger only because the header's old Refresh
  // button, which stood between them, was UNREACHABLE: with a project directory
  // set, `useTreeState` answers `ready` or `error` and nothing else — `ready`
  // covers the in-flight case on purpose (see its own doc comment) — so
  // `!treeAvailable && treeState !== 'error'` was never true here, and the two
  // existing header tests already asserted no Refresh renders. The reachable
  // refreshes are unaffected: the rail owns one, and the tree-error state below
  // owns the other, which is where the remedy belongs anyway.
  const iconBtn = 'flex items-center justify-center w-[26px] h-[26px] rounded-md cursor-pointer transition-colors text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none shrink-0'
  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="flex items-center gap-2 h-[38px] px-3 shrink-0 border-b border-border">
        <span className="text-[12px] font-semibold text-text-strong">{t('pages.chat.filesHome.title')}</span>
        {projectDir && <span className="text-[11.5px] text-muted truncate" title={projectDir}>{basename(projectDir)}</span>}
        <span className="flex-1" />
        {projectDir && (
          <>
            {isLocal && (
              <button onClick={() => { void revealOrOpen(projectDir, 'reveal', reveal) }} className={iconBtn} title={revealLabel} aria-label={revealLabel}>
                <ExternalLink size={14} />
              </button>
            )}
            {onOpenTerminal && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button className={iconBtn} title={t('pages.chat.filesHome.project_actions')} aria-label={t('pages.chat.filesHome.project_actions')}>
                    <MoreHorizontal size={14} />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="min-w-[260px]">
                  {/* Label plus one muted line, the shape the side panel's `+`
                      menu already uses. The line states what the action IS: an
                      earlier attempt reassured instead ("Nothing runs until you
                      type") and the same reader took the reassurance for a
                      warning, so naming the absence of danger introduced it. */}
                  <DropdownMenuItem className="items-start" onSelect={onOpenTerminal}>
                    <TerminalSquare size={13} className="shrink-0 mt-0.5" />
                    <span className="flex flex-col gap-0.5">
                      <span>{t('pages.chat.filesHome.open_terminal')}</span>
                      <span className="text-[11px] text-muted leading-snug">{t('pages.chat.filesHome.open_terminal_hint')}</span>
                    </span>
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
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
            <>
              {/* A failed fetch is not a missing setting: the directory is set
                  (the header is naming it), the tree endpoint just would not
                  serve it. Retrying is the remedy, so the affordance sits with
                  the message instead of only as a header icon. The Files tab
                  holds no draft → hand-off on, beside the retry. */}
              <ErrorNotice message={t('pages.chat.filesHome.tree_error')} askAgent />
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
