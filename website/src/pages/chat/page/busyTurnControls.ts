import { useCallback, useMemo, type MutableRefObject, type RefObject } from 'react'

import type { ComposerHandle } from '../../../chat-core/composer/Composer'
import type { ComposerDraftStore } from '../../../chat-core/composer/draftStore'
import type { MessageQuote } from '../../../chat-core/composer/messageQuote'
import { buildOutgoingTurn, isEmptyTurn } from '../../../chat-core/composer/outgoingTurn'
import type { UseMessageQuote } from '../../../chat-core/composer/useMessageQuote'
import { isNonInteractiveQueued, isSystemDelivery } from '../../../components/QueueStack'
import { useQueuedMessageActions } from '../../../hooks/useQueuedMessageActions'
import { drainPendingChunks } from '../../../lib/pendingChunkDrain'
import { store, type AppDispatch } from '../../../store'
import { appendMessage, appendSlotMessage, capturePendingAskId, clearPendingPermissions, requestStop, selectComposerBusy, type pendingQuestionFor } from '../../../store/chatSlice'
import type { ChatMessage, ChatSlot } from '../../../types'
import { mergeIntoDraft, mergeRecoveredDraft, setDraft } from '../../../utils/chatDrafts'
import { expandAll as expandPasteTokens } from '../../../utils/pasteTokens'
import { handleStopPress, isEscalationState } from '../../../utils/stopDebounce'
import { answerRejectedMessage } from '../../../utils/questionAnswers'
import { interceptSlashCommand, isInterceptedSlashCommand } from '../ChatInput'
import { mintSendId } from '../ChatPageMessageContent'
import type { ComposerDraftStores } from './composerDrafts'
import type { ComposerStaging } from './composerStaging'

interface BusyTurnControlsOptions {
  activeSlot: string | null
  currentSlot: ChatSlot | undefined
  connected: boolean
  activeSlotRef: MutableRefObject<string | null>
  slotRunning: boolean
  messages: ChatMessage[]
  /** The page's send, for the busy-but-not-running case and nothing else. */
  send: (optionText?: string, targetSlot?: string, steerNow?: boolean, isolated?: boolean) => Promise<boolean>
  /** The receipt-aware steer POST (ChatPage's `applySteerReceipt` adapter). */
  steerMutation: { mutate: (vars: { text: string; sendId?: string; slot: string; auto?: boolean; quote?: MessageQuote | null; askId?: string | null }) => void }
  /** The whole-message quote stage (`useMessageQuote`): a steer consumes it
   *  like a send does, and a cancelled queued send hands its quote back here. */
  messageQuote: UseMessageQuote
  composerRef: RefObject<ComposerHandle | null>
  composerSlotRef: MutableRefObject<string | null>
  inputRef: MutableRefObject<string>
  setInput: ComposerDraftStore['set']
  stores: ComposerDraftStores
  staging: ComposerStaging
  /** The question card on screen for the active slot, if any. */
  pendingQuestion: ReturnType<typeof pendingQuestionFor>
  /** Per-slot time of the last soft-stop press (the force-stop arming window). */
  softStopAtMapRef: MutableRefObject<Map<string, number>>
  dispatch: AppDispatch
  /** Reports a refused slash command on the composer's notice, while its
   *  origin chat is still on screen (the restored text is then right below). */
  onSlashRefused?: (res: { stage?: 'open' | 'turn' | 'agent'; error?: string }) => void
}

/**
 * The composer's controls for a turn that is already busy: steering the text
 * into the running turn (or starting a real turn when only sub-agents are
 * running), answering a question card raised by that turn, stopping it, and the
 * queued-message cards' cancel / interrupt / edit / reorder actions.
 */
export function useBusyTurnControls({
  activeSlot,
  currentSlot,
  connected,
  activeSlotRef,
  slotRunning,
  messages,
  send,
  steerMutation,
  messageQuote,
  composerRef,
  composerSlotRef,
  inputRef,
  setInput,
  stores,
  staging,
  pendingQuestion,
  softStopAtMapRef,
  dispatch,
  onSlashRefused,
}: BusyTurnControlsOptions) {
  const { drafts, fileDrafts, pasteDrafts, saveDrafts } = stores
  const { pendingFilesRef, pasteBlocksRef, setPasteBlocks, setPendingFiles, pickedFileTokens, mergeSlotTokens } = staging
  const allQueuedMessages = useMemo(() => messages.filter(m => m.role === 'queued'), [messages])
  // Only user-typed queued messages get the interactive (edit/cancel) card
  // stack. System injections are excluded (isNonInteractiveQueued): sub-agent
  // deliveries collapse into one progress line, and synthetic turn-recovery
  // continuations (tool refusal / stalled turn / stalled tool / interrupted /
  // empty response) are machine-facing orchestration — they drain
  // automatically and must never render as an editable/cancellable "user" card
  // (editing or cancelling one corrupts the recovery). They surface as a
  // compact RecoveryCard in the transcript once dequeued instead.
  const queuedMessages = useMemo(
    () => allQueuedMessages.filter(m => !isNonInteractiveQueued(m)),
    [allQueuedMessages],
  )
  // Count sub-agent deliveries directly (not by subtraction): recovery
  // injections are also excluded from queuedMessages, but they are NOT
  // sub-agent results and must not inflate the delivery progress line.
  const systemDeliveryCount = useMemo(
    () => allQueuedMessages.filter(m => isSystemDelivery(m)).length,
    [allQueuedMessages],
  )

  // A text steer outside the composer (a chip's text, a question card's answer):
  // an optimistic steer bubble the echo reconciles by sendId, then the
  // receipt-aware steer POST, which hands the text back on a refused or
  // unconfirmed steer.
  const steerText = useCallback((text: string, slot: string, auto = false) => {
    const steerSendId = mintSendId()
    drainPendingChunks()
    dispatch(appendMessage({ role: 'user', content: text, cls: 'msg msg-u', ts: new Date().toISOString(), meta: { steer: true, optimistic: true, sendId: steerSendId } }))
    steerMutation.mutate({ text, sendId: steerSendId, slot, auto })
  }, [steerMutation, dispatch])

  // Mid-turn steer: inject the composer content into the RUNNING turn instead
  // of queueing for the next one. Mirrors send()'s payload prep so pending
  // files ride along — images become `![image](path)` markdown and other
  // files `[attached_file N]` tokens. kiro-cli's `_session/steer` is a
  // text-only channel, so unlike a queued send the image travels as its
  // absolute path for the agent to open with a tool, not as an inline
  // content block. Paste tokens are expanded for the LLM the same way
  // send() does. The POST goes through ChatPage's steerMutation; fire-and-forget
  // — the backend falls back to the queue if steer is unavailable, and echoes
  // the text inline via the 'steer_push' WS event. Composer, pending files,
  // paste blocks, and the per-slot drafts are all cleared HERE (not in
  // ChatInput) so text and attachments clear atomically.
  const steer = useCallback((opts?: { auto?: boolean; text?: string }) => {
    if (!activeSlot) return
    // A follow-up chip's own text, steered as-is: like an option send, it
    // leaves the composer draft, attachments and staged quote alone.
    const optionText = opts?.text
    // Nothing to inject into: the composer is busy purely because background
    // sub-agents are still running for this slot (spawn_run is fire-and-forget,
    // so the parent turn already ended). The intent is the same — act on this
    // text now, don't park it — so start a real turn through the normal send
    // path, which carries `ws=1` and so streams, and flag it to skip the
    // server-side hold that keeps a user message behind running sub-agents.
    // Delegating here, BEFORE the composer is read and cleared below, leaves
    // send() owning the draft, attachment and optimistic-bubble bookkeeping.
    if (!slotRunning) { void send(optionText, undefined, true); return }
    if (optionText) {
      // Offline, a chip send is a no-op, as send() makes it: the chip stays.
      if (!connected) return
      composerRef.current?.voice()?.disarmForSend()
      steerText(optionText, activeSlot, opts?.auto === true)
      return
    }
    const askAtSteer = capturePendingAskId(store.getState().chat.pendingQuestions, activeSlot)
    const raw = inputRef.current.trim()
    const files = pendingFilesRef.current
    // A staged quote alone is a payload; a slash command is not a send, so the
    // quote stays staged through it.
    const steerQuote = isInterceptedSlashCommand(raw) ? null : messageQuote.consume('').quote
    if (isEmptyTurn({ text: raw, files, quote: steerQuote })) return
    // Same rule as send(): a steer while STREAMING dictation is live ends the
    // dictation before the composer is cleared below. AFTER the empty-payload
    // check, like send(): an Enter on an empty composer before the first
    // partial has landed sends nothing, so it must not end the capture — that
    // would drop the utterance in flight with nothing to show for it.
    composerRef.current?.voice()?.disarmForSend()
    // Client-side slash commands (/side, /onboarding) are UI commands, not
    // turn content: they must work identically whether the agent is mid-turn
    // or idle. Without this guard the command text is steered into the
    // running turn as a literal message and the command never runs (#1857).
    // interceptSlashCommand is async, so gate on the sync matcher first and
    // fire-and-forget the handler — same contract as send()'s intercepted
    // branch, which also doesn't await side-open before clearing the composer.
    if (isInterceptedSlashCommand(raw)) {
      // Expand paste tokens first: a large paste after "/side " sits in the
      // composer as a `[ Paste #N ]` token whose backing block is cleared
      // below — without expansion the side chat would receive the literal
      // token instead of the pasted content.
      const pastes = pasteBlocksRef.current
      const cmdTxt = pastes.length ? expandPasteTokens(raw, pastes) : raw
      // Fire-and-forget, but recoverable: on failure (409 side turn in
      // flight, 400 question too long, side-open rejected) the question is
      // merged back so it is never silently lost. The restore is bound to
      // the ORIGINATING slot, captured here — the user may switch slots
      // before the rejection lands. On-screen and settled (same dance as
      // ChatPage's voice-transcript delivery): merge into the live composer.
      // Otherwise: merge into the origin slot's persisted draft.
      // mergeIntoDraft appends after a paragraph break instead of replacing,
      // so text the user typed in the meantime survives alongside the
      // recovered question (same contract as the hand-off paths).
      const originSlot = activeSlotRef.current
      void interceptSlashCommand(cmdTxt, originSlot, dispatch).then(res => {
        if (!res.intercepted || !res.failed || !originSlot) return
        const onScreen = originSlot === activeSlotRef.current && composerSlotRef.current === originSlot
        if (onScreen) {
          setInput(mergeIntoDraft(inputRef.current, cmdTxt))
          onSlashRefused?.(res)
        } else {
          const merged = mergeIntoDraft(drafts.current[originSlot], cmdTxt)
          setDraft(drafts.current, originSlot, merged)
          // Mid-switch guard (same as the voice-transcript delivery): if the
          // composer still belongs to originSlot — activeSlot advanced in
          // render but the outgoing-slot persist effect hasn't run yet — that
          // effect will flush inputRef.current into drafts[originSlot] and
          // overwrite the merge. Carry the merged value into inputRef too so
          // the flush preserves it.
          if (composerSlotRef.current === originSlot) inputRef.current = merged
          saveDrafts()
        }
      })
      setInput(''); setPasteBlocks([])
      return
    }
    // The text-only steer turn (`buildOutgoingTurn`): files inlined, folder
    // tokens kept in their `@rel/` form (a marker with no `meta.dirs` to
    // replay against would truncate a spaced path), the live paste tokens
    // expanded, the quote opening the text.
    const llmTxt = buildOutgoingTurn({ text: raw, files, pastes: pasteBlocksRef.current, quote: steerQuote }, 'steer').wire
    // Optimistically show the steered text immediately. Steer is the default
    // mid-turn action (split send button), so pressing Enter while a turn is
    // running routes here; without an optimistic bubble the message only appears
    // once the backend echoes it via the 'steer_push' WS event, making it look
    // like nothing happened until the response resumes.
    // Tagged meta.optimistic so the echo reconciles this bubble in place
    // (appendSlotMessage) instead of rendering a duplicate. The sendId is the
    // reconciliation key: it travels in the POST's meta, which both backend
    // paths persist — the accepted-steer row and the new-turn row a steer that
    // races chat_done falls onto — so the bubble is resolvable by id identity
    // whichever path the server took (#6075).
    const steerSendId = mintSendId()
    // Drain the per-frame chunk buffer first: a pre-steer chunk still pending
    // in useWebSocket's buffer means appendMessage's finalize-on-steer finds
    // no streaming row to freeze, so that text would flush BELOW this card
    // and post-steer chunks would append to it (see lib/pendingChunkDrain.ts).
    drainPendingChunks()
    dispatch(appendMessage({ role: 'user', content: llmTxt, cls: 'msg msg-u', ts: new Date().toISOString(), meta: { steer: true, optimistic: true, sendId: steerSendId, ...(steerQuote ? { quote: steerQuote } : {}) } }))
    // The optimistic bubble above stays a STEER bubble for an `auto` send: steer
    // is the answer every refusal keeps, so it is the honest guess while the POST
    // is in flight, and a queue answer replaces this row through the same
    // `queue_push` reconcile a manual queue uses.
    steerMutation.mutate({ text: llmTxt, sendId: steerSendId, slot: activeSlot, auto: opts?.auto === true, quote: steerQuote, askId: askAtSteer })
    // Staged session references are deliberately NOT part of steering: neither
    // carried into the payload nor cleared. Only the TEXT has a restore path
    // (steerMutation hands it back on a refused, failed or unconfirmed steer);
    // attachments and pastes are still discarded, and adding refs to that set
    // would lose a reference the user cannot recover except by dragging again.
    // Leaving them staged is lossless and predictable: the chip stays in the
    // composer and rides the next real send, which does have a full restore path.
    // Drops only this slot's own token sub-map -- see the same-shaped
    // comment at send()'s send-clear site in ChatPage.
    setInput(''); setPendingFiles([]); delete pickedFileTokens.current[activeSlot]; setPasteBlocks([])
    delete drafts.current[activeSlot]; delete fileDrafts.current[activeSlot]; delete pasteDrafts.current[activeSlot]
    saveDrafts()
  }, [activeSlot, slotRunning, connected, send, steerText, steerMutation, messageQuote, saveDrafts, dispatch, setInput, onSlashRefused,
    // Refs and state setters: stable, so none of these re-creates the callback.
    activeSlotRef, composerRef, composerSlotRef, inputRef, drafts, fileDrafts, pasteDrafts,
    pendingFilesRef, pasteBlocksRef, setPasteBlocks, setPendingFiles, pickedFileTokens])

  // The queue-card recipe is shared with every other host that draws a
  // QueueStack over this slot queue (#5891) — see useQueuedMessageActions for
  // why cancel/edit stay optimistic and what is deliberately left to item 1.
  //
  // Restore MERGES, via the same helper every other recovery site on this page
  // uses (send()'s failed create and failed send). Assigning was this surface's older
  // spelling and it destroyed text: cancelling two cards in a row overwrote the
  // first card's restored draft with the second's, and by then the first card had
  // already been optimistically retired, so that text existed nowhere else.
  // Whatever lands here is persisted into this slot's draft by the draft
  // commit (useComposerDraftLifecycle), so a recovered draft survives a slot switch.
  const restoreQueuedDraft = useCallback(
    (text: string, files: string[], aliases?: Record<string, string[]>, quote?: MessageQuote) => {
      // The stash carries the quote the send consumed: back as a staged card
      // when the stage is free, else in the restored text.
      setInput(prev => mergeRecoveredDraft(prev, messageQuote.recoverInto(text, quote ?? null)))
      // Chips MERGE like the text does: paths join whatever is already staged,
      // deduped, so a re-send serializes each attachment exactly once.
      if (files.length) setPendingFiles(prev => [...new Set([...prev, ...files])])
      // The stash's alias snapshot comes back with them (fork GPT review), so
      // the restored mentions are reconciled again -- without it a
      // hand-deleted mention left a stale chip the next send re-attached.
      if (aliases) {
        const slot = composerSlotRef.current
        if (slot) mergeSlotTokens(slot, aliases)
      }
    },
    [mergeSlotTokens, setInput, setPendingFiles, composerSlotRef, messageQuote],
  )
  const {
    onCancel: handleCancelQueued,
    onInterrupt: handleInterruptQueued,
    onEdit: handleEditQueued,
    onReorder: handleReorderQueued,
    pendingIds: queuePendingIds,
  } = useQueuedMessageActions({
    slot: activeSlot,
    allQueued: allQueuedMessages,
    visibleQueued: queuedMessages,
    restoreDraft: restoreQueuedDraft,
  })
  /** The composer's Stop press: soft stop, then an armed force stop. */
  const stopTurn = () => {
    const slot = activeSlot
    if (!slot) return
    const isEscalation = isEscalationState(currentSlot?.stop_state)
    // Per-slot view over the map, satisfying SoftStopRef so the
    // arming window is measured against THIS slot's soft press.
    const map = softStopAtMapRef.current
    const slotRef = {
      get current() { return map.get(slot) ?? 0 },
      set current(v: number) { map.set(slot, v) },
    }
    const action = handleStopPress(
      isEscalation,
      Date.now(),
      slotRef,
      () => dispatch(requestStop({ slotId: slot, force: false })),
      () => dispatch(requestStop({ slotId: slot, force: true })),
    )
    // 'ignore' = accidental rapid double-tap during the arming window
    if (action !== 'ignore') dispatch(clearPendingPermissions())
  }
  /** A question card's answer that could not be delivered (a 404): kept for an explicit retry. */
  const keepQuestionAnswer = (text: string, questions?: { question: string }[]) => {
    // A 404 means the blocked wait is gone and the card has
    // already cleared. Keep the user's answer in the composer
    // for an explicit retry instead of auto-sending: even with
    // a live WS, /api/chat can resolve with an HTTP error (for
    // example Kiro becoming unavailable), which would otherwise
    // leave the answer only in a non-persisted optimistic bubble.
    setInput((prev) => (prev.trim() ? `${prev}\n${text}` : text))
    // The submit was rejected, so this is an error row (ErrorNotice), not a warn notice.
    if (activeSlot) dispatch(appendSlotMessage({ slot: activeSlot, message: answerRejectedMessage(questions) }))
  }
  /** A question card's one-click answer. */
  const answerQuestionCard = (text: string) => {
    // No-ask_id card: the card IS the interaction, so answer
    // and send in one click.
    //
    // Offline, both paths below would clear the card and drop
    // the answer, so keep it in the composer for retry — the
    // same recovery the 404 path uses.
    if (!connected) {
      setInput((prev) => (prev.trim() ? `${prev}\n${text}` : text))
      return
    }
    // A native AskUserQuestion card is raised WHILE its own
    // turn is still running and waiting on the answer, so a
    // plain send would queue behind that turn and the question
    // would never be consumed (#10634). When the slot's turn
    // is live, inject the answer INTO it through the same
    // receipt-aware steer path `steer()` uses:
    // `steerMutation` hands the text back and shows the
    // delivery-unconfirmed notice on a `response-late`, so a
    // busy steer whose bubble is suppressed can never silently
    // lose the answer (the loss a raw `send(…, steerNow)`
    // through send()'s bare `response-late` return would risk).
    // `selectComposerBusy` is the shared "turn is live for this
    // slot" rule (chatSlice) both surfaces key on, so the two
    // routes cannot drift.
    //
    // When the turn has already ended (the card outlived it),
    // there is nothing to steer into: fall back to an ordinary
    // next-turn send, exactly as the non-blocking `ask_question`
    // card always does.
    //
    // Steer ONLY the native card, which the server marks
    // `native` on the `question_card` frame and the /pending
    // row. The non-blocking `ask_question` MCP card carries
    // the same server `card_id` but no such mark: it can be
    // answered while sub-agents keep the slot busy, and it
    // must still start a next turn.
    const slot = activeSlot || undefined
    const isNativeCard = pendingQuestion?.native === true
    if (slot && isNativeCard && selectComposerBusy(store.getState(), slot)) {
      steerText(text, slot)
      return
    }
    void send(text, slot)
  }
  return {
    queuedMessages, systemDeliveryCount, steer, stopTurn, keepQuestionAnswer, answerQuestionCard,
    handleCancelQueued, handleInterruptQueued, handleEditQueued, handleReorderQueued, queuePendingIds,
  }
}
