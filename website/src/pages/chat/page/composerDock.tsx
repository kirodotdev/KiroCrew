import { useCallback, useMemo, useRef, useState } from 'react'
import { AnimatePresence } from 'framer-motion'

import ErrorNotice from '../../../components/ErrorNotice'
import { TipCard, type useTipTrigger } from '../../../components/TipCard'
import { i18nT } from '../../../i18n/t'
import type { RootState } from '../../../store'
import FolderSuggestionCard from '../FolderSuggestionCard'
import type { useScrollManager } from '../useScrollManager'
import type { useComposerSessionControls } from './sessionControls'

/**
 * The composer dock that floats over the bottom of the transcript: the
 * clearance it measures for the scroller underneath, and the memoized band it
 * shows above the composer (session-control failures, the folder-suggestion
 * card, the ambient tip).
 */

/**
 * The dock's measured height and scrollbar gutter, whether its status stack
 * holds a bar, plus the composer box ref (the quote flight's target).
 */
export function useComposerDockMetrics(scrollerRef: ReturnType<typeof useScrollManager>['scrollerRef']) {
  const inputAreaRef = useRef<HTMLDivElement>(null)
  // The composer dock floats over the bottom of the transcript scroller, so the
  // scroller has to be told how much of its bottom edge is covered. Measured
  // rather than summed from parts: the dock's height is whatever the status
  // stack, the follow-up chips, the approval bar and the composer's own growth
  // add up to at this instant, and every one of those changes independently.
  // A callback ref, not a mount effect: the dock lives inside the pane's
  // conditional branch, so a `[]` effect can run before it exists and never
  // look again. The ref fires in the commit phase each time the box mounts or
  // unmounts, and its synchronous setState lands before paint — the first
  // painted frame already carries the right padding, where an effect-timed
  // measurement paints one frame with the last line under the glass, then jumps.
  const [dockH, setDockH] = useState(0)
  // Whether the status stack above the composer (the sub-agent tray, the task
  // and workflow bars, the queue cards) holds anything right now. The composer
  // alone may float over the transcript: it is a sparse glass pane. The stack
  // may not: it is dense text, and a paragraph scrolling under the tray's rows
  // collided with them in every theme, worst in glass mode. So while the stack
  // is occupied the page ends the scroller's box above the dock instead of
  // running it under (see ChatPage's `dockReserved`). Read as "some child has
  // rendered height", not the band's own height (its 11px padding/negative-
  // margin pair for QueueStack's fuse leaves it 11px tall when empty) and not
  // child presence (CommandCenterDock and QueueStack keep a zero-height wrapper
  // mounted while they show nothing). Measured in the same callback as the
  // height, since a bar mounting is exactly what resizes the dock.
  const [statusStackOccupied, setStatusStackOccupied] = useState(false)
  // The scroller reserves a `scrollbar-gutter: stable` column on its right, and
  // its rows are centred in the content box that EXCLUDES that column. The dock
  // is inset by the same width, so its column lines up with the transcript's and
  // the thumb stays uncovered down to the pane's bottom edge. Measured, not the
  // 6px the stylesheet asks for: an engine that ignores `::-webkit-scrollbar`
  // reserves its own width.
  const [dockGutter, setDockGutter] = useState(0)
  const dockObserverRef = useRef<ResizeObserver | null>(null)
  const dockRef = useCallback((el: HTMLDivElement | null) => {
    dockObserverRef.current?.disconnect()
    dockObserverRef.current = null
    if (!el) { setDockH(0); setDockGutter(0); setStatusStackOccupied(false); return }
    const measure = () => {
      setDockH(el.offsetHeight)
      const band = el.querySelector<HTMLElement>('[data-testid="composer-status-stack"]')
      setStatusStackOccupied(!!band && Array.from(band.children).some(c => (c as HTMLElement).offsetHeight > 0))
      const sc = scrollerRef.current
      setDockGutter(sc ? Math.max(0, sc.offsetWidth - sc.clientWidth) : 0)
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    // The gutter is the scroller's own reserved column, so watch the scroller
    // too: an engine with overlay scrollbars changes that width without the
    // dock resizing.
    if (scrollerRef.current) ro.observe(scrollerRef.current)
    dockObserverRef.current = ro
  }, [scrollerRef])
  return { inputAreaRef, dockH, dockGutter, dockRef, statusStackOccupied }
}

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
