import { useMemo } from 'react'
import { AnimatePresence } from 'framer-motion'

import ErrorNotice from '../../../components/ErrorNotice'
import { TipCard, type useTipTrigger } from '../../../components/TipCard'
import { i18nT } from '../../../i18n/t'
import type { RootState } from '../../../store'
import FolderSuggestionCard from '../FolderSuggestionCard'
import type { useComposerSessionControls } from './sessionControls'

/**
 * The memoized band the composer dock shows above the composer
 * (session-control failures, the folder-suggestion card, the ambient tip).
 * The dock's own measurement — the clearance the scroller underneath pays —
 * is `pages/chat/composerDockMetrics.ts`, shared with the pane.
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
  activeTip,
  dismissTip,
}: ComposerAboveBandOptions) {
  const { sessionControls, sessionControlsError, sessionControlStatusError, chatFolders, chatFoldersError, folderSortMode, folderSortError } = controls
  // Memoized so the composer's memo holds across page renders that change
  // nothing it shows (a pin, a streamed frame): JSX written inline in the prop
  // is a new element on every render. `langGen`: i18nT labels inside.
  return useMemo(() => {
    void langGen
    return (
    <>
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
  }, [sessionControlsError, sessionControlStatusError, chatFoldersError, sessionControls.length, folderSuggestion, activeSlot, chatFolders, folderSortMode, folderSortError, sidebarOnScreen, folderSuggestionAccept, folderSuggestionDecline, activeTip, dismissTip, langGen])
}
