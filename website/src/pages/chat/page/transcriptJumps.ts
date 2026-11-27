import { useEffect, type MutableRefObject } from 'react'

import { clearMessageJump, usePendingMessageJump } from '../../../lib/pendingMessageJump'
import { store, useAppSelector } from '../../../store'
import type { ChatMessage } from '../../../types'
import { resolveMsgIndex } from '../../../utils/shareUrl'
import type { MarkJump } from './jumpMark'
import { drawnSuccessorIdx } from './transcriptPins'

interface TranscriptJumpsOptions {
  messages: ChatMessage[]
  /** Changes when the rows on screen do: a target drawn late is looked for again. */
  messageToDisplayIdx: Map<number, number>
  /** Scroll to the row that draws a message; false when no row on screen draws it. */
  navToMessage: (msg: ChatMessage, opts?: { behavior?: ScrollBehavior; align?: ScrollLogicalPosition; offset?: number }) => boolean
  /** The display index of the row on screen that draws a message, or -1. */
  rowOfMessage: (msg: ChatMessage) => number
  activeSlot: string | null
  cursorIsForActiveSlot: boolean
  slotHasMore: boolean
  slotOldestIndex: number
  /** The session controller's mount-time `?msg=` / `?mid=` / `?sid=` capture. */
  initialMsgRef: MutableRefObject<string | null>
  initialMidRef: MutableRefObject<string | null>
  initialSidRef: MutableRefObject<string | null>
  /** The landing highlight of a row this hook was sent to (`landedTs`). */
  markJump: MarkJump
  /** The pins' jump, which pages back for a message not yet loaded. */
  handleJumpToPinnedMessage: (messageTs: string, mid: string | undefined, opts: { origin: 'link' }) => void
}

/**
 * Scroll intent that targets one message: the approval bar's "Show in chat", a
 * `?msg=` deep link on a cold load (which hands a target outside the loaded
 * window to the pins' paging jump), and a jump Search all chats filed.
 */
export function useTranscriptJumps({
  messages,
  messageToDisplayIdx,
  navToMessage,
  rowOfMessage,
  activeSlot,
  cursorIsForActiveSlot,
  slotHasMore,
  slotOldestIndex,
  initialMsgRef,
  initialMidRef,
  initialSidRef,
  markJump,
  handleJumpToPinnedMessage,
}: TranscriptJumpsOptions) {
  // "Show in chat" button on the approval bar dispatches openActivityToTool,
  // which sets `focusToolCallId`. Pulling a virtualised pill back into the DOM
  // requires Virtuoso's own scrollToIndex — direct DOM scrollIntoView fails
  // because the element doesn't exist. ToolCallLine's own effect then takes
  // over once it mounts: refines the scroll position and clears the focus.
  const focusToolCallId = useAppSelector(s => s.chat.focusToolCallId)
  useEffect(() => {
    if (!focusToolCallId) return
    const target = messages.find(m =>
      m.role === 'tool' && m.meta?.tool_call_id === focusToolCallId
    )
    // `messageToDisplayIdx` runs this again once the rows on screen draw it.
    if (target) navToMessage(target, { behavior: 'smooth', align: 'center' })
  }, [focusToolCallId, messages, messageToDisplayIdx, navToMessage])

  // Deep-link: scroll to ?msg= timestamp on cold load.
  // When ?mid= is also present (copied from a pinned-message link), resolve by
  // mid first (stable per-message identity) and fall back to ts for legacy links.
  // The scroll-to-bottom effect above is suppressed while initialMsgRef is set.
  // Safety net: clear both refs after 5s to restore scroll-to-bottom if deep-link fails.
  useEffect(() => {
    if (!initialMsgRef.current) return
    const timer = setTimeout(() => { initialMsgRef.current = null; initialMidRef.current = null }, 5000)
    return () => clearTimeout(timer)
  }, [initialMsgRef, initialMidRef])
  useEffect(() => {
    const targetTs = initialMsgRef.current
    const targetMid = initialMidRef.current
    if (!targetTs || messages.length === 0) return
    // `messages` can still be the chat being left while a ?sid= switch settles,
    // so decide only once this window is known to belong to the target chat.
    if (initialSidRef.current && initialSidRef.current !== activeSlot) return
    if (!cursorIsForActiveSlot) return
    // The captured pair predates the mount effect that dispatches `switchSlot`, whose
    // `pending` nulls the cursor key even on a same-key switch -- so read it live.
    const liveChat = store.getState().chat
    if (liveChat.slotCursorKey !== liveChat.activeSlot) return
    const resolved = resolveMsgIndex(messages, targetTs, targetMid)
    // A mid that is merely OFF-PAGE falls back to ts in the helper, and that is a
    // DIFFERENT row of the same tick -- treat it as unresolved so the hand-off runs.
    const exactIdx = targetMid && messages[resolved]?.meta?.mid !== targetMid ? -1 : resolved
    // A link to a superseded steer lands on the row drawn in its place.
    const msgIdx = exactIdx < 0 ? exactIdx : drawnSuccessorIdx(messages, exactIdx)
    if (msgIdx < 0) {
      // A bounded first page need not contain the target; the jump path already
      // gates on the cursor and reports a dead link, so the decision lives there.
      initialMsgRef.current = null
      // Carries `targetMid`: paging back re-resolves, and ts alone would pick the
      // wrong message of a same-ts pair that the mid exists to disambiguate.
      handleJumpToPinnedMessage(targetTs, targetMid ?? undefined, { origin: 'link' })
      return
    }
    // Its row is looked for in the rows on screen, and the jump aims at the
    // message: a page that lands in the next 500ms renumbers the rows.
    const target = messages[msgIdx]
    if (rowOfMessage(target) < 0) return
    initialMsgRef.current = null
    initialMidRef.current = null
    setTimeout(() => {
      navToMessage(target, { behavior: 'auto', align: 'center' })
      const markedMid = (target.meta as Record<string, unknown> | undefined)?.mid
      markJump(target.ts ?? targetTs, true, typeof markedMid === 'string' ? markedMid : undefined)
    }, 500)
  }, [messages, messageToDisplayIdx, slotHasMore, slotOldestIndex, handleJumpToPinnedMessage, activeSlot, cursorIsForActiveSlot]) // eslint-disable-line react-hooks/exhaustive-deps

  // A jump another surface asked for (Search all chats), taken once its chat is
  // the active one and the loaded window belongs to it. It goes through the pins'
  // jump, which scrolls to a loaded message or pages back to an older one, and
  // reports a message that is gone as the dead link it is.
  const pendingJump = usePendingMessageJump()
  useEffect(() => {
    if (!pendingJump || pendingJump.slotKey !== activeSlot || !cursorIsForActiveSlot) return
    const liveChat = store.getState().chat
    if (liveChat.slotCursorKey !== liveChat.activeSlot) return
    clearMessageJump(pendingJump)
    handleJumpToPinnedMessage(pendingJump.ts, pendingJump.mid, { origin: 'link' })
  }, [pendingJump, activeSlot, cursorIsForActiveSlot, handleJumpToPinnedMessage])
}
