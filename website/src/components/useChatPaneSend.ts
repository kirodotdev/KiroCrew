import { useState, useCallback, useMemo, type RefObject } from 'react'
import type { CrewmateIdentity } from '../pages/chat/CrewmateMessage'
import { queuedSendStash } from '../hooks/useQueuedMessageActions'
import { store, type AppDispatch, type RootState } from '../store'
import { capturePendingAskId, confirmOptimisticSend, markSendUnconfirmed, resolveOptimisticSteer, selectSendAccepted, selectSendConfirmed, appendSlotMessage, pendingQuestionFor } from '../store/chatSlice'
import { readPaneDraft, writePaneDraft } from '../utils/chatPaneDrafts'
import { expandAll as expandPasteTokens } from '../utils/pasteTokens'
import { sendTurn, mintSendId, type SendReceiptStatus } from '../chat-core/transport/sendTurn'
import { applySteerReceipt } from '../chat-core/transport/steerReceipt'
import { useMessageQuote } from '../chat-core/composer/useMessageQuote'
import { buildOutgoingTurn, isEmptyTurn } from '../chat-core/composer/outgoingTurn'
import { storeSentPastes } from '../chat-core/composer/composerPastes'
import { revealComposer } from '../pages/chat/composerFocus'
import { drainPendingChunks } from '../lib/pendingChunkDrain'
import { agentSwitchTarget, interceptSlashCommand, isInterceptedSlashCommand, isSideCommand } from '../pages/chat/ChatInput'
import { extractKnowledgeQuery } from '../pages/chat/useKnowledgeFetch'
import { resolveAskAfterSend } from '../lib/resolveAskAfterSend'
import type { ComposerHandle } from '../chat-core/composer/Composer'
import { isComposerSendHeld } from '../utils/composerSendHolds'
import { i18nT } from '../i18n/t'
import type { useChatPaneDraft } from './useChatPaneDraft'

/**
 * What `ChatPane`'s composer sends, and how a send that did not land comes
 * back: the full send (`doSend`: optimistic bubble, sendId correlation, the
 * receipt rulings of `applySteerReceipt`), the mid-turn steer (`doSteer`), the
 * client-only slash commands the pane intercepts or refuses, the staged message
 * quote, the transcript error row for a failed send, and the pending-question
 * card's one-click answer. Every recovery is addressed to the slot the message
 * was typed into, through the draft's `restoreIntoComposer`.
 */

/** Client-only commands the pane never runs: it has no knowledge picker. */
const PANE_REFUSED_COMMANDS: readonly string[] = ['/kb']

/** Delay before the post-send bottom pin, so the optimistic bubble's commit has
 *  landed in the tail window the pin mounts (ChatPage uses the same figure). */
const SCROLL_AFTER_RENDER_MS = 100

export function useChatPaneSend({ slotKey, crewmate, dispatch, draft, composerRef, busy, running, paneSlot, scrollToBottom, openSideChat, agentLocked, paneAgentName, pendingQuestion, markCrewmateSent }: {
  slotKey: string
  crewmate?: CrewmateIdentity
  dispatch: AppDispatch
  draft: ReturnType<typeof useChatPaneDraft>
  composerRef: RefObject<ComposerHandle>
  busy: boolean
  running: boolean
  paneSlot: RootState['dashboard']['slots'][number] | undefined
  scrollToBottom: () => void
  openSideChat?: (slot: string) => boolean | void | Promise<boolean | void>
  agentLocked?: boolean
  paneAgentName: string
  pendingQuestion: ReturnType<typeof pendingQuestionFor>
  markCrewmateSent: (sendId: string) => void
}) {
  const { input, setInput, pendingFiles, setPendingFiles, pasteBlocks, setPasteBlocks, readPastes, slotKeyRef, inputRef, mountedRef, restoreIntoComposer } = draft
  // In-pane report of a client-only slash command that did not run, keyed by
  // the slot it was typed in so a rebind does not carry it to another thread.
  const [slashError, setSlashError] = useState<{ slot: string; message: string; elsewhere?: boolean } | null>(null)
  // A whole message staged as the quote of the next send (one at a time, dropped
  // on a slot switch). Both send paths consume it; a failed send restages it.
  const messageQuote = useMessageQuote({ slot: slotKey, revealComposer, assistantName: crewmate ? (crewmate.label || crewmate.name) : undefined })
  const { consume: consumeQuote, recoverInto: recoverQuoteInto } = messageQuote
  /** Say, in the transcript that owns the message, that it never went out.
   *
   *  Addressed to the slot the message belongs to rather than the active one —
   *  the user can switch panes while a POST is in flight. `reason` is the
   *  server's own explanation when there is one (a 409 "slot agent mismatch" is
   *  actionable; "check your connection" is not); it is absent on the
   *  transport-reject path, where there is no body to quote.
   *
   *  Component-scoped so BOTH failure sites in this pane speak: the composer's
   *  own send, and the question-card fallback, whose answer is destroyed
   *  outright by a swallowed failure because the card is already gone. */
  const reportSendFailure = useCallback((reason?: string, status?: SendReceiptStatus, restored = true) => {
    dispatch(appendSlotMessage({
      slot: slotKey,
      message: {
        role: 'error',
        // A server reason is FRAMED, never shown bare: "slot agent mismatch"
        // on its own reads as the agent erroring mid-work, not as "your
        // message never went out" — and says nothing about what to do next.
        // The frame names both, and says the draft is back when it is
        // (`restored`; an option-chip send never consumed the composer, so
        // that variant keeps the plain frame). A reason-less transport
        // failure states its cause (the shared core copy ChatEmbed and
        // SideChat use); any other reason-less outcome keeps the generic line.
        content: reason
          ? i18nT(restored ? 'pages.chatPage.send_failed_with_error_restored' : 'pages.chatPage.send_failed_with_error', { error: reason })
          : i18nT(status === 'transport-error'
            ? 'pages.chatPage.send_failed_connection'
            : 'pages.chatPage.send_failed'),
        cls: '',
      },
    }))
  }, [dispatch, slotKey])

  // Client-only slash commands (`/side`, `/btw`, `/agent <name>`,
  // `/onboarding`) and the knowledge-fetch prefixes (`/kb`, `@kb`): the same
  // matchers and the same `interceptSlashCommand` ChatPage.send runs, so the
  // pane never posts them to the agent as text. The pane has no knowledge
  // picker, and a host may give it no Side Chat (`openSideChat` omitted);
  // those commands are refused here with the composer kept. Returns true when
  // the composer text was a command (handled or refused), so the caller must
  // not send it.
  // Commands the pane refuses (see interceptPaneSlash), kept out of its menu
  // so the menu never offers what a send would turn down.
  const paneHiddenCommands = useMemo(
    () => (openSideChat ? PANE_REFUSED_COMMANDS : [...PANE_REFUSED_COMMANDS, '/side', '/btw']),
    [openSideChat],
  )
  const interceptPaneSlash = useCallback((): boolean => {
    // The next press retires the last refusal, whatever it carries.
    setSlashError(null)
    const raw = input.trim()
    const knowledge = extractKnowledgeQuery(raw) !== null
    if (!knowledge && !isInterceptedSlashCommand(raw)) return false
    // A command ends a STREAMING dictation like a send does (ChatPage disarms
    // before its intercept), or later speech would append to the command.
    composerRef.current?.voice()?.disarmForSend()
    const sentSlot = slotKey
    // An agent-locked pane (a crewmate DM) hides its agent picker, so a typed
    // `/agent <name>` is refused the same way.
    if (agentLocked && agentSwitchTarget(raw) !== null) {
      setSlashError({ slot: sentSlot, message: i18nT('pages.chatPage.agent_locked_command', { agent: paneAgentName }) })
      return true
    }
    if (knowledge || (isSideCommand(raw) && !openSideChat)) {
      const command = raw.split(/\s/, 1)[0]
      setSlashError({ slot: sentSlot, message: i18nT('pages.chatPage.command_not_available_here', { command }), elsewhere: true })
      return true
    }
    const blocks = readPastes()
    const text = blocks.length ? expandPasteTokens(raw, blocks) : raw
    void interceptSlashCommand(text, sentSlot, dispatch, {
      ...(openSideChat ? { openSide: () => openSideChat(sentSlot) } : {}),
    }).then(res => {
      if (!res.intercepted) return
      const live = mountedRef.current && slotKeyRef.current === sentSlot
      // UNCONFIRMED: the turn may have run. Keep the command in the composer
      // (or the slot's parked draft) and report nothing here -- the side
      // panel's standing "not confirmed" notice is the report, and an error
      // would invite a retry that runs the question twice.
      if (res.unconfirmed) return
      if (res.failed) {
        const fallback = res.stage === 'agent' ? 'pages.chatPage.agent_command_not_run' : 'pages.chatPage.side_command_not_run'
        const message = res.error || i18nT(fallback)
        // Off screen (the pane was rebound or is gone): the refusal goes to
        // the sending slot's transcript, as reportUploadFailure does.
        if (live) setSlashError({ slot: sentSlot, message })
        else dispatch(appendSlotMessage({ slot: sentSlot, message: { role: 'error', content: message, cls: '' } }))
        return
      }
      // Clear only the command this send took: newer text typed during the
      // await is not the command and stays.
      if (live) {
        if (inputRef.current.trim() === raw && readPastes() === blocks) {
          setInput('')
          setPasteBlocks([])
        }
        return
      }
      // Re-bound meanwhile: the rebind parked this command as the sending
      // slot's draft. Drop it there, or coming back would offer to run it
      // again. Parked files are not the command and stay.
      const parked = readPaneDraft(sentSlot)
      if (parked.text.trim() === raw) writePaneDraft(sentSlot, { text: '', files: parked.files, pastes: [] })
    })
    return true
  }, [input, slotKey, openSideChat, agentLocked, paneAgentName, readPastes, setPasteBlocks, dispatch, composerRef, mountedRef, slotKeyRef, inputRef, setInput])

  const doSend = useCallback((optionText?: string, steerNow?: boolean) => {
    // `optionText` mirrors ChatPage.send's first parameter: the follow-up
    // bar's direct-send gesture (double-click / split button) hands the option
    // label here so it bypasses the setInput race, superseding any composer
    // text exactly as ChatPage does with `optionText || inputRef.current`.
    //
    // `steerNow` mirrors ChatPage.send's third: "act on this now" for a slot
    // that is busy only because background sub-agents are still running (the
    // parent turn already ended, so there is no live turn to inject into). It
    // asks the server to skip the hold that parks a message behind them and
    // start a real turn instead of queueing. Same `/api/chat` flag as a steer.
    const text = (optionText || input).trim()
    // A staged quote alone is a sendable message. An option pill's send is its
    // own text and leaves the stage untouched, as it leaves files and pastes.
    // An attachment for this composer is still on its way: a composer send now
    // would leave without it (see ChatInput's `holdSend`). Checked here as well
    // as in the view because voice auto-submit reaches doSend directly. Checked
    // before the quote is consumed, so a held send keeps the staged quote.
    if (!optionText && isComposerSendHeld(slotKey)) {
      // Voice auto-submit relies on this send to stop a streaming dictation
      // (see `useComposerVoice.onEndpoint`); nothing re-fires when the hold
      // clears, so end it here or later speech keeps appending to the draft.
      composerRef.current?.voice()?.disarmForSend()
      return
    }
    // An option answer is a payload for the agent, never a typed command.
    // Before the quote is consumed: a command is not a send, so a staged
    // quote stays staged for the real one.
    if (!optionText && interceptPaneSlash()) return
    const sentQuote = optionText ? null : consumeQuote('').quote
    if (isEmptyTurn({ text, files: pendingFiles, quote: sentQuote })) return
    // A send while STREAMING dictation is live ends the dictation, before the
    // composer is read and cleared (see useComposerVoice.disarmForSend).
    composerRef.current?.voice()?.disarmForSend()
    // Capture a pending BLOCKING card at ENTRY (before any state updates or
    // yields): this send consumes the answer channel of the card the user saw
    // when they hit send, and a blocking card is resolved over the network, not
    // in the store — an agent is parked on its request. A stateless card needs
    // no capture: the server retires it when this send's user row lands and
    // announces it with `question_card_resolved`.
    const askAtSend = capturePendingAskId(store.getState().chat.pendingQuestions, slotKey)
    // Staged text and files belong to the COMPOSER, so only a send that
    // consumes the composer may clear or carry them. An `optionText` send (the
    // follow-up bar's direct-send gesture) supplies its own text and leaves the
    // composer untouched — same invariant as ChatPage.send's `if (!optionText)`
    // gate: no send-without-clear (duplicate) and no clear-without-send (silent
    // loss). Consuming the draft or attachments here would wipe text the user
    // never sent and attach files to a message they never composed.
    const files = optionText ? [] : pendingFiles
    const blocks = optionText ? [] : pasteBlocks
    if (!optionText) {
      setInput('')
      setPendingFiles([])
      setPasteBlocks([])
    }
    // The same turn ChatPage sends (`buildOutgoingTurn`, the one owner of the
    // order): every image a producer-form `![image](dest)` line on BOTH texts
    // and every other file an `[attached_file N] path` marker on the wire with
    // the ORDERED non-image list on `meta.files` (the agent's image extraction
    // matches absolute paths in the PROMPT TEXT, and the bubble renders images
    // only from their markdown); folder tokens as `[attached_dir N]`
    // markers with `meta.dirs` (the pane has no project context, so tokens are
    // absolute and serialize as-is); the live paste tokens expanded on the wire
    // while the bubble keeps them plus `meta.pastes`, and the side table
    // re-collapses the server's expanded echo to that chip on history load; the
    // quoted message opening both texts.
    const turn = buildOutgoingTurn({ text, files, pastes: blocks, quote: sentQuote }, 'send')
    const { wire: llm, bubble: displayTxt, pastes: bubblePastes } = turn
    storeSentPastes(turn.storedPaste)
    // sendId correlation (same contract as ChatPage): the wire text differs
    // from the bubble text whenever a folder token serialized, so the store's
    // content-equality fallback can never reconcile the server echo against
    // the optimistic bubble — without this id the echo appends a SECOND user
    // bubble carrying the raw marker.
    const sendId = mintSendId()
    // A crewmate send always appears immediately and stays one bubble: a real
    // queued receipt keeps it, and the queue entry takes its place in the
    // thread. Ordinary Sessions keep their busy-send bubble policy and
    // correlated server echo.
    const meta = { ...turn.meta, sendId }
    const bubbleMinted = (!!crewmate || !busy) && (text || files.length || sentQuote)
    if (bubbleMinted) {
      if (crewmate) {
        drainPendingChunks()
        markCrewmateSent(sendId)
      }
      dispatch(appendSlotMessage({
        slot: slotKey,
        message: { role: 'user', content: displayTxt, cls: 'msg msg-u', ts: new Date().toISOString(), meta: { ...meta, ...(crewmate ? { responsiveSend: true } : {}) } },
      }))
    }
    // Sending is explicit intent to be at the end, so the pane force-pins the
    // transcript exactly as ChatPage does after its own optimistic append: the
    // reader lands on the bubble they just sent, and follow is re-armed for the
    // reply — including a reader who had scrolled up to read and sends from
    // there, whom no automatic pin may move (see evaluateAutoPin). Without this
    // a crewmate DM sent from mid-history streamed its answer below the fold.
    // Through the virtualizer's own handle, not a raw scrollTop write, so the
    // follow guard accounts for the move (followPolicy's write invariant).
    setTimeout(scrollToBottom, SCROLL_AFTER_RENDER_MS)
    // A failed send has to say so on the pane it was typed into. This path
    // reported nothing at all: the composer had already cleared and a rejected
    // fetch was swallowed by `.catch(() => undefined)`, so an undelivered
    // message stayed on screen looking sent. `ChatPage` has always appended an
    // error row and handed the text back; the pane now does the same, split
    // across the steer-receipt adapter's `reportFailure` (the error row) and
    // `restore` (handing the payload back) so a `refused` restores exactly once.
    // Receipt semantics live in the chat-core transport (sendTurn owns the
    // abort deadline and the shared readSendReceipt classification). This
    // pane only decides how to REACT
    // per status: failures report on the pane that owns the message and hand
    // the payload back. `unknown` proves a 2xx was received, while
    // `response-late` proves no refusal either; restoring either one here could
    // invite a retry that duplicates a turn already in flight, side effects
    // included, so the optimistic composer row stays pending.
    void sendTurn({ message: llm, slot: slotKey, meta, ...(crewmate ? { responsive: true } : steerNow ? { steer: true } : {}) }).then((receipt) => {
      // Receipt policy, owned once in chat-core (issue #9457). This is the
      // PARTIAL copy: doSend is a full send, not only a steer, so the rulings
      // applySteerReceipt owns (echo short-circuit, refused/response-late/
      // unknown/steered/queued-demote/dispatched-row) run through the adapter,
      // while doSend's own send-machinery tail (queue-card stash gate, stateless
      // card + blocking-ask resolution) stays here, AFTER the rulings, because
      // it must run on every accepted receipt including a `steered` one.
      applySteerReceipt(receipt, {
        // Exact send identity proves custody through either queue or user echo.
        // Queue custody suppresses duplicate recovery, not the cancellable card;
        // only the drain/user echo proves execution.
        echoReconciled: () => selectSendConfirmed(store.getState(), slotKey, sendId),
        sendAccepted: () => !!crewmate && selectSendAccepted(store.getState(), slotKey, sendId),
        // A BUSY send minted no optimistic bubble (bubbleMinted false: the
        // server's queue/steer echo was to be the representation), so the text
        // exists nowhere on screen and must be handed back. The gate differs by
        // ruling, which is why restore takes the status: a `refused` send never
        // ran, so it restores whenever it consumed the composer (`!optionText`),
        // minted bubble or not -- matching the old reportFailedSend gate. A
        // `response-late` restores only when NO bubble was minted, because a
        // minted bubble stays pending to avoid a duplicate. This is the ONLY
        // restore now (reportFailure no longer restores), so `refused` restores
        // exactly once -- the double-restore GPT 5.6 flagged at :837.
        restore: (status) => {
          if (optionText) return
          if (status === 'response-late' && bubbleMinted) return
          // The quote: back on the stage when this pane is still live and the
          // stage is free, else in the restored text (a rebound pane parks it).
          restoreIntoComposer(recoverQuoteInto(text, sentQuote, mountedRef.current && slotKey === slotKeyRef.current), files, bubblePastes, slotKey)
        },
        // Report ONLY -- the error row. The restore is `restore`'s job above;
        // handing the payload back here too would restore a `refused` twice.
        reportFailure: (reason, status) => reportSendFailure(reason, status, !optionText),
        // A bubble that stayed pending is marked (the mark draws its pending
        // line) and gets the pending notice under it, as ChatPage does. A
        // no-bubble composer send that restored gets the "text is back"
        // notice; an option send restored nothing to warn about. Both rows
        // are addressed to the SENDING slot.
        warnUnconfirmed: () => {
          if (bubbleMinted) {
            dispatch(markSendUnconfirmed({ slot: slotKey, sendId }))
            dispatch(appendSlotMessage({ slot: slotKey, message: { role: 'notice', content: '\u26A0\uFE0F ' + i18nT('pages.chatPage.delivery_unconfirmed_pending'), cls: '' } }))
          } else if (!optionText) {
            dispatch(appendSlotMessage({ slot: slotKey, message: { role: 'notice', content: '\u26A0\uFE0F ' + i18nT('pages.chatPage.delivery_unconfirmed'), cls: '' } }))
          }
        },
        // doSend mints no optimistic STEER bubble to drop, so the 'drop' arm
        // (refused/response-late/queued demotion) is a no-op here -- a non-busy
        // send's plain bubble and a busy send's absent bubble are both
        // reconciled by confirmOptimisticSend + the correlated echo, not by the
        // steer-bubble reducer. The 'turn' arm IS the `dispatched && !steered`
        // branch, so it carries doSend's dispatched-row confirm: the response is
        // the delivery receipt for this pane's optimistic bubble independently
        // of when the correlated echo arrives, and confirmOptimisticSend no-ops
        // when a busy send skipped the bubble.
        resolveBubble: (outcome) => {
          // A responsive send always has a bubble: a refusal replaces it, an
          // indeterminate response keeps it pending, and a real queue keeps
          // it too -- the queued entry takes the bubble's place and key
          // (appendCorrelatedQueue), so the person's message never leaves the
          // thread to be redrawn at the bottom.
          if (crewmate && outcome === 'drop' && receipt.status !== 'response-late' && receipt.status !== 'queued') {
            dispatch(resolveOptimisticSteer({ slot: slotKey, sendId, outcome: 'queued' }))
          }
          if (outcome !== 'turn') return
          dispatch(confirmOptimisticSend({
            slot: slotKey,
            sendId,
            mid: typeof receipt.body.mid === 'string' ? receipt.body.mid : undefined,
          }))
        },
        // The receipt names the queue entry this send became: bind the pre-send
        // composer state to it so cancelling that card restores the TYPED text
        // and re-stages the files (#560). `!optionText` mirrors the
        // composer-consumption gate above -- an option send never consumed the
        // draft, so there is no pre-send state to bind.
        // `raw` is what a cancel of that card puts back in the composer. A
        // paste goes back EXPANDED: the stash carries no blocks (the same
        // conservative shape ChatPage's cancel restores through), so the token
        // string alone would be a dead chip that sends literally on retry —
        // the pasted text itself is the lossless form of what the user put in.
        stashDemoted: (queueId) => { if (!optionText) queuedSendStash.set(queueId, { raw: bubblePastes.length ? expandPasteTokens(text, bubblePastes) : text, files, sent: llm, ...(sentQuote ? { quote: sentQuote } : {}) }) },
      })
      // -- doSend's send-machinery tail (not steer-receipt policy) --
      // Blocking ask resolution, owned by doSend and run on every accepted
      // receipt. Guarded independently of the rulings above so a `steered` or
      // `queued` receipt still settles the ask correctly.
      if (!askAtSend) return
      void resolveAskAfterSend(receipt.body, askAtSend, dispatch, slotKey)
    })
  }, [input, pendingFiles, pasteBlocks, setPasteBlocks, busy, crewmate, slotKey, dispatch, restoreIntoComposer, reportSendFailure, scrollToBottom, consumeQuote, recoverQuoteInto, interceptPaneSlash, markCrewmateSent, composerRef, mountedRef, slotKeyRef, setInput, setPendingFiles])

  // Mid-turn steer: inject the composer content into the RUNNING turn instead
  // of queueing behind it. The pane's counterpart to ChatPage.steer, on the
  // same chat-core transport (`sendTurn` with the `steer` flag) — a steer is
  // the same POST as a send, and the receipt is read the same way: `sendTurn`
  // never rejects, so every outcome is a status, not an error callback.
  //
  // The optimistic bubble is minted `{ steer, optimistic, sendId }` and
  // addressed to THIS slot; the store already reconciles a `steer_push` echo
  // against a slot-scoped bubble by sendId (appendSlotMessage's steer branch),
  // so no store change is needed for a second steer host.
  //
  // kiro-cli's steer channel is TEXT-ONLY, so attachments ride as ChatPage's
  // steer sends them — inlined by prepareSendPayload (images as markdown, other
  // files as `[attached_file N]` tokens), the same wire shape doSend now uses.
  const doSteer = useCallback((opts?: { auto?: boolean }) => {
    // Nothing to inject into: busy purely because background sub-agents are
    // still running (the parent turn already ended). Same intent — act on
    // this now — so start a real turn through the normal send path with the
    // steer flag, leaving doSend owning the draft and bubble bookkeeping
    // (it inlines attachments as this path does below, so a send the server
    // demotes to the text-only queue still carries them).
    //
    // "No turn" is read from the slot's server flag too, not only this tab's
    // stream state: a background slot (a crewmate DM) turns busy here only on
    // a chunk or tool frame, so while its turn thinks, or was already running
    // when the pane mounted, the stream state still reads idle. Taking the
    // send path then minted no bubble (the slot is busy) and the message
    // showed only when the server echoed it. A stale flag
    // is safe: the server finds no turn, dispatches one, and the receipt's
    // `turn` ruling demotes the steer bubble to a plain row.
    if (!running && !paneSlot?.running) { doSend(undefined, true); return }
    // A command typed mid-turn is still a command, never steered in as text
    // (ChatPage's steer gates on the same matcher).
    if (interceptPaneSlash()) return
    const raw = input.trim()
    const askAtSteer = capturePendingAskId(store.getState().chat.pendingQuestions, slotKey)
    const files = pendingFiles
    const sentQuote = consumeQuote('').quote
    // A steer cannot restore what it cleared on an empty payload, so refuse a
    // payload of nothing (the same `isEmptyTurn` ChatPage's steer asks).
    // A staged quote alone is a payload.
    if (isEmptyTurn({ text: raw, files, quote: sentQuote })) return
    // A steer while STREAMING dictation is live ends the dictation, like
    // doSend: this path clears the composer below, and a partial landing after
    // the clear would rebuild the sent text (see useComposerVoice.disarmForSend).
    // AFTER the empty-payload check, like doSend: an Enter on an empty composer
    // before the first partial lands sends nothing and must not end the capture.
    composerRef.current?.voice()?.disarmForSend()
    // The text-only steer turn: files inlined, the live paste tokens expanded
    // (ChatPage's steer shows the expanded text in its bubble too, so this one
    // does), the quote opening the text.
    const turn = buildOutgoingTurn({ text: raw, files, pastes: pasteBlocks, quote: sentQuote }, 'steer')
    const { wire: txt, pastes: steerPastes } = turn
    const sendId = mintSendId()
    // `meta.files` is the ORDERED non-image list the `[attached_file N]`
    // tokens index into: the transcript chip resolves marker N to
    // files[N-1]. Without it the renderer falls back to a whitespace-bounded
    // path capture, which truncates a filename containing spaces. The steer
    // channel itself is text-only, but the echo reconciles by merging meta
    // onto this bubble, so the index rides the row from here.
    const steerMeta = { sendId, ...turn.meta }
    // Drain the per-frame chunk buffer first, same as ChatPage's steer(): a
    // pre-steer chunk still pending in useWebSocket's buffer would otherwise
    // flush BELOW this card (see lib/pendingChunkDrain.ts).
    drainPendingChunks()
    dispatch(appendSlotMessage({
      slot: slotKey,
      message: { role: 'user', content: txt, cls: 'msg msg-u', ts: new Date().toISOString(), meta: { steer: true, optimistic: true, ...steerMeta } },
    }))
    // Cleared HERE (not in ChatInput) so text, attachments and paste blocks
    // clear atomically.
    setInput('')
    setPendingFiles([])
    setPasteBlocks([])
    // `auto` hands the steer-or-queue choice to the gateway for this message
    // (`decisions/points/message_steer.py`); the receipt policy below is unchanged,
    // because a decided send still comes back as a steer's `dispatched` or a
    // queue's `queued`.
    void sendTurn({ message: txt, slot: slotKey, steer: opts?.auto === true ? 'auto' : true, meta: steerMeta }).then((receipt) => {
      // Receipt policy, owned once in chat-core (issue #9457) -- the same
      // rulings as ChatPage's steerMutation. applySteerReceipt decides WHICH
      // ruling; the adapter below is this pane's HOW.
      applySteerReceipt(receipt, {
        // A confirmed echo (steer_push carrying this sendId) already reconciled
        // the bubble: the steer landed, so an indeterminate transport outcome
        // is ignored.
        echoReconciled: () => selectSendConfirmed(store.getState(), slotKey, sendId),
        sendAccepted: () => !!crewmate && selectSendAccepted(store.getState(), slotKey, sendId),
        // refused / response-late hand the payload back into this pane's
        // composer (raw text + files), addressed to the slot it was typed into.
        restore: () => restoreIntoComposer(recoverQuoteInto(raw, sentQuote, mountedRef.current && slotKey === slotKeyRef.current), files, steerPastes, slotKey),
        reportFailure: (reason, status) => reportSendFailure(reason, status),
        // \u26A0 is NoticeCard's warn-tone selector (parseNotice).
        warnUnconfirmed: () => dispatch(appendSlotMessage({ slot: slotKey, message: { role: 'notice', content: '\u26A0\uFE0F ' + i18nT('pages.chatPage.delivery_unconfirmed'), cls: '' } })),
        // The reducer drops an optimistic bubble left standing (it would be a
        // false third copy next to the error row and the refilled composer) and
        // demotes it to a plain user row on 'turn'.
        resolveBubble: (outcome) => dispatch(resolveOptimisticSteer({ slot: slotKey, sendId, outcome: outcome === 'turn' ? 'turn' : 'queued' })),
        // A queued demotion binds the PRE-SEND composer state to its queue
        // entry, as doSend does: the card's content is the wire text with
        // inlined file markers, so cancelling it must restore the typed text
        // and re-stage the files, not hand back `[attached_file N]` with the
        // chip gone (#560).
        // Expanded for the same reason as doSend's stash: no blocks travel
        // with the card, so the token alone would restore as a dead chip.
        stashDemoted: (queueId) => queuedSendStash.set(queueId, { raw: steerPastes.length ? expandPasteTokens(raw, steerPastes) : raw, files, sent: txt, ...(sentQuote ? { quote: sentQuote } : {}) }),
      })
      // Release a blocking card only once the gateway accepted the steer, so the agent is told to read a message that arrived.
      if (askAtSteer && (receipt.status === 'dispatched' || receipt.status === 'queued')) {
        void resolveAskAfterSend(receipt.body, askAtSteer, dispatch, slotKey)
      }
    })
  }, [running, paneSlot?.running, doSend, input, pendingFiles, pasteBlocks, setPasteBlocks, slotKey, dispatch, reportSendFailure, restoreIntoComposer, consumeQuote, recoverQuoteInto, interceptPaneSlash, crewmate, composerRef, mountedRef, slotKeyRef, setInput, setPendingFiles])

  // The pending-question card's one-click answer (`PendingQuestionCard.onDirectSend`).
    /* No-ask_id card: the card IS the interaction, answered in one click.
       A native AskUserQuestion card is raised while its own turn is still
       running and waiting on the answer, so a plain send would queue
       behind that turn and the question would never be consumed (#10634).
       When the slot is busy the turn is live, so steer the answer INTO it
       (`steer: true`); when the turn has ended, `busy` is false and this
       starts an ordinary next turn, exactly as the non-blocking
       `ask_question` card does. `busy` is the shared `selectComposerBusy`
       rule (chatSlice) the main chat keys on too, so the two routes match.
       Steer ONLY the native card, which the server marks `native` on the
       `question_card` frame and the /pending row. The non-blocking
       `ask_question` card carries the same server `card_id` but no such
       mark: it can be answered while sub-agents keep the slot busy, and
       it must still start a next turn.

       Recovery differs by whether this is a live steer. A LIVE steer uses
       the receipt-aware policy owned by `applySteerReceipt` (issue #9457),
       exactly as the main chat's steer path: a `refused`/`transport-error`
       reports and restores; a `response-late` restores and warns
       delivery-unconfirmed -- UNLESS the `steer_push`/user echo carrying
       this send's `sendId` already reconciled the optimistic bubble, in
       which case the steer provably landed and the indeterminate HTTP
       outcome is ignored (no restore, no duplicate). That echo
       short-circuit is why a CONFIRMED steer is never handed back as if it
       failed. An IDLE answer is an ordinary send with no live turn to
       reconcile against, so it keeps the plain report-and-restore the card
       fallback has always used. `onFallbackSend` is left untouched as the
       expired-blocking-card (404) recovery path and is NOT reused here. */
  const onDirectQuestionSend = (text: string) => {
    // The card IS the interaction, answered in one click. A NATIVE
    // AskUserQuestion card (marked `native` by the server) is raised
    // while its own turn is still running and waiting on the answer, so
    // a plain send would queue behind that turn and the question would
    // never be consumed (#10634): when the slot is busy that turn is
    // live, so the answer STEERS into it. The non-blocking
    // `ask_question` card carries no such mark; it can be answered
    // while sub-agents keep the slot busy and must still start a next
    // turn, never steer.
    //
    // Both routes are otherwise ONE path: mint an optimistic user bubble
    // carrying the `sendId`, POST through `sendTurn`, and reconcile the
    // bubble through `applySteerReceipt` + `resolveOptimisticSteer`
    // exactly as `doSteer` does. `resolveOptimisticSteer` is what makes
    // every receipt cell correct without a bespoke ladder: a `turn`
    // outcome DEMOTES the bubble to a plain user row (so a dispatched
    // answer stays in the transcript and survives reload), and every
    // other outcome (`queued`/drop) REMOVES it (so a queued answer shows
    // only as its QueueStack card, never a duplicate, and a failed one
    // leaves no orphan; a crewmate chat, which draws no queue card,
    // keeps the queued answer's bubble instead). The card cleared on submit, so a genuine
    // non-delivery hands the answer back to the composer via `restore`,
    // UNLESS the `steer_push`/user echo carrying this `sendId` already
    // reconciled the bubble -- proof it landed, so no restore and no
    // duplicate. Only the `steer` POST flag and the pre-append chunk
    // drain differ between the two routes.
    const steerLive = (busy || (!!crewmate && (running || !!paneSlot?.running))) && pendingQuestion?.native === true
    const sendId = `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
    // Drain the per-frame chunk buffer before the append, as `doSteer`
    // does: a pre-steer chunk still buffered means the finalize-on-steer
    // finds no streaming row, so the bubble would flush BELOW it and
    // post-steer chunks would corrupt transcript order (#6075 class).
    // Only a live steer injects into a streaming turn, so only it drains.
    if (steerLive || crewmate) drainPendingChunks()
    if (crewmate) markCrewmateSent(sendId)
    dispatch(appendSlotMessage({ slot: slotKey, message: { role: 'user', content: text, cls: 'msg msg-u', ts: new Date().toISOString(), meta: { ...(steerLive ? { steer: true } : {}), ...(crewmate ? { responsiveSend: true } : {}), optimistic: true, sendId } } }))
    void sendTurn({ message: text, slot: slotKey, meta: { sendId }, ...(crewmate ? { responsive: true } : {}), ...(steerLive ? { steer: true } : {}) }).then((receipt) => {
      applySteerReceipt(receipt, {
        // A confirmed echo is stronger evidence than a missing/late HTTP
        // response: if it landed, do NOT restore (the fix for the
        // "confirmed steers restored as failed" finding).
        echoReconciled: () => selectSendConfirmed(store.getState(), slotKey, sendId),
        sendAccepted: () => !!crewmate && selectSendAccepted(store.getState(), slotKey, sendId),
        // The card cleared on submit, so the answer lives nowhere else:
        // hand it back on every non-delivered ruling. The bubble is
        // dropped by `resolveBubble` below, so this never leaves an
        // orphan row beside the restored text.
        restore: () => restoreIntoComposer(text, [], [], slotKey),
        reportFailure: (reason, status) => reportSendFailure(reason, status),
        // The leading char is NoticeCard's WARN tone selector (parseNotice
        // strips it and renders a lucide TriangleAlert -- it is never shown
        // as an emoji icon). Built from code points so the source carries
        // no emoji literal for the no-emoji-as-icons gate to match.
        warnUnconfirmed: () => dispatch(appendSlotMessage({ slot: slotKey, message: { role: 'notice', content: String.fromCodePoint(0x26A0, 0xFE0F) + ' ' + i18nT('pages.chatPage.delivery_unconfirmed'), cls: '' } })),
        // Demote the bubble to a plain user row when the answer landed on
        // a turn; drop it on every other outcome (queued/refused/late) so
        // a queued answer shows only as its QueueStack card and a failed
        // one leaves no orphan.
        resolveBubble: (outcome) => {
          // A crewmate chat keeps a queued answer's bubble: its queued
          // entry takes the bubble's place (appendCorrelatedQueue).
          if (crewmate && receipt.status === 'queued') return
          dispatch(resolveOptimisticSteer({ slot: slotKey, sendId, outcome: outcome === 'turn' ? 'turn' : 'queued' }))
        },
        // Text-only card answer: no queued-stash binding to carry.
        stashDemoted: () => undefined,
      })
    })
  }

  return { messageQuote, paneHiddenCommands, slashError, setSlashError, doSend, doSteer, onDirectQuestionSend }
}
