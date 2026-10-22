import { useRef, useEffect, useCallback, type MutableRefObject, type Dispatch, type SetStateAction, type RefObject } from 'react'
import type { UseMessageQuote } from '../../../chat-core/composer/useMessageQuote'
import { store, type AppDispatch } from '../../../store'
import type { ChatMessage, ChatSlot } from '../../../types'
import { expandAll as expandPasteTokens, carryPastes, mergeCarriedDraft, type PasteBlock } from '../../../utils/pasteTokens'
import { mergeSessionRefs, type SessionRef } from '../../../utils/sessionRefs'
import type { PickedFileTokens } from './composerFileMentions'
import { queuedSendStash } from '../../../hooks/useQueuedMessageActions'
import { syncSlotRunningFromServer, capturePendingAskId, createSlot, appendMessage, selectComposerBusy, startLocalTurn, endLocalTurn, appendSlotMessage, setSlotRunning, selectSendConfirmed, markSendUnconfirmed, confirmOptimisticSend, ageFolderSuggestion } from '../../../store/chatSlice'
import { confirmedDelivered } from '../../../utils/sendDelivery'
import { mintSendId, sendTurn } from '../../../chat-core/transport/sendTurn'
import type { MessageQuote } from '../../../chat-core/composer/messageQuote'
import { isEmptyTurn, buildOutgoingTurn } from '../../../chat-core/composer/outgoingTurn'
import { storeSentPastes } from '../../../chat-core/composer/composerPastes'
import { addNotification } from '../../../store/notificationsSlice'
import { isInterceptedSlashCommand, interceptSlashCommand } from '../ChatInput'
import { api } from '../../../api/client'
import { resolveAskAfterSend } from '../../../lib/resolveAskAfterSend'
import { uniqueNotificationTs } from './notificationTs'
import { PREFILL_STORAGE_KEY } from '../../../utils/navIntent'
import { isComposerSendHeld } from '../../../utils/composerSendHolds'
import type { ComposerHandle } from '../../../chat-core/composer/Composer'
import { mergeRecoveredDraft, setDraft } from '../../../utils/chatDrafts'
import { setFileDraft } from '../../../utils/chatFileDrafts'
import { setPasteDraft } from '../../../utils/chatPasteDrafts'
import { setSessionRefDraft } from '../../../utils/chatSessionRefDrafts'
import { extractKnowledgeQuery } from '../useKnowledgeFetch'
import { i18nT } from '../../../i18n/t'
import type { useActiveSlotState } from './activeSlotState'
import { slashRefusal, type RefusedPressAction } from './turnActions'

/** Delay (ms) before scrolling to bottom after a state update, giving React time to commit. */
const SCROLL_AFTER_RENDER_MS = 100

interface ComposerSendOptions {
  mode?: string
  dispatch: AppDispatch
  slots: ChatSlot[]
  connected: boolean
  activeSlot: string | null
  knowledgeFetchRef: ReturnType<typeof useActiveSlotState>['knowledgeFetchRef']
  slotRunning: boolean
  drafts: MutableRefObject<Record<string, string>>
  fileDrafts: MutableRefObject<Record<string, string[]>>
  pasteDrafts: MutableRefObject<Record<string, PasteBlock[]>>
  saveDrafts: () => void
  sessionRefDrafts: MutableRefObject<Record<string, SessionRef[]>>
  activeSlotRef: MutableRefObject<string | null>
  composerSlotRef: MutableRefObject<string | null>
  composerMountedRef: MutableRefObject<boolean>
  setInput: (next: string | ((prev: string) => string)) => void
  inputRef: MutableRefObject<string>
  sendingRef: MutableRefObject<boolean>
  pendingAgentRef: MutableRefObject<string>
  pendingAgentKindRef: MutableRefObject<'member' | 'template' | undefined>
  setPendingAgent: (v: string, kind?: 'member' | 'template') => void
  pendingModelRef: MutableRefObject<string>
  setPendingModel: (v: string) => void
  pendingProjectRef: MutableRefObject<string>
  setPendingProject: (v: string) => void
  setActionError: Dispatch<SetStateAction<{ title?: string; message: string; preserveOnSwitch?: boolean; } | null>>
  showActionError: (message: string, title?: string) => void
  messageQuote: UseMessageQuote
  setPrefillHint: Dispatch<SetStateAction<boolean>>
  autoSendRef: MutableRefObject<string | null>
  setAutoSendTick: Dispatch<SetStateAction<number>>
  newSessionRef: MutableRefObject<boolean>
  widgetPrefillRef: MutableRefObject<string | null>
  mergeSlotTokens: (slot: string, aliases: Record<string, string[]>) => void
  pasteBlocksRef: MutableRefObject<PasteBlock[]>
  pendingFilesRef: MutableRefObject<string[]>
  pendingSessionsRef: MutableRefObject<SessionRef[]>
  pickedFileTokens: PickedFileTokens
  setPasteBlocks: Dispatch<SetStateAction<PasteBlock[]>>
  setPendingFiles: Dispatch<SetStateAction<string[]>>
  setPendingSessions: Dispatch<SetStateAction<SessionRef[]>>
  composerRef: RefObject<ComposerHandle>
  currentProjectRef: MutableRefObject<string | undefined>
  colorThemeRef: MutableRefObject<string>
  autoFollowAllowed: () => boolean
  scrollBottom: (instant?: boolean) => void
  setRefusedPress: Dispatch<SetStateAction<{ action: RefusedPressAction; message: string; } | null>>
  createFailReason: (e: unknown) => string
}

/**
 * send(): the main composer's whole turn submission, from slash interception and the
 * outgoing turn through session creation, delivery receipts and every recovery path,
 * together with the turn-boundary effects declared just ahead of it (bring the footer
 * into view when a turn starts; reconcile the running flag from slot frames).
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useComposerSend({
  mode, dispatch, slots, connected, activeSlot, knowledgeFetchRef, slotRunning, drafts, fileDrafts, pasteDrafts, saveDrafts, sessionRefDrafts, activeSlotRef, composerSlotRef, composerMountedRef,
  setInput, inputRef, sendingRef, pendingAgentRef, pendingAgentKindRef, setPendingAgent, pendingModelRef, setPendingModel, pendingProjectRef, setPendingProject, setActionError, showActionError,
  messageQuote, setPrefillHint, autoSendRef, setAutoSendTick, newSessionRef, widgetPrefillRef, mergeSlotTokens, pasteBlocksRef, pendingFilesRef, pendingSessionsRef, pickedFileTokens, setPasteBlocks,
  setPendingFiles, setPendingSessions, composerRef, currentProjectRef, colorThemeRef, autoFollowAllowed, scrollBottom, setRefusedPress, createFailReason,
}: ComposerSendOptions) {
  // Scroll to show Footer when agent starts running (loading indicator appears)
  const prevRunningRef = useRef(false)
  useEffect(() => {
    // LIVE geometry, not just the follow flag. A turn can start without the
    // reader having asked for anything -- a subagent completion, a cron
    // notification and an auto-nudge cycle all flip `slotRunning` on their own --
    // and a stale armed flag then teleports a reader who is deep in history to
    // the bottom. Requiring them to actually BE near the bottom makes that
    // impossible: the flag can be wrong, the distance cannot.
    if (slotRunning && !prevRunningRef.current && autoFollowAllowed()) {
      setTimeout(() => scrollBottom(), SCROLL_AFTER_RENDER_MS)
    }
    prevRunningRef.current = slotRunning
  }, [slotRunning, scrollBottom, autoFollowAllowed])

  // Reconcile the active slot's running state from WS slot updates. The reducer
  // guards against a stale snapshot overwriting an unconfirmed local turn.
  useEffect(() => {
    if (!activeSlot) return
    const s = slots.find(s => s.key === activeSlot)
    if (!s) return
    dispatch(syncSlotRunningFromServer({ slot: s.key, running: s.running, stopping: s.stopping ?? false, turn: s.turn, turn_gen: s.turn_gen }))
  }, [slots, activeSlot, dispatch])

  // Raw send — sends pre-built text directly to the server
  const modeRef = useRef(mode)
  modeRef.current = mode

  // Resolves true when the server accepted the message (dispatched, queued,
  // or received-but-late), false when nothing was delivered (offline, empty,
  // intercepted locally, transport error, refused). UI reactions all stay
  // inside send(); the verdict exists for callers that persist state only on
  // delivery (ArtifactPanel's submit-to-chat batch marks comments sent on it).
  const send = useCallback(async (optionText?: string, targetSlot?: string, steerNow?: boolean | 'auto', isolated = false): Promise<boolean> => {
    // Defense-in-depth: ChatInput already gates Send/Optimize buttons and
    // the keyboard Enter shortcut on `connected`, but a future caller (a
    // programmatic dispatch from a hotkey, a follow-up option click, an
    // intent handler) could call send() while offline. Bail before we
    // clear the draft via setInput('') below — losing the user's typed
    // message with no recovery path is the offline-UX regression we're
    // guarding against. Cheap belt-and-braces.
    if (!connected) return false
    // An attachment for either the active slot or the slot that still owns the
    // live composer is still on its way. Composer ownership trails activeSlot
    // for one commit during a switch, so checking only the active slot can let
    // an auto-submit send the outgoing slot's draft without its attachment.
    // ChatInput holds its own controls (`holdSend`); this also covers voice
    // auto-submit, which calls send() directly. Option sends keep the composer.
    // Before anything below consumes composer state (widget prefill included).
    const activeSendSlot = activeSlotRef.current
    const composerSendSlot = composerSlotRef.current
    const composerHeld = isComposerSendHeld(activeSendSlot)
      || (composerSendSlot !== activeSendSlot && isComposerSendHeld(composerSendSlot))
    if (!isolated && !optionText && composerHeld) {
      // Voice auto-submit relies on this send to stop a streaming dictation
      // (see `useComposerVoice.onEndpoint`); nothing re-fires when the hold
      // clears, so end it here or later speech keeps appending to the draft.
      composerRef.current?.voice()?.disarmForSend()
      return false
    }
    const raw = (isolated ? optionText ?? '' : optionText || inputRef.current).trim()
    // App launches own only their explicit text, not the composer's staged data.
    const widgetOrigin = !isolated && !!widgetPrefillRef.current && raw.includes(widgetPrefillRef.current)
    if (!isolated) widgetPrefillRef.current = null
    // A staged quote alone is a sendable message (the quote IS the text).
    const sentQuote: MessageQuote | null = isolated || optionText ? null : messageQuote.consume('').quote
    if (isEmptyTurn(isolated ? { text: raw } : { text: raw, quote: sentQuote, files: pendingFilesRef.current, sessionRefs: pendingSessionsRef.current })) return false

    // Sending while STREAMING dictation is live ends the dictation (see
    // `useComposerVoice.disarmForSend` for the full rationale — streaming only,
    // batch keeps capturing and lands its transcript when the user stops).
    if (!isolated) composerRef.current?.voice()?.disarmForSend()

    // The session actually on screen at send time. Read from the ref (fresh
    // every render), not the closure `activeSlot` (stale until send() is
    // re-memoized). Under lag a reducer-driven activeSlot change can move the
    // active slot before ChatPage re-renders, so the closure would route into
    // the slot the user just left. Used for slash routing, the composer draft
    // clear, and (below) the send target.
    const uiSlot = activeSlotRef.current
    // Put a consumed quote back for a send that did not happen. On the stage
    // only while this slot still owns the live composer (an awaited step may
    // have spanned a slot switch); otherwise its block is parked in this
    // slot's draft text, so it is neither lost nor carried into another chat.
    const restageOrPark = () => {
      if (!sentQuote || !uiSlot) return
      // Live only while this page is still mounted: a slash command awaited
      // across a route change has no stage to restage onto (same gate as the
      // other recovery sites), so the block goes into the parked draft.
      const live = composerMountedRef.current && uiSlot === activeSlotRef.current && composerSlotRef.current === uiSlot
      // Live: `recoverInto` restages when the stage is free; a newer quote
      // staged during the await keeps its seat and this one's block goes into
      // the live text instead, so neither is lost. Off screen: the block goes
      // into this slot's parked text.
      if (live) {
        const text = messageQuote.recoverInto('', sentQuote)
        if (text) setInput(prev => mergeRecoveredDraft(prev, text))
        return
      }
      setDraft(drafts.current, uiSlot, messageQuote.recoverInto(drafts.current[uiSlot] ?? '', sentQuote, false))
      saveDrafts()
    }

    // Capture a pending BLOCKING card at ENTRY — before the first await below.
    // This send consumes the answer channel of the card the user saw when they
    // hit send; captured after an await, the card-submit flow can resolve the
    // card (or a newer one can land) in the gap, and the capture would compare
    // against the wrong baseline. Its staleness is
    // resolved over the network, not in the store. A STATELESS card needs no
    // capture: the server retires it when this send's user row lands and
    // announces it with `question_card_resolved`.
    const entrySendSlot = targetSlot ?? uiSlot
    // An app's supplied text is not the human's answer to a pending card.
    // Null captures keep all composer-owned completion effects inert.
    const askAtSend = isolated ? null : capturePendingAskId(store.getState().chat.pendingQuestions, entrySendSlot)
    // Entry-time capture of the folder-suggestion card, ONLY when it was
    // actually on screen for this send: the card renders solely in this page's
    // composer band for the ACTIVE slot, so a targeted send into another slot —
    // and any send from a surface that never renders the card (ChatPane) — must
    // not age it. The captured `ts` pins the card GENERATION the user saw; the
    // aging dispatch below is ts-guarded so a replacement card arriving while
    // the POST is in flight does not inherit this send's age.
    const folderCardAtSend =
      !isolated && entrySendSlot && entrySendSlot === uiSlot ? store.getState().chat.folderSuggestions?.[entrySendSlot] : undefined

    // Slash command interception (e.g. /side): runs before knowledge so a
    // bare prefix like /side returns immediately without touching input parse.
    // Gate on the RAW composer text first — a pasted block whose content
    // happens to start with "/side " must stay main-chat content, never
    // become a command. Only a command the user actually typed is expanded
    // (so a paste after "/side " reaches the side chat as content) and
    // delegated. On failure keep the composer intact so the question stays
    // recoverable — same rules as steer()'s guard.
    // An option answer (optionText — a question-card, follow-up or decision-
    // card choice) is an answer payload for the agent, never a typed UI
    // command: a choice that happens to look like "/side …" must reach the
    // turn as text rather than open Side Chat and strand the card. Same
    // carve-out the knowledge-fetch branch below applies.
    if (!optionText && isInterceptedSlashCommand(raw)) {
      const slashPastes = pasteBlocksRef.current
      const slashTxt = slashPastes.length ? expandPasteTokens(raw, slashPastes) : raw
      const slashResult = await interceptSlashCommand(slashTxt, uiSlot, dispatch, {
        // No slot yet: the same pending pick the agent picker makes, and like
        // it, drop a model chosen for the previous agent (#2035).
        onPendingAgent: (name) => { setPendingAgent(name, 'template'); setPendingModel('') },
      })
      if (slashResult.intercepted) {
        // Not a send either way: the staged quote stays staged for the real one
        // -- while this slot's composer is still the live one. The await above
        // can span a slot switch, and the stage is not per slot, so restaging
        // then would hand this slot's quote to the next send from another chat;
        // off screen the block goes into this slot's parked text instead.
        if (!optionText && !slashResult.failed) {
          // Clear only the command this send took. The await can span edits
          // and a slot switch: newer text in this composer, or another chat's
          // draft now on screen, is not the command and stays. "On screen" is
          // composerSlotRef, as the steer hand-back reads it.
          if (composerMountedRef.current && composerSlotRef.current === uiSlot) {
            if (inputRef.current.trim() === raw && pasteBlocksRef.current === slashPastes) { setInput(''); setPasteBlocks([]) }
          } else if (uiSlot && (drafts.current[uiSlot] ?? '').trim() === raw) {
            delete drafts.current[uiSlot]; delete pasteDrafts.current[uiSlot]
            saveDrafts()
          }
        }
        // After the clear above, so a block that falls back into the live text
        // (stage taken by a newer quote) is not wiped by it.
        restageOrPark()
        // Keeping the composer intact is the recovery; this is the report.
        // Same surface as a refused footer press, so the reason sits above the
        // draft it left in place instead of only in the console. An
        // UNCONFIRMED turn keeps the composer too but is not a refusal: its
        // report is the side panel's own standing notice (set by the
        // interceptor), never this error surface.
        if (slashResult.failed && !slashResult.unconfirmed) setRefusedPress(slashRefusal(slashResult))
        return false
      }
    }

    // Knowledge fetch: intercept @knowledge prefix, show picker instead of sending
    const kq = extractKnowledgeQuery(raw)
    if (kq && !optionText) {
      knowledgeFetchRef.current.searchKnowledge(kq)
      setInput('')
      // Not a send: the staged quote stays staged for the real one.
      restageOrPark()
      return false
    }

    // Snapshot the staged attachments BEFORE the composer is cleared below, so a
    // failed send can put them back (prepareSendPayload's `filePaths` drops
    // images, which would silently lose them on restore).
    const sentFiles = isolated ? [] : pendingFilesRef.current.slice()
    // Explicit text already excludes staged session refs. App launches also
    // exclude files, paste expansion and knowledge, without changing legacy
    // option-click or composer-send behavior.
    const sentSessionRefs = isolated || optionText ? [] : pendingSessionsRef.current.slice()
    const stagedFilesAtSend = [...new Set(sentFiles)]
    // The staged collapsed pastes: the turn expands the live ones on the wire,
    // and a failed send hands them all back with its text.
    const activePastes = isolated ? [] : pasteBlocksRef.current
    const knowledgeBlock = isolated ? null : knowledgeFetchRef.current.pendingKnowledge ?? null
    // The whole message, in the one fixed order `buildOutgoingTurn` owns: staged
    // files, folder tokens as `[attached_dir N]` markers resolved against the
    // project (`meta.dirs[N-1]` is marker N), staged session references as
    // appended links -- a pointer the agent follows on demand, never the
    // referenced transcript -- the live paste tokens expanded on the wire only,
    // the knowledge block, and the quoted message opening both texts. An app
    // launch (`isolated`) sends its own text verbatim, folder tokens included.
    const turn = isolated
      ? { wire: raw, bubble: raw, inlined: raw, meta: {}, storedPaste: undefined, typedOnly: true }
      : buildOutgoingTurn({ text: raw, files: sentFiles, pastes: activePastes, sessionRefs: sentSessionRefs, knowledge: knowledgeBlock, quote: sentQuote, project: currentProjectRef.current || '' }, 'send')
    if (!isolated) knowledgeFetchRef.current.clearPending()
    storeSentPastes(turn.storedPaste)

    if (!isolated) setPrefillHint(false)
    // The alias sub-map this send is about to drop, kept as a frozen snapshot
    // (deleting the key unhooks it; the object survives) so EVERY composer-
    // recovery path can put the bookkeeping back beside the text and files it
    // restores (fork GPT review): a queued send cancelled after this clear
    // restored text + chips whose mentions reconciliation could no longer
    // see -- hand-deleting one left a stale chip that the next send silently
    // re-attached. On the optionText path the live map is mutated in place
    // below (only the SENT paths are dropped, since the draft survives), so
    // snapshot a COPY of just those paths' aliases here -- a reference would be
    // emptied by that delete before a recovery path could re-merge it. The
    // `!optionText` path keeps the whole map by reference: it deletes the slot
    // KEY, leaving the referenced object intact for the merge.
    const sentSlotTokens = !isolated && uiSlot
      ? (!optionText
          ? pickedFileTokens.current[uiSlot]
          : (stagedFilesAtSend.length && pickedFileTokens.current[uiSlot]
              ? Object.fromEntries(stagedFilesAtSend
                  .filter(p => pickedFileTokens.current[uiSlot]![p])
                  .map(p => [p, pickedFileTokens.current[uiSlot]![p]]))
              : undefined))
      : undefined
    if (!isolated && !optionText) {
      // Drops only THIS slot's own token sub-map (`pickedFileTokens` is
      // slot-scoped -- see useFileMentionTokens), never another slot's: a
      // DIFFERENT, still-open slot's attachment is untouched regardless of
      // what this send just carried.
      setInput(''); setPendingFiles([]); if (uiSlot) delete pickedFileTokens.current[uiSlot]; setPasteBlocks([]); setPendingSessions([]); if (uiSlot) { delete drafts.current[uiSlot]; delete fileDrafts.current[uiSlot]; delete pasteDrafts.current[uiSlot]; delete sessionRefDrafts.current[uiSlot]; saveDrafts() }
      // The challenge-handoff prompt is seeded into PREFILL_STORAGE_KEY and the
      // slot-restore effect re-applies it on slot changes. Once that prompt is
      // sent, clear the seed so a later slot-restore can't re-fill the (now
      // empty) composer with the already-sent text.
      try { sessionStorage.removeItem(PREFILL_STORAGE_KEY) } catch { /* sessionStorage unavailable */ }
    } else if (!isolated && stagedFilesAtSend.length) {
      // An optionText send (a follow-up chip's Send-now / double-click, or a
      // decision-card choice) skips the full clear above so it does not wipe the
      // user's TYPED draft -- the forceNew/autoSend path reuses send(optionText)
      // and that draft must survive. But staged attachments ARE consumed and
      // transmitted on this path exactly as on a normal send (`sentFiles` ->
      // buildOutgoingTurn -> meta, all ungated on optionText), so leaving them
      // staged re-attaches the same media to the NEXT human turn. Clear only
      // what this send actually carried, plus its slot-scoped file-token and
      // file-draft bookkeeping, and leave the typed text, pastes and session
      // refs untouched.
      const sent = new Set(stagedFilesAtSend)
      setPendingFiles(prev => prev.filter(p => !sent.has(p)))
      if (uiSlot) {
        // Drop only the SENT paths' aliases, not the whole slot map: the typed
        // draft survives this branch, so an alias for a file NOT in this send
        // (e.g. a mention the user hand-deleted that undo could revive) must
        // stay. `sentSlotTokens` captured the pre-delete map above, so the
        // create-failure and queued-cancel recovery paths still re-merge the
        // sent files' aliases and keep their restored chips reconcilable with
        // the surviving draft text.
        const m = pickedFileTokens.current[uiSlot]
        if (m) for (const p of sent) delete m[p]
        const draft = fileDrafts.current[uiSlot]
        if (draft) { setFileDraft(fileDrafts.current, uiSlot, draft.filter(p => !sent.has(p))); saveDrafts() }
      }
    }
    // Target the slot the user is actually looking at (uiSlot, from the ref),
    // not the stale closure `activeSlot`. See the uiSlot note above.
    let slot = targetSlot ?? uiSlot
    // Only a normal (non-targeted) send consumes the one-shot "new session"
    // intent. A targeted send — e.g. submitting document comments to the
    // document's origin slot — must leave it intact for the user's next send.
    let forceNew = false
    if (!targetSlot) {
      forceNew = newSessionRef.current
      newSessionRef.current = false
    }
    if (!slot || forceNew) {
      sendingRef.current = true;
      // The composer was cleared above, so a create failure here would destroy
      // the user's text: `.unwrap()` rejects, send() unwinds, and nothing is
      // ever sent — no error bubble, no draft to recover, and sendingRef stuck
      // true (which suppresses the welcome state). Restore the composer, its
      // paste blocks and attachments, surface the failure, and bail.
      let created: { key: string } | null = null
      try {
        // No agent when none is pending: the server stamps the default custom
        // agent (a template) on an agent-less create, the same name
        // `defaultAgent` (the catalog's `default_template`) reads; sending it
        // would only pin a stamp the server already makes, and in the member
        // namespace when the slot kind is unset.
        // A project picked on the fresh composer is applied right after the
        // create below, so the server must not make a session folder for it.
        // An isolated send is an app's chat launch, and a forceNew send with
        // optionText is a page's auto-send launch (Projects, Dev Fleet, Prompts
        // via ?autoSend=1&newSession=1). Neither is the person's own first
        // message, so neither asks for their default sidebar folder.
        const launched = isolated || (forceNew && optionText !== undefined)
        created = await dispatch(createSlot({ agent: pendingAgentRef.current || undefined, agent_kind: pendingAgentRef.current ? pendingAgentKindRef.current : undefined, model: pendingModelRef.current || undefined, mode: modeRef.current, ...(pendingProjectRef.current ? { sessionFolder: false } : launched ? {} : { defaultFolder: true }) })).unwrap()
      } catch (e: unknown) {
        sendingRef.current = false
        if (isolated) {
          // The app never consumed the composer. Keep its payload in the
          // page-level copyable notice, which survives slot switches, instead
          // of a draft or a user row that would age pending approvals.
          const failure = i18nT('pages.chatPage.send_failed_with_error', { error: createFailReason(e) })
          setActionError({ message: i18nT('appChatLaunch.unsent', { error: failure, message: raw }), title: i18nT('pages.chatPage.could_not_start_a_new_session'), preserveOnSwitch: true })
          return false
        }
        // Recover the payload WITHOUT clobbering anything newer. Two traps make a
        // plain assignment lossy here:
        //  - The composer is only cleared above when `!optionText`, and the
        //    reachable forceNew path IS the optionText path (Projects / Dev Fleet /
        //    Prompts navigate to ?autoSend=1&newSession=1), so the composer still
        //    holds the user's own draft — overwriting it would destroy exactly the
        //    kind of text this guard exists to protect.
        //  - The create is awaited, so meanwhile the user may have typed, attached
        //    files, or switched sessions.
        // So MERGE into whatever the target slot holds now, and only touch live
        // composer state while that slot is still the one on screen.
        // Restore in place ONLY when the composer still belongs to the slot that
        // issued the send. A no-slot send (auto-send that fires before the slot list
        // resolves) must NOT fall back to whatever session auto-selection has since
        // activated: that would splice a new-session payload into an unrelated
        // session and send it there on retry. Those cases get a notification.
        const sameSlot = activeSlotRef.current === uiSlot
        const onScreen = sameSlot
        // Un-consume the one-shot new-session intent while the user is still on the
        // slot that issued the send — re-arming after they switched away would make
        // THAT session's next message spawn an unintended new session. Also re-arm
        // whenever there was no origin slot: the queued retry below MUST still create
        // its own session, and `sameSlot` is false there as soon as auto-selection
        // activates one mid-await, which would otherwise send the payload into an
        // unrelated existing session.
        // `|| !uiSlot` on the VALUE too, not just the condition: a slotless send also
        // reaches the create branch via `!slot` with `forceNew === false` (the
        // challenge-token flow, whose own createSlot failed), and arming `false` there
        // would let the queued retry deliver the payload as a user turn in whatever
        // unrelated session auto-selection activates. A send that had no origin slot
        // must always create its own session on retry.
        if (sameSlot || !uiSlot) newSessionRef.current = forceNew || !uiSlot
        const keepFiles = onScreen ? pendingFilesRef.current : (uiSlot ? fileDrafts.current[uiSlot] ?? [] : [])
        const restoredFiles = [...new Set([...keepFiles, ...sentFiles])]
        // Session refs merge by key (they carry no sequence to collide on, unlike
        // pastes), keeping whatever the user staged since the failed send.
        const keepRefs = onScreen ? pendingSessionsRef.current : (uiSlot ? sessionRefDrafts.current[uiSlot] ?? [] : [])
        const restoredRefs = mergeSessionRefs(keepRefs, sentSessionRefs)
        const keepPastes = onScreen ? pasteBlocksRef.current : (uiSlot ? pasteDrafts.current[uiSlot] ?? [] : [])
        // Collapsed pastes resolve by `seq`, not id, and a paste made while the
        // composer was empty restarts at #1 — so a naive id-merge can leave two
        // blocks sharing #1, with both markers resolving to one of them and
        // silently swapping the user's content on retry. `carryPastes` owns the
        // rule: re-sequence the carried blocks past the kept ones and rewrite
        // their markers in the payload text.
        const carried = carryPastes(raw, activePastes, keepPastes)
        // `full` keeps every token: it is the payload a retry re-sends whole.
        const { full: payload, pastes: restoredPastes } = carried
        const keepText = onScreen ? inputRef.current : (uiSlot ? drafts.current[uiSlot] ?? '' : '')
        // Keep whatever the user typed while the create was in flight and append
        // the payload after it, without duplicating one the composer already
        // holds — a synchronously rejected create can land before React flushes
        // the clear. `mergeRecoveredDraft` owns that rule for every recovery
        // site, including the send-failure path further down.
        // The quote: back on the stage when live and free, else in the text.
        const restoredText = messageQuote.recoverInto(mergeCarriedDraft(keepText, carried), sentQuote, onScreen && !!uiSlot && composerMountedRef.current)
        if (onScreen && uiSlot) {
          setInput(restoredText); setPasteBlocks(restoredPastes); setPendingFiles(restoredFiles); setPendingSessions(restoredRefs)
          // clearPending() above already consumed the knowledge selection, so a
          // retry would otherwise go out WITHOUT the context the user picked. Slot-
          // gated: selection is per-slot, so re-injecting while the user views another
          // session would smear it there. MERGE rather than skip-or-replace — `inject`
          // replaces, so skipping when a newer selection exists would drop the failed
          // turn's context, and replacing would drop what the user picked since. Newer
          // items win on an id collision.
          if (knowledgeBlock) {
            const newer = knowledgeFetchRef.current.pendingKnowledge?.items ?? []
            const newerIds = new Set(newer.map(i => i.id))
            knowledgeFetchRef.current.inject([...knowledgeBlock.items.filter(i => !newerIds.has(i.id)), ...newer])
          }
          dispatch(appendMessage({ role: 'error', content: i18nT('pages.chatPage.could_not_start_session_message_restored', { error: createFailReason(e) }), cls: '' }))
        }
        // Announce the failure wherever the in-chat bubble could not. Two shapes:
        //  - No origin slot at all: nothing durable can hold the text (a draft under
        //    the session auto-selection just activated would splice this payload into
        //    an unrelated conversation, and a composer restore lives in state the
        //    next slot switch wipes). So the notification CARRIES the message —
        //    expanded pastes and attachment paths included.
        //  - Origin slot exists but the user moved on: the draft is parked there, so
        //    point at it. An error bubble would land in the wrong session.
        if (!uiSlot) {
          // No session to restore into or persist to (a draft under the session
          // auto-selection just activated would splice this into an unrelated
          // conversation, and a notification body reaches the OS notification centre
          // — `useNativeNotification` publishes the latest unacked body, and any entry
          // can be re-marked unread, so `acked` is no barrier). Hand the payload back
          // to the mechanism that produced it instead: re-arming `autoSendRef` makes
          // the auto-send effect resend it. Text only — paste blocks and attachments
          // cannot exist on this path (no composer renders without a slot).
          //
          // If a slot is ALREADY active, the effect's deps
          // (`[send, connected, autoSendTick]`) will not change again on their own, so
          // bump the tick to drive the retry now — and stay silent, because that
          // retry reports its own outcome (it runs with a slot, so a second failure
          // produces the error bubble or the moved-on notification below). Telling the
          // user to retype while a retry is in flight invites a duplicate turn.
          // Otherwise nothing can drive it until a real `connected`/slot change, so
          // report it and be honest that the queue is tab-local.
          const retryNow = !!activeSlotRef.current
          autoSendRef.current = payload
          if (retryNow) {
            setAutoSendTick(t => t + 1)
          } else {
            // In-page as well as the toast: with no slot there is no composer
            // restore and no error bubble, so the notice is the only thing on
            // the page that says the send did not happen.
            const queuedBody = i18nT('pages.chatPage.message_queued_until_session_ready', { error: createFailReason(e) })
            showActionError(queuedBody, i18nT('pages.chatPage.could_not_start_a_new_session'))
            dispatch(addNotification({
              ts: uniqueNotificationTs(),
              kind: 'agent',
              priority: 'critical',
              title: i18nT('pages.chatPage.could_not_start_a_new_session'),
              body: queuedBody,
            }))
          }
        } else if (!onScreen) {
          // The knowledge selection is NOT restored here: `inject` writes to the slot
          // the user is now viewing, so restoring it off-screen would attach the failed
          // turn's context to an unrelated session. Re-selecting is a two-click library
          // action (unlike typed text, which is unrecoverable), so this reports the gap
          // instead of routing knowledge per-slot — but it must not be silent.
          const lostContext = knowledgeBlock
            ? ' ' + i18nT('pages.chatPage.knowledge_context_not_kept')
            : ''
          // The restored draft lives in a session that is not on screen, so the
          // page the user is looking at shows nothing without this notice.
          const draftBody = i18nT('pages.chatPage.message_saved_as_draft', { error: createFailReason(e), extra: lostContext })
          showActionError(draftBody, i18nT('pages.chatPage.could_not_start_a_new_session'))
          dispatch(addNotification({
            ts: uniqueNotificationTs(),
            kind: 'agent',
            priority: 'critical',
            title: i18nT('pages.chatPage.could_not_start_a_new_session'),
            body: draftBody,
            slot: uiSlot,
          }))
        }
        // The failed create's alias snapshot goes back beside the text and
        // chips it restores (fork GPT review): without it a restored mention
        // is invisible to reconciliation and its chip sticks. Slot-keyed, so
        // this one merge serves both the on-screen and parked-draft arms.
        if (uiSlot && sentSlotTokens) mergeSlotTokens(uiSlot, sentSlotTokens)
        if (uiSlot) {
          setDraft(drafts.current, uiSlot, restoredText)
          setPasteDraft(pasteDrafts.current, uiSlot, restoredPastes)
          setFileDraft(fileDrafts.current, uiSlot, restoredFiles)
          setSessionRefDraft(sessionRefDrafts.current, uiSlot, restoredRefs)
          saveDrafts()
        }
        return false
      }
      const result = created
      slot = result.key;
      if (pendingProjectRef.current) {
        await api.chatSlotProject(result.key, pendingProjectRef.current).catch(e => {
          // eslint-disable-next-line no-console -- surface project-assign failures for debugging
          console.error('chatSlotProject failed', e)
        })
      }
    }
    setPendingAgent(''); setPendingModel(''); setPendingProject('')
    // Meta for persistence (files, folders, pastes, quote, knowledge), in the
    // turn's wire order.
    const meta: Record<string, unknown> = { ...turn.meta }
    if (widgetOrigin) meta.origin = 'widget'
    // A client-generated correlation ID so the server echo can be matched
    // to this exact optimistic bubble without relying on content equality.
    // The server preserves meta fields on the user row it appends, so the
    // echo carries both this sendId AND the server-minted `mid` (#2845).
    const sendId = mintSendId()
    meta.sendId = sendId
    const metaPayload = meta
    // A busy snapshot may be stale. The server's user event supplies the
    // bubble for an immediate dispatch; a real queue has its own card.
    const _busy = selectComposerBusy(store.getState(), slot ?? null)
    // Whether THIS send drew its own bubble. A plain send on a busy slot does
    // not: the server's `queue_push` card represents it. The receipt arms
    // below read this so none of them describes a row that was never rendered.
    const bubbleMinted = !_busy || forceNew
    if (bubbleMinted) {
      const bubble: ChatMessage = { role: 'user', content: turn.bubble, cls: '', ts: new Date().toISOString(), meta: metaPayload }
      // A create whose activation `forkMayOpen` refused (the user moved away
      // and back while it was in flight) leaves another session on screen, so
      // the bubble goes to the slot this send targets, not the active mirror.
      if (slot && slot !== store.getState().chat.activeSlot) dispatch(appendSlotMessage({ slot, message: bubble }))
      else dispatch(appendMessage(bubble))
    }
    if (!isolated) window.dispatchEvent(new Event('voice-stop'))
    sendingRef.current = false
    setTimeout(() => scrollBottom(), SCROLL_AFTER_RENDER_MS)
    if (slot) dispatch(startLocalTurn(slot))
    /**
     * Put the composer back the way it was before this send.
     *
     * Called from BOTH failure shapes: a transport error (fetch rejected) and a
     * REJECTED RESPONSE (`!body.queued && !body.ok` — e.g. an expired cookie
     * answering 403). Both mean the message did not go out, so both must recover
     * identically; previously only the transport branch restored, so a dropped
     * connection kept the user's message while a 403 discarded it.
     *
     * Persist for `slot` unconditionally (recoverable on disk), but only touch
     * the live input/blocks when `slot` is the one on screen. Compare against
     * activeSlotRef.current, NOT the closure's `activeSlot`: a new-session /
     * forceNew send creates a fresh slot and switches the UI to it, so the
     * closure value is stale — using it would leave the user's just-typed message
     * empty on the very session they are now viewing. The ref reflects what is
     * actually on screen, so it restores visibly for a new-session failure while
     * still not splicing a targeted send's text into an unrelated slot.
     *
     * Restores the turn's `inlined` text — what the user actually TYPED, files
     * inlined — and brings the staged references back as chips, rather than
     * restoring the link-appended wire. Restoring that preserved the reference
     * (the link is in the text) but left it as a raw URL, and re-staging the
     * chips ON TOP of that text would make the retry append each link a SECOND
     * time. Splitting them puts the composer back in exactly its pre-send
     * state: chip visible, link appended once on retry. Paste blocks come back
     * too, or the restored text would show a dead `[ Paste #N · M lines ]`
     * literal. Shares the create-failure path's merge rule so a reference
     * staged while the send was in flight is not clobbered.
     */
    const restoreComposerAfterFailedSend = () => {
      // App payloads remain in the page-level error notice for copying; the
      // composer was never consumed and must not gain the app's text or chips.
      if (!slot || isolated) return
      // Ownership of the live composer state, not the active tab: see the
      // steer receipt's `onScreenNow` for the mid-switch window this closes.
      const onScreenNow = composerSlotRef.current === slot
      const liveRefs = onScreenNow ? pendingSessionsRef.current : (sessionRefDrafts.current[slot] ?? [])
      const refsBack = mergeSessionRefs(liveRefs, sentSessionRefs)
      // MERGE, never overwrite. The send is in flight for up to 10s, and the user
      // can type a fresh message in that window — clobbering it with the failed
      // payload would lose newer work to recover older. Mirrors the create-failure
      // path above: keep what is there, append the failed payload unless it is
      // already the same text, and re-sequence the carried paste blocks so two
      // blocks cannot claim one `[ Paste #N ]` marker.
      const keepText = onScreenNow ? inputRef.current : (drafts.current[slot] ?? '')
      const keepPastes = onScreenNow ? pasteBlocksRef.current : (pasteDrafts.current[slot] ?? [])
      const carried = carryPastes(turn.inlined, activePastes, keepPastes)
      const pastesBack = carried.pastes
      // Same merge rule as the create-failure path above, and the separator lives
      // in `mergeRecoveredDraft` rather than in a template literal here: the blank
      // line between the kept draft and the recovered payload is message
      // structure, not copy, so it stays off the i18n gate honestly rather than by
      // exemption (same treatment as appendSessionRefLinks).
      // The quote: back on the stage when live and free, else in the text.
      const textBack = messageQuote.recoverInto(mergeCarriedDraft(keepText, carried), sentQuote, onScreenNow && composerMountedRef.current)
      setDraft(drafts.current, slot, textBack)
      setPasteDraft(pasteDrafts.current, slot, pastesBack)
      setSessionRefDraft(sessionRefDrafts.current, slot, refsBack)
      saveDrafts()
      if (onScreenNow) {
        setInput(textBack); setPasteBlocks(pastesBack); setPendingSessions(refsBack)
      }
      // Aliases come back with the text they describe -- MERGE, never
      // overwrite, so a file picked while the send was in flight keeps its
      // fresh bookkeeping (fork GPT review; same rule as the text above).
      if (sentSlotTokens) mergeSlotTokens(slot, sentSlotTokens)
    }
    // The POST, its 10 s deadline, the resolves-not-rejects trap and the body
    // classification all live in the chat-core transport now; this surface
    // only decides how to REACT to the receipt. `sendTurn` never rejects.
    const receipt = await sendTurn({
      message: turn.wire,
      slot: slot ?? undefined,
      meta: metaPayload,
      steer: steerNow,
      colorTheme: colorThemeRef.current,
    })
    const { body } = receipt
    // - `transport-error`: the fetch rejected. Restore and report only when
    //   no correlated server echo has already proved delivery.
    // - `response-late`: the deadline fired; the request may have arrived.
    //   The optimistic bubble stays pending -- `markSendUnconfirmed` stamps
    //   it, which draws its delivery-pending line, and `selectTurnInterrupted`
    //   never offers Resume for a row still carrying `meta.optimistic` -- and a
    //   WARN notice under it says so. No restore: the text may have been
    //   delivered late, and a duplicate turn is worse than a visible pending
    //   row (the same ruling the steer path applies to a minted bubble).
    // - `unknown`: a 2xx whose body would not parse. The request was accepted
    //   and only its answer is mangled, so it may have started a turn that is
    //   streaming right now. Reporting a refusal would hand the payload back
    //   and invite a retry that duplicates a delivered turn, so an unknown
    //   takes no action rather than asserting a refusal it cannot prove. It
    //   still falls through to the body-driven steps below, which all read
    //   `ok` / `queued` and are no-ops on an empty body.
    // Both failure branches are addressed to the SENDING slot: the user can
    // switch sessions while the POST is in flight, and a failure that lands
    // then must neither clear the new session's running state nor put its
    // error row in the new session's transcript (`endLocalTurn` is the
    // slot-keyed inverse of the `startLocalTurn` above; `appendSlotMessage`
    // routes to the slot's own list, the active one included).
    const failLocalTurn = (message: ChatMessage) => {
      if (isolated) {
        if (slot) dispatch(endLocalTurn(slot))
        setActionError({ message: i18nT('appChatLaunch.unsent', { error: message.content, message: raw }), preserveOnSwitch: true })
        return
      }
      if (slot) {
        dispatch(endLocalTurn(slot))
        dispatch(appendSlotMessage({ slot, message }))
      } else {
        dispatch(setSlotRunning(false))
        dispatch(appendMessage(message))
      }
    }
    if (receipt.status === 'transport-error') {
      if (slot && selectSendConfirmed(store.getState(), slot, sendId)) return true
      // Cause-stating and naming the restore ("...and try again"), the shared
      // core copy the other surfaces use, instead of a bare "Connection error".
      failLocalTurn({ role: 'error', content: i18nT('pages.chatPage.send_failed_connection'), cls: '' })
      restoreComposerAfterFailedSend()
      return false
    }
    if (receipt.status === 'response-late') {
      // Indeterminate, not failed: the local turn stays pending (a late WS
      // frame or the next slot refresh settles it) and the bubble keeps its
      // `optimistic` flag. A correlated echo is stronger evidence than the
      // missing response -- a channel-linked slot can deliver one -- and then
      // there is nothing to warn about.
      if (slot && selectSendConfirmed(store.getState(), slot, sendId)) return true
      // An unconfirmed STEER is not counted as delivered: the steer receipt
      // policy hands its text back (applySteerReceipt), so a caller that
      // persists on this verdict (a comment batch) keeps its payload.
      const lateVerdict = !steerNow
      // A busy-slot Queue send minted no bubble, so there is no row to mark and
      // the notice below would point at nothing. It keeps the bare pending
      // verdict; what becomes of its text is the restore decision this arm
      // does not take.
      if (!bubbleMinted || !slot) return lateVerdict
      // The mark is what draws the bubble's pending line (the `optimistic` flag
      // alone cannot, see `markSendUnconfirmed`), and the row under the bubble
      // says what the line cannot: that the deadline passed and what to do.
      // WARN tone via NoticeCard's lead-glyph selector (parseNotice), exactly
      // as the steer receipt's notice above. Both addressed to the SENDING
      // slot like the failure rows: the user can switch sessions inside the
      // deadline window.
      dispatch(markSendUnconfirmed({ slot, sendId }))
      dispatch(appendSlotMessage({ slot, message: { role: 'notice', content: '\u26A0\uFE0F ' + i18nT('pages.chatPage.delivery_unconfirmed_pending'), cls: '' } }))
      return lateVerdict
    }
    if (body.queued && turn.typedOnly) {
      // The server queued this send and its receipt names the entry:
      // `queue_id` is the same id `queue_push` broadcasts and the card's
      // cancel button carries, so the pre-send composer state binds to
      // exactly this card — content plays no part in the key, which is what
      // makes duplicate texts, serialization-colliding captions, and other
      // tabs' cards structurally unable to consume someone else's record.
      // A receipt without `queue_id` (an older gateway, a requeued steer)
      // simply doesn't stash — the parser fallback covers those cards.
      //
      // Eligibility is DERIVED, not enumerated: stash only when the POSTed
      // text is exactly what {raw, staged files} alone explain (the turn's
      // `typedOnly`: its file and folder markers and the quote, nothing more).
      // Expanded paste blocks, appended session-ref links, a prepended
      // knowledge block, and ANY FUTURE feature that diverges the wire
      // from the composer state all fail this equality and fall to the
      // parser — a stash hit for such a send would restore `raw` WITHOUT
      // the context the user staged, silently dropping it, so the failure
      // mode of forgetting is a conservative fallback, not silent loss.
      //
      // No size bound on purpose: an entry is deleted on the cancel that
      // consumes it, and evicting a live entry would degrade that queued
      // card's cancel to the parser fallback — for a spaced attachment path
      // that is exactly the marker-in-composer data loss this PR exists to
      // fix. Entries orphaned by normal delivery are three small strings
      // and are bounded by how many sends a single tab queues in one
      // session.
      if (typeof body.queue_id === 'string' && body.queue_id) {
        queuedSendStash.set(body.queue_id, { raw, files: stagedFilesAtSend, sent: turn.wire, aliases: sentSlotTokens, ...(sentQuote ? { quote: sentQuote } : {}) })
      }
    }
    if (receipt.status === 'refused') {
      // FRAMED like the steer's refusal (and ChatEmbed's): a raw backend reason
      // ("slot agent mismatch") reads as the agent erroring mid-work, not as
      // "your request never went out".
      failLocalTurn({
        role: 'error',
        content: receipt.reason
          ? i18nT('pages.chatPage.send_failed_with_error', { error: receipt.reason })
          : i18nT('pages.chatPage.send_failed'),
        cls: '',
      })
      // The server explicitly accepted neither (`ok` nor `queued`), so nothing
      // was sent — recovering the composer cannot duplicate a delivered turn.
      restoreComposerAfterFailedSend()
    }
    if (slot && confirmedDelivered(body)) {
      // The response remains a delivery receipt (#4131), even if the correlated
      // user echo is missed. The echo owns insertion before streaming, so the
      // receipt must never append another row.
      // Addressed to the SENDING slot because the user can switch sessions
      // while the POST is in flight. A queued acceptance is not delivery.
      // The receipt carries the server-minted user-row `mid` (when the send
      // dispatched immediately); handing it to the reconcile stamps it onto
      // this optimistic bubble so message-pinning works this turn instead of
      // only after the chat_done refresh.
      dispatch(confirmOptimisticSend({ slot, sendId, mid: typeof body.mid === 'string' ? body.mid : undefined }))
    }
    // No stateless-card retirement here: the server retires the card when the
    // user row lands (immediate dispatch) or when the queued entry pops, and
    // announces it with `question_card_resolved` to every window, this one
    // included. A failed send appends no row, so the card stays; a queued send
    // stays cancellable with the card intact until its pop.
    if (body.ok && !body.queued && folderCardAtSend && slot === entrySendSlot) {
      // Delivery bar and slot-identity guard for the folder-suggestion card's
      // turn-aging: the card was on screen when the user hit send (captured at
      // entry, active slot only) and the server confirmed the send was
      // delivered. Failed sends never reach here; queued sends are still
      // cancellable; forceNew reroutes (the send lands in a freshly created
      // session) answer nothing in the entry slot. ts pins the card
      // generation, so a replacement that landed mid-flight is not aged.
      dispatch(ageFolderSuggestion({ slot, ts: folderCardAtSend.ts }))
    }
    // The user answered in the composer instead of the card; a blocking card
    // is resolved over the network, so this cannot be a store-only retirement.
    void resolveAskAfterSend(body, slot === entrySendSlot ? askAtSend : null, dispatch, slot)
    // The delivery verdict (see the callback's doc above). Only an explicit
    // `refused` reads as not-delivered here; `unknown` (a 2xx whose body did
    // not parse) may have started a turn, so it counts as delivered for the
    // same reason the composer above does not restore on it — a retry it
    // invited could duplicate a delivered turn.
    return receipt.status !== 'refused'
    // `send` is deliberately kept stable: it reads volatile values (agent,
    // model, project, mode, colorTheme, activeSlot) through refs so it does not
    // re-create on every keystroke/theme/agent change (it is passed to children
    // and consumed by the auto-send effect). setPending*/saveDrafts/scrollBottom
    // are stable, and defaultAgent is only a creation-time fallback — pulling
    // them into the dep array would defeat that stability without changing
    // outcomes.
    // send() no longer reads the closure `activeSlot` for its target. It reads
    // uiSlot = activeSlotRef.current, so it routes to the on-screen slot even
    // between the reducer flip and this callback's re-memoization.
    // activeSlot is left in deps as a harmless no-op: dropping it churns the
    // array for no behavior change (the ref is always current regardless).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSlot, dispatch, connected, messageQuote.consume, messageQuote.restage, setPendingAgent, setPendingModel])
  return {
    send,
  }
}
