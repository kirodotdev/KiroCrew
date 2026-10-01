import { useCallback, useEffect, useRef, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'
import type { SetURLSearchParams } from 'react-router-dom'

import type { ChatLaunchOptions } from '../../../app-sdk'
import type { ComposerDraftStore } from '../../../chat-core/composer/draftStore'
import { i18nT } from '../../../i18n/t'
import type { AppDispatch } from '../../../store'
import { createSlot, setPendingInput } from '../../../store/chatSlice'
import { addNotification } from '../../../store/notificationsSlice'
import { mergeIntoDraft, setDraft } from '../../../utils/chatDrafts'
import { consumeChatHandoff, handoffToChat, persistClaimedChatHandoffs, subscribeChatHandoff } from '../../../utils/errorReport'
import { PREFILL_STORAGE_KEY, writePrefill } from '../../../utils/navIntent'
import { safeSetSessionItem } from '../../../utils/safeStorage'
import { revealComposer } from '../composerFocus'
import { uniqueNotificationTs } from './notificationTs'

/**
 * How text reaches the composer from outside a keystroke: the Redux
 * `pendingInput` hand-off (the Projects page, the command palette, a follow-up
 * card), a popout's `?prefill=` fallback URL, an error surface's "Ask the
 * agent" hand-off, an app's launch intent, the auto-send those arm, and a
 * widget action's prefill. Only an explicit request
 * (`?autoSend=1`, a signed token, an app launch) ever sends; everything else
 * seeds the composer for the user to review.
 *
 * The hooks run where the page's inline effects ran; the signed-token intake
 * between them stays in ChatPage with the session switches it makes.
 */

interface PendingInputIntakeOptions {
  pendingInput: string | null
  activeSlot: string | null
  embedded: boolean | undefined
  searchParams: URLSearchParams
  setSearchParams: SetURLSearchParams
  dispatch: AppDispatch
  autoSendRef: MutableRefObject<string | null>
  newSessionRef: MutableRefObject<boolean>
  setAutoSendTick: Dispatch<SetStateAction<number>>
  drafts: MutableRefObject<Record<string, string>>
  saveDraftsDebounced: () => void
  setInput: ComposerDraftStore['set']
  raisePrefillHint: () => void
}

/** The Redux `pendingInput` hand-off and the `?prefill=` fallback URL. */
export function usePendingInputIntake({
  pendingInput,
  activeSlot,
  embedded,
  searchParams,
  setSearchParams,
  dispatch,
  autoSendRef,
  newSessionRef,
  setAutoSendTick,
  drafts,
  saveDraftsDebounced,
  setInput,
  raisePrefillHint,
}: PendingInputIntakeOptions) {
  // Consume pendingInput from Redux (e.g. from "Chat" button on Projects page)
  useEffect(() => {
    if (pendingInput) {
      dispatch(setPendingInput(null))
      const shouldAutoSend = embedded ? false : searchParams.get('autoSend') === '1'
      const wantNew = embedded ? false : searchParams.get('newSession') === '1'
      if (!embedded && (searchParams.get('prefill') || shouldAutoSend)) setSearchParams({}, { replace: true })
      if (shouldAutoSend) {
        autoSendRef.current = pendingInput
        newSessionRef.current = wantNew
        // Bump the tick, because arming the ref alone is not enough when ChatPage is
        // ALREADY mounted. The send effect's deps are `[send, connected,
        // autoSendTick]`: on a cold navigation, mounting and connecting move
        // `connected` and it fires on its own, but a caller already on /chat only
        // changes the search params. `send`'s identity does move with `activeSlot` --
        // yet a seeder that awaits between activating its slot and setting the pending
        // input (the command bar does, since the switch must land first) puts those in
        // two different renders, and by the render that arms the ref none of the deps
        // change. The prompt would then be neither sent nor visible: this branch is the
        // one that does not fall back to the composer.
        //
        // Harmless on the cold path -- the effect runs, finds `connected` still false,
        // and leaves the ref armed for the real connect. Same remedy the no-slot retry
        // below already uses for the same reason.
        setAutoSendTick(t => t + 1)
      } else {
        if (activeSlot) { setDraft(drafts.current, activeSlot, pendingInput); saveDraftsDebounced() }
        setInput(pendingInput)
        raisePrefillHint()
      }
    }
  }, [pendingInput, activeSlot, dispatch, searchParams, setSearchParams, saveDraftsDebounced, embedded, raisePrefillHint, setInput,
    autoSendRef, newSessionRef, setAutoSendTick, drafts])

  // Consume ?prefill= — the no-main-window fallback path for navigation
  // intents forwarded from a popout (see utils/popoutController.ts). The
  // fallback opens `/chat?sid=<slot>&prefill=<prompt>` in a fresh tab, which
  // has no sessionStorage of its own yet: seed PREFILL_STORAGE_KEY from the
  // param so the slot-restore effect prefills the composer when the ?sid slot
  // activates, then strip the param (keep ?sid) so the prompt doesn't leak
  // into history/bookmarks or re-seed on refresh.
  useEffect(() => {
    if (embedded) return
    const sp = new URLSearchParams(window.location.search)
    const prefill = sp.get('prefill')
    if (prefill === null) return
    const sid = sp.get('sid') || sp.get('slot')
    if (sid && prefill) {
      safeSetSessionItem(
        PREFILL_STORAGE_KEY,
        JSON.stringify({ slotKey: sid, prompt: prefill, ts: Date.now() }),
      )
    }
    sp.delete('prefill')
    const qs = sp.toString()
    // PRESERVE the existing state: react-router keeps its stack position in
    // history.state.idx, and replacing it with {} makes idx NaN for every
    // later push — permanently disabling the top-bar Back/Forward arrows and
    // the ⌘/Ctrl+arrow chords (routeHistoryPosition reads that bookkeeping).
    window.history.replaceState(window.history.state, '', window.location.pathname + (qs ? `?${qs}` : ''))
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
}

interface ErrorHandoffIntakeOptions {
  embedded: boolean | undefined
  connected: boolean
  mode: string | undefined
  dispatch: AppDispatch
  drafts: MutableRefObject<Record<string, string>>
  inputRef: MutableRefObject<string>
  activeSlotRef: MutableRefObject<string | null>
  showActionError: (message: string, title?: string) => void
  /**
   * Activates the slot a hand-off just created. ChatPage supplies it because
   * that switch is its classified keep-target `switchSlot` call site
   * (switchSlotCallsiteClassification): a 404 from the fresh slot's detail
   * fetch is a create/fetch race on a slot that exists.
   */
  activateCreatedSlot: (dispatch: AppDispatch, key: string) => Promise<unknown>
  /** The create failure's reason, in plain language (ChatPage's `createFailReason`). */
  createFailReason: (e: unknown) => string
}

/**
 * The error hand-off ("Ask the agent" on an error surface): every prompt is
 * claimed into a component-owned FIFO at once, even while disconnected, and one
 * fresh session at a time is created, seeded through the keyed prefill, and
 * activated. Unfinished work is re-staged for the next mount.
 */
export function useErrorHandoffIntake({
  embedded,
  connected,
  mode,
  dispatch,
  drafts,
  inputRef,
  activeSlotRef,
  showActionError,
  activateCreatedSlot,
  createFailReason,
}: ErrorHandoffIntakeOptions) {
  // Error hand-offs are claimed into a component-owned FIFO immediately, even
  // while disconnected. That removes the sessionStorage TTL from the reconnect
  // wait, while the processing flag guarantees only one create/switch sequence
  // can run at a time.
  const errorHandoffQueueRef = useRef<string[]>([])
  const errorHandoffActiveRef = useRef<string | null>(null)
  const errorHandoffActiveDurableRef = useRef(false)
  const errorHandoffProcessingRef = useRef(false)
  const errorHandoffConnectedRef = useRef(connected)
  const errorHandoffModeRef = useRef(mode)
  const errorHandoffMountedRef = useRef(false)
  // Invalidates async processors when this effect lifecycle ends. Mounted alone
  // is insufficient because StrictMode can clean up and re-run effects on the
  // same component instance, reusing every ref while an old create is pending.
  const errorHandoffLifecycleRef = useRef(0)
  const processErrorHandoffsRef = useRef<() => void>(() => {})
  const persistErrorHandoffClaims = useCallback(() => {
    const active = errorHandoffActiveRef.current
    persistClaimedChatHandoffs([
      ...(active && !errorHandoffActiveDurableRef.current ? [active] : []),
      ...errorHandoffQueueRef.current,
    ])
  }, [])
  errorHandoffConnectedRef.current = connected
  errorHandoffModeRef.current = mode

  const processErrorHandoffs = useCallback(async () => {
    if (
      errorHandoffProcessingRef.current
      || !errorHandoffMountedRef.current
      || !errorHandoffConnectedRef.current
    ) return
    const prompt = errorHandoffQueueRef.current[0]
    if (!prompt) return

    const lifecycle = errorHandoffLifecycleRef.current
    const ownsLifecycle = () => (
      errorHandoffMountedRef.current
      && errorHandoffLifecycleRef.current === lifecycle
    )
    let failureRestageAttempted = false
    const restageFailure = (error: unknown) => {
      failureRestageAttempted = true
      const queued = errorHandoffQueueRef.current
      const restaged = handoffToChat([prompt, ...queued])
      if (restaged) {
        queued.splice(0)
        // Ingress now owns the entire FIFO in one atomic write. Clear the
        // claimed copy only after that write succeeds.
        persistClaimedChatHandoffs([])
      } else {
        // Keep a same-document retry path as well as the unchanged claimed
        // crash copy when sessionStorage rejected the ingress write.
        queued.unshift(prompt)
      }
      // The restaged prompt goes back into the hand-off FIFO, not into the
      // composer, so nothing on the page shows it was recovered: the toast alone
      // would leave a blank chat. Same title and body, in-page as well.
      const restageTitle = i18nT('pages.chatPage.could_not_start_a_new_session')
      const restageBody = i18nT('pages.chatPage.could_not_start_session_message_restored', {
        error: createFailReason(error),
      })
      showActionError(restageBody, restageTitle)
      dispatch(addNotification({
        ts: uniqueNotificationTs(),
        kind: 'agent',
        priority: 'critical',
        title: restageTitle,
        body: restageBody,
      }))
    }
    errorHandoffProcessingRef.current = true
    errorHandoffActiveRef.current = prompt
    errorHandoffActiveDurableRef.current = false
    // Persist the complete local FIFO before removing its head. A reload can
    // now recover both the active diagnostic and every prompt waiting behind it.
    persistClaimedChatHandoffs(errorHandoffQueueRef.current)
    errorHandoffQueueRef.current.shift()
    try {
      let slotKey: string
      try {
        const slot = await dispatch(createSlot({ mode: errorHandoffModeRef.current, activate: false })).unwrap()
        if (!slot?.key) throw new Error('the server returned no session')
        slotKey = slot.key
      } catch (e) {
        // Cleanup may already have handed this FIFO to a newer ChatPage. An old
        // rejection must not append a duplicate batch or clear its replacement's
        // crash snapshot.
        if (!ownsLifecycle()) return
        restageFailure(e)
        return
      }

      // A route remount may have re-staged this prompt while createSlot was in
      // flight. The abandoned request may leave an unused server slot, but it
      // must not write shared recovery state or steal focus from its successor.
      if (!ownsLifecycle()) return
      // Seed before switching: the draft-restore effect runs in the same commit
      // as switchSlot.pending and would otherwise overwrite pendingInput with
      // the new slot's empty draft. The keyed prefill survives that race.
      if (!writePrefill(slotKey, prompt)) {
        // Do not acknowledge durability or activate an empty session when the
        // keyed prompt was rejected. Preserve active + queued work together.
        restageFailure(new Error('browser storage is unavailable'))
        return
      }
      // The keyed target-slot prefill is now the durable owner. A reload no
      // longer needs to replay this active prompt, but queued prompts still do.
      errorHandoffActiveDurableRef.current = true
      persistErrorHandoffClaims()
      try {
        // `keepTargetOnMissing`: this slot was JUST created, so a 404 from its
        // detail fetch is a create/fetch race on a slot that exists -- the
        // reducer keeps it selected (with the seeded composer) atomically
        // instead of unwinding to the previous chat (#6309), and this catch
        // stays a no-op rather than patching state back from the caller.
        await activateCreatedSlot(dispatch, slotKey)
      } catch {
        // switchSlot.pending already activated the fresh slot. Its detail fetch
        // may fail independently; keep the seeded composer usable in that slot.
      }
      // Do not dispatch pendingInput after the detail fetch. The keyed prefill
      // seeded the composer when switchSlot.pending activated the slot; a late
      // second write would overwrite anything the user typed during the fetch.
      //
      // The prefill channel is single-slot and the seeded prompt only becomes
      // durable-in-slot when the input commit's persist effect records it under
      // the fresh slot's draft key. Hold this turn (bounded well inside the
      // prefill's 30s staleness window) until one of those in-component signals
      // confirms the seed landed: yielding a single task is not enough — the
      // next handoff's slot switch can outrun the consuming commit, and its
      // outgoing-slot save would then overwrite this slot's draft with the
      // stale empty composer, silently dropping the diagnostic.
      for (let i = 0; i < 300 && ownsLifecycle(); i++) {
        // Seed committed: the persist effect keyed a draft to the fresh slot,
        // or the composer already holds exactly this prompt (a same-text
        // setInput bails out of re-rendering, so no draft write follows).
        if (Object.prototype.hasOwnProperty.call(drafts.current, slotKey)) break
        if (inputRef.current === prompt) break
        // User deliberately moved on; the keyed prefill stays staged for the
        // fresh slot and expires on its own clock.
        if (activeSlotRef.current !== slotKey) break
        await new Promise(resolve => setTimeout(resolve, 10))
      }
    } finally {
      // A newer lifecycle owns the shared claim key after unmount/remount. The
      // stale processor may clean up only its abandoned local promise state.
      if (!ownsLifecycle()) return
      errorHandoffActiveRef.current = null
      errorHandoffActiveDurableRef.current = false
      errorHandoffProcessingRef.current = false
      if (!failureRestageAttempted) persistErrorHandoffClaims()
      // Yield a task between sessions. React gets a commit in which the current
      // slot consumes its keyed prefill before another handoff can replace the
      // single prefill channel and activate the next fresh slot. A create failure
      // deliberately stops here: the atomically re-staged FIFO waits for a later
      // user handoff/remount instead of entering an immediate retry loop.
      if (
        !failureRestageAttempted
        && errorHandoffConnectedRef.current
        && errorHandoffQueueRef.current.length
      ) {
        setTimeout(() => processErrorHandoffsRef.current(), 0)
      }
    }
  }, [dispatch, persistErrorHandoffClaims, showActionError, drafts, inputRef, activeSlotRef, activateCreatedSlot, createFailReason])
  processErrorHandoffsRef.current = () => { void processErrorHandoffs() }

  // Drain the error hand-off channel ("Ask the agent" on an error surface).
  // sessionStorage rather than Redux because the root ErrorBoundary's button has
  // to work after a hard reload, when the store it would have dispatched to is
  // gone. Claim every prompt synchronously into the local FIFO; processing waits
  // for connection and opens one fresh slot at a time.
  //
  // Two triggers: on mount (arriving from another route, or a full reload) and on
  // the subscription (an error surface inside chat hands off with no route
  // change, so nothing remounts).
  useEffect(() => {
    if (embedded) return
    errorHandoffLifecycleRef.current += 1
    errorHandoffMountedRef.current = true
    const handoffQueue = errorHandoffQueueRef.current
    const drain = () => {
      let prompt: string | null
      while ((prompt = consumeChatHandoff()) !== null) {
        // A repeated click while the same diagnostic is creating/retrying is one
        // retry request, not a request for a duplicate session.
        if (
          prompt !== errorHandoffActiveRef.current
          && !handoffQueue.includes(prompt)
        ) handoffQueue.push(prompt)
      }
      persistErrorHandoffClaims()
      processErrorHandoffsRef.current()
    }
    drain()
    const unsubscribe = subscribeChatHandoff(drain)
    return () => {
      errorHandoffMountedRef.current = false
      errorHandoffLifecycleRef.current += 1
      unsubscribe()
      // Atomically return every nondurable item in original FIFO order. The
      // lifecycle token prevents the abandoned processor from later clearing a
      // newer component's claim or switching its active slot.
      const active = errorHandoffActiveRef.current
      const restaged = [
        ...(active && !errorHandoffActiveDurableRef.current ? [active] : []),
        ...handoffQueue,
      ]
      if (handoffToChat(restaged)) {
        handoffQueue.splice(0)
        errorHandoffActiveRef.current = null
        errorHandoffActiveDurableRef.current = false
        errorHandoffProcessingRef.current = false
        persistClaimedChatHandoffs([])
      }
    }
  }, [embedded, persistErrorHandoffClaims])

  // A disconnected mount still CLAIMS the handoff above. Reconnection only
  // starts its queued network work, so waiting longer than the storage TTL cannot
  // discard the diagnostic.
  useEffect(() => {
    if (!embedded && connected) processErrorHandoffsRef.current()
  }, [embedded, connected, mode])
}

interface AppLaunchIntakeOptions {
  embedded: boolean | undefined
  connected: boolean
  activeSlot: string | null
  slotLoading: boolean
  locationKey: string
  /** An app launch whose target slot the session controller activated. */
  appSlotLaunch: ChatLaunchOptions | null
  setAppSlotLaunch: (intent: ChatLaunchOptions | null) => void
  autoSendRef: MutableRefObject<string | null>
  appLaunchSendRef: MutableRefObject<{ slotKey?: string } | null>
  newSessionRef: MutableRefObject<boolean>
  setAutoSendTick: Dispatch<SetStateAction<number>>
  drafts: MutableRefObject<Record<string, string>>
  saveDraftsDebounced: () => void
  setInput: ComposerDraftStore['set']
  raisePrefillHint: () => void
  setPendingAgent: (v: string, kind?: 'member' | 'template') => void
  setActionError: Dispatch<SetStateAction<{ title?: string; message: string; preserveOnSwitch?: boolean } | null>>
}

/** An app's launch intent (`window.__mc_chat_launch`) on the routed chat. */
export function useAppLaunchIntake({
  embedded,
  connected,
  activeSlot,
  slotLoading,
  locationKey,
  appSlotLaunch,
  setAppSlotLaunch,
  autoSendRef,
  appLaunchSendRef,
  newSessionRef,
  setAutoSendTick,
  drafts,
  saveDraftsDebounced,
  setInput,
  raisePrefillHint,
  setPendingAgent,
  setActionError,
}: AppLaunchIntakeOptions) {
  // Only the routed chat consumes app launch intents. Existing-slot messages
  // arrive here only after the session controller has fulfilled activation.
  useEffect(() => {
    if (embedded || !connected) return
    const launchWindow = window as Window & {
      __mc_chat_launch?: { ts?: number; agent?: string; message?: string; slotKey?: string; autoSend?: boolean }
    }
    const intent = appSlotLaunch ?? launchWindow.__mc_chat_launch
    if (!intent) return
    if (!appSlotLaunch) {
      if (Date.now() - (launchWindow.__mc_chat_launch?.ts ?? 0) > 10_000) {
        delete launchWindow.__mc_chat_launch
        return
      }
      // The controller owns existing-slot activation and fresh-draft creation.
      if (intent.slotKey || intent.autoSend === false) return
      delete launchWindow.__mc_chat_launch
    } else {
      if (slotLoading) return
      setAppSlotLaunch(null)
      // A user switch while activation was pending cancels this launch rather
      // than sending into whichever conversation they chose instead.
      if (activeSlot !== intent.slotKey) {
        if (intent.message) setActionError({ message: i18nT('appChatLaunch.unsent', { error: i18nT('appChatLaunch.cancelled'), message: intent.message }), preserveOnSwitch: true })
        return
      }
    }
    // An existing slot keeps its own agent. Agent selection applies only to a
    // new session, never as an implicit switch of an existing private binding.
    if (intent.agent && !intent.slotKey) setPendingAgent(intent.agent)
    if (!intent.message) return
    if (intent.autoSend === false && activeSlot) {
      newSessionRef.current = false
      const merged = mergeIntoDraft(drafts.current[activeSlot], intent.message)
      setDraft(drafts.current, activeSlot, merged)
      saveDraftsDebounced()
      setInput(merged)
      raisePrefillHint()
    } else {
      autoSendRef.current = intent.message
      appLaunchSendRef.current = { slotKey: intent.slotKey }
      newSessionRef.current = !intent.slotKey
      setAutoSendTick(t => t + 1)
    }
  }, [embedded, connected, activeSlot, slotLoading, locationKey, appSlotLaunch, setAppSlotLaunch, saveDraftsDebounced, raisePrefillHint, setPendingAgent, setInput,
    autoSendRef, appLaunchSendRef, newSessionRef, setAutoSendTick, drafts, setActionError])
}

interface AutoSendIntakeOptions {
  connected: boolean
  send: (optionText?: string, targetSlot?: string, steerNow?: boolean, isolated?: boolean) => Promise<boolean>
  autoSendTick: number
  autoSendRef: MutableRefObject<string | null>
  appLaunchSendRef: MutableRefObject<{ slotKey?: string } | null>
  /** The exact text a widget action pre-filled, so the send is tagged `widget`. */
  widgetPrefillRef: MutableRefObject<string | null>
  setInput: ComposerDraftStore['set']
  raisePrefillHint: () => void
}

/** The armed auto-send, and a widget action's composer prefill. */
export function useAutoSendIntake({
  connected,
  send,
  autoSendTick,
  autoSendRef,
  appLaunchSendRef,
  widgetPrefillRef,
  setInput,
  raisePrefillHint,
}: AutoSendIntakeOptions) {
  // Auto-send when navigated with ?autoSend=1 or ?token= with prompt
  useEffect(() => {
    if (!connected || !autoSendRef.current) return
    const txt = autoSendRef.current
    const appLaunch = appLaunchSendRef.current
    autoSendRef.current = null
    appLaunchSendRef.current = null
    send(txt, appLaunch?.slotKey, undefined, !!appLaunch)
  }, [send, connected, autoSendTick, autoSendRef, appLaunchSendRef])

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
    const handler = (e: Event) => {
      const text = (e as CustomEvent).detail?.text
      if (typeof text !== 'string' || !text) return
      widgetPrefillRef.current = text
      setInput(prev => (prev.trim() ? `${prev.trimEnd()}\n${text}` : text))
      raisePrefillHint()
      revealComposer()
    }
    window.addEventListener('mc-widget-send', handler)
    return () => window.removeEventListener('mc-widget-send', handler)
  }, [raisePrefillHint, setInput, widgetPrefillRef])
}
