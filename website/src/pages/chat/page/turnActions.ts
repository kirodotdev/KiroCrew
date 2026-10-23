import { useState, useCallback, useMemo, useEffect, type MutableRefObject, type Dispatch, type SetStateAction } from 'react'
import { useMutation } from '@tanstack/react-query'
import { store, type AppDispatch } from '../../../store'
import { isHiddenInvisibleAssistantRow } from '../../../utils/invisibleText'
import { appendSlotMessage, selectSendConfirmed, resolveOptimisticSteer, truncateAfterIndex, replaceMessages } from '../../../store/chatSlice'
import { sendTurn } from '../../../chat-core/transport/sendTurn'
import { applySteerReceipt } from '../../../chat-core/transport/steerReceipt'
import { useMessageQuote } from '../../../chat-core/composer/useMessageQuote'
import { type MessageQuote, stripQuoteBlock } from '../../../chat-core/composer/messageQuote'
import { api } from '../../../api/client'
import { resolveAskAfterSend } from '../../../lib/resolveAskAfterSend'
import { isSystemNoticeRow } from '../CompactionCard'
import { mergeRecoveredDraft, setDraft } from '../../../utils/chatDrafts'
import { revealComposer } from '../composerFocus'
import type { ChatMessage } from '../../../types'
import type { ErrorReport } from '../../../utils/errorReport'
import { i18nT } from '../../../i18n/t'

// Per-action titles for the refused-press notice above the composer. A press
// added later gets its refusal surfaced by adding one entry here and calling
// `showRefusedPress` from its catch — the `as const` map keeps every key
// statically resolvable for the catalog-key gate.
export const REFUSED_PRESS_TITLE_KEYS = {
  continue: 'pages.chatPage.could_not_continue',
  regenerate: 'pages.chatPage.could_not_regenerate',
  switch_variant: 'pages.chatPage.could_not_switch_variant',
  // Two keys because the two /side failures leave different visible states: a
  // refused open leaves no panel, a refused turn leaves the panel open beside
  // the notice — "couldn't open" over an open panel contradicts what the user sees.
  side_open: 'pages.chatPage.could_not_open_side_chat',
  side_turn: 'pages.chatPage.could_not_send_to_side_chat',
  agent_switch: 'pages.chatPage.could_not_switch_agent',
  fresh_session: 'pages.chatPage.could_not_start_fresh_session',
} as const

export type RefusedPressAction = keyof typeof REFUSED_PRESS_TITLE_KEYS

/** The refused-press notice for an intercepted slash command that failed. The
 *  title names the command that was refused; the message is the gateway's
 *  reason, or a fallback saying the draft is still in the composer. */
export function slashRefusal(res: { stage?: 'open' | 'turn' | 'agent'; error?: string }): { action: RefusedPressAction; message: string } {
  if (res.stage === 'agent') {
    return { action: 'agent_switch', message: res.error || i18nT('pages.chatPage.agent_command_not_run') }
  }
  return {
    action: res.stage === 'turn' ? 'side_turn' : 'side_open',
    message: res.error || i18nT('pages.chatPage.side_command_not_run'),
  }
}

interface SteerDeliveryOptions {
  dispatch: AppDispatch
  activeSlot: string | null
  drafts: MutableRefObject<Record<string, string>>
  saveDrafts: () => void
  composerSlotRef: MutableRefObject<string | null>
  composerMountedRef: MutableRefObject<boolean>
  setInput: (next: string | ((prev: string) => string)) => void
  inputRef: MutableRefObject<string>
}

/**
 * Steering a running turn: the steer mutation and its receipt policy (hand the text
 * back when delivery is not proven), together with the two pieces of page state it
 * reports into: the in-page action-error slot and the staged whole-message quote.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useSteerDelivery({
  dispatch, activeSlot, drafts, saveDrafts, composerSlotRef, composerMountedRef, setInput, inputRef,
}: SteerDeliveryOptions) {
  // One in-page slot for every failed action whose only report used to be a
  // notification-centre toast, a native alert() or a swallowed catch (fork,
  // apply-plan, steer, rename, title generation, the agent
  // default-model pin, a session create that failed while sending, a file the
  // panel could not read). Rendered once, above the composer, through
  // ErrorNotice; the newest failure wins, the same shape as `refusedPress`.
  // `title` is optional because several sites already own a whole-sentence
  // message ("Fork failed: …") that must stay intact for the error-journal match.
  const [actionError, setActionError] = useState<{ title?: string; message: string; preserveOnSwitch?: boolean; report?: ErrorReport } | null>(null)
  const showActionError = useCallback((message: string, title?: string, report?: ErrorReport) => {
    // The report is part of the identity: a retry failing the same way carries its
    // OWN diagnostic context, so matching on the copy alone would pin the first one.
    setActionError(prev => (
      prev && prev.message === message && prev.title === title && prev.report === report
    ) ? prev : { title, message, report })
  }, [])
  // NOT fire-and-forget: the receipt is the only thing that knows whether the
  // text reached the running turn, and the optimistic bubble asserts that it did.
  // The same `/api/chat` POST as `send()` with the `steer` flag, through the
  // same transport -- `sendTurn` never rejects, so the outcome is read from the
  // receipt, not from an error callback.
  // A whole message staged as the quote of the next send (one at a time,
  // slot-scoped). `send` and `steer` consume it; the render callback stages it.
  const messageQuote = useMessageQuote({ slot: activeSlot, revealComposer })
  const steerMutation = useMutation({
    // `auto` sends the same POST with `steer: 'auto'`: the gateway then chooses
    // between injecting into the running turn and queueing for the next one
    // (`decisions/points/message_steer.py`). The receipt policy below is unchanged,
    // because the answer arrives as the `dispatched` of a steer or the `queued` of
    // a queue -- both rulings `applySteerReceipt` already owns.
    mutationFn: ({ text, sendId, slot, auto, quote }: { text: string; sendId?: string; slot: string; auto?: boolean; quote?: MessageQuote | null; askId?: string | null }) =>
      sendTurn({ message: text, slot, steer: auto ? 'auto' : true, ...(sendId ? { meta: { sendId, ...(quote ? { quote } : {}) } } : {}) }),
    onSuccess: (receipt, { text, sendId, slot, quote, askId }) => {
      // Receipt policy for a steer, owned once in chat-core (issue #9457):
      // applySteerReceipt decides WHICH ruling applies; the adapter below is
      // ChatPage's HOW. The composer was cleared at submit and the optimistic
      // bubble is NOT persisted -- the next transcript rebuild drops it -- so a
      // steer that did not provably reach the gateway hands its text back.
      // Everything here is addressed to the SENDING slot, not the active one:
      // the user can switch sessions inside the deadline window, and this text
      // and its rows belong to the transcript they were typed into (the same
      // rule send()'s restore and steer-echo append follow).
      //
      // "On screen" means the LIVE composer state belongs to this slot --
      // composerSlotRef, not activeSlotRef: during a slot switch the active
      // slot has already flipped while the composer still holds (and is about
      // to flush) the outgoing slot's text. Writing only the persisted draft in
      // that window would be overwritten by that flush from the stale input;
      // updating the live input instead is what the flush then persists.
      const onScreenNow = composerSlotRef.current === slot
      const handBack = () => {
        const kept = onScreenNow ? inputRef.current : (drafts.current[slot] ?? '')
        // On screen the quote goes back as a staged CARD and the text without
        // its block; off screen (no card store per slot) the block stays in
        // the text, so nothing is lost either way.
        // On screen: the quote goes back on the stage when it is free, else
        // its block stays in the text. Off screen (no stage per slot): the
        // block stays in the parked text. Nothing is lost either way.
        const typed = onScreenNow && quote ? messageQuote.recoverInto(stripQuoteBlock(text, quote), quote, composerMountedRef.current) : text
        const back = mergeRecoveredDraft(kept, typed)
        setDraft(drafts.current, slot, back)
        saveDrafts()
        if (onScreenNow) setInput(back)
      }
      const row = (message: ChatMessage) => dispatch(appendSlotMessage({ slot, message }))
      applySteerReceipt(receipt, {
        // A confirmed echo is stronger evidence than a missing HTTP response,
        // including when a steer raced onto a new turn and lost its steer flag.
        echoReconciled: () => !!sendId && selectSendConfirmed(store.getState(), slot, sendId),
        restore: handBack,
        // The reducer drops only an optimistic bubble; left standing it would be
        // a third, false representation of the same text next to the error row
        // and the refilled composer. 'queued' is the reducer's DROP arm, 'turn'
        // demotes. Guarded on sendId/slot exactly as before: with no sendId the
        // reducer has no key and there is nothing to resolve (the old code's
        // `if (sendId)` on the failure arms and its `if (!sendId || !slot)
        // return` before the accepted arms both collapse to this guard).
        resolveBubble: (outcome) => {
          if (!sendId || !slot) return
          dispatch(resolveOptimisticSteer({ slot, sendId, outcome: outcome === 'turn' ? 'turn' : 'queued' }))
        },
        reportFailure: (reason, status) => row({
          role: 'error',
          content: reason
            ? i18nT('pages.chatPage.send_failed_with_error', { error: reason })
            : i18nT(status === 'transport-error' ? 'pages.chatPage.send_failed_connection' : 'pages.chatPage.send_failed'),
          cls: '',
        }),
        // The lead glyph is NoticeCard's tone selector (parseNotice): \u26A0 =
        // warn, which also gives the row its "Warning" screen-reader label.
        // Kept out of the catalog string so the copy stays shared with the
        // surfaces that render it in their own strip.
        warnUnconfirmed: () => row({ role: 'notice', content: '\u26A0\uFE0F ' + i18nT('pages.chatPage.delivery_unconfirmed'), cls: '' }),
        // ChatPage's steer is TEXT-ONLY and carries no raw/files split into the
        // mutation (`text` is already the wire text; attachments are excluded
        // from ChatPage steer by design, see steer()). It never stashed on a
        // queued demotion and structurally cannot do so losslessly, so this arm
        // stays a no-op -- the queue card falls to the parser fallback exactly
        // as it did before #9457. resolveBubble('drop') still fires for the
        // demotion via the helper's queued path.
        stashDemoted: () => undefined,
      })
      // Release a blocking card only once the gateway accepted the steer, so the agent is told to read a message that arrived.
      if (askId && (receipt.status === 'dispatched' || receipt.status === 'queued')) {
        void resolveAskAfterSend(receipt.body, askId, dispatch, slot)
      }
    },
  })
  return {
    actionError, setActionError, showActionError, messageQuote, steerMutation,
  }
}

/**
 * The refused-press notice above the composer: one state slot for any press the
 * server refused, with the per-action titles and the slash-command refusal mapping.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useRefusedPress() {
  // ---- Refused-press notice ---------------------------------------------------
  // One surface for any press the server refuses. These endpoints re-check under
  // the slot lock and can refuse a press the client believed was available (a
  // turn already running, a stop in progress, a pending approval, a readiness
  // probe that timed out). Left in the console, that refusal reaches the user as
  // the button flicking to disabled and straight back — a control that promises
  // action and then says nothing. The server names the reason; this shows it
  // above the composer with a per-action title. One state slot serves every
  // refusable press (the newest refusal wins), so a press added later inherits
  // the surface by calling `showRefusedPress` instead of re-discovering
  // console.warn. The title map is `as const` so the key gate resolves every
  // member from the single render-site call.
  const [refusedPress, setRefusedPress] = useState<{ action: RefusedPressAction; message: string } | null>(null)
  const showRefusedPress = useCallback((action: RefusedPressAction, e: unknown) => {
    setRefusedPress({ action, message: e instanceof Error && e.message ? e.message : String(e) })
  }, [])
  return {
    refusedPress, setRefusedPress, showRefusedPress,
  }
}

interface RegenerateOptions {
  dispatch: AppDispatch
  activeSlot: string | null
  messages: ChatMessage[]
  /** The page's latest-value ref of `messages`, read by the press handlers so they keep one identity across frames. */
  messagesRef: MutableRefObject<ChatMessage[]>
  slotRunning: boolean
  activeSlotRemoteBound: boolean
  isStreaming: boolean
  setRefusedPress: Dispatch<SetStateAction<{ action: RefusedPressAction; message: string; } | null>>
  showRefusedPress: (action: RefusedPressAction, e: unknown) => void
}

/**
 * Regenerating the last reply: which row is the newest text reply, the regenerate
 * press and its spinner (with the streaming and safety-timeout resets), plus the two
 * effects that retire a stale refused-press notice (slot switch, turn start).
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useRegenerate({
  dispatch, activeSlot, messages, messagesRef, slotRunning, activeSlotRemoteBound, isStreaming, setRefusedPress, showRefusedPress,
}: RegenerateOptions) {
  const lastTextIdx = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      // Agree with renderMessage's skip: a hidden invisible-only row draws
      // nothing, so anchoring Regenerate/variant-switching on it would make
      // those affordances unreachable for the rest of a quiet monitor run.
      // System-notice rows (compaction / session reload) are passed over too:
      // they draw a system card, not a reply, so hosting Regenerate/variant
      // switching on them would target the wrong row.
      if (messages[i].role === 'assistant' && !isHiddenInvisibleAssistantRow(messages[i]) && !isSystemNoticeRow(messages[i])) return i
    }
    return -1
  }, [messages])
  const [regenerating, setRegenerating] = useState(false)
  useEffect(() => { setRegenerating(false) }, [activeSlot])
  // Clear typing dots as soon as streaming starts
  useEffect(() => {
    if (regenerating && isStreaming) setRegenerating(false)
  }, [regenerating, isStreaming])
  // Safety timeout
  useEffect(() => {
    if (!regenerating) return
    const t = setTimeout(() => { setRegenerating(false) }, 30_000)
    return () => clearTimeout(t)
  }, [regenerating])
  useEffect(() => { setRefusedPress(null) }, [activeSlot, setRefusedPress])
  // A turn that actually starts retires the refusal: whatever the slot was busy
  // with is over, so the old reason would now describe a state that passed.
  useEffect(() => { if (slotRunning) setRefusedPress(null) }, [slotRunning, setRefusedPress])
  // Reads `messagesRef`, not `messages`: the array is new on every streamed
  // frame, and this callback feeds the transcript renderers memo, so a
  // `messages` dep re-rendered every mounted row on every frame.
  const handleRegenerate = useCallback(() => {
    if (!activeSlot || regenerating || slotRunning || activeSlotRemoteBound) return
    const messages = messagesRef.current
    // Mirror the server's scan exactly (chat_regenerate.py): the turn being
    // regenerated ends at the last assistant row BY ROLE — hidden
    // invisible-only rows included — so this optimistic truncation cannot
    // diverge from the history rewrite the server persists. System-notice
    // rows (compaction / session reload) ARE skipped, on both sides: they are
    // status rows, not the reply, and capturing one as the variant would drop
    // the real reply from variant history. The skip-aware lastTextIdx only
    // decides which drawn row HOSTS the affordance; its extra invisible-row
    // skip would truncate after an earlier user row than the server does.
    let aiIdx = -1
    for (let i = messages.length - 1; i >= 0; i--) {
      // Never cross a real user turn (mirror the server): a reply found past
      // a newer user row (e.g. a /compact row awaiting only its notice) would
      // be regenerated by deleting that newer turn, irreversibly.
      if (messages[i].role === 'user') break
      if (messages[i].role !== 'assistant') continue
      if (isSystemNoticeRow(messages[i])) continue
      aiIdx = i
      break
    }
    if (aiIdx < 0) return
    const uIdx = messages.slice(0, aiIdx).map(mm => mm.role).lastIndexOf('user')
    if (uIdx < 0) return
    const snapshot = [...messages]
    dispatch(truncateAfterIndex(uIdx + 1))
    setRegenerating(true)
    api.regenerateSlot(activeSlot).catch((e: unknown) => {
      showRefusedPress('regenerate', e)
      dispatch(replaceMessages(snapshot))
      setRegenerating(false)
    })
  }, [activeSlot, regenerating, slotRunning, activeSlotRemoteBound, messagesRef, dispatch, showRefusedPress])
  return {
    lastTextIdx, regenerating, setRegenerating, handleRegenerate,
  }
}
