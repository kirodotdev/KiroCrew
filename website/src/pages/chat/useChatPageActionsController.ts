import {
  type MouseEvent as ReactMouseEvent,
  type MutableRefObject,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from '../../api/client'
import { isNonInteractiveQueued, isSystemDelivery } from '../../components/QueueStack'
import { useMoveSlotToFolder } from '../../hooks/useMoveSlotToFolder'
import { isPlanAction, usePlanActionMutation } from '../../hooks/usePlanActionMutation'
import { useQueuedMessageActions, queuedSendStash } from '../../hooks/useQueuedMessageActions'
import { i18nT } from '../../i18n/t'
import { performAgentSlotSwitch } from '../../lib/agentSwitch'
import { resolveAskAfterSend } from '../../lib/resolveAskAfterSend'
import { performSlotSwitch } from '../../lib/slotSwitch'
import type { AppDispatch } from '../../store'
import { store, useAppSelector } from '../../store'
import {
  ageFolderSuggestion,
  appendMessage,
  appendSlotMessage,
  capturePendingAskId,
  captureStatelessCard,
  clearFolderSuggestion,
  clearFollowupCard,
  confirmOptimisticSend,
  createSlot,
  dismissFollowupItem,
  openActivityToTab,
  pendingQuestionFor,
  replaceMessages,
  resolveByApprovalId,
  retireStatelessQuestion,
  selectComposerBusy,
  selectContinuable,
  selectTurnInterrupted,
  setAgentSwitchNotice,
  setPendingInput,
  setSlotRunning,
  startLocalTurn,
  switchSlot,
  truncateAfterIndex,
  type FollowupItem,
} from '../../store/chatSlice'
import { updateSlot } from '../../store/dashboardSlice'
import { addNotification, removeNotificationByTs } from '../../store/notificationsSlice'
import type { ChatMessage, ChatSlot } from '../../types'
import {
  mergeIntoDraft,
  mergeRecoveredDraft,
  setDraft,
} from '../../utils/chatDrafts'
import { setFileDraft } from '../../utils/chatFileDrafts'
import { setPasteDraft } from '../../utils/chatPasteDrafts'
import { setSessionRefDraft } from '../../utils/chatSessionRefDrafts'
import { prepareSendPayload, serializeDirTokens } from '../../utils/fileTokens'
import { PREFILL_STORAGE_KEY, writePrefill } from '../../utils/navIntent'
import {
  expandAll as expandPasteTokens,
  pruneBlocks as pruneBlocksUtil,
  remapCarriedBlocks,
  saveStoredPaste,
} from '../../utils/pasteTokens'
import { rewindWithRollback } from '../../lib/rewindCall'
import { tryQuickSend } from '../../lib/quickSend'
import { confirmedDelivered, readSendReceipt } from '../../utils/sendDelivery'
import { appendSessionRefLinks, mergeSessionRefs } from '../../utils/sessionRefs'
import { agentSwitchFailureMessage } from '../../utils/agentSwitchFeedback'
import { deriveFollowUpOptions } from '../../app-sdk/protocol'
import { interceptSlashCommand, isInterceptedSlashCommand } from './ChatInput'
import { mintSendId } from './ChatPageMessageContent'
import { revealComposer } from './composerFocus'
import {
  createFailReason,
  uniqueNotificationTs,
  type ChatPageComposerController,
} from './useChatPageComposerController'
import type { ChatPageResourcesController } from './useChatPageResourcesController'
import {
  expandKnowledgeBlock,
  extractKnowledgeQuery,
  type KnowledgeBlock,
} from './useKnowledgeFetch'

/** Delay (ms) before scrolling to bottom after a state update, giving React time to commit. */
const SCROLL_AFTER_RENDER_MS = 100
const COMPOSER_PARAGRAPH_BREAK = String.fromCharCode(10).repeat(2)

// Stable identity for the "no follow-up cards" case: returning a fresh {} from
// the selector would make it a new reference on every store update.
const EMPTY_FOLLOWUPS: Record<string, { items: FollowupItem[]; ts: number }> = {}

// Per-action titles for the refused-press notice above the composer. A press
// added later gets its refusal surfaced by adding one entry here and calling
// `showRefusedPress` from its catch — the `as const` map keeps every key
// statically resolvable for the catalog-key gate.
export const REFUSED_PRESS_TITLE_KEYS = {
  continue: 'pages.chatPage.could_not_continue',
  regenerate: 'pages.chatPage.could_not_regenerate',
  switch_variant: 'pages.chatPage.could_not_switch_variant',
} as const
export type RefusedPressAction = keyof typeof REFUSED_PRESS_TITLE_KEYS

export interface ChatPageActionsSessionPorts {
  messages: ChatMessage[]
  messagesRef: MutableRefObject<ChatMessage[]>
  slots: ChatSlot[]
  currentProjectRef: MutableRefObject<string | undefined>
}

export interface ChatPageActionsScrollPorts {
  scrollBottom: (instant?: boolean) => void
}

export type ChatPageActionsComposerPorts = Pick<
  ChatPageComposerController,
  | 'activeSlotRef'
  | 'autoSendRef'
  | 'autoSendTick'
  | 'composerSlotRef'
  | 'defaultAgent'
  | 'drafts'
  | 'effectiveDefaultAgent'
  | 'fileDrafts'
  | 'frozenInputRef'
  | 'inputRef'
  | 'knowledgeFetchRef'
  | 'lastDictationAnchorRef'
  | 'lastDictationValueRef'
  | 'newSessionRef'
  | 'pasteBlocksRef'
  | 'pasteDrafts'
  | 'pendingAgentRef'
  | 'pendingFilesRef'
  | 'pendingModelRef'
  | 'pendingProjectRef'
  | 'pendingSessionsRef'
  | 'pickedFileTokens'
  | 'postStopEditedRef'
  | 'saveDrafts'
  | 'sendRef'
  | 'sendingRef'
  | 'sessionRefDrafts'
  | 'setAutoSendTick'
  | 'setInput'
  | 'setPasteBlocks'
  | 'setPendingAgent'
  | 'setPendingFiles'
  | 'setPendingModel'
  | 'setPendingProject'
  | 'setPendingSessions'
  | 'setPrefillHint'
  | 'steerMutation'
  | 'streamEnabledRef'
  | 'sttDisarmedRef'
  | 'switchAgentRef'
  | 'switchModelRef'
  | 'voiceRef'
  | 'widgetPrefillRef'
>

export interface UseChatPageActionsControllerOptions {
  activeSlot: string | null
  connected: boolean
  dispatch: AppDispatch
  mode?: string
  slotRunning: boolean
  composer: ChatPageActionsComposerPorts
  resources: Pick<ChatPageResourcesController, 'colorThemeRef' | 'tabsCtl'>
  session: ChatPageActionsSessionPorts
  scroll: ChatPageActionsScrollPorts
}

/**
 * Owns ChatPage's write-side transaction boundary.
 *
 * The port groups are deliberately structural. In particular, `send` takes the
 * composer/voice/session refs themselves rather than a pre-built payload,
 * because entry snapshots, clearing, optimistic rows, receipt reconciliation
 * and failure recovery must stay one ordered transaction that reads live state.
 */
export function useChatPageActionsController({
  activeSlot,
  connected,
  dispatch,
  mode,
  slotRunning,
  composer,
  resources,
  session,
  scroll,
}: UseChatPageActionsControllerOptions) {
  const {
    activeSlotRef,
    autoSendRef,
    autoSendTick,
    composerSlotRef,
    defaultAgent,
    drafts,
    effectiveDefaultAgent,
    fileDrafts,
    frozenInputRef,
    inputRef,
    knowledgeFetchRef,
    lastDictationAnchorRef,
    lastDictationValueRef,
    newSessionRef,
    pasteBlocksRef,
    pasteDrafts,
    pendingAgentRef,
    pendingFilesRef,
    pendingModelRef,
    pendingProjectRef,
    pendingSessionsRef,
    pickedFileTokens,
    postStopEditedRef,
    saveDrafts,
    sendRef,
    sendingRef,
    sessionRefDrafts,
    setAutoSendTick,
    setInput,
    setPasteBlocks,
    setPendingAgent,
    setPendingFiles,
    setPendingModel,
    setPendingProject,
    setPendingSessions,
    setPrefillHint,
    steerMutation,
    streamEnabledRef,
    sttDisarmedRef,
    switchAgentRef,
    switchModelRef,
    voiceRef,
    widgetPrefillRef,
  } = composer
  const { colorThemeRef, tabsCtl } = resources
  const { messages, messagesRef, slots, currentProjectRef } = session
  const { scrollBottom } = scroll

  messagesRef.current = messages
  const currentSlot = slots.find(slot => slot.key === activeSlot)
  // Unified ChatPage often has no page-level mode; the active slot remains the
  // authoritative mode for plan follow-up dispatch and the rendered composer.
  const effectiveMode = currentSlot?.mode || mode
  currentProjectRef.current = currentSlot?.project || undefined

  const pendingQuestion = useAppSelector(state => (
    pendingQuestionFor(state.chat.pendingQuestions, state.chat.activeSlot)
  ))
  const pendingFollowup = useAppSelector(state => (
    state.chat.activeSlot ? state.chat.followups?.[state.chat.activeSlot] : undefined
  ))
  const folderSuggestion = useAppSelector(state => (
    state.chat.activeSlot ? state.chat.folderSuggestions?.[state.chat.activeSlot] : undefined
  ))
  const followupTsBySlot = useAppSelector(state => state.chat.followups) ?? EMPTY_FOLLOWUPS

  const isStreaming = messages[messages.length - 1]?.role === 'streaming'
  // Follow-up options derived from the last assistant message in the current chat.
  // Swapping chats (activeSlot change) → messages change → memo recomputes fresh.
  // A pending question card suppresses them: both would offer the same choices in
  // the same band, and only the card can answer the blocked tool call.
  const { followUpOptions, followUpIsPlan, followUpSourceKey } = useMemo(
    () => deriveFollowUpOptions(messages, isStreaming, !!pendingQuestion),
    [messages, isStreaming, pendingQuestion],
  )
  // Orchestrator plan dispatch — the hook owns the latch acknowledgement,
  // keyed on the derived options-row identity passed here.
  const planActionMutation = usePlanActionMutation(activeSlot, followUpSourceKey)
  // Visual-only highlight state; text in the input is the source of truth for
  // what gets sent. Cleared whenever the options list changes (new assistant
  // message) or the active chat switches — both signal a fresh turn.
  const [followUpPicked, setFollowUpPicked] = useState<Set<string>>(() => new Set())
  // Read by the option handler instead of the state: two clicks landing before a
  // re-render would both see the same set and both take the append branch.
  const followUpPickedRef = useRef(followUpPicked)
  followUpPickedRef.current = followUpPicked
  const followUpOptionsKey = followUpOptions.join('\x00')
  useEffect(() => { setFollowUpPicked(new Set()) }, [followUpOptionsKey, activeSlot])
  const { data: dashCfg } = useQuery<{
    quick_send?: boolean
    session_grid?: boolean
    link_previews?: boolean
    social_share_enabled?: boolean
  }>({
    queryKey: ['dashboardConfig'],
    queryFn: () => api.dashboardConfig(),
    staleTime: 30_000,
  })

  // Raw send — sends pre-built text directly to the server
  const modeRef = useRef(mode)
  modeRef.current = mode
  const planActionMutationRef = useRef(planActionMutation)
  planActionMutationRef.current = planActionMutation

  const send = useCallback(async (
    optionText?: string,
    targetSlot?: string,
    steerNow?: boolean,
  ) => {
    // Defense-in-depth: ChatInput already gates Send/Optimize buttons and
    // the keyboard Enter shortcut on `connected`, but a future caller (a
    // programmatic dispatch from a hotkey, a follow-up option click, an
    // intent handler) could call send() while offline. Bail before we
    // clear the draft via setInput('') below — losing the user's typed
    // message with no recovery path is the offline-UX regression we're
    // guarding against. Cheap belt-and-braces.
    if (!connected) return
    const raw = (optionText || inputRef.current).trim()
    // Capture + clear the widget-origin tag: attribute this
    // turn to a widget only if the composer still carries the exact text a
    // widget action pre-filled. Cleared on every send so it can't go stale.
    const widgetOrigin = !!widgetPrefillRef.current && raw.includes(widgetPrefillRef.current)
    widgetPrefillRef.current = null
    if (!raw && !pendingFilesRef.current.length && !pendingSessionsRef.current.length) return

    // Sending while STREAMING dictation is live ends the dictation. The panel
    // advertises "Enter to send", so this path is reachable by design — and
    // without it, streaming STT keeps running past the send: `onPartial`
    // re-derives the composer value from `frozenInputRef`, which was snapshotted
    // BEFORE the send cleared it, so the next partial repopulates the composer
    // with text the user already sent. Disarm FIRST so any partial/final already
    // in flight is dropped, then stop capture (stop() is async — up to 5s for
    // the backend close).
    //
    // STREAMING ONLY, deliberately. In batch mode the transcription arrives
    // exactly once, from `MediaRecorder.onstop` AFTER capture ends, and it
    // arrives through `onText` — which honours `sttDisarmedRef`. Disarming here
    // would throw away the entire recording, which is the opposite of the bug
    // being fixed. Batch therefore keeps its pre-existing behaviour untouched:
    // capture continues, and the transcript lands when the user stops.
    if (voiceRef.current.recording && streamEnabledRef.current) {
      sttDisarmedRef.current = true
      frozenInputRef.current = null
      lastDictationAnchorRef.current = null
      lastDictationValueRef.current = null
      postStopEditedRef.current = false
      voiceRef.current.toggle()
    }

    // The session actually on screen at send time. Read from the ref (fresh
    // every render), not the closure `activeSlot` (stale until send() is
    // re-memoized). Under lag a reducer-driven activeSlot change can move the
    // active slot before ChatPage re-renders, so the closure would route into
    // the slot the user just left. Used for slash routing, the composer draft
    // clear, and (below) the send target.
    const uiSlot = activeSlotRef.current

    // Capture the stateless card pending at ENTRY — before the first await
    // below. This send consumes the answer channel of the card the user saw
    // when they hit send; captured after an await, the card-submit flow can
    // clear the card (or a newer one can land) in the gap, and the capture
    // would compare against the wrong baseline (fork GPT review, 995718f).
    const entrySendSlot = targetSlot ?? uiSlot
    const cardAtSend = captureStatelessCard(store.getState().chat.pendingQuestions, entrySendSlot)
    // Same entry-time capture for a BLOCKING card, whose staleness is resolved
    // over the network instead of in the store.
    const askAtSend = capturePendingAskId(store.getState().chat.pendingQuestions, entrySendSlot)
    // Entry-time capture of the folder-suggestion card, ONLY when it was
    // actually on screen for this send: the card renders solely in this page's
    // composer band for the ACTIVE slot, so a targeted send into another slot —
    // and any send from a surface that never renders the card (ChatPane) — must
    // not age it. The captured `ts` pins the card GENERATION the user saw; the
    // aging dispatch below is ts-guarded so a replacement card arriving while
    // the POST is in flight does not inherit this send's age.
    const folderCardAtSend = entrySendSlot && entrySendSlot === uiSlot
      ? store.getState().chat.folderSuggestions?.[entrySendSlot]
      : undefined

    // Slash command interception (e.g. /side): runs before knowledge so a
    // bare prefix like /side returns immediately without touching input parse.
    // Gate on the RAW composer text first — a pasted block whose content
    // happens to start with "/side " must stay main-chat content, never
    // become a command. Only a command the user actually typed is expanded
    // (so a paste after "/side " reaches the side chat as content) and
    // delegated. On failure keep the composer intact so the question stays
    // recoverable — same rules as steer()'s guard.
    if (isInterceptedSlashCommand(raw)) {
      const slashPastes = pasteBlocksRef.current
      const slashTxt = slashPastes.length ? expandPasteTokens(raw, slashPastes) : raw
      const slashResult = await interceptSlashCommand(slashTxt, uiSlot, dispatch)
      if (slashResult.intercepted) {
        if (!optionText && !slashResult.failed) {
          setInput('')
          setPasteBlocks([])
        }
        return
      }
    }

    // Knowledge fetch: intercept @knowledge prefix, show picker instead of sending
    const knowledgeQuery = extractKnowledgeQuery(raw)
    if (knowledgeQuery && !optionText) {
      knowledgeFetchRef.current.searchKnowledge(knowledgeQuery)
      setInput('')
      return
    }

    // Snapshot the staged attachments BEFORE the composer is cleared below, so a
    // failed send can put them back (prepareSendPayload's `filePaths` drops
    // images, which would silently lose them on restore).
    const sentFiles = pendingFilesRef.current.slice()
    // Staged refs belong to the COMPOSER, so only a send that consumes the
    // composer may carry them. An `optionText` send (a follow-up option click)
    // supplies its own text and deliberately leaves the composer untouched —
    // the clear below is skipped for exactly that reason. Consuming refs there
    // anyway would attach them to an unrelated message AND leave them staged, so
    // the same links would go out again on the user's next real send.
    //
    // Gated on the same condition as the clear, so the two can never disagree in
    // either direction: no send-without-clear (duplicate) and no clear-without-
    // send (silent loss). Scoped to refs on purpose — `pendingFiles` has carried
    // this shape since long before this feature, and changing it here would widen
    // the PR into pre-existing attachment behaviour.
    const sentSessionRefs = optionText ? [] : pendingSessionsRef.current.slice()
    // Snapshot the staged files BEFORE the optimistic clear below: if the
    // server answers `queued`, this send's pre-serialization composer state
    // is stashed so a cancel can restore it losslessly (see queuedSendStash).
    const stagedFilesAtSend = [...new Set(pendingFilesRef.current)]
    const {
      txt: typedTxt,
      displayTxt: typedDisplayTxt,
      filePaths,
    } = prepareSendPayload(raw, pendingFilesRef.current)
    // Folder references serialize like files but from the text alone: each
    // `@rel/` token becomes `[attached_dir N] /abs/path` in the LLM-facing
    // text (absolute, so the reference survives a cwd/project mismatch and
    // history replay), while the display text keeps the `@rel/` token for the
    // bubble chip — the same fresh-vs-wire split files use. Runs AFTER the
    // file pass: file tokens never end in `/`, so the two rewrites are
    // disjoint. `dirPaths` rides `meta.dirs`, ordered so marker N indexes
    // dirPaths[N-1] losslessly.
    const { llm: typedTxtDirs, dirPaths } = serializeDirTokens(
      typedTxt,
      currentProjectRef.current || '',
    )
    // Staged session references become plain markdown links appended to the
    // message — deliberately a POINTER, not the referenced transcript. Inlining
    // another session's content would spend a large share of THIS session's
    // context window in one turn and can trip autocompact, compacting away the
    // conversation the reference was meant to enrich. The agent follows the link
    // on demand instead, through a read path that is already bounded, redacted,
    // and incognito-refusing server-side.
    //
    // The link is built by the SAME helper the session menu's "Copy link" uses,
    // so a referenced session and a hand-copied one are the same string.
    //
    // Appended to the sent and displayed text alike: unlike a paste token there
    // is no collapsed form to preserve in the bubble, so what the user sees is
    // exactly what was sent. Appending (never splicing) also means paste-token
    // ranges found earlier in the string are untouched.
    const txt = appendSessionRefLinks(typedTxtDirs, sentSessionRefs)
    const displayTxt = appendSessionRefLinks(typedDisplayTxt, sentSessionRefs)
    // Expand paste tokens for the LLM; UI-facing displayTxt keeps the tokens
    // intact so the user bubble can render them as clickable chips.
    const activePastes = pasteBlocksRef.current
    let llmTxt = activePastes.length ? expandPasteTokens(txt, activePastes) : txt
    // Prepend knowledge context if pending
    let knowledgeBlock: KnowledgeBlock | null = null
    if (knowledgeFetchRef.current.pendingKnowledge) {
      knowledgeBlock = knowledgeFetchRef.current.pendingKnowledge
      llmTxt = `${expandKnowledgeBlock(knowledgeBlock)}\n${llmTxt}`
    }
    knowledgeFetchRef.current.clearPending()
    const bubblePastes = pruneBlocksUtil(displayTxt, activePastes)
    if (bubblePastes.length) saveStoredPaste(llmTxt, displayTxt, bubblePastes, filePaths)

    setPrefillHint(false)
    if (!optionText) {
      setInput('')
      setPendingFiles([])
      pickedFileTokens.current = {}
      setPasteBlocks([])
      setPendingSessions([])
      if (uiSlot) {
        delete drafts.current[uiSlot]
        delete fileDrafts.current[uiSlot]
        delete pasteDrafts.current[uiSlot]
        delete sessionRefDrafts.current[uiSlot]
        saveDrafts()
      }
      // The challenge-handoff prompt is seeded into PREFILL_STORAGE_KEY and the
      // slot-restore effect re-applies it on slot changes. Once that prompt is
      // sent, clear the seed so a later slot-restore can't re-fill the (now
      // empty) composer with the already-sent text.
      try {
        sessionStorage.removeItem(PREFILL_STORAGE_KEY)
      } catch {
        // sessionStorage unavailable
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
      sendingRef.current = true
      // The composer was cleared above, so a create failure here would destroy
      // the user's text: `.unwrap()` rejects, send() unwinds, and nothing is
      // ever sent — no error bubble, no draft to recover, and sendingRef stuck
      // true (which suppresses the welcome state). Restore the composer, its
      // paste blocks and attachments, surface the failure, and bail.
      let created: { key: string } | null = null
      try {
        created = await dispatch(createSlot({
          agent: pendingAgentRef.current || defaultAgent || undefined,
          model: pendingModelRef.current || undefined,
          mode: modeRef.current,
        })).unwrap()
      } catch (error: unknown) {
        sendingRef.current = false
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
        const keepFiles = onScreen
          ? pendingFilesRef.current
          : (uiSlot ? fileDrafts.current[uiSlot] ?? [] : [])
        const restoredFiles = [...new Set([...keepFiles, ...sentFiles])]
        // Session refs merge by key (they carry no sequence to collide on, unlike
        // pastes), keeping whatever the user staged since the failed send.
        const keepRefs = onScreen
          ? pendingSessionsRef.current
          : (uiSlot ? sessionRefDrafts.current[uiSlot] ?? [] : [])
        const restoredRefs = mergeSessionRefs(keepRefs, sentSessionRefs)
        const keepPastes = onScreen
          ? pasteBlocksRef.current
          : (uiSlot ? pasteDrafts.current[uiSlot] ?? [] : [])
        const keptPasteIds = new Set(keepPastes.map(block => block.id))
        // Collapsed pastes resolve by `seq`, not id, and a paste made while the
        // composer was empty restarts at #1 — so a naive id-merge can leave two
        // blocks sharing #1, with both markers resolving to one of them and
        // silently swapping the user's content on retry. Re-sequence the carried
        // blocks past the kept ones and rewrite their markers in the payload text.
        const { text: payload, blocks: carriedPastes } = remapCarriedBlocks(
          raw,
          activePastes.filter(block => !keptPasteIds.has(block.id)),
          new Set(keepPastes.map(block => block.seq)),
        )
        const restoredPastes = [...keepPastes, ...carriedPastes]
        const keepText = onScreen
          ? inputRef.current
          : (uiSlot ? drafts.current[uiSlot] ?? '' : '')
        // Keep whatever the user typed while the create was in flight and append
        // the payload after it, without duplicating one the composer already
        // holds — a synchronously rejected create can land before React flushes
        // the clear. `mergeRecoveredDraft` owns that rule for every recovery
        // site, including the send-failure path further down.
        const restoredText = mergeRecoveredDraft(keepText, payload)
        if (onScreen && uiSlot) {
          setInput(restoredText)
          setPasteBlocks(restoredPastes)
          setPendingFiles(restoredFiles)
          setPendingSessions(restoredRefs)
          // clearPending() above already consumed the knowledge selection, so a
          // retry would otherwise go out WITHOUT the context the user picked. Slot-
          // gated: selection is per-slot, so re-injecting while the user views another
          // session would smear it there. MERGE rather than skip-or-replace — `inject`
          // replaces, so skipping when a newer selection exists would drop the failed
          // turn's context, and replacing would drop what the user picked since. Newer
          // items win on an id collision.
          if (knowledgeBlock) {
            const newer = knowledgeFetchRef.current.pendingKnowledge?.items ?? []
            const newerIds = new Set(newer.map(item => item.id))
            knowledgeFetchRef.current.inject([
              ...knowledgeBlock.items.filter(item => !newerIds.has(item.id)),
              ...newer,
            ])
          }
          dispatch(appendMessage({
            role: 'error',
            content: i18nT('pages.chatPage.could_not_start_session_message_restored', {
              error: createFailReason(error),
            }),
            cls: '',
          }))
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
            setAutoSendTick(tick => tick + 1)
          } else {
            dispatch(addNotification({
              ts: uniqueNotificationTs(),
              kind: 'agent',
              priority: 'critical',
              title: i18nT('pages.chatPage.could_not_start_a_new_session'),
              body: i18nT('pages.chatPage.message_queued_until_session_ready', {
                error: createFailReason(error),
              }),
            }))
          }
        } else if (!onScreen) {
          // The knowledge selection is NOT restored here: `inject` writes to the slot
          // the user is now viewing, so restoring it off-screen would attach the failed
          // turn's context to an unrelated session. Re-selecting is a two-click library
          // action (unlike typed text, which is unrecoverable), so this reports the gap
          // instead of routing knowledge per-slot — but it must not be silent.
          // Compatibility: ChatPage put this English suffix into the parent
          // `{{extra}}` interpolation regardless of the active locale. Preserve its
          // source language and placement so every locale, including en-XA, keeps the
          // exact legacy notification bytes.
          const lostKnowledgeContext = knowledgeBlock
            ? String.fromCharCode(32) + i18nT('pages.chatPage.knowledge_context_was_not_kept', {
              lng: 'en',
            })
            : ''
          dispatch(addNotification({
            ts: uniqueNotificationTs(),
            kind: 'agent',
            priority: 'critical',
            title: i18nT('pages.chatPage.could_not_start_a_new_session'),
            body: i18nT('pages.chatPage.message_saved_as_draft', {
              error: createFailReason(error),
              extra: lostKnowledgeContext,
            }),
            slot: uiSlot,
          }))
        }
        if (uiSlot) {
          setDraft(drafts.current, uiSlot, restoredText)
          setPasteDraft(pasteDrafts.current, uiSlot, restoredPastes)
          setFileDraft(fileDrafts.current, uiSlot, restoredFiles)
          setSessionRefDraft(sessionRefDrafts.current, uiSlot, restoredRefs)
          saveDrafts()
        }
        return
      }
      slot = created.key
      if (pendingProjectRef.current) {
        await api.chatSlotProject(created.key, pendingProjectRef.current).catch(error => {
          // eslint-disable-next-line no-console -- surface project-assign failures for debugging
          console.error('chatSlotProject failed', error)
        })
      }
    }
    setPendingAgent('')
    setPendingModel('')
    setPendingProject('')
    // Build meta for persistence (knowledge, files, pastes)
    const meta: Record<string, unknown> = {}
    if (filePaths.length) meta.files = filePaths
    if (dirPaths.length) meta.dirs = dirPaths
    if (bubblePastes.length) meta.pastes = bubblePastes
    if (knowledgeBlock) {
      meta.knowledge = {
        items: knowledgeBlock.items.length,
        tokens: knowledgeBlock.totalTokens,
        titles: knowledgeBlock.items.map(item => item.title),
        content: knowledgeBlock.items.map(item => ({
          title: item.title,
          text: item.content.slice(0, 2000),
        })),
      }
    }
    if (widgetOrigin) meta.origin = 'widget'
    // A client-generated correlation ID so the server echo can be matched
    // to this exact optimistic bubble without relying on content equality.
    // The server preserves meta fields on the user row it appends, so the
    // echo carries both this sendId AND the server-minted `mid` (#2845).
    const sendId = mintSendId()
    meta.sendId = sendId
    const metaPayload = meta
    // Skip optimistic user bubble when the slot is busy (shared rule:
    // chatSlice.selectComposerBusy) — the backend sends a "queued" role
    // message instead, avoiding a duplicate. A steer-flagged send usually
    // bypasses the queue and starts a turn, so nothing would represent it; its
    // bubble is appended from the response instead (see below), because only
    // the server knows whether this particular send got queued after all.
    const busyAtSend = selectComposerBusy(store.getState(), slot ?? null)
    if (!busyAtSend || forceNew) {
      dispatch(appendMessage({
        role: 'user',
        content: displayTxt,
        cls: '',
        ts: new Date().toISOString(),
        meta: metaPayload,
      }))
    }
    window.dispatchEvent(new Event('voice-stop'))
    sendingRef.current = false
    setTimeout(() => scrollBottom(), SCROLL_AFTER_RENDER_MS)
    if (slot) dispatch(startLocalTurn(slot))
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), 10_000)
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
     * Restores `typedTxt` — what the user actually TYPED — and brings the staged
     * references back as chips, rather than restoring the link-appended `txt`.
     * Restoring `txt` preserved the reference (the link is in the text) but left
     * it as a raw URL, and re-staging the chips ON TOP of that text would make
     * the retry append each link a SECOND time. Splitting them puts the composer
     * back in exactly its pre-send state: chip visible, link appended once on
     * retry. Paste blocks come back too, or the restored text would show a dead
     * `[ Paste #N · M lines ]` literal. Shares the create-failure path's merge
     * rule so a reference staged while the send was in flight is not clobbered.
     */
    const restoreComposerAfterFailedSend = () => {
      if (!slot) return
      const onScreenNow = slot === activeSlotRef.current
      const liveRefs = onScreenNow
        ? pendingSessionsRef.current
        : (sessionRefDrafts.current[slot] ?? [])
      const refsBack = mergeSessionRefs(liveRefs, sentSessionRefs)
      // MERGE, never overwrite. The send is in flight for up to 10s, and the user
      // can type a fresh message in that window — clobbering it with the failed
      // payload would lose newer work to recover older. Mirrors the create-failure
      // path above: keep what is there, append the failed payload unless it is
      // already the same text, and re-sequence the carried paste blocks so two
      // blocks cannot claim one `[ Paste #N ]` marker.
      const keepText = onScreenNow ? inputRef.current : (drafts.current[slot] ?? '')
      const keepPastes = onScreenNow
        ? pasteBlocksRef.current
        : (pasteDrafts.current[slot] ?? [])
      const keptIds = new Set(keepPastes.map(block => block.id))
      const { text: carriedText, blocks: carriedPastes } = remapCarriedBlocks(
        typedTxt,
        activePastes.filter(block => !keptIds.has(block.id)),
        new Set(keepPastes.map(block => block.seq)),
      )
      const pastesBack = [...keepPastes, ...carriedPastes]
      // Same merge rule as the create-failure path above, and the separator lives
      // in `mergeRecoveredDraft` rather than in a template literal here: the blank
      // line between the kept draft and the recovered payload is message
      // structure, not copy, so it stays off the i18n gate honestly rather than by
      // exemption (same treatment as appendSessionRefLinks).
      const textBack = mergeRecoveredDraft(keepText, carriedText)
      setDraft(drafts.current, slot, textBack)
      setPasteDraft(pasteDrafts.current, slot, pastesBack)
      setSessionRefDraft(sessionRefDrafts.current, slot, refsBack)
      saveDrafts()
      if (onScreenNow) {
        setInput(textBack)
        setPasteBlocks(pastesBack)
        setPendingSessions(refsBack)
      }
    }
    try {
      const response = await api.sendChat(
        llmTxt,
        slot ?? undefined,
        colorThemeRef.current,
        controller.signal,
        metaPayload,
        steerNow,
      )
      clearTimeout(timeout)
      const { body, outcome } = await readSendReceipt(response)
      // An UNKNOWN outcome — a 2xx whose body would not parse — reaches neither
      // arm below and is the point of routing through `readSendReceipt`. The
      // request was accepted and only its answer is mangled, so this send sits
      // where the abort in the catch below sits: it may have started a turn that
      // is streaming right now. Reporting a refusal there would hand the payload
      // back and invite a retry that duplicates a delivered turn, so an unknown
      // takes no action rather than asserting a refusal it cannot prove.
      if (body.queued && llmTxt === typedTxtDirs) {
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
        // text is exactly what {raw, staged files} alone explain
        // (`typedTxtDirs` — prepareSendPayload + dir-token serialization).
        // Expanded paste blocks, appended session-ref links, a prepended
        // knowledge block, and ANY FUTURE feature that diverges `llmTxt`
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
          queuedSendStash.set(body.queue_id, { raw, files: stagedFilesAtSend, sent: llmTxt })
        }
      }
      if (outcome === 'refused') {
        dispatch(setSlotRunning(false))
        const reason = typeof body.error === 'string' ? body.error : ''
        dispatch(appendMessage({
          role: 'error',
          content: reason || i18nT('pages.chatPage.send_failed'),
          cls: '',
        }))
        // The server explicitly accepted neither (`ok` nor `queued`), so nothing
        // was sent — recovering the composer cannot duplicate a delivered turn.
        restoreComposerAfterFailedSend()
      } else if (
        outcome === 'accepted'
        && steerNow
        && busyAtSend
        && !body.queued
        && !body.steered
      ) {
        // A steer-flagged send the server neither queued nor injected: it
        // started a turn, so no `queue_push` or `steer_push` echo is coming and
        // the busy rule above left the text with nothing to represent it.
        // Append only once the answer rules out both echoes — a mid-plan send
        // is queued, and a child turn that started while this POST was in
        // flight is injected mid-turn, each of which brings its own bubble.
        // Addressed to the SENDING slot, not the active one: the user can
        // switch sessions while the POST is in flight, and this text belongs to
        // the transcript it was typed into (same reason `steer_push` uses this).
        if (slot) {
          dispatch(appendSlotMessage({
            slot,
            message: {
              role: 'user',
              content: displayTxt,
              cls: '',
              ts: new Date().toISOString(),
              meta: metaPayload,
            },
          }))
        }
      }
      if (slot && confirmedDelivered(body)) {
        // The response IS the delivery receipt (#4131). The server accepted the
        // message and appended (or queued) the row, so the optimistic bubble is
        // confirmed and must stop being a candidate for the 30s "may not have
        // been delivered" sweep. Nothing else can retire it on this surface: the
        // `chat_message` user echo `reconcileOptimisticEcho` waits for is
        // suppressed for every dashboard send by design (`DashboardState.append`
        // defaults `broadcast_user=False` precisely because the composer already
        // rendered this bubble), so before this the flag survived the whole turn
        // and only vanished when `chat_done`'s refresh rebuilt the transcript
        // from disk.
        //
        // Addressed to the SENDING slot for the same reason as the steer-echo
        // append above. Harmless when the busy rule appended no bubble — no row
        // carries this `sendId`, so it is a no-op. Deliberately NOT dispatched on
        // a rejected response, a queued acceptance, or the abort-timeout path:
        // there delivery of THIS row is unknown, which is what the indicator
        // exists to say (see `confirmedDelivered`).
        // The receipt carries the server-minted user-row `mid` (when the send
        // dispatched immediately); handing it to the reconcile stamps it onto
        // this optimistic bubble so message-pinning works this turn instead of
        // only after the chat_done refresh.
        dispatch(confirmOptimisticSend({
          slot,
          sendId,
          mid: typeof body.mid === 'string' ? body.mid : undefined,
        }))
      }
      if (body.ok && !body.queued && cardAtSend && slot === entrySendSlot) {
        // Immediate dispatch confirmed (`ok`): the message consumed the slot's
        // next-turn channel, so the card captured at entry is now stale. An
        // independent check, not part of the else-if chain above — the
        // steer-echo branch also implies `ok && !queued`, and the card must
        // retire regardless of which transcript-echo rule applied. A QUEUED
        // acceptance deliberately does NOT retire here — the queued message is
        // still cancellable, and cancelling must keep the card; it retires at
        // its queue_pop instead (removeQueuedMessage). The slot-identity guard
        // covers forceNew rerouting the send into a freshly created session —
        // that send answers nothing in the entry slot, whose card must stay.
        // Deliberately NOT done on the optimistic append (a failed send must
        // keep the card) nor on the abort-timeout path below (delivery
        // unconfirmed — a wrongly kept card is dismissible, a wrongly deleted
        // one is not recoverable).
        dispatch(retireStatelessQuestion({ slot, expected: cardAtSend }))
      }
      if (body.ok && !body.queued && folderCardAtSend && slot === entrySendSlot) {
        // Same delivery bar and slot-identity guard as the stateless-card
        // retirement above, for the folder-suggestion card's turn-aging: the
        // card was on screen when the user hit send (captured at entry, active
        // slot only) and the server confirmed the send was delivered. Failed
        // sends never reach here; queued sends are still cancellable; forceNew
        // reroutes answer nothing in the entry slot. ts pins the card
        // generation, so a replacement that landed mid-flight is not aged.
        dispatch(ageFolderSuggestion({ slot, ts: folderCardAtSend.ts }))
      }
      // The user answered in the composer instead of the card; a blocking card
      // is resolved over the network, so this cannot be a store-only retirement.
      void resolveAskAfterSend(body, slot === entrySendSlot ? askAtSend : null, dispatch)
    } catch (error: unknown) {
      clearTimeout(timeout)
      if (error instanceof DOMException && error.name === 'AbortError') {
        // Timeout — message was received, WS will deliver response
      } else {
        dispatch(setSlotRunning(false))
        dispatch(appendMessage({
          role: 'error',
          content: i18nT('pages.chatPage.connection_error'),
          cls: '',
        }))
        restoreComposerAfterFailedSend()
      }
    }
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
  }, [activeSlot, dispatch, connected])

  // Submit inline document comments to the session the file was opened from,
  // not the currently-active one. If the user switched sessions while the
  // panel was open, switch back to the origin session so the prompt + reply
  // land where the document belongs. switchSlot.pending sets activeSlot
  // synchronously, but send()'s closure activeSlot is stale until re-render,
  // so the origin slot is passed to send() explicitly.
  // Keep sendRef current so the streaming endpointer's auto-submit callback
  // (wired into the voice hook above, before send is declared) always invokes
  // the latest send(). Assigned in render like inputRef.current = input above.
  sendRef.current = send
  const submitComments = useCallback((message: string) => {
    // Defense-in-depth: the panels' submit buttons are gated on `connected`,
    // but bail here too so an offline call can't switch the active session
    // and then have send() silently drop the message.
    if (!connected) return
    const target = tabsCtl.activeTab?.slot ?? null
    if (target && target !== activeSlot) dispatch(switchSlot(target))
    void send(message, target ?? undefined)
  }, [connected, tabsCtl.activeTab, activeSlot, dispatch, send])

  // Auto-send when navigated with ?autoSend=1 or ?token= with prompt
  useEffect(() => {
    if (connected && autoSendRef.current) {
      const text = autoSendRef.current
      autoSendRef.current = null
      void send(text)
    }
  }, [send, connected, autoSendTick, autoSendRef])

  // Widget interactivity: when a mcwidget iframe fires an action, PRE-FILL the
  // composer instead of auto-submitting. Auto-submitting would be a
  // trust-boundary bypass: LLM-emitted <script> inside the sandboxed widget
  // iframe can call parent.postMessage directly, bypassing the in-iframe
  // isTrusted click guard, and the parent cannot distinguish that from a
  // genuine click. So a widget action must never become a user-role turn
  // without an explicit human gesture — the user reviews the pre-filled text
  // and presses Enter. We also record the pre-filled text so the resulting
  // send is tagged meta.origin='widget' for forensics.
  useEffect(() => {
    const handler = (event: Event) => {
      const text = (event as CustomEvent).detail?.text
      if (typeof text !== 'string' || !text) return
      widgetPrefillRef.current = text
      setInput(previous => (previous.trim() ? `${previous.trimEnd()}\n${text}` : text))
      setPrefillHint(true)
      revealComposer()
    }
    window.addEventListener('mc-widget-send', handler)
    return () => window.removeEventListener('mc-widget-send', handler)
  }, [setInput, setPrefillHint, widgetPrefillRef])

  const approve = useCallback(async (action: string) => {
    if (activeSlot) await api.approveChatSlot(activeSlot, action)
  }, [activeSlot])
  // Approvals dismissed through this mapping resolve via the ONE-SHOT
  // `api.resolveApproval` endpoint, which has no trust verb: it can honor
  // exactly `approve` or `reject`, and the next identical call prompts again.
  // Any UI feeding this path must offer only those decisions — a Trust
  // affordance here would claim a standing grant the backend never records
  // (#5400 on the spawn-approval card, #5434 on the collapsed tool row).
  const toApiDecision = useCallback((action: string): 'approve' | 'reject' => (
    action === 'approved' ? 'approve' : 'reject'
  ), [])
  const dismissApproval = useCallback((approvalId: string, decision?: string) => {
    dispatch(resolveByApprovalId({ id: approvalId, decision }))
    const notification = store.getState().notifications.items.find(
      item => item.approval_id === approvalId,
    )
    if (notification) dispatch(removeNotificationByTs(notification.ts))
  }, [dispatch])

  const switchAgent = useCallback(async (agentName: string) => {
    if (!activeSlot) {
      setPendingAgent(agentName)
      // Clear any explicit pick made for the PREVIOUS agent rather than
      // re-seeding a resolved model: an empty pendingModel makes createSlot omit
      // `model`, which lets the backend resolve the new agent's own chain at
      // create time. Seeding the resolved id here pinned it instead (#2035).
      setPendingModel('')
      return
    }
    dispatch(setAgentSwitchNotice(null))
    try {
      // Same protocol as switchModel below (#4523): the acting tab must not
      // depend on the coalesced slots rebroadcast to see its own pick.
      // performAgentSlotSwitch mirrors exactly what the response names.
      await performAgentSlotSwitch(activeSlot, agentName, dispatch)
    } catch (error) {
      // Closing the picker is the call sites' job and already happens
      // synchronously alongside this call, so a failure surfaces as the shared
      // notice rather than by holding the dropdown open.
      dispatch(setAgentSwitchNotice(agentSwitchFailureMessage(error)))
    }
    // The setPending* setters are useState setters, so they are stable and cost
    // nothing to list. `installedAgents`, `provider` and `queryClient` are
    // deliberately absent: this body reads none of them, and `installedAgents` is
    // a fresh array on every agents refetch, so naming it would rebuild the
    // callback — and every picker holding it — for no behavioral gain.
  }, [
    activeSlot,
    dispatch,
    setPendingAgent,
    setPendingModel,
  ])

  const switchModel = useCallback(async (modelName: string) => {
    // 'auto' is stored VERBATIM, not collapsed to ''. Both resolve to the same
    // provider behaviour server-side, but '' is also the "never chosen" state,
    // and every reader of an empty model re-resolves it to the agent template's
    // model (the `resolvedModel` / `_initResolvedModel` queries below, and the
    // backend's slot.model backfill). Writing '' therefore made an explicit Auto
    // pick snap straight back to e.g. claude-opus-5 — Auto was unselectable.
    // kiro-cli advertises `auto` as a real model id (and its default_model), and
    // the ChatPane + Alt+Shift model-cycle paths already send it verbatim.
    if (!activeSlot) {
      setPendingModel(modelName)
      return
    }
    try {
      // performSlotSwitch owns the whole protocol: per-slot+field serialized
      // dispatch, latest-request-wins adjudication, hung-request timeout, and
      // exactly-one store write on the authoritative value (#4523). The store
      // write is deliberately NOT awaited on the server's slots rebroadcast:
      // that push is coalesced and never arrives with the websocket down.
      await performSlotSwitch(
        'model',
        activeSlot,
        modelName,
        async () => {
          // The response's `model` is the stored value (deprecated ids are
          // remapped server-side), so prefer it over the requested name.
          const response = await api.chatSlotModel(activeSlot, modelName)
          return response?.model ?? modelName
        },
        value => dispatch(updateSlot({ key: activeSlot, model: value })),
      )
    } catch (error) {
      // Same failure surface as the agent switch beside this: the shared
      // notice toast, preferring the server's own message. The chip keeps
      // showing what is actually running either way.
      dispatch(setAgentSwitchNotice(agentSwitchFailureMessage(error)))
      // eslint-disable-next-line no-console -- surface switchModel failures for debugging
      console.error('switchModel failed', error)
    }
    // Keep the dropdown open after selecting — the user may switch models again
    // or drill into the reasoning-effort panel. Dismiss is via outside-click/Escape.
    // setPendingModel is a stable useState setter.
  }, [activeSlot, dispatch, setPendingModel])

  const setProject = useCallback(async (path: string) => {
    if (!activeSlot) {
      setPendingProject(path)
      return
    }
    try {
      // Same protocol as switchModel above; the server realpath-normalizes
      // the directory, so the response's spelling is what gets written.
      await performSlotSwitch(
        'project',
        activeSlot,
        path,
        async () => {
          const response = await api.chatSlotProject(activeSlot, path)
          return response?.project ?? path
        },
        value => dispatch(updateSlot({ key: activeSlot, project: value })),
      )
    } catch (error) {
      dispatch(setAgentSwitchNotice(agentSwitchFailureMessage(error)))
      // eslint-disable-next-line no-console -- surface setProject failures for debugging
      console.error('setProject failed', error)
    }
    // setPendingProject is a stable ref-backed setter.
  }, [activeSlot, dispatch, setPendingProject])

  // The filtered-dropdown keyboard handlers are created in the composer
  // controller before these actions exist. Keep their forward refs current in
  // render, exactly as the voice endpointer's sendRef above.
  switchAgentRef.current = switchAgent
  switchModelRef.current = switchModel

  // One source for both same-meaning markers in the agent pop-up: the row's check and
  // the default-agent row's label. Reading the slot twice let them disagree.
  // A peer-bound session falls back to the PEER's default (effectiveDefaultAgent),
  // never this machine's: the backend deliberately stores no agent for such a slot,
  // so `defaultAgent` here would advertise a crew from the wrong roster while the
  // peer answered with its own.
  const activeAgentName = currentSlot?.agent || effectiveDefaultAgent || 'default'

  // ── Follow-up card actions (suggest_followup MCP tool) ───────────────────
  // Both routes PRE-FILL a composer and stop; neither sends. `setPendingInput`
  // is consumed by the effect above, which drops the text into the composer and
  // flags the prefill hint — the same path the Projects page and command
  // palette use, so there is one prefill mechanism, not a parallel one.
  //
  // Live per-slot card timestamps, read inside async actions without making them
  // depend on (and re-create on) every card change.
  const followupTsRef = useRef<Record<string, { items: FollowupItem[]; ts: number }>>({})
  followupTsRef.current = followupTsBySlot
  const followupAddToSession = useCallback((item: FollowupItem) => {
    if (!activeSlot) return
    // APPEND when the composer already holds unsent text: the pending-input path
    // replaces the draft and persists it, so a plain set would silently destroy
    // whatever the user was mid-way through typing. `inputRef` is the live
    // composer value; `mergeIntoDraft` is shared with the error → agent hand-off
    // drain so the two paths cannot drift.
    dispatch(setPendingInput(mergeIntoDraft(inputRef.current, item.prompt)))
    // Clear by the RENDERED card's ts, as the worktree action does: a newer card
    // for this slot can land between render and click, and an unqualified clear
    // would delete suggestions the user never saw.
    dispatch(clearFollowupCard({
      slot: activeSlot,
      ts: followupTsRef.current[activeSlot]?.ts,
    }))
  }, [dispatch, activeSlot, inputRef])

  // Folder suggestion: accepting reuses the ONE move path every other surface
  // (row menu, drag-to-folder, new-chat-in-folder) already funnels through, so
  // the optimistic update and its guarded rollback are inherited rather than
  // re-implemented here. Both answers clear the card by the ts it rendered with,
  // for the same reason the follow-up actions do.
  const moveSlotToFolder = useMoveSlotToFolder()
  const folderSuggestionAccept = useCallback(() => {
    if (!activeSlot || !folderSuggestion) return
    moveSlotToFolder(activeSlot, folderSuggestion.folderId)
    dispatch(clearFolderSuggestion({ slot: activeSlot, ts: folderSuggestion.ts }))
  }, [activeSlot, folderSuggestion, moveSlotToFolder, dispatch])

  const folderSuggestionDecline = useCallback(() => {
    if (!activeSlot || !folderSuggestion) return
    // Nothing to tell the backend: it already spent its one offer for this slot,
    // so declining is purely "take the card away".
    dispatch(clearFolderSuggestion({ slot: activeSlot, ts: folderSuggestion.ts }))
  }, [activeSlot, folderSuggestion, dispatch])

  // Fallback branch name when the agent did not supply one: slugify the title
  // under FOLLOWUP_BRANCH_RE's grammar (the server re-validates, so a slug that
  // degenerates to empty is replaced rather than sent and rejected).
  const followupBranchFor = useCallback((item: FollowupItem) => {
    if (item.branch) return item.branch
    const slug = item.title
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 40)
    return `followup/${slug || 'suggestion'}`
  }, [])

  const followupStartInWorktree = useCallback(async (item: FollowupItem) => {
    const repo = currentSlot?.project
    if (!repo) {
      throw new Error(i18nT('pages.chatPage.this_session_has_no_project_directory_to_branch'))
    }
    const originSlot = activeSlot
    // Capture the card's ts up front so completion clears only THIS card. A
    // newer card can arrive for the same slot while the request is in flight;
    // without the guard the older action's completion would clobber it.
    const originTs = originSlot ? followupTsRef.current[originSlot]?.ts : undefined
    // Create the worktree FIRST: if git refuses (branch exists, not a repo),
    // we must not have already spawned an empty session the user has to clean
    // up. The card surfaces the thrown message inline.
    const result = await api.createWorktree(repo, followupBranchFor(item))
    const path = result?.path
    if (!path) {
      throw new Error(
        result?.error || i18nT('pages.chatPage.worktree_creation_returned_no_path'),
      )
    }
    let slotKey = ''
    try {
      // `activate: false` on purpose: the slot must be SCOPED to the worktree
      // before the user can type into it. Activating first (the default) leaves a
      // window where the composer is live but `chatSlotProject` is still pending,
      // so a turn sent in that window would run in the default directory — agent
      // tools writing to the wrong checkout. It also means a scoping failure can
      // render its error on the still-mounted card instead of unmounting it.
      const slot = await dispatch(createSlot({ mode, project: path, activate: false })).unwrap()
      slotKey = slot?.key || ''
    } catch {
      // The worktree exists but the session does not. Say so, and name the path:
      // the create endpoint is idempotent for its own destination, so pressing
      // the button again reuses this worktree instead of 409-ing on it.
      throw new Error(
        `Worktree created at ${path}, but its session could not be opened and scoped. `
        + 'Press the button again to retry — the existing worktree will be reused.',
      )
    }
    // A fulfilled thunk with no key would skip every guard below (scoping,
    // activation, focus verification) and prefill whatever session is on screen
    // — the exact fail-open the docs promise not to do. Fail closed instead.
    if (!slotKey) {
      throw new Error(
        `Worktree created at ${path}, but no session was returned. `
        + 'Press the button again to retry — the existing worktree will be reused.',
      )
    }
    // Scoping is NOT done here: `createSlot({ activate: false })` awaits the
    // project assignment before it publishes the slot, and deletes the session if
    // that fails, so the slot is never reachable in an unscoped state. A failure
    // therefore rejects the thunk and is reported by the catch above.
    // createSlot's fulfilled reducer deliberately does NOT activate its result
    // if the user switched sessions while the create was in flight. The
    // prefill below writes to the *active* composer, so without this the
    // prompt would land in whatever unrelated session is on screen and the new
    // worktree session would open empty. The user asked for this worktree by
    // clicking; take them to it — and if that fails, surface the error and
    // keep the card rather than prefilling the wrong conversation.
    // Read the store directly, NOT activeSlotRef: the ref is refreshed by a
    // render, and `unwrap()` resolves as soon as the reducer ran — so a stale
    // ref would report a failure (and skip the prefill) on a switch that in
    // fact succeeded. store.getState() sees the committed value immediately.
    // Hand the prompt over through PREFILL_STORAGE_KEY *before* the switch — the
    // same channel the ?sid / popout paths use. `setPendingInput` alone loses the
    // race: its consuming effect is declared BEFORE the per-slot draft-restore
    // effect, so when the switch and the prefill land in one React commit the
    // restore runs last and overwrites the composer with the incoming slot's
    // (empty) draft, and the prompt vanishes. Seeding the prefill makes the
    // restore itself apply the prompt, so there is nothing left to race.
    writePrefill(slotKey, item.prompt)
    if (store.getState().chat.activeSlot !== slotKey) {
      try {
        await dispatch(switchSlot(slotKey)).unwrap()
      } catch {
        throw new Error(
          `Worktree ready at ${path}, but its session could not be opened. `
          + 'Switch to it in the sidebar, or press the button again.',
        )
      }
    }
    if (store.getState().chat.activeSlot !== slotKey) {
      throw new Error(
        `Worktree ready at ${path}, but its session is not in focus. `
        + 'Switch to it in the sidebar, or press the button again.',
      )
    }
    dispatch(setPendingInput(item.prompt))
    if (originSlot) {
      dispatch(clearFollowupCard({ slot: originSlot, ts: originTs }))
    }
  }, [currentSlot?.project, followupBranchFor, dispatch, mode, activeSlot])

  const dismissFollowup = useCallback((index: number) => {
    if (!activeSlot || !pendingFollowup) return
    dispatch(dismissFollowupItem({
      slot: activeSlot,
      index,
      ts: pendingFollowup.ts,
    }))
  }, [activeSlot, pendingFollowup, dispatch])

  const [regenerating, setRegenerating] = useState(false)
  useEffect(() => { setRegenerating(false) }, [activeSlot])
  // Clear typing dots as soon as streaming starts
  useEffect(() => {
    if (regenerating && isStreaming) setRegenerating(false)
  }, [regenerating, isStreaming])
  // Safety timeout
  useEffect(() => {
    if (!regenerating) return
    const timeout = setTimeout(() => { setRegenerating(false) }, 30_000)
    return () => clearTimeout(timeout)
  }, [regenerating])

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
  const [refusedPress, setRefusedPress] = useState<{
    action: RefusedPressAction
    message: string
  } | null>(null)
  const showRefusedPress = useCallback((action: RefusedPressAction, error: unknown) => {
    setRefusedPress({
      action,
      message: error instanceof Error && error.message ? error.message : String(error),
    })
  }, [])
  useEffect(() => { setRefusedPress(null) }, [activeSlot])
  // A turn that actually starts retires the refusal: whatever the slot was busy
  // with is over, so the old reason would now describe a state that passed.
  useEffect(() => { if (slotRunning) setRefusedPress(null) }, [slotRunning])

  const handleRegenerate = useCallback(() => {
    if (!activeSlot || regenerating || slotRunning) return
    // Mirror the server's scan exactly (chat_regenerate.py): the turn being
    // regenerated ends at the last assistant row BY ROLE — hidden
    // invisible-only rows included — so this optimistic truncation cannot
    // diverge from the history rewrite the server persists. The skip-aware
    // `lastTextIdx` the page derives for rendering only decides which DRAWN row
    // hosts the affordance; using it here would truncate after an earlier user
    // row than the server does.
    const assistantIndex = messages.map(message => message.role).lastIndexOf('assistant')
    if (assistantIndex < 0) return
    const userIndex = messages
      .slice(0, assistantIndex)
      .map(message => message.role)
      .lastIndexOf('user')
    if (userIndex < 0) return
    const snapshot = [...messages]
    dispatch(truncateAfterIndex(userIndex + 1))
    setRegenerating(true)
    api.regenerateSlot(activeSlot).catch((error: unknown) => {
      showRefusedPress('regenerate', error)
      dispatch(replaceMessages(snapshot))
      setRegenerating(false)
    })
  }, [
    activeSlot,
    regenerating,
    slotRunning,
    messages,
    dispatch,
    showRefusedPress,
  ])

  // ---- Continue the thread ---------------------------------------------------
  // A turn can end without the assistant handing the floor back: the connection
  // dropped, the gateway restarted during an app update, the app was force-quit,
  // or the runner's own recovery ladder gave up. Some of those leave evidence (an
  // unanswered user row, a trailing error card) and some leave none at all — a
  // force-quit runs no cleanup, so its transcript is indistinguishable from a
  // clean finish. Continue is therefore offered on any idle slot with a
  // conversation, and `interrupted` only decides how the button describes itself.
  //
  // The two COMPOSE at the ErrorCard; neither alone is right. `continuable` is the
  // availability half (running, stopping, pending turn, autopilot, subagents,
  // queue) and `interrupted` is the placement half — `i === lastErrorIdx` means
  // "newest error row", never "the transcript ends badly", so on
  // `[user, error, user, assistant]` availability alone would put a Continue
  // button on a superseded failure card that acts on a LATER request. Dropping
  // `continuable` instead is the mirror-image bug: `selectTurnInterrupted` carries
  // none of the busy checks, so a card would offer a Continue that `handleContinue`
  // early-returns on — a dead control in the one place recovery is promised.
  const continuable = useAppSelector(selectContinuable)
  const interrupted = useAppSelector(selectTurnInterrupted)
  const [continuing, setContinuing] = useState(false)
  // Why the refusal is rendered rather than logged: the server re-checks under
  // the slot lock and can refuse a press the client believed was available
  // (`slot_running`, `slot_subagents_running`, an approval still pending). Left
  // in the console, that refusal reached the user as the button flicking to
  // disabled and straight back — a control that promises recovery and then says
  // nothing at all. `showRefusedPress` is the shared surface for exactly that.
  useEffect(() => { setContinuing(false) }, [activeSlot])
  // The turn taking over is the success signal; clear the spinner then.
  useEffect(() => {
    if (continuing && slotRunning) setContinuing(false)
  }, [continuing, slotRunning])
  // Backstop: a request that neither starts a turn nor rejects must not strand
  // the button in a disabled state. Mirrors the regenerate safety timeout.
  useEffect(() => {
    if (!continuing) return
    const timeout = setTimeout(() => { setContinuing(false) }, 30_000)
    return () => clearTimeout(timeout)
  }, [continuing])
  const handleContinue = useCallback(() => {
    if (!activeSlot || continuing || !continuable) return
    setContinuing(true)
    // No optimistic transcript mutation: the backend appends the continuation as
    // an `inject` row and the WS `slots` update flips `running`, so the UI
    // converges from the server. Nothing to roll back on failure.
    api.continueSlot(activeSlot).catch((error: unknown) => {
      showRefusedPress('continue', error)
      setContinuing(false)
    })
  }, [activeSlot, continuing, continuable, showRefusedPress])
  // (The newest-error index that gates the Continue button is derived inside
  // the shared row set from the transcript it is handed -- see
  // transcriptRenderers.tsx `lastErrorIndex`.)

  const [flyingQuote, setFlyingQuote] = useState<{
    text: string
    from: DOMRect
  } | null>(null)
  const inputAreaRef = useRef<HTMLDivElement>(null)

  const handleQuote = useCallback((text: string, rect: DOMRect) => {
    const quoted = text.split('\n').map(line => `> ${line}`).join('\n')
    setInput(previous => {
      // Append new quote after existing content (supports multiple quotes)
      if (!previous.trim()) return `${quoted}${COMPOSER_PARAGRAPH_BREAK}`
      return `${previous.trimEnd()}${COMPOSER_PARAGRAPH_BREAK}${quoted}${COMPOSER_PARAGRAPH_BREAK}`
    })
    // Trigger flying animation
    setFlyingQuote({ text, from: rect })
    revealComposer()
  }, [setInput])

  // "Ask" (Select-to-Ask): open the isolated /side conversation seeded with the
  // selection, WITHOUT touching the main chat context (unlike handleQuote, which
  // injects into the main composer). Mirrors the /side slash command's
  // openActivityToTab('side') bridge, then hands the selection to SideChat via a
  // `side-seed` CustomEvent (same event-bridge pattern as openActivityToTab —
  // no new prop-drilling, no backend change). No transit
  // animation: the popup routes the selection straight to the Side Chat panel
  // (matches Codex's "Ask in side chat" behavior).
  const handleAsk = useCallback((text: string) => {
    dispatch(openActivityToTab('side'))
    // The Side Chat panel (and its `side-seed` listener) mounts asynchronously
    // once the panel opens. Poll a few frames for its input as a mount signal,
    // then dispatch the seed. Fall back to dispatching after a cap so the
    // feature still works even if the input never resolves.
    const trySeed = (attempt = 0) => {
      const mounted = document.querySelector(
        '[data-side-chat-input] textarea[data-composer-input]',
      )
      if (mounted || attempt >= 20) {
        window.dispatchEvent(new CustomEvent('side-seed', { detail: { text } }))
      } else {
        requestAnimationFrame(() => trySeed(attempt + 1))
      }
    }
    requestAnimationFrame(() => trySeed())
  }, [dispatch])

  const handleEditResend = useCallback((
    index: number,
    timestamp: string,
    newContent: string,
  ) => {
    if (!activeSlot || slotRunning) return
    const snapshot = [...messages]
    dispatch(truncateAfterIndex(index))
    dispatch(appendMessage({
      role: 'user',
      content: newContent,
      cls: '',
      ts: new Date().toISOString(),
    }))
    setRegenerating(true)
    // Use /rewind (fork-and-swap) — discards the orphan kiro-cli session so
    // truncated forward turns can't resurface on resume. Mirrors kiro-cli's
    // native /rewind slash command, but swaps the session under the same
    // slot identity so the UI stays in place (no new tab, no title change).
    void rewindWithRollback(activeSlot, timestamp, newContent, () => {
      dispatch(replaceMessages(snapshot))
      setRegenerating(false)
    })
  }, [activeSlot, slotRunning, messages, dispatch])

  const allQueuedMessages = useMemo(
    () => messages.filter(message => message.role === 'queued'),
    [messages],
  )
  // Only user-typed queued messages get the interactive (edit/cancel) card
  // stack. System injections are excluded (isNonInteractiveQueued): sub-agent
  // deliveries collapse into one progress line, and synthetic turn-recovery
  // continuations (tool refusal / stalled turn / stalled tool / interrupted /
  // empty response) are machine-facing orchestration — they drain
  // automatically and must never render as an editable/cancellable "user" card
  // (editing or cancelling one corrupts the recovery). They surface as a
  // compact RecoveryCard in the transcript once dequeued instead.
  const queuedMessages = useMemo(
    () => allQueuedMessages.filter(message => !isNonInteractiveQueued(message)),
    [allQueuedMessages],
  )
  // Count sub-agent deliveries directly (not by subtraction): recovery
  // injections are also excluded from queuedMessages, but they are NOT
  // sub-agent results and must not inflate the delivery progress line.
  const systemDeliveryCount = useMemo(
    () => allQueuedMessages.filter(message => isSystemDelivery(message)).length,
    [allQueuedMessages],
  )

  // Mid-turn steer: inject the composer content into the RUNNING turn instead
  // of queueing for the next one. Mirrors send()'s payload prep so pending
  // files ride along — images become `![image](path)` markdown and other
  // files `[attached_file N]` tokens. kiro-cli's `_session/steer` is a
  // text-only channel, so unlike a queued send the image travels as its
  // absolute path for the agent to open with a tool, not as an inline
  // content block. Paste tokens are expanded for the LLM the same way
  // send() does. The POST goes through the composer controller's steerMutation,
  // whose receipt resolves the optimistic bubble — the backend falls back to the
  // queue if steer is unavailable, and echoes
  // the text inline via the 'steer_push' WS event. Composer, pending files,
  // paste blocks, and the per-slot drafts are all cleared HERE (not in
  // ChatInput) so text and attachments clear atomically.
  const steer = useCallback(() => {
    if (!activeSlot) return
    // Nothing to inject into: the composer is busy purely because background
    // sub-agents are still running for this slot (spawn_run is fire-and-forget,
    // so the parent turn already ended). The intent is the same — act on this
    // text now, don't park it — so start a real turn through the normal send
    // path, which carries `ws=1` and so streams, and flag it to skip the
    // server-side hold that keeps a user message behind running sub-agents.
    // Delegating here, BEFORE the composer is read and cleared below, leaves
    // send() owning the draft, attachment and optimistic-bubble bookkeeping.
    // A multi-stage autopilot plan also reads busy-but-not-running. There the
    // server keeps `_in_stage_execution` set for the WHOLE plan, so the flag
    // finds no live session to inject into and the message queues — the right
    // answer between stages, and unconditional across the plan rather than a
    // race with the gaps.
    if (!slotRunning) {
      void send(undefined, undefined, true)
      return
    }
    const raw = inputRef.current.trim()
    const files = pendingFilesRef.current
    if (!raw && !files.length) return
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
      const commandText = pastes.length ? expandPasteTokens(raw, pastes) : raw
      // Fire-and-forget, but recoverable: on failure (409 side turn in
      // flight, 400 question too long, side-open rejected) the question is
      // merged back so it is never silently lost. The restore is bound to
      // the ORIGINATING slot, captured here — the user may switch slots
      // before the rejection lands. On-screen and settled (same dance as
      // the voice-transcript delivery above): merge into the live composer.
      // Otherwise: merge into the origin slot's persisted draft.
      // mergeIntoDraft appends after a paragraph break instead of replacing,
      // so text the user typed in the meantime survives alongside the
      // recovered question (same contract as the hand-off paths).
      const originSlot = activeSlotRef.current
      void interceptSlashCommand(commandText, originSlot, dispatch).then(result => {
        if (!result.intercepted || !result.failed || !originSlot) return
        const onScreen = originSlot === activeSlotRef.current
          && composerSlotRef.current === originSlot
        if (onScreen) {
          setInput(mergeIntoDraft(inputRef.current, commandText))
        } else {
          const merged = mergeIntoDraft(drafts.current[originSlot], commandText)
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
      setInput('')
      setPasteBlocks([])
      return
    }
    const { txt } = prepareSendPayload(raw, files)
    // Folder tokens deliberately stay in their `@rel/` form on steer: the
    // steer transport is TEXT-ONLY (no meta), so a `[attached_dir N] /abs
    // path` marker would have no meta.dirs index to replay against and the
    // whitespace-bounded fallback truncates a path containing spaces — the
    // chip would then open the wrong directory. The raw token is what the
    // agent resolved before serialization existed, and it stays correct
    // under replay. Serialize on steer only if that transport ever carries
    // attachment metadata.
    const activePastes = pasteBlocksRef.current
    const llmTxt = activePastes.length ? expandPasteTokens(txt, activePastes) : txt
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
    dispatch(appendMessage({
      role: 'user',
      content: llmTxt,
      cls: 'msg msg-u',
      ts: new Date().toISOString(),
      meta: { steer: true, optimistic: true, sendId: steerSendId },
    }))
    steerMutation.mutate({ text: llmTxt, sendId: steerSendId, slot: activeSlot })
    // Staged session references are deliberately NOT part of steering: neither
    // carried into the payload nor cleared. `steerMutation`'s onError only logs,
    // so anything cleared here is gone for good — text, attachments and pastes
    // have always been discarded on a failed steer, and adding refs to that set
    // would lose a reference the user cannot recover except by dragging again.
    // Leaving them staged is lossless and predictable: the chip stays in the
    // composer and rides the next real send, which does have a restore path.
    setInput('')
    setPendingFiles([])
    pickedFileTokens.current = {}
    setPasteBlocks([])
    delete drafts.current[activeSlot]
    delete fileDrafts.current[activeSlot]
    delete pasteDrafts.current[activeSlot]
    saveDrafts()
  // eslint-disable-next-line react-hooks/exhaustive-deps -- stable composer refs/setters are intentionally read through the controller port
  }, [activeSlot, slotRunning, send, steerMutation, saveDrafts, dispatch])

  // The queue-card recipe is shared with every other host that draws a
  // QueueStack over this slot queue (#5891) — see useQueuedMessageActions for
  // why cancel/edit stay optimistic and what is deliberately left to item 1.
  //
  // Restore MERGES, via the same helper every other recovery site in this
  // controller uses (a failed create, a failed send). Assigning was this
  // surface's older spelling and it destroyed text: cancelling two cards in a
  // row overwrote the first card's restored draft with the second's, and by then
  // the first card had already been optimistically retired, so that text existed
  // nowhere else. Whatever lands here is persisted into this slot's draft by the
  // composer controller's `[input]` effect, so a recovered draft survives a slot
  // switch.
  const restoreQueuedDraft = useCallback(
    (text: string, files: string[]) => {
      setInput(previous => mergeRecoveredDraft(previous, text))
      // Chips MERGE like the text does: paths join whatever is already staged,
      // deduped, so a re-send serializes each attachment exactly once.
      if (files.length) setPendingFiles(previous => [...new Set([...previous, ...files])])
    },
    [setInput, setPendingFiles],
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

  // One spelling for every plan-chip gesture (single-click, double-click,
  // Send-now). `sourceKeyAtClick` is the row the gesture started on.
  const dispatchPlanFollowUp = useCallback((
    action: string,
    sourceKeyAtClick?: string | null,
  ): boolean => {
    if (!(followUpIsPlan && isPlanAction(action) && effectiveMode === 'orchestrator' && activeSlot)) {
      return false
    }
    // No isPending pre-check: single-flight lives in the hook's per-slot latch,
    // which drops a duplicate Go/Go All but lets Cancel through. A render-scoped
    // isPending check would swallow the stop control while a Go settles.
    planActionMutationRef.current.mutate({
      slot: activeSlot,
      action,
      clickedSourceKey: sourceKeyAtClick,
    })
    return true
  }, [activeSlot, effectiveMode, followUpIsPlan])

  const handleFollowUpSend = useCallback((
    text?: string,
    sourceKeyAtClick?: string | null,
  ) => {
    // Double-click and Send-now share the plan gate with single-click (#6240).
    // First-click row identity refuses a straddled gesture on a replaced footer.
    if (text && dispatchPlanFollowUp(text, sourceKeyAtClick)) return
    return send(text)
  }, [dispatchPlanFollowUp, send])

  const handleFollowUpSelect = useCallback((
    option: string,
    event: ReactMouseEvent,
    sourceKeyAtClick?: string | null,
  ) => {
    // Plan options (Go / Go All / Cancel) dispatch directly — no input fill.
    // Non-protocol labels on a plan-shaped message keep the composer path:
    // the endpoint would 400 them while the append was already skipped.
    if (dispatchPlanFollowUp(option, sourceKeyAtClick)) return
    // One-click: enabled + no shift + not busy + not already in multi-select
    if (tryQuickSend(
      option,
      dashCfg?.quick_send,
      event.shiftKey,
      slotRunning,
      followUpPickedRef.current.size,
      send,
    )) return
    // Regular options: toggle. Click unpicked → append + mark; click
    // picked → try to remove text + unmark (if the user edited the
    // text so it no longer matches, leave text alone — the chip
    // still un-highlights for consistency).
    if (followUpPickedRef.current.has(option)) {
      const pickedSuffix = Array.from(followUpPickedRef.current).join(', ')
      const next = new Set(followUpPickedRef.current)
      next.delete(option)
      const remainingSuffix = Array.from(next).join(', ')
      followUpPickedRef.current = next
      setInput(previous => {
        // Options are appended as one ordered suffix. Remove only
        // from that complete generated structure: searching for a
        // last occurrence still corrupts an earlier ", Go" if the
        // user has already deleted the appended ", Go" by hand.
        if (previous === pickedSuffix) return remainingSuffix
        const delimitedSuffix = `, ${pickedSuffix}`
        if (!previous.endsWith(delimitedSuffix)) return previous
        const draft = previous.slice(0, -delimitedSuffix.length)
        return remainingSuffix ? `${draft}, ${remainingSuffix}` : draft
      })
      setFollowUpPicked(next)
    } else {
      const next = new Set(followUpPickedRef.current)
      next.add(option)
      followUpPickedRef.current = next
      setInput(previous => (
        previous.trim() ? `${previous.trimEnd()}, ${option}` : option
      ))
      setFollowUpPicked(next)
    }
  }, [
    dashCfg?.quick_send,
    dispatchPlanFollowUp,
    send,
    setInput,
    slotRunning,
  ])

  return {
    dashCfg,
    currentSlot,
    effectiveMode,
    activeAgentName,
    currentProjectRef,
    send,
    submitComments,
    approve,
    toApiDecision,
    dismissApproval,
    switchAgent,
    switchModel,
    setProject,
    pendingQuestion,
    pendingFollowup,
    folderSuggestion,
    followupAddToSession,
    followupStartInWorktree,
    dismissFollowup,
    folderSuggestionAccept,
    folderSuggestionDecline,
    followUpOptions,
    followUpIsPlan,
    followUpSourceKey,
    followUpPicked,
    handleFollowUpSend,
    handleFollowUpSelect,
    regenerating,
    refusedPress,
    setRefusedPress,
    showRefusedPress,
    handleRegenerate,
    continuable,
    interrupted,
    continuing,
    handleContinue,
    flyingQuote,
    setFlyingQuote,
    inputAreaRef,
    handleQuote,
    handleAsk,
    handleEditResend,
    allQueuedMessages,
    queuedMessages,
    systemDeliveryCount,
    steer,
    handleCancelQueued,
    handleInterruptQueued,
    handleEditQueued,
    handleReorderQueued,
    queuePendingIds,
  }
}

export type ChatPageActionsController = ReturnType<typeof useChatPageActionsController>
