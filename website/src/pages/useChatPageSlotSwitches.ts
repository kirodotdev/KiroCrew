import type { SetURLSearchParams } from 'react-router-dom'
import { useCallback, useEffect, useRef, type MutableRefObject, type Dispatch, type SetStateAction } from 'react'
import { useQuery } from '@tanstack/react-query'
import { store, type AppDispatch, type AppStore } from '../store'
import type { ErrorReport } from '../utils/errorReport'
import { forkSlot, switchSlot, createSlot, type FollowupItem, setPendingInput, clearFollowupCard, openActivityToTab } from '../store/chatSlice'
import { api } from '../api/client'
import { safeSetSessionItem } from '../utils/safeStorage'
import { extractPromptFromToken, extractSlackContextFromToken } from '../utils/tokenPrompt'
import { PREFILL_STORAGE_KEY, writePrefill } from '../utils/navIntent'
import { errMessage } from '../utils/thunkError'
import { showTranscriptForkFailure } from '../utils/forkFailure'
import { i18nT } from '../i18n/t'
import { fetchDashboardConfig } from '../api/dashboardConfigQuery'

interface ForkSessionOptions {
  activeSlot: string | null
  dispatch: AppDispatch
  showActionError: (message: string, title?: string, report?: ErrorReport) => void
}

/**
 * Fork from a transcript row: reads whether a tail fork is offered, forks the slot at that
 * row, and switches to the new session.
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
      const result = await dispatch(forkSlot({ slot: activeSlot, atIndex: visibleIndex, messageId, direction })).unwrap()
      if (result.ok) {
        await dispatch(switchSlot(result.key))
      } else {
        showTranscriptForkFailure(showActionError, result.code, result.error, direction)
      }
    } catch (e) {
      // A refusal is a non-2xx, so the thunk REJECTS: its code rides the rejection payload.
      showTranscriptForkFailure(showActionError, (e as { code?: string } | null)?.code, errMessage(e), direction)
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
  setSearchParams: SetURLSearchParams
  composerSlotRef: MutableRefObject<string | null>
  setInput: (next: string | ((prev: string) => string)) => void
  autoSendRef: MutableRefObject<string | null>
  newSessionRef: MutableRefObject<boolean>
  raisePrefillHint: () => void
  setAutoSendTick: Dispatch<SetStateAction<number>>
  tokenConsumingRef: MutableRefObject<boolean>
  getNoCarryCreateIdsRef: () => MutableRefObject<Set<string>>
}

/**
 * A signed chat link (`?t=<token>`, from a Slack thread or channel): consumes the token once, then
 * reconnects to the session it names, or creates one and links it back to its thread, with the
 * link's prompt seeded so the previous slot's draft cannot clobber it.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useSignedLinkAnchor({
  embedded, mode, dispatch, setSearchParams, composerSlotRef, setInput, autoSendRef, newSessionRef, raisePrefillHint, setAutoSendTick, tokenConsumingRef, getNoCarryCreateIdsRef,
}: SignedLinkAnchorOptions) {
  // Consume prompt from token payload (channel challenge-and-redirect flow).
  // The prompt is HMAC-signed in the token — server validates the signature
  // and sets the session cookie before the SPA loads. No auto-send — the user
  // must press Enter to confirm.
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
    // tokenConsumingRef is initialized true when a token is in the URL; every
    // early return below MUST clear it, or the auto-select guard stays engaged
    // for the whole session and blocks slot selection.
    if (embedded) { tokenConsumingRef.current = false; return }
    const token = new URLSearchParams(window.location.search).get('token')
    if (!token) { tokenConsumingRef.current = false; return }
    // Always strip token from URL to prevent leakage via referrer/history.
    // Two layers, both required (PR #11112 review):
    //  1. Raw replaceState scrubs the visible URL immediately — but ONLY the
    //     `token` param, preserving `?sid` so a session deep link
    //     (`/chat?sid=…&token=…`) still names its slot after the strip.
    //     Preserves history.state for the same reason as the prefill strip.
    //  2. setSearchParams scrubs the ROUTER's own searchParams state, which
    //     raw replaceState never updates — the `?sid` sync effects
    //     (useChatPageSessionController) copy router params back into the URL
    //     on slot activation, and would otherwise restore the live credential
    //     into browser history.
    const scrubbed = new URLSearchParams(window.location.search)
    scrubbed.delete('token')
    const scrubbedQs = scrubbed.toString()
    window.history.replaceState(
      window.history.state, '', window.location.pathname + (scrubbedQs ? `?${scrubbedQs}` : ''))
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      next.delete('token')
      return next
    }, { replace: true })
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
          try { await api.slackLink(slotKey, channel, threadTs) } catch { /* non-fatal */ }
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
      autoSendRef.current = prompt
      setAutoSendTick(t => t + 1)
     } finally {
      // Release the auto-select guard once the session is created/linked (or
      // failed), so normal slot selection resumes.
      tokenConsumingRef.current = false
     }
    })()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
}

interface OpenFollowupSessionOptions {
  mode?: string
  dispatch: AppDispatch
}

/**
 * A follow-up card's "start elsewhere" target: creates (or reuses) the session the follow-up
 * runs in and switches to it with the follow-up pre-filled.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useOpenFollowupSession({
  mode, dispatch,
}: OpenFollowupSessionOptions) {
  // The post-create half both spawn routes share: open a session (scoped to
  // `project` when there is one), hand it the prompt, take the user to it, and
  // only then clear the card. Every step fails CLOSED with the caller's message
  // for that step, so a route can name what it already left behind (the
  // worktree route names the reusable worktree) and no failure ever prefills
  // an unrelated conversation.
  const openFollowupSession = useCallback(async (
    item: FollowupItem,
    target: {
      project?: string
      folderId?: string | null
      originSlot: string | null
      originTs: number | undefined
      fail: { notCreated: string; noSession: string; notOpened: string; notFocused: string }
    },
  ) => {
    const { project, folderId, originSlot, originTs, fail } = target
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
    // A fulfilled thunk with no key would skip every guard below (scoping,
    // activation, focus verification) and prefill whatever session is on screen
    // — the exact fail-open the docs promise not to do. Fail closed instead.
    if (!slotKey) throw new Error(fail.noSession)
    // Scoping is NOT done here: `createSlot({ activate: false })` awaits the
    // project assignment before it publishes the slot, and deletes the session if
    // that fails, so the slot is never reachable in an unscoped state. A failure
    // therefore rejects the thunk and is reported by the catch above.
    // createSlot's fulfilled reducer deliberately does NOT activate its result
    // if the user switched sessions while the create was in flight. The
    // prefill below writes to the *active* composer, so without this the
    // prompt would land in whatever unrelated session is on screen and the new
    // session would open empty. The user asked for this session by clicking;
    // take them to it — and if that fails, surface the error and keep the card
    // rather than prefilling the wrong conversation.
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
        throw new Error(fail.notOpened)
      }
    }
    if (store.getState().chat.activeSlot !== slotKey) throw new Error(fail.notFocused)
    dispatch(setPendingInput(item.prompt))
    if (originSlot) dispatch(clearFollowupCard({ slot: originSlot, ts: originTs }))
  }, [dispatch, mode])
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
