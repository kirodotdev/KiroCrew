import { useCallback, useEffect, useRef, type MutableRefObject, type Dispatch, type SetStateAction } from 'react'
import { useQuery } from '@tanstack/react-query'
import { store, type AppDispatch, type AppStore } from '../store'
import type { ErrorReport } from '../utils/errorReport'
import { captureForkOrigin, forkMayOpen, forkOpenSettled, forkOpenStarted, forkSlot, switchSlot, createSlot, type FollowupItem, setPendingInput, dismissFollowupItem, deleteSlot, openActivityToTab, requestSlotFolderOpen, setAgentSwitchNotice } from '../store/chatSlice'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { safeSetSessionItem } from '../utils/safeStorage'
import { extractPromptFromToken, extractSlackContextFromToken } from '../utils/tokenPrompt'
import { takeUrlToken } from '../lib/urlToken'
import { markChallengeAutoSend } from '../utils/channelChallengeSend'
import { PREFILL_STORAGE_KEY, writePrefill } from '../utils/navIntent'
import { errMessage } from '../utils/thunkError'
import { showTranscriptForkFailure } from '../utils/forkFailure'
import { i18nT } from '../i18n/t'
import type { FollowupStartMode } from '../components/FollowUpCard'
import { fetchDashboardConfig } from '../api/dashboardConfigQuery'

interface ForkSessionOptions {
  activeSlot: string | null
  dispatch: AppDispatch
  showActionError: (message: string, title?: string, report?: ErrorReport) => void
}

/**
 * Fork from a transcript row: reads whether a tail fork is offered, forks the slot at that
 * row, and switches to the new session unless the user opened another one meanwhile.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useForkSession({
  activeSlot, dispatch, showActionError,
}: ForkSessionOptions) {
  const { data: forkCfg } = useQuery<{ tail_fork_enabled?: boolean }>({ queryKey: ['dashboardConfig'], queryFn: fetchDashboardConfig, staleTime: 30_000 })
  // While Tail-only Fork is on the plain entry forks the tail, so the menu also offers a fork with
  // history; with it off the plain entry already forks with history. A primitive, so it can sit in
  // the transcript renderer's dependency list.
  const forkDirectionChoice = forkCfg?.tail_fork_enabled === true
  const handleFork = useCallback(async (visibleIndex: number, messageId?: string, explicitDirection?: 'head') => {
    if (!activeSlot) return
    let direction: 'head' | 'tail' = 'head' // hoisted so the catch advises the same way
    let control: string | undefined
    // Pending until the child is active or the fork failed (the `finally`), so
    // the chat page does not open another session if this one leaves the list.
    // Released once: `switchSlot.pending` makes the child active synchronously,
    // so the count drops before the child's transcript fetch, not after it.
    let released = false
    const release = () => { if (!released) { released = true; dispatch(forkOpenSettled()) } }
    dispatch(forkOpenStarted())
    // Read after the start, which numbers this fork (`requestSeq`), and before
    // the config fetch and the fork request: the user can open another
    // session while either runs, and the fork then must not take the view.
    const origin = captureForkOrigin(store.getState())
    try {
      // Fork WITHOUT a prompt: an unsent composer draft must never be
      // auto-submitted into the freshly forked session. The
      // per-slot draft mechanism saves the source slot's composer text on
      // slot-switch, so the user's parked draft stays safe in the original
      // session and the fork opens with an empty composer.
      //
      // forkCfg is undefined until the dashboardConfig query resolves for the
      // first time. Use the cache when warm; otherwise fetch a fresh value
      // directly so direction never silently falls back to an undefined config
      // — which would downgrade an intended tail-fork to a head-fork whenever
      // the query has errored or settled with no data, not just while loading.
      const resolvedCfg = forkCfg ?? await api.dashboardConfig()
      // A direction named by a menu entry wins; a plain fork follows the setting.
      direction = explicitDirection ?? (resolvedCfg?.tail_fork_enabled ? 'tail' : 'head')
      if (resolvedCfg?.tail_fork_enabled === true) {
        control = i18nT(direction === 'head' ? 'pages.chat.assistantMessage.fork_earlier_messages' : 'pages.chat.assistantMessage.fork_later_messages_only')
      }
      const result = await dispatch(forkSlot({ slot: activeSlot, atIndex: visibleIndex, messageId, direction })).unwrap()
      if (result.ok) {
        if (forkMayOpen(store.getState(), origin)) {
          const switching = dispatch(switchSlot({ key: result.key, requestSeq: origin.seq }))
          // The child inherits this session's folder, which can be collapsed when
          // the session was opened from the command bar, a tab or a link: open
          // that path so the child's row shows. switchSlot.pending has already
          // made the child active, so the request follows the activation.
          dispatch(requestSlotFolderOpen({ key: result.key, folder_id: result.folder_id }))
          release()
          await switching
        }
      } else {
        showTranscriptForkFailure(showActionError, result.code, result.error, direction, control)
      }
    } catch (e) {
      // A refusal is a non-2xx, so the thunk REJECTS: its code rides the rejection payload.
      showTranscriptForkFailure(showActionError, (e as { code?: string } | null)?.code, errMessage(e), direction, control)
    } finally {
      release()
    }
  }, [activeSlot, dispatch, forkCfg, showActionError])
  return {
    handleFork, forkDirectionChoice,
  }
}

interface SignedLinkAnchorOptions {
  embedded?: boolean
  mode?: string
  dispatch: AppDispatch
  composerSlotRef: MutableRefObject<string | null>
  setInput: (next: string | ((prev: string) => string)) => void
  autoSendRef: MutableRefObject<string | null>
  newSessionRef: MutableRefObject<boolean>
  raisePrefillHint: () => void
  setAutoSendTick: Dispatch<SetStateAction<number>>
  tokenConsumingRef: MutableRefObject<boolean>
  getNoCarryCreateIdsRef: () => MutableRefObject<Set<string>>
}

/** The slack-link refusal for a channel served by another agent, read from
 *  the raw response body: `{ agent }` (empty when the server could not name
 *  it), or null for any other failure. */
export function agentMismatchRefusal(body: string): { agent: string } | null {
  try {
    const parsed: unknown = JSON.parse(body)
    if (!parsed || typeof parsed !== 'object') return null
    const { code, agent } = parsed as { code?: unknown, agent?: unknown }
    if (code !== 'channel_agent_mismatch') return null
    return { agent: typeof agent === 'string' ? agent : '' }
  } catch {
    return null
  }
}

/**
 * A signed chat link (`?t=<token>`, from a Slack thread or channel): consumes the token once, then
 * reconnects to the session it names, or creates one and links it back to its thread, with the
 * link's prompt seeded so the previous slot's draft cannot clobber it.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useSignedLinkAnchor({
  embedded, mode, dispatch, composerSlotRef, setInput, autoSendRef, newSessionRef, raisePrefillHint, setAutoSendTick, tokenConsumingRef, getNoCarryCreateIdsRef,
}: SignedLinkAnchorOptions) {
  // Consume prompt from token payload (channel challenge-and-redirect flow).
  // The prompt is HMAC-signed in the token — server validates the signature
  // and sets the session cookie before the SPA loads. Once the target session is
  // active the prompt is sent, tagged as already in the channel thread so the
  // mirror does not quote it back there (`utils/channelChallengeSend`).
  //
  // Three cases, driven by signed claims in the token:
  //  1. session_key present → the originating Slack thread is already linked to
  //     a dashboard session; reconnect to THAT session instead of making a new
  //     one (fixes "thread reply spawns a disconnected session").
  //  2. channel + thread_ts present (no session_key) → fresh thread; create a
  //     new session and auto-link it back to that Slack thread so agent
  //     responses flow into the thread.
  //  3. neither → plain new session (e.g. a top-level channel message).
  // In all cases the prompt is seeded via PREFILL_STORAGE_KEY (the channel the
  // slot-restore effect honors) AND set directly once the target slot is
  // active, so the previous slot's draft can't clobber it.
  useEffect(() => {
    // tokenConsumingRef is initialized true when boot captured a token; every
    // early return below MUST clear it, or the auto-select guard stays engaged
    // for the whole session and blocks slot selection.
    if (embedded) { tokenConsumingRef.current = false; return }
    // Boot stripped it already (lib/urlToken.ts).
    const token = takeUrlToken()
    if (!token) { tokenConsumingRef.current = false; return }
    const prompt = extractPromptFromToken(token)
    if (!prompt) { tokenConsumingRef.current = false; return }
    const { sessionKey, channel, threadTs } = extractSlackContextFromToken(token)
    // Backend session keys are history keys (dashboard:chat-…); the frontend
    // slot key is the bare form.
    const targetSlot = sessionKey ? sessionKey.replace(/^dashboard:/, '') : null
    tokenConsumingRef.current = true
    ;(async () => {
     try {
      let slotKey: string | null = null
      let agentMismatch = false
      if (targetSlot) {
        // Case 1: reconnect to the existing linked session.
        try {
          await dispatch(switchSlot(targetSlot)).unwrap()
          slotKey = targetSlot
        } catch {
          // Session vanished (deleted/expired) — fall back to a new one.
        }
      }
      if (!slotKey) {
        // No targetSlot (or reconnect failed): create the session HERE and,
        // for a fresh thread, slack-link it so responses mirror to Slack.
        try {
          const create = dispatch(createSlot({ mode, sessionFolder: false }))
          getNoCarryCreateIdsRef().current.add(create.requestId)
          const slot = await create.unwrap()
          slotKey = slot?.key ?? null
        } catch {
          // ignore — fall back to prefilling the current slot
        }
        // Case 2: auto-link the new session back to the originating thread so
        // responses flow into Slack. Best-effort; failure just leaves it
        // unlinked.
        if (slotKey && channel && threadTs) {
          try {
            await api.slackLink(slotKey, channel, threadTs)
          } catch (err) {
            // The channel is served by a different agent than this new
            // session: the server refused the link. Do not run the prompt
            // here on its own; leave it prefilled and say why, naming the
            // channel's agent when the server did.
            const refusal = err instanceof ApiError ? agentMismatchRefusal(err.body) : null
            if (refusal) {
              agentMismatch = true
              dispatch(setAgentSwitchNotice(refusal.agent
                ? i18nT('pages.chatPage.slack_link_agent_mismatch', { agent: refusal.agent })
                : i18nT('pages.chatPage.slack_link_agent_mismatch_unknown')))
            }
          }
        }
      }
      // We have created/reconnected AND made the target slot active. Critically,
      // clear newSessionRef and pin activeSlot to this slot so send() reuses it
      // on Enter — otherwise send()'s forceNew path would spawn a SECOND,
      // unlinked slot and break Slack mirroring.
      if (slotKey) {
        newSessionRef.current = false
        dispatch(switchSlot(slotKey))
        safeSetSessionItem(
          PREFILL_STORAGE_KEY,
          JSON.stringify({ slotKey, prompt, ts: Date.now() }),
        )
      }
      // While the switch to `slotKey` has not committed yet, the composer still
      // belongs to the old session and a write here would persist the prompt
      // into ITS draft. The slot-change effect restores the prompt from the
      // prefill hand-off written above, so only write when the composer is
      // already on the target (or there is no target to switch to).
      if (!slotKey || composerSlotRef.current === slotKey) setInput(prompt)
      raisePrefillHint()
      if (!agentMismatch) {
        markChallengeAutoSend(autoSendRef, prompt)
        autoSendRef.current = prompt
        setAutoSendTick(t => t + 1)
      }
     } finally {
      // Release the auto-select guard once the session is created/linked (or
      // failed), so normal slot selection resumes.
      tokenConsumingRef.current = false
     }
    })()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
}

/** Characters that already close a sentence, Latin and CJK full-width. A
 *  refusal reason ending in one gets no extra full stop before the card's text. */
const SENTENCE_ENDINGS = ['.', '!', '?', '\u3002', '\uff01', '\uff1f'] as const

interface OpenFollowupSessionOptions {
  mode?: string
  dispatch: AppDispatch
  /** The composer's send path (useComposerSend). */
  send: (optionText?: string, targetSlot?: string, steerNow?: boolean, isolated?: boolean, sendNow?: boolean, onUndelivered?: (kind: 'refused' | 'indeterminate', reason?: string) => void) => Promise<boolean>
  setActionError: Dispatch<SetStateAction<{ title?: string; message: string; preserveOnSwitch?: boolean } | null>>
}

/**
 * A follow-up card's "start elsewhere" target: creates the session the follow-up
 * runs in and switches to it with the follow-up pre-filled, or, when Start
 * sends (one-click send on), sends it the prompt as its first message and
 * leaves the user where they are.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useOpenFollowupSession({
  mode, dispatch, send, setActionError,
}: OpenFollowupSessionOptions) {
  // A start can finish after the person left the Chat route (Settings, say),
  // when this page and its error setter are unmounted. Its notice then goes to
  // the shell-level banner, which every route renders, instead of being lost.
  const mountedRef = useRef(true)
  useEffect(() => { mountedRef.current = true; return () => { mountedRef.current = false } }, [])
  const reportNotice = useCallback((notice: { message: string; preserveOnSwitch?: boolean }) => {
    if (mountedRef.current) setActionError(notice)
    else dispatch(setAgentSwitchNotice({ message: notice.message, error: true }))
  }, [dispatch, setActionError])
  // The post-create half both spawn routes share: open a session (scoped to
  // `project` when there is one), hand it the prompt (pre-filled, or sent when
  // `mode` is 'send'), and remove this item from the card. A pre-fill takes the
  // user to the session, where they press send; a send runs there in the
  // background. Every step before the hand-over fails CLOSED with the caller's
  // message for that step and keeps the card, so a route can name what it
  // already left behind (the worktree route names the reusable worktree), and
  // no failure ever prefills an unrelated conversation.
  const openFollowupSession = useCallback(async (
    item: FollowupItem,
    target: {
      project?: string
      folderId?: string | null
      originSlot: string | null
      originTs: number | undefined
      originIndex: number
      mode: FollowupStartMode
      /** The notice for a run that went through, naming where it runs. */
      started: string
      fail: { notCreated: string; noSession: string; notSent: string; notSentKept: string; unconfirmed: string; notOpened: string; notFocused: string }
    },
  ) => {
    const { project, folderId, originSlot, originTs, originIndex, mode: startMode, started, fail } = target
    let slotKey = ''
    try {
      // `activate: false` on purpose: the slot must be SCOPED to its directory
      // before the user can type into it. Activating first (the default) leaves a
      // window where the composer is live but `chatSlotProject` is still pending,
      // so a turn sent in that window would run in the default directory — agent
      // tools writing to the wrong checkout. It also means a scoping failure can
      // render its error on the still-mounted card instead of unmounting it.
      const slot = await dispatch(createSlot({ mode, project, folder_id: folderId, activate: false })).unwrap()
      slotKey = slot?.key || ''
    } catch {
      throw new Error(fail.notCreated)
    }
    // A fulfilled thunk with no key would skip every guard below and leave the
    // prompt for whatever session is on screen. Fail closed instead.
    if (!slotKey) throw new Error(fail.noSession)
    // Scoping is NOT done here: `createSlot({ activate: false })` awaits the
    // project assignment before it publishes the slot, and deletes the session if
    // that fails, so the slot is never reachable in an unscoped state and the
    // turn below runs in the follow-up's own directory. A failure rejects the
    // thunk and is reported by the catch above.
    if (startMode !== 'send') {
      // The default: the prompt goes into the new session's message box and
      // nothing runs until the person presses send there. Only an explicit
      // 'send' takes the one-click path below, so a caller that passes no mode
      // pre-fills.
      // createSlot's fulfilled reducer deliberately does NOT activate its result
      // if the user switched sessions while the create was in flight. The
      // prefill below writes to the *active* composer, so without this the
      // prompt would land in whatever unrelated session is on screen and the new
      // session would open empty. The user asked for this session by clicking;
      // take them to it, and if that fails, surface the error and keep the card
      // rather than prefilling the wrong conversation.
      // Read the store directly, NOT activeSlotRef: the ref is refreshed by a
      // render, and `unwrap()` resolves as soon as the reducer ran, so a stale
      // ref would report a failure (and skip the prefill) on a switch that in
      // fact succeeded. store.getState() sees the committed value immediately.
      // Hand the prompt over through PREFILL_STORAGE_KEY *before* the switch, the
      // same channel the ?sid / popout paths use. `setPendingInput` alone loses
      // the race: its consuming effect is declared BEFORE the per-slot
      // draft-restore effect, so when the switch and the prefill land in one
      // React commit the restore runs last and overwrites the composer with the
      // incoming slot's (empty) draft, and the prompt vanishes. Seeding the
      // prefill makes the restore itself apply the prompt, so there is nothing
      // left to race.
      writePrefill(slotKey, item.prompt)
      if (store.getState().chat.activeSlot !== slotKey) {
        try {
          await dispatch(switchSlot(slotKey)).unwrap()
        } catch {
          throw new Error(fail.notOpened)
        }
      }
      if (store.getState().chat.activeSlot !== slotKey) throw new Error(fail.notFocused)
      dispatch(setPendingInput(item.prompt))
      // `originTs` pins the card the click came from, so a newer card that landed
      // while the create was in flight is left alone; `item` lets the reducer find
      // this suggestion even if a Skip shifted its index meanwhile.
      if (originSlot) dispatch(dismissFollowupItem({ slot: originSlot, index: originIndex, ts: originTs, item }))
      return
    }
    //
    // One-click send, addressed to the new slot by key and never through the
    // composer on screen, which belongs to the session the user stays on. The
    // card is still up, so a refused send reports on the card item it came from.
    // `isolated` sends the prompt verbatim and leaves every composer alone (the
    // origin's draft, files and quote are not this turn's), the same mode an app
    // launch uses. The card owns the failure report, so the send posts no page
    // notice of its own.
    let undelivered: 'refused' | 'indeterminate' | undefined
    let refusalReason: string | undefined
    const delivered = await send(item.prompt, slotKey, undefined, true, false, (kind, reason) => { undelivered = kind; refusalReason = reason })
    // The server's own reason (a Shared Crew limit, a policy refusal) goes
    // before the card's recovery text, so the person learns why it was refused
    // and not only that it was.
    // `send_failed_with_error` has no closing stop, so one is added to keep the
    // reason and the recovery text as two sentences.
    const withReason = (text: string) => {
      if (!refusalReason) return text
      const stop = SENTENCE_ENDINGS.some(c => refusalReason!.endsWith(c)) ? '' : '.'
      return `${refusalReason}${stop} ${text}`
    }
    // A lost answer is not a refusal: the turn may already be running, so the
    // start goes on as delivered (no retry that could run the prompt twice) and
    // says to check the session instead.
    const unconfirmed = !delivered && undelivered === 'indeterminate'
    if (!delivered && !unconfirmed) {
      // Nothing reached the server. Discard the empty session so a retry starts
      // clean: on the worktree route a session left scoped to the worktree makes
      // the create endpoint refuse every retry as in use.
      // Only while it is still untouched: the slot is published, so another
      // window may have opened it and started a turn of its own meanwhile, and
      // closing it then would end that turn. The row read here skips the request
      // when this tab already sees that; the discard endpoint re-checks on the
      // live slot before and at the close, so a turn this tab has not seen yet
      // keeps the session too. A refused discard is quiet (no sidebar "could not
      // close" notice), because the kept-session notice below reports it with
      // what to do next.
      const row = store.getState().dashboard.slots.find(s => s.key === slotKey)
      const untouched = !row || (!row.running && !row.messages)
      let closed = false
      if (untouched) {
        // Only a discard that closed the session itself offers the retry. A
        // 404 means another close popped the key first, and that close can
        // still put the session back if its save fails; a refusal may also
        // come from a close this tab cannot see the end of. Both count as kept,
        // so a retry never meets a session that comes back holding the
        // worktree. The one refusal that follows a finished close (the History
        // row's delete was refused) is reported as kept too: no retry is the
        // safe side of that rare case.
        const discard = await dispatch(deleteSlot({ key: slotKey, discard: true }))
        closed = deleteSlot.fulfilled.match(discard) && !discard.meta.alreadyGone
      }
      if (closed) throw new Error(withReason(fail.notSent))
      // The session is still there: someone else is using it, or it would not
      // close. On the worktree route it holds the worktree, so a retry would be
      // refused as in use. The item leaves the card (no retry to offer) and a
      // page notice points at the session in the sidebar instead
      // (rfc-followup-start-sends.md, failure table rows 2 and 3).
      if (originSlot) dispatch(dismissFollowupItem({ slot: originSlot, index: originIndex, ts: originTs, item }))
      reportNotice({ message: withReason(fail.notSentKept), preserveOnSwitch: true })
      return
    }
    // The prompt is running from here on, so this item leaves the card now: left
    // up, its Start button would run the same prompt a second time in yet another
    // session. Its siblings stay. `originTs` pins the card the click came from,
    // so a newer card that landed while the start was in flight is left alone;
    // `item` lets the reducer find this suggestion even if a Skip shifted its
    // index meanwhile.
    if (originSlot) dispatch(dismissFollowupItem({ slot: originSlot, index: originIndex, ts: originTs, item }))
    // The run stays in the background: the user keeps the session they are on,
    // and the new one shows in the sidebar, running its first turn. The shell
    // notice names it and offers to open it, from whatever page the user is on
    // by now. A lost answer gets the same offer as an error, because the item
    // has left the card and opening that session is how to check on it.
    dispatch(setAgentSwitchNotice(unconfirmed
      ? { message: fail.unconfirmed, error: true, openSlot: slotKey }
      : { message: started, openSlot: slotKey }))
  }, [dispatch, mode, send, reportNotice])
  return {
    openFollowupSession,
  }
}

interface OpenSideChatForPaneOptions {
  activeSlot: string | null
  boundStore: AppStore
  connected: boolean
  dispatch: AppDispatch
  showActionError: (message: string, title?: string, report?: ErrorReport) => void
}

/**
 * Ask from a split-view pane: re-binds the activity panel to that pane's slot, then opens its
 * Side Chat (withheld while offline).
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useOpenSideChatForPane({
  activeSlot, boundStore, connected, dispatch, showActionError,
}: OpenSideChatForPaneOptions) {
  // Split view's panes ask about THEIR slot, but the activity panel — and the
  // Side Chat inside it — is bound to the active slot. Re-bind it first (the
  // same switchSlot the grid's collapse path uses; split mode itself is not
  // left), then open the tab. The seed names the slot, so it waits for the
  // re-bound panel's composer rather than landing on the old slot's.
  //
  // Offline, the re-bind is withheld like every other switchSlot in this file
  // (the tab strip, the sidebar row, the ?sid deep link): a rejected switch
  // clears the pane's active messages and the transcript the reader just
  // selected from disappears until reconnect. The grid is handed no
  // `openSideChat` at all while disconnected, so the panes' toolbars offer
  // Copy / Quote only (capability by omission, never an Ask into the void);
  // the guard here covers the frame between the drop and the re-render, and
  // rather than open a Side Chat bound to some OTHER slot it does nothing.
  // Read at click time, not captured: the callback is memoized and the
  // gateway can drop between renders (the tab strip's own gate, now inside
  // useChatPageSessionController, keeps its ref the same way).
  const connectedRef = useRef(connected)
  connectedRef.current = connected
  const openSideChatForPane = useCallback((slot: string): boolean | Promise<boolean> => {
    if (slot === activeSlot) {
      dispatch(openActivityToTab('side'))
      return true
    }
    // `false` tells the selection seam the Ask did NOT happen, so it does
    // not seed a quote into a Side Chat that never opened.
    if (!connectedRef.current) return false
    // The re-bind is a request the server can reject (the pane's session was
    // deleted under it); `switchSlot.rejected` then falls back to the slot the
    // page was on. Report the verdict only once it is known: the seam seeds
    // on `true`, and a rejection is surfaced through the page's ErrorNotice
    // instead of leaving a silent, invisible seed behind.
    return dispatch(switchSlot(slot)).unwrap().then(
      () => {
        // A later switch (the user clicked another pane, or Asked from it)
        // may have landed while this one was in flight; the panel is bound to
        // whatever is active NOW, so opening the Side tab here would show the
        // other pane's Side Chat with this quote hidden in this slot's draft.
        // Report the Ask as not happened instead of seeding a stale slot.
        if (boundStore.getState().chat.activeSlot !== slot) return false
        dispatch(openActivityToTab('side'))
        return true
      },
      (e: unknown) => {
        showActionError(i18nT('pages.chatPage.side_chat_pane_gone', { error: errMessage(e) || i18nT('pages.chatPage.unknown_error') }))
        return false
      },
    )
  }, [activeSlot, boundStore, dispatch, showActionError])
  return {
    openSideChatForPane,
  }
}
