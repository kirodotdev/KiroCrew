import { useMemo } from 'react'
import { AnimatePresence } from 'framer-motion'
import { FolderGit2 } from 'lucide-react'

import ErrorNotice from '../../../components/ErrorNotice'
import { Glass } from '../../../components/Glass'
import { setMovedSlot, useMovedSlot } from '../../../hooks/useMoveSlotToFolder'
import { pathBasename } from '../../../utils/pathBasename'
import { TipCard, type useTipTrigger } from '../../../components/TipCard'
import VoicePlaybackBar from '../../../components/VoicePlaybackBar'
import { i18nT } from '../../../i18n/t'
import type { RootState } from '../../../store'
import FolderSuggestionCard from '../FolderSuggestionCard'
import { folderProjectMismatch } from '../../../utils/folderAgent'
import type { useComposerSessionControls } from './sessionControls'

/**
 * The memoized band the composer dock shows above the composer (read-aloud
 * playback, session-control failures, the folder-suggestion card, the ambient
 * tip). The dock's own measurement — the clearance the scroller underneath
 * pays — is `pages/chat/composerDockMetrics.ts`, shared with the pane.
 */

interface ComposerAboveBandOptions {
  /** Catalog generation: the band's i18nT labels re-key on a catalog load. */
  langGen: number
  controls: Pick<ReturnType<typeof useComposerSessionControls>,
    'sessionControls' | 'sessionControlsError' | 'sessionControlStatusError' | 'chatFolders' | 'chatFoldersError' | 'folderSortMode' | 'folderSortError'>
  folderSuggestion: RootState['chat']['folderSuggestions'][string] | undefined
  activeSlot: string | null
  /** Whether the sidebar (and its folder-order banner) is on this screen. */
  sidebarOnScreen: boolean
  folderSuggestionAccept: (folderId: string) => void
  folderSuggestionDecline: () => void
  /** The active session's folder and project, for the folder-project hint. */
  slotFolderId: string | undefined
  slotProject: string | undefined
  setProject: (path: string) => Promise<void>
  /** The project picker's lock (sub-agents attached); the hint's rebind obeys it. */
  projectLocked: boolean
  activeTip: ReturnType<typeof useTipTrigger>['tip']
  dismissTip: ReturnType<typeof useTipTrigger>['dismiss']
}

/** The element ChatInput renders above the composer (`aboveComposer`). */
export function useComposerAboveBand({
  langGen,
  controls,
  folderSuggestion,
  activeSlot,
  sidebarOnScreen,
  folderSuggestionAccept,
  folderSuggestionDecline,
  slotFolderId,
  slotProject,
  setProject,
  projectLocked,
  activeTip,
  dismissTip,
}: ComposerAboveBandOptions) {
  const { sessionControls, sessionControlsError, sessionControlStatusError, chatFolders, chatFoldersError, folderSortMode, folderSortError } = controls
  // Memoized so the composer's memo holds across page renders that change
  // nothing it shows (a pin, a streamed frame): JSX written inline in the prop
  // is a new element on every render. `langGen`: i18nT labels inside.
  // A move never rewrites a session's project, so the hint says when the two
  // disagree for the session the user just moved. It stays until they press
  // Keep, switch projects, or move another session. A session created into the
  // folder may differ on purpose (a follow-up worktree), so it is never flagged.
  const movedSlot = useMovedSlot()
  const folderDir = activeSlot && activeSlot === movedSlot
    ? folderProjectMismatch(chatFolders, slotFolderId, slotProject) : undefined
  return useMemo(() => {
    void langGen
    return (
    <>
      <VoicePlaybackBar />
      {/* Session-control failures surface HERE, beside the chips they
          are about, rather than on the chat. Both hooks fail closed —
          a failed `/api/apps` renders no chips, a failed status probe
          renders a stateless one — and either is indistinguishable
          from "no app declares a control", so without this the user
          sees a feature silently missing and has nothing to act on.
          One notice covers both: they are the same feature to the
          user, and the composer shares a row with the message input.
          `askAgent` is on because nothing here holds an unsaved
          draft, and a failed app-list or status route is squarely
          something the agent can investigate.

          The folder query rides along rather than getting its own
          banner: it feeds the folder NAME handed to each control, and
          on `/embed/chat` no sidebar is mounted to consume the shared
          ['chat-folders'] cache — so this is the only place its
          failure can be seen at all. It is gated on a control
          actually existing, though: with no chips on screen a folder
          failure is not a session-control problem, and calling it one
          would put an unexplained notice on every composer. */}
      {(sessionControlsError
        || sessionControlStatusError
        || (chatFoldersError && sessionControls.length > 0)) && (
        <div className="pt-1.5" key="session-controls-error">
          <ErrorNotice
            title={i18nT('components.sessionControlHost.controls_unavailable')}
            message={
              (sessionControlsError || sessionControlStatusError || chatFoldersError)
                ?.message
            }
            askAgent
            variant="inline"
          />
        </div>
      )}
      {/* In-flow tip inside the composer's own width wrapper: shares
       the composer's exact box geometry (Raymond 2026-07-21: tip
       width must always match the input box) while still pushing
       chat content up like QueueStack (team decision: never cover
       thinking/output; queue and question card keep priority via
       tipSuppressed). ChatInput renders this slot LAST in the
       above-composer stack, so the card stays flush against the
       input box and an options row sits above it. */}
      {folderDir && (
        <div className="pt-1.5" key="folder-project-hint">
          <Glass variant="chip" radius={6} role="status" data-testid="folder-project-hint"
            className="glass-accent glass-shadow w-full flex flex-wrap items-center gap-x-2.5 gap-y-1.5 px-4 py-2 rounded-md text-xs">
            <FolderGit2 size={14} className="shrink-0" aria-hidden="true" style={{ color: 'var(--accent)' }} />
            <span className="flex-1 text-[12px] leading-tight min-w-0 break-words" style={{ color: 'var(--text)' }}>
              {i18nT('components.folderProjectHint.differs', { current: pathBasename(slotProject || '') || '—', folder: pathBasename(folderDir) })}
            </span>
            <div className="ml-auto flex flex-wrap items-center gap-1.5 min-w-0 max-w-full">
              <button onClick={() => { void setProject(folderDir) }} disabled={projectLocked} data-testid="folder-project-hint-use"
                title={projectLocked ? i18nT('components.chatInput.project_locked_while_subagents_run') : folderDir}
                className="max-w-full min-w-0 break-words whitespace-normal text-left px-2.5 py-1 rounded text-[11px] font-medium hover:brightness-110 transition disabled:opacity-40 disabled:cursor-not-allowed"
                style={{ color: 'var(--accent)', background: 'color-mix(in srgb, var(--accent) 12%, transparent)', border: '1px solid color-mix(in srgb, var(--accent) 30%, transparent)' }}>
                {i18nT('components.folderProjectHint.use_folder_project', { folder: pathBasename(folderDir) })}
              </button>
              <button onClick={() => setMovedSlot(null)} data-testid="folder-project-hint-dismiss" title={slotProject}
                className="max-w-full min-w-0 break-words whitespace-normal text-left px-2.5 py-1 rounded text-[11px] transition-colors hover:bg-[var(--bg-hover)]" style={{ color: 'var(--muted)', border: '1px solid var(--border)' }}>
                {i18nT('components.folderProjectHint.keep_current', { current: pathBasename(slotProject || '') || '—' })}
              </button>
            </div>
            <span className="basis-full text-[11px] leading-snug min-w-0 break-words" style={{ color: 'var(--muted)' }} data-testid="folder-project-hint-note">
              {projectLocked ? i18nT('components.chatInput.project_locked_while_subagents_run')
                : i18nT('components.folderProjectHint.switch_note', { path: folderDir })}
            </span>
          </Glass>
        </div>
      )}
      <AnimatePresence>
        {folderSuggestion && activeSlot ? (
          <div className="pt-1.5" key="folder-suggestion">
        {/* The card's option list follows the sidebar's folder
            order. A failed read of that order is said once per
            screen -- by the sidebar's banner while the sidebar is
            on this screen, and here, above the card, only when it
            is not (embed chat, the drawer closed, the panel
            collapsed): otherwise the list is drawn in the stored
            order with nothing on screen to say why.
            No hand-off: the card's dropdown holds a pick that is
            not saved until Accept, and the hand-off navigates
            away and unmounts it -- so the line under the notice
            is the one phrase every surface uses for this failure
            plus where the hand-off lives. */}
        {folderSortError !== null && !sidebarOnScreen && (
          <ErrorNotice
            title={i18nT('pages.chatSidebar.folder_order_unavailable')}
            message={folderSortError}
            messagePlacement="below"
            footer={i18nT('pages.chatSidebar.folder_order_unavailable_detail_picker_ask')}
            className="mb-1.5"
            testId="folder-suggestion-order-unavailable"
          />
        )}
            {/* Keyed by the suggestion's ts: a replacement card
                remounts the component, so its dropdown re-prefills
                and a selection made against the previous suggestion
                cannot leak onto the new one. `chatFolders` is the
                sidebar's own ['chat-folders'] cache (normalized to
                [] on error by useComposerSessionControls), so the dropdown costs no extra
                request and degrades to a suggestion-only option
                list when folders are unavailable. */}
            <FolderSuggestionCard
              key={folderSuggestion.ts}
              suggestedFolderId={folderSuggestion.folderId}
              suggestedFolderName={folderSuggestion.folderName}
              suggestedFolderBreadcrumb={folderSuggestion.breadcrumb}
              folders={chatFolders}
          folderSortMode={folderSortMode}
              onAccept={folderSuggestionAccept}
              onDecline={folderSuggestionDecline}
            />
          </div>
        ) : activeTip && (
          <div className="pt-1.5" key="tip">
            <TipCard tip={activeTip} onDismiss={dismissTip} />
          </div>
        )}
      </AnimatePresence>
    </>
    )
  }, [sessionControlsError, sessionControlStatusError, chatFoldersError, sessionControls.length, folderSuggestion, activeSlot, chatFolders, folderSortMode, folderSortError, sidebarOnScreen, folderSuggestionAccept, folderSuggestionDecline, folderDir, slotProject, setProject, projectLocked, activeTip, dismissTip, langGen])
}
