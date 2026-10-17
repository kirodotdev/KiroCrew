import { useTranslation } from 'react-i18next'
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { FileText, ExternalLink, TerminalSquare, MoreHorizontal, AppWindow } from 'lucide-react'
import { useBranding } from '../../hooks/useBranding'
import { revealOrOpen, useRevealFailure, useRevealLabel } from '../../components/FilePathMenu'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from '../../components/ui/dropdown-menu'
import FileBrowserRail, { useTreeState, useTreeQuery } from './FileBrowserRail'
import { searchErrorCause, TREE_FAILURE_KEYS } from '../../lib/searchErrorCause'
import { reportForError } from '../../utils/errorReport'
import { pathBasename, stripTrailingSeparators } from '../../utils/pathBasename'
import { api } from '../../api/client'
import { canOpenExternalTerminal, detectExternalTerminals, fallbackReason, openInExternalTerminal } from '../../lib/externalTerminal'
import type { ExternalTerminalFallbackNote } from '../../lib/externalTerminal'

/** Last path segment, trailing slashes ignored. */
function basename(p: string): string {
  return pathBasename(stripTrailingSeparators(p)) || p
}

/**
 * The pinned Files tab: the file-browser tree at full width under one header.
 * Clicking a
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
export default function FilesHomePanel({ projectDir, onFileOpen, onAddToContext, onOpenTerminal, onOpenFolder, onExternalTerminalFailed, active = true }: {
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
  /** Open an absolute folder path in its own folder tab; see FileBrowserRail. */
  onOpenFolder?: (absPath: string) => void
  /** A launch from "Open in <app>" was refused or failed: the note to show
   *  while the integrated terminal opens in its place, and whether it sends
   *  the user to Settings → Display → Terminal (`offerSettings`), where the
   *  host links to. The host shows it, because opening that terminal takes
   *  this tab out of view. */
  onExternalTerminalFailed?: (note: ExternalTerminalFallbackNote) => void
  /** False while kept mounted in a hidden panel: see FileBrowserRail. */
  active?: boolean
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
  // Every failed read lands in this panel's own error arm below, whichever of `useTreeState`'s
  // two failure states names it. The `recoverable` split exists for FolderPanel, which names the
  // tree over its one-level listing, and re-reads it on Refresh, only when another read can answer
  // differently; this surface states every failure itself and carries its own labelled Refresh.
  const treeFailed = treeState === 'error' || treeState === 'recoverable'
  // The failed read's own error, for the notice's copy: the verdict above says only that it
  // failed, and a refused or missing root must not read as an outage. Same key as the verdict's
  // query, so this is a second observer on the one request, not a second request.
  const { error: treeErr } = useTreeQuery(projectDir)
  const treeAvailable = treeState === 'ready'
  // "Open in external terminal": the user's own terminal app, beside the
  // integrated one and never in its place. Offered only in the desktop shell
  // (the only place with a launch bridge), against a gateway on this machine,
  // and once an app is picked in Settings (`dashboard.terminal.external_app`,
  // unset by default). Hiding the row is a convenience, not the gate: the
  // gateway refuses the launch itself unless the same conditions hold. The
  // config read is skipped where the row could never show, including a window
  // the desktop shell answers as not capable (a remote crew reached through an
  // SSH tunnel also reads as direct-local), which shows no row and no notice.
  const externalCapable = isLocal && canOpenExternalTerminal()
  const externalWanted = externalCapable && !!onOpenTerminal
  // The installed apps as the desktop shell detects them: the row names the
  // picked app by its display label, and a pick that is no longer installed
  // shows no row (the Settings picker shows it as None), since its launch
  // could only fail.
  const appsQ = useQuery({
    queryKey: ['externalTerminalApps'],
    queryFn: detectExternalTerminals,
    enabled: externalWanted,
  })
  const shellCapable = appsQ.data?.capable === true
  const shellRefused = appsQ.data?.capable === false
  const cfgQ = useQuery<{ dashboard?: { terminal?: { external_app?: unknown } } }>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    enabled: externalWanted && shellCapable,
  })
  // Either read failing means the row's absence says nothing, so it is said
  // instead of leaving the menu silently without it. A window the shell
  // refuses has nothing to report: no Retry could change that answer.
  const externalCheckError = externalWanted && !shellRefused ? (cfgQ.error ?? appsQ.error ?? null) : null
  const [externalCheckDismissed, setExternalCheckDismissed] = useState(false)
  const pickedApp = cfgQ.data?.dashboard?.terminal?.external_app
  const pickedLabel = shellCapable && typeof pickedApp === 'string' && pickedApp !== ''
    ? appsQ.data?.apps.find(a => a.id === pickedApp)?.label
    : undefined
  const showExternal = externalCapable && !!pickedLabel
  // A refused or failed launch opens the integrated terminal instead, and says
  // so with the reason, rather than leaving a click that did nothing. Opening
  // the terminal switches the side panel away from this tab, so the note goes
  // to the host (`onExternalTerminalFailed`), which shows it above whichever
  // tab is in front.
  const openExternal = () => {
    const app = pickedLabel ?? ''
    void openInExternalTerminal(projectDir).then(result => {
      if (result.ok) return
      const reason = fallbackReason(result.error)
      onExternalTerminalFailed?.({
        message: reason === 'unpicked'
          ? t('pages.chat.filesHome.external_terminal_fell_back_unpicked', { app })
          : reason === 'not_installed'
            ? t('pages.chat.filesHome.external_terminal_fell_back_not_installed', { app })
            : reason === 'not_local'
              ? t('pages.chat.filesHome.external_terminal_fell_back_not_local')
              : t('pages.chat.filesHome.external_terminal_fell_back', { app }),
        // The two notes that send the user to the picker link to it.
        offerSettings: reason === 'unpicked' || reason === 'not_installed',
      })
      onOpenTerminal?.()
    })
  }
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
                  {showExternal && (
                    <DropdownMenuItem className="items-start" onSelect={openExternal}>
                      <AppWindow size={13} className="shrink-0 mt-0.5" />
                      <span className="flex flex-col gap-0.5">
                        <span>{t('pages.chat.filesHome.open_external_terminal', { app: pickedLabel })}</span>
                        <span className="text-[11px] text-muted leading-snug">{t('pages.chat.filesHome.open_external_terminal_hint', { app: pickedLabel })}</span>
                      </span>
                    </DropdownMenuItem>
                  )}
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
      {externalCheckError && !externalCheckDismissed && (
        <div className="px-3 py-2 border-b border-border flex flex-col items-start gap-1.5">
          {/* Hand-off on, as for the reveal failure above: the Files tab holds
              no draft. The notice's row carries the hand-off and the dismiss,
              so the retry takes a line of its own under the message, as in the
              tree-error state below (`max-two-buttons-per-row`). It says Retry
              rather than Refresh: it re-runs only the two checks, and a second
              "Refresh" beside the file list's own refresh reads as reloading
              the files. */}
          <ErrorNotice
            variant="inline"
            className="whitespace-normal max-w-full"
            message={t('pages.chat.filesHome.external_terminal_check_failed')}
            report={reportForError(externalCheckError)}
            askAgent
            onDismiss={() => setExternalCheckDismissed(true)}
            testId="files-home-external-terminal-check-error"
          />
          <Btn
            onClick={() => { void appsQ.refetch(); if (cfgQ.isError) void cfgQ.refetch() }}
            className="shrink-0 text-[12px] h-[26px] text-muted hover:text-text"
          >{t('pages.chat.filesHome.retry')}</Btn>
        </div>
      )}
      <div className="flex-1 min-h-0 flex">
        {treeAvailable ? (
          // No preview pane beside the tree: every open spawns a file tab, so a
          // pane here could only ever hold a "select a file" hint. The tree takes
          // the whole tab instead.
          <FileBrowserRail projectDir={projectDir} onFileOpen={onFileOpen} onAddToContext={onAddToContext} onOpenFolder={onOpenFolder} active={active} fill />
        ) : (
        <div className="flex-1 min-w-0 flex flex-col items-center justify-center gap-2 text-muted px-6 text-center">
          <FileText size={22} className="opacity-40" />
          {treeFailed ? (
            <>
              {/* A failed fetch is not a missing setting: the directory is set
                  (the header is naming it), the tree endpoint just would not
                  serve it. Retrying is the remedy, so the affordance sits with
                  the message instead of only as a header icon. The Files tab
                  holds no draft → hand-off on, beside the retry. The copy is
                  cause-keyed through the map the rail and the folder panel
                  share, so a refused or missing root says so here as well
                  rather than wearing the generic "couldn't load" line. */}
              <ErrorNotice
                message={t(TREE_FAILURE_KEYS[searchErrorCause(treeErr)])}
                // The read's own report, as the rail and the folder tab pass it: the
                // translated line is not a journal key, so without this the hand-off
                // carried a sentence naming no endpoint, status or code.
                report={reportForError(treeErr)}
                askAgent
              />
              <button
                onClick={refresh}
                className="text-[12px] px-2.5 h-[26px] rounded-md cursor-pointer transition-colors text-muted hover:text-text hover:bg-bg-hover bg-transparent border border-border"
              >{t('pages.chat.filesHome.refresh')}</button>
            </>
          ) : (
            <span className="text-[12.5px]">{t('pages.chat.filesHome.no_project_dir')}</span>
          )}
        </div>
        )}
      </div>
    </div>
  )
}
