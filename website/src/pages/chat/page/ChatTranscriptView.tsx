import type { RefObject, Dispatch, SetStateAction, ReactNode } from 'react'
import { motion } from 'framer-motion'
import type { ChatNavigationData } from '../../../hooks/useChatNavigation'
import type { UseVirtualChatReturn, VirtualItem } from '../../../hooks/virtualizer/types'
import type { ChatSlot, ChatMessage } from '../../../types'
import type { PinnedPromptState } from '../../../utils/pinnedPrompt'
import type { TurnItem, DisplayItem } from '../types'
import { toApiDecision } from '../../../utils/approvalDecision'
import { isHiddenInvisibleAssistantRow } from '../../../utils/invisibleText'
import { api } from '../../../api/client'
import { ChatTranscriptSkeleton } from '../../../components/ChatTranscriptSkeleton'
import CollapsibleToolGroup from '../CollapsibleToolGroup'
import { MeasureFarm } from '../../../hooks/virtualizer/MeasureFarm'
import { msgIdentityKey } from '../ChatPageMessageContent'
import type { useActiveSlotState } from './activeSlotState'
import { DOCK_CLEARANCE_PX } from '../composerDockMetrics'
import EarlierMessagesBar from '../EarlierMessagesBar'
import TranscriptScrollShell from '../TranscriptScrollShell'
import TurnNavigationMinimap from '../TurnNavigationMinimap'
import WelcomeView from '../../../components/WelcomeView'
import SessionPulseSurveyCard from '../../../components/SessionPulseSurveyCard'
import { ChatFooter } from '..'
import type { ChatConfig } from '../ChatSettings'
import TurnBlock from '../TurnBlock'

/**
 * Height of the transcript's tail spacer, in px.
 *
 * This plus the scroller's own dock clearance is the gap between the last line of
 * the transcript and the top of the composer dock when the reader is at the
 * bottom. It is a px value and not `vh` on purpose: as `2vh` the clearance tracked
 * the viewport and the margin was one pixel at 844px tall, so every shorter
 * viewport — i.e. every phone — landed the last line under the dock.
 */
const TRANSCRIPT_TAIL_SPACER_PX = 16

interface ChatTranscriptViewProps {
  embedded?: boolean
  popout?: boolean
  activeSlot: string | null
  appToolCallIds: ReadonlySet<string>
  titleInTopbar: boolean
  activityOpen: boolean
  completedTurnCount: number
  cursorIsForActiveSlot: boolean
  kiroCrewVersion: string
  loadingOlder: boolean
  olderFailed: boolean
  setTurnDisclosureFor: (key: string, expanded: boolean) => void
  slotHasMore: boolean
  slotLoading: boolean
  slotRunning: boolean
  slotState: ReturnType<typeof useActiveSlotState>['slotState']
  environmentStart: ReturnType<typeof useActiveSlotState>['environmentStart']
  slotStopping: boolean
  slotSwitchTarget: string | null
  turnDisclosure: Record<string, boolean>
  turnRowMayBeLive: (it: TurnItem) => boolean
  setInput: (next: string | ((prev: string) => string)) => void
  isWelcomeState: boolean
  chatConfig: ChatConfig
  scrollerRef: RefObject<HTMLDivElement>
  bindScroller: Dispatch<SetStateAction<HTMLDivElement | null>>
  handleSurveyLayoutChange: () => void
  onScrollPin: () => void
  pinned: PinnedPromptState | null
  approve: (action: string) => Promise<void>
  dismissApproval: (aid: string, decision?: string) => void
  currentSlot: ChatSlot | undefined
  regenerating: boolean
  sendUnconfirmed: boolean
  dockH: number
  lastRole: string
  streamTick: number
  toggleAct: () => void
  renderedDisplayItems: DisplayItem[]
  stableMsgKey: (m: ChatMessage) => string
  virtualKey: (it: DisplayItem, i: number) => string
  handleLoadEarlier: () => void
  spinnerNearTop: boolean
  virt: UseVirtualChatReturn<DisplayItem>
  visibleDisplayItems: VirtualItem<DisplayItem>[]
  chatNav: ChatNavigationData
  navigateToTurn: (displayIndex: number, opts?: { instant?: boolean; }) => void
  turnStepAnnouncement: string
  renderFarmItem: (i: number) => React.ReactNode
  renderMessage: (i: number, m: ChatMessage) => ReactNode
  renderTurnItem: (it: TurnItem, _j: number) => JSX.Element | null
}

/**
 * The transcript column: the welcome hero, or the turn minimap and the scroll shell with its rows, the earlier-messages bar, the measurement farm, the footer, the pulse survey and the tail spacer; and the restore skeleton.
 */
export default function ChatTranscriptView({
  embedded, popout, activeSlot, appToolCallIds, titleInTopbar, activityOpen, completedTurnCount, cursorIsForActiveSlot, kiroCrewVersion, loadingOlder, olderFailed, setTurnDisclosureFor, slotHasMore,
  slotLoading, slotRunning, slotState, environmentStart, slotStopping, slotSwitchTarget, turnDisclosure, turnRowMayBeLive, setInput, isWelcomeState, chatConfig, scrollerRef, bindScroller, handleSurveyLayoutChange,
  onScrollPin, pinned, approve, dismissApproval, currentSlot, regenerating, sendUnconfirmed, dockH, lastRole, streamTick, toggleAct, renderedDisplayItems, stableMsgKey, virtualKey, handleLoadEarlier,
  spinnerNearTop, virt, visibleDisplayItems, chatNav, navigateToTurn, turnStepAnnouncement, renderFarmItem, renderMessage, renderTurnItem,
}: ChatTranscriptViewProps) {
  return (
    <>
      {isWelcomeState ? (
        <motion.div
          key="welcome-hero"
          // `position`, not a full `layout`: the box now changes SIZE on
          // every dock resize (`marginBottom: dockH` below), and a full
          // layout animation would scale the greeting and cards through
          // each change -- the 0 -> dockH first measurement included --
          // instead of moving them. Position still animates.
          layout="position"
          // `isolate`: the hero is its own stacking context. WelcomeView
          // layers its cards with z-indexes of its own (a hovered card
          // grows over the next row at z-10, the Refresh link sits above
          // that at z-20). The dock root deliberately has no z-index, and
          // the composer inside it is z-10, so without a context here those
          // values compared against the composer's and the Refresh link
          // painted ON TOP of the input box. Inside its own context the
          // hero's z-indexes order only the cards against each other.
          // `safe center`: when the welcome column is taller than this
          // box (a phone, a short window), plain centring pushes its top
          // out of scroll reach; safe centring falls back to the start.
          className="isolate flex-1 flex flex-col items-center [justify-content:safe_center] gap-6 px-8 min-h-0 overflow-y-auto"
          // The hero's BOX ends above the dock (a margin, not padding
          // under it): the cards and the Refresh link are controls with
          // labels, and a label blurred under the glass reads as a glitch
          // and a tap there is ambiguous (maintainer ruling on #15820). A
          // column taller than the box scrolls inside it, never under the
          // dock; the memory-mode chip is part of the dock and so sits
          // below the box too.
          style={{ marginBottom: dockH }}
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.18 }}
        >
          <WelcomeView setInput={setInput} />
        </motion.div>
      ) : (
      <>
      {/* Speaks the turn an Alt+↑ / Alt+↓ step landed on. */}
      <span className="sr-only" aria-live="polite" aria-atomic="true">{turnStepAnnouncement}</span>
      <TurnNavigationMinimap
        items={chatNav.sections}
        scrollerRef={scrollerRef}
        onNavigate={navigateToTurn}
        // The rail maps loaded turns only; while the server holds older
        // rows its labels say "of N loaded" (#8221's disclosure).
        windowed={slotHasMore && cursorIsForActiveSlot}
        side={chatConfig.minimapSide}
      />
      <TranscriptScrollShell
        // The 64px spacer clears the inline title row; with the row in
        // the shell's bar (phone) the transcript starts at the top of the
        // column and the spacer would be a blank band under the bar.
        headerSpacer={!titleInTopbar}
        scrollerRef={scrollerRef}
        onScrollerElement={bindScroller}
        onScroll={onScrollPin}
        virt={virt}
        loadingOlder={loadingOlder}
        spinnerNearTop={spinnerNearTop}
        // The strip of the scroller the floating dock covers, plus
        // DOCK_CLEARANCE_PX, alongside TRANSCRIPT_TAIL_SPACER_PX. Unlike
        // the tail spacer this one also applies to a transcript short
        // enough not to scroll, so both are needed for the last line to
        // clear the dock in every state.
        // `visibility` is not one of the properties the shell claims, so
        // adding it here is inside its documented contract. Hiding rather
        // than unmounting keeps the scroller's geometry and the height
        // cache intact -- the restore needs to WRITE scrollTop while this
        // is up, which a display:none element cannot do.
        scrollerStyle={{ paddingBottom: dockH + DOCK_CLEARANCE_PX, ...(virt.restoreGate ? { visibility: 'hidden' as const } : null) }}
        aboveRows={<>
        {/* Mid-switch `slotHasMore` still describes the outgoing chat, so the cursor
            key gates the bar to match the paging thunk's own precondition. */}
        {slotHasMore && cursorIsForActiveSlot && (
          <EarlierMessagesBar loading={loadingOlder} failed={olderFailed} onLoad={handleLoadEarlier} onFocusRelease={() => scrollerRef.current?.focus()} />
        )}
        </>}
        belowRows={<>
        {/* Off-screen measurement farm. Kept FIRST in `belowRows` — i.e.
            after the rows and before the footer, exactly where it sat
            before the scroll shell was extracted: it renders into a
            zero-height overflow-hidden box, so its position only has to
            be inside the transcript's own column (the width is what
            makes the measurements true). */}
        <MeasureFarm
          enabled={!!activeSlot && !slotLoading}
          // NOT paused on slotRunning: a session with an agent working
          // is "running" for minutes at a stretch (tool executions),
          // and gating the farm on it disabled measured geometry on
          // exactly the sessions people actively read — every row they
          // scrolled to was estimate-priced and corrected under their
          // finger. Streaming regroups are safe: farmRecord revalidates
          // row identity at write time and drops stale writes.
          paused={loadingOlder}
          count={renderedDisplayItems.length}
          originIndex={visibleDisplayItems[0]?.index ?? Math.max(0, renderedDisplayItems.length - 1)}
          isMeasured={virt.farmIsMeasured}
          isMounted={virt.farmRowMounted}
          keyAt={(i) => { const it = renderedDisplayItems[i]; return it ? virtualKey(it, i) : null }}
          record={virt.farmRecord}
          renderItem={renderFarmItem}
          scrollerEl={() => scrollerRef.current}
        />
        {/* Footer */}
        <ChatFooter statusLabel={environmentStart} running={slotRunning} stopping={slotStopping} state={slotState} lastRole={lastRole} streamTick={streamTick} regenerating={regenerating} stopState={currentSlot?.stop_state} sendUnconfirmed={sendUnconfirmed} />
        {activeSlot && !slotLoading && !embedded && !popout && slotSwitchTarget !== activeSlot && (
          <div className="px-4 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
            <SessionPulseSurveyCard
              // Remount on session switch: without this, React reuses
              // the same component instance across sessions, so an
              // in-progress rating/feedback/email from session A would
              // still be sitting in state when the user switches to
              // session B and hits Submit — attributing A's answers to
              // B's sessionId prop, which had already updated.
              //
              // Gated on !slotLoading: the card captures its baseline
              // turn count on FIRST MOUNT (see the component's own
              // comment), so mounting before history finishes loading
              // would baseline at 0 and then count every loaded
              // historical turn as "live" once the fetch resolves —
              // reintroducing the exact reopened-session bug the
              // baseline exists to prevent, just via a race instead of
              // a missing check.
              key={activeSlot}
              sessionId={activeSlot}
              kiroCrewVersion={kiroCrewVersion}
              turnCount={completedTurnCount}
              slotOrigin={currentSlot?.origin}
              onLayoutChange={handleSurveyLayoutChange}
            />
          </div>
        )}
        {/* Tail spacer, in px rather than vh. It plus the scroller's own
            bottom padding is the clearance between the last line of the
            transcript and the top of the floating composer dock, so
            expressing it in `vh` made that clearance viewport-dependent: at
            2vh + 8px it cleared by 1px at 844px tall and cut INTO the last
            line on anything shorter (−1px at 740, −2px at 700, −5px at 560),
            which is the sliced-glyph hairline reported from a phone and the
            reason it looked mobile-only. */}
        <div style={{ height: TRANSCRIPT_TAIL_SPACER_PX }} />
        </>}
      >
        {/* Message items — only the mounted window renders; everything
            else is represented by the top/bottom spacers. */}
        {visibleDisplayItems.map((vi) => {
          if (!vi.mounted) return null
          const item = vi.data
          const displayIdx = vi.index
          // A hidden invisible-only assistant row grouped as a loose
          // single (short quiet-cycle batches never wrap into a turn)
          // draws nothing in renderMessage; skip its measured py-1
          // wrapper too, or each quiet cycle leaves an empty spacer row.
          if (item.kind === 'single' && isHiddenInvisibleAssistantRow(item.msg)) return null
          if (item.kind === 'turn') {
            return <div key={vi.key} ref={virt.measureRef(vi.index)} data-display-index={displayIdx}><TurnBlock turn={item} renderItem={renderTurnItem} collapseAll={chatConfig.collapseAllSteps} appToolCallIds={appToolCallIds} disclosure={turnDisclosure[vi.key]} disclosureKey={vi.key} onDisclosureChange={setTurnDisclosureFor} mayBeLive={turnRowMayBeLive} /></div>
          }
          // This row's bubble is the one the pinned banner is standing in for
          // (identity rule below): hide the row and mark it for index.css. The
          // marker reads `folding` while the hook reports the row's re-shown
          // action strip still UNCOVERED — some of it below the card's resting
          // bottom, on screen. That is the whole fold and the strip's own
          // height of scroll after it: the card rests at its clamp while the
          // strip, hanging under the bubble, is still sliding under it, so a
          // marker keyed on the fold alone dropped the strip out from under
          // the pointer for that last stretch. Once the strip is under the card
          // or behind the header the marker is empty and the strip hides with
          // its row again, or Tab would stop on controls nobody can see.
          const pinnedStandin = !!(pinned && (pinned.ts != null
            ? (item.kind === 'single' && item.msg.ts === pinned.ts)
            : pinned.idx === displayIdx))
          return <div key={vi.key} ref={virt.measureRef(vi.index)} data-display-index={displayIdx} className={`px-4 mx-auto w-full py-1`} data-pinned-standin={pinnedStandin ? (pinned?.stripUncovered ? 'folding' : '') : undefined} style={{
            maxWidth: 'var(--mc-content-width, 900px)',
            // The pinned banner is styled as this row's own bubble and sits
            // at the exact position and width the bubble had when its top
            // edge reached the fold, so leaving both visible is what betrays
            // them as two containers. Hide the real one (visibility, NOT
            // display — the virtualizer must keep measuring its height or
            // the transcript would reflow under the reader) and the bubble
            // appears to simply stop travelling and stick. The row hides the
            // moment its top crosses the fold (the top-edge hand-off), and
            // the card then folds down the BUBBLE's remaining height, so the
            // row's action strip beneath the bubble is never behind the card
            // — `data-pinned-standin` lets index.css re-show that strip
            // (visibility is inherited, so a `visible` descendant of a hidden
            // row is drawn and clickable). Without it a prompt taller than
            // the viewport never shows its copy / copy-link / pin row at all:
            // its bottom is only on screen once its top is above the fold.
            //
            // Match by message IDENTITY (ts), not display index. `pinned.idx`
            // is computed in a scroll rAF against `displayItemsRef`, which is
            // refreshed in a layout effect — but a streaming append or a turn
            // regroup can still shift the list between that read and this
            // render, leaving `pinned.idx` pointing one row off. When it did,
            // the WRONG row was hidden and the real pinned bubble painted
            // alongside the banner — the "two stacked boxes" bug. The ts is
            // stable across any index shift, so it hides the right row every
            // frame; fall back to the index only for a message with no ts.
            visibility: pinnedStandin ? 'hidden' : undefined,
          }}>{item.kind === 'group' ? (() => {
          const unresolvedGroupPerms = item.msgs.filter(m => m.role === 'permission' && !m.meta?.resolved)
          if (item.msgs.every(m => m.role === 'permission')) return null
          return (
          <CollapsibleToolGroup
            count={item.msgs.filter(m => m.role !== 'permission').length}
            disclosureKey={`ctg-${vi.key}`}
            hasPermission={false}
            isRunning={slotRunning && displayIdx === renderedDisplayItems.length - 1}
            permissionMeta={unresolvedGroupPerms.at(-1)?.meta as Record<string, unknown> | undefined}
            pendingPermCount={unresolvedGroupPerms.length}
            onApprove={(() => {
              const aid = unresolvedGroupPerms.at(-1)?.meta?.approval_id as string | undefined
              if (!aid) return approve
              return async (action: string) => {
                await api.resolveApproval(aid, toApiDecision(action))
                dismissApproval(aid)
              }
            })()}
            onViewActivity={toggleAct}
            activityOpen={activityOpen}
          >{item.msgs.map((m, j) => <div key={msgIdentityKey(m, stableMsgKey)}>{renderMessage(item.startIdx + j, m)}</div>)}</CollapsibleToolGroup>)
        })() : renderMessage(item.idx, item.msg)}</div>
        })}
              
      </TranscriptScrollShell>
      </>
      )}
      {/* Restore cover. A session left mid-history reopens on a transcript
          that hydrates in chunks and is only positioned once its anchored
          row lands, so the rows underneath are briefly partial and in the
          wrong place. Showing them means the reader watches the transcript
          assemble and then jump; covering that window turns it into one
          deliberate load. A session left AT the live end never raises this
          -- it is placed on the first commit, with nothing to wait for. */}
      {/* ONE placeholder for both waits. Fetching the slot used to show a
          centred spinner and restoring a reading position used to show
          bars, so two readings of the same fact -- the transcript is not
          ready -- looked like different events. A spinner also says only
          "wait", where a skeleton previews the shape that is coming. */}
      {(slotLoading || virt.restoreGate) && <ChatTranscriptSkeleton />}
    </>
  )
}
