import { useEffect, useMemo, useRef, type MutableRefObject } from 'react'
import { type QueryClient, useQueries } from '@tanstack/react-query'

import { disposeTerminalSession, type useDeleteTerminalSession } from '../../../components/CliPanel'
import { normalizeUrl, setSessionPreviewPending } from '../../../components/WebPreviewPanel'
import { addTab as addDockTerminal, removeTab as removeDockTerminal, hasTab as hasDockTerminal, reuseCurrentTab as reuseDockTerminal } from '../../../hooks/useBottomTerminal'
import type { usePanelTabs } from '../../../hooks/usePanelTabs'
import { i18nT } from '../../../i18n/t'
import type { AppDispatch, RootState } from '../../../store'
import { noteRedactionHostsGen, openActivityPanel, setPendingInput } from '../../../store/chatSlice'
import type { ChatMessage, ChatSlot } from '../../../types'
import { mergeIntoDraft } from '../../../utils/chatDrafts'
import { copyToClipboard } from '../../../utils/clipboard'
import { detectPreviewUrl, previewFeedDecision } from '../../../utils/detectPreviewUrl'
import { runInTerminalText, RUN_IN_TERMINAL_READY_DEADLINE_MS, RUN_IN_TERMINAL_OPENING_GRACE_MS } from '../../../utils/fenceShell'
import { fetchFileRead, fileReadQueryKey, FILE_READ_STALE_MS, isPartialRead } from '../../../utils/fileReadQuery'
import { safeSetItem } from '../../../utils/safeStorage'
import { isPopoutOpen as isTerminalPopoutOpen, focusPopout as focusTerminalPopout } from '../../../utils/terminalPopout'
import { onTerminalReady, sendToTerminalSession, sendRawToTerminalSession, getTerminalShell, getTerminalFenceShells } from '../../../utils/terminalRegistry'
import { errMessage } from '../../../utils/thunkError'

type PanelTabs = ReturnType<typeof usePanelTabs>

interface ChatEventBridgesOptions {
  activeSlot: string | null
  activeSlotRef: MutableRefObject<string | null>
  messages: ChatMessage[]
  slots: ChatSlot[]
  dispatch: AppDispatch
  queryClient: QueryClient
  showActionError: (message: string, title?: string) => void
  /** The live panel-tab controller, read at event time. */
  tabsCtlRef: MutableRefObject<PanelTabs>
  /** The active slot's project directory; dock terminals start there. */
  currentProjectRef: MutableRefObject<string | undefined>
  /** Kills a Run-in-terminal tab's backend PTY when the dispatch rolls back. */
  deleteTerminalSessionRef: MutableRefObject<ReturnType<typeof useDeleteTerminalSession>>
  /** dashboard.terminal.reuse_current, and whether its read is still pending or failed (ChatPage mirrors the config query). */
  terminalReuseRef: MutableRefObject<boolean>
  terminalCfgUnsettledRef: MutableRefObject<boolean>
  terminalCfgReadFailedRef: MutableRefObject<boolean>
  inputRef: MutableRefObject<string>
}

/**
 * The chat page's bridges from outside events into its own surfaces: the Web
 * Preview feed from the transcript, the Electron built-in browser's session
 * reachability and agent-opened signal, the dock terminal's run / prefill
 * requests from code blocks and redaction cards, and the composer prefill and
 * transcript reload a redaction card asks for. Each listener registers once
 * (its deps are stable) and reads live state through refs.
 *
 * Event names, detail shapes, `reqId` echoes and result ordering are the
 * contract the emitters depend on and are unchanged from the page.
 */
export function useChatEventBridges({
  activeSlot,
  activeSlotRef,
  messages,
  slots,
  dispatch,
  queryClient,
  showActionError,
  tabsCtlRef,
  currentProjectRef,
  deleteTerminalSessionRef,
  terminalReuseRef,
  terminalCfgUnsettledRef,
  terminalCfgReadFailedRef,
  inputRef,
}: ChatEventBridgesOptions) {
  // Feed the Web Preview tab from chat, by signal type (previewFeedDecision).
  // Neither path ever navigates the iframe: both hand the URL to the panel as a
  // "Load preview" card (setSessionPreviewPending) — the GET fires only on the
  // user's explicit Load click, so agent output can never drive the scripted
  // iframe to an arbitrary host without consent.
  //   • marker (`kirocrew:preview`, explicit agent intent) → also OPEN the tab,
  //     once per distinct URL. The applied URL is PERSISTED per slot so a route
  //     remount doesn't reopen a card the user dismissed; an in-memory ref
  //     backstops a failed localStorage write.
  //   • heuristic (a localhost URL merely mentioned in prose) → offer the card
  //     WITHOUT opening the tab, and only when no target is set yet.
  // Reuses the page's tabsCtlRef so the effect stays mount-stable as the strip churns.
  const appliedPreviewMemRef = useRef<Record<string, string>>({})
  useEffect(() => {
    const slot = activeSlot
    if (!slot) return
    let existing = ''
    try {
      existing = localStorage.getItem(`mc-webpreview-url:${slot}`)
        || localStorage.getItem(`mc-webpreview-pending:${slot}`) || ''
    } catch { /* ignore */ }
    const feed = previewFeedDecision(detectPreviewUrl(messages), !!existing)
    if (!feed) return
    const norm = normalizeUrl(feed.url)
    if (!norm) return
    if (feed.open) {
      // Marker → surface the Load-preview card + open the tab, deduped via a
      // PERSISTED applied key (survives remounts) plus an in-memory ref
      // (survives a failed localStorage write) so it never re-opens.
      let applied = ''
      try { applied = localStorage.getItem(`mc-webpreview-applied:${slot}`) || '' } catch { /* ignore */ }
      if (applied === norm || appliedPreviewMemRef.current[slot] === norm) return
      appliedPreviewMemRef.current[slot] = norm
      safeSetItem(`mc-webpreview-applied:${slot}`, norm)
      // Loopback-only (enforced inside setSessionPreviewPending): a rejected
      // (non-loopback) marker feeds nothing — and must not open the tab either.
      if (!setSessionPreviewPending(slot, norm)) return
      dispatch(openActivityPanel())
      tabsCtlRef.current.openView('browser')
    } else {
      setSessionPreviewPending(slot, norm)      // heuristic offer: card only, no open, no load
    }
  }, [messages, activeSlot, dispatch, tabsCtlRef])
  // Reachability: declare open chat slots to the Electron main process so the
  // agent command channel polls for them (see listPanelIds) even before the Browser
  // tab is ever opened — this is what makes the built-in browser the default for a
  // fresh chat. It is NOT a grant: authorization to drive the built-in browser is
  // Browser Mode (the Settings toggle), and the main-process gate is just the view
  // precondition. There is no separate per-session consent registration — the
  // command channel can only deliver an op for a session key it polls for, and it
  // must poll before any URL is known, so gating reachability on a per-session
  // grant would make the whole native path unreachable for a fresh chat.
  //
  // EVERY open chat is declared, not just the active one.
  //
  // The command channel can only deliver an op for a session key it polls for,
  // and it must poll BEFORE any URL is known. Declaring only `activeSlot` made
  // that a moving target, and both consequences were observed live in a diagnostic
  // run:
  //   * a chat created and messaged within seconds RACED the registration — the
  //     navigate reached the gateway first, which answered `no-native-panel` (503)
  //     because no poller held that key yet, so the proxy fell back to the
  //     Playwright mirror for the whole turn (observed: slot created at T+0, the
  //     navigate at T+15s, the key first reported 9 minutes later);
  //   * a BACKGROUND chat was never reachable at all, even when it was the session
  //     the agent was acting for.
  //
  // Declaring a key is NOT authorization — it grants nothing, and every op still
  // runs the same gate — so there is no reason to report one key instead of all of
  // them. Tracking is diffed rather than torn down per change: re-registering the
  // same keys on every slot-list edit would churn IPC for no reason, and dropping
  // them mid-turn is exactly the race above.
  const trackedSlotsRef = useRef<Set<string>>(new Set())
  const trackableSlotKeys = useMemo(
    () => slots.map(s => s.key).filter((k): k is string => !!k),
    [slots],
  )
  useEffect(() => {
    const api = (window as unknown as {
      browserAPI?: { trackSession?: (id: string, tracked: boolean) => Promise<unknown> }
    }).browserAPI
    if (!api?.trackSession) return      // plain browser (no bridge)
    const want = new Set(trackableSlotKeys)
    const tracked = trackedSlotsRef.current
    for (const key of want) {
      if (tracked.has(key)) continue
      tracked.add(key)
      void api.trackSession(key, true)
    }
    for (const key of [...tracked]) {
      if (want.has(key)) continue
      tracked.delete(key)
      void api.trackSession(key, false)
    }
  }, [trackableSlotKeys])
  // Native counterpart of the mirror auto-open above. When the agent opens a page
  // in the BUILT-IN browser, the WebContentsView is created in the Electron main
  // process but the dashboard owns layout — until the Browser panel mounts and
  // reports its rect, the page is composited nowhere and the user sees nothing.
  // So surface the panel on the main process's `browser:agent-opened` signal.
  //
  // Same active-slot guard as the mirror path: a background session's page must
  // not open another session's panel.
  useEffect(() => {
    const api = window.browserAPI
    if (!api?.onAgentOpened) return      // plain browser (no preload bridge)
    return api.onAgentOpened(({ panelId }) => {
      if (!panelId || panelId !== activeSlotRef.current) return
      dispatch(openActivityPanel())
      tabsCtlRef.current.openView('browser')
    })
  }, [dispatch, activeSlotRef, tabsCtlRef])
  // "Run in terminal" (from chat code blocks): open a terminal tab in the
  // app-wide dock panel and run the command in it, starting in the chat's
  // working dir. The dock panel persists across routes (unlike chat-scoped
  // terminal tabs) so the running shell survives navigation.
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail || {}
      const code: string = detail.code
      const reqId: string = detail.reqId
      const lang: string | undefined = typeof detail.lang === 'string' ? detail.lang : undefined
      if (typeof code !== 'string' || !code) return
      // Opt-in reuse focuses the terminal tab the user selected and copies the
      // command for a manual paste. Sending raw bytes into a live terminal is
      // unsafe: a partially typed shell command or a foreground program owns
      // stdin, so appending `text + "\\n"` could merge and execute unrelated
      // input. The dashboard has no shell-prompt protocol that can prove an
      // empty, idle prompt, so manual paste is the safe boundary. With no
      // existing terminal to focus, fall through and mint a fresh tab, where
      // this dispatch owns the entire input stream and can safely send.
      if (terminalReuseRef.current || terminalCfgUnsettledRef.current) {
        // Reuse-on ALWAYS copies — never runs. The confirm dialog it shows
        // promises a copy for manual paste (RunInTerminalConfirm willCopy), and
        // the action must match that promise unconditionally: a fall-through to
        // minting a fresh tab and sending the bytes would EXECUTE a command the
        // dialog said would only be copied. The same copy-never-run path also
        // covers the UNSETTLED initial-read window: until the config query
        // settles we cannot know whether reuse was saved on, and copying on an
        // unknown is the non-destructive choice (a reuse-off user merely pastes
        // it themselves), whereas executing on an unknown is the harm removed.
        //
        // Focusing an existing tab is a best-effort nicety, so try to reuse one
        // and, if it lives in a popped-out window, raise that window — but the
        // copy proceeds whether or not a reusable tab exists. `reuseCurrentTab`
        // returns null when there is no settled tab to focus; that is not a
        // reason to run, only a reason to skip the focus step.
        const reusedId = reuseDockTerminal()
        // Raise the popped-out window whenever a tab was reused: the main-window
        // BroadcastChannel map (isTerminalPopoutOpen) is not synchronously
        // correct across a main-window reload — in the beacon-only window it
        // reads false though the popout is live — so gating the focus on it
        // leaves the reused window behind. focusTerminalPopout is a no-op when
        // no popout exists, so an unconditional call on a real reuse is safe.
        if (reusedId) focusTerminalPopout()
        // With a focused tab, fence-transform for that shell; with none, copy
        // the command verbatim (no target shell to transform for).
        const text = reusedId
          ? runInTerminalText(code, lang, getTerminalShell(reusedId), getTerminalFenceShells(reusedId))
          : code
        void copyToClipboard(text).then(copied => {
          // A refused copy is a user-facing failure, not a transient icon:
          // route it through ErrorNotice (errors-use-error-notice) so the
          // command the user asked to run is not silently lost.
          if (!copied) {
            showActionError(i18nT('pages.chatPage.run_in_terminal_copy_failed_error'))
          }
          window.dispatchEvent(new CustomEvent('mc:run-in-terminal-result', {
            detail: { reqId, ok: copied, copied },
          }))
        })
        return
      }
      // When reuse is off — whether the user set it off, or its config query is
      // still loading so the saved value is not yet known — the command opens a
      // fresh terminal. That fresh tab IS the shipped default, so no notice
      // fires for the off/pending case: a reuse-downgrade notice there would
      // assert a reuse preference the off-majority never set (a pending query is
      // indistinguishable from a genuinely-off setting).
      //
      // A config-read FAILURE is different, and is the errors-use-error-notice
      // case: the read errored, so a saved reuse-on is being silently ignored
      // with nothing on screen. Surface that read failure through ErrorNotice
      // (the query's isError reaching a user-facing notice), then still run in a
      // fresh tab so the command the user asked for is not dropped.
      if (terminalCfgReadFailedRef.current) {
        showActionError(i18nT('pages.chatPage.run_in_terminal_config_read_failed_error'))
      }
      const sessionId = addDockTerminal(currentProjectRef.current ?? undefined)
      let settled = false
      const emit = (ok: boolean) => {
        if (settled) return
        settled = true
        window.dispatchEvent(new CustomEvent('mc:run-in-terminal-result', { detail: { reqId, ok } }))
      }
      if (!sessionId) {
        // F3: no fresh tab could be minted (the terminal cap is full). The
        // command is neither copied nor run, so say so through ErrorNotice
        // rather than leaving only the button's error glyph
        // (errors-use-error-notice).
        showActionError(i18nT('pages.chatPage.run_in_terminal_no_tab_error'))
        emit(false); return
      }
      // The shell is known only once `ready` has arrived, which is exactly when
      // this fires — so read it here, not at dispatch time.
      const unsub = onTerminalReady(sessionId, () => {
        const text = runInTerminalText(
          code, lang, getTerminalShell(sessionId), getTerminalFenceShells(sessionId),
        )
        emit(sendToTerminalSession(sessionId, text))
      })
      // Give the PTY time to connect. A missing `ready` frame is not enough to
      // prove the dispatch died because a shell profile can replace the
      // readiness hook while the child process stays live. At the deadline,
      // report failure for the button hint, then ask the existing terminal
      // sessions route whether this dispatch's shell is still running.
      // `settled` distinguishes the normal ready path: once ready has fired,
      // the result is already emitted and the deadline does nothing.
      setTimeout(() => {
        if (settled) return
        unsub()
        emit(false)

        // Only probe while this dispatch still owns the tab it minted. Closing
        // the tab or popping the panel out transfers teardown ownership.
        if (!hasDockTerminal(sessionId) || isTerminalPopoutOpen()) return

        void (async () => {
          // One look at the sessions route. `reuseMs` is the cache window: the
          // first probe shares a request with any concurrent deadline, the
          // confirm probe must see the present.
          const probe = async (reuseMs: number) => {
            const payload: unknown = await queryClient.fetchQuery({
              queryKey: ['terminal-sessions'],
              queryFn: async () => {
                const response = await fetch('/api/terminal/sessions')
                if (!response.ok) {
                  throw new Error(`Failed to list terminal sessions (${response.status})`)
                }
                return response.json()
              },
              staleTime: reuseMs,
            })
            if (
              !payload
              || typeof payload !== 'object'
              || !('sessions' in payload)
              || !Array.isArray(payload.sessions)
            ) {
              throw new Error('Invalid terminal sessions response')
            }
            const found: Record<string, unknown> | undefined = payload.sessions.find(
              (entry: unknown): entry is Record<string, unknown> => (
                !!entry
                && typeof entry === 'object'
                && 'session_id' in entry
                && entry.session_id === sessionId
              ),
            )
            if (found && typeof found.alive !== 'boolean') {
              throw new Error('Invalid terminal session liveness response')
            }
            return found
          }

          let session: Record<string, unknown> | undefined
          try {
            // Concurrent deadlines are what this reuse window dedupes, so it is
            // far shorter than the deadline itself: a session young enough to be
            // missing from a reused snapshot cannot have reached its own
            // deadline yet, so no probe can read a snapshot older than itself.
            session = await probe(1_000)
            if (!session) {
              // Absent is not gone. A shell still opening holds a placeholder
              // the sessions route skips, so it reads exactly like a session
              // that never existed -- and rolling that back would remove the tab
              // from under a shell about to come up. Confirm once, uncached,
              // after a bounded grace.
              await new Promise(resolve => setTimeout(resolve, RUN_IN_TERMINAL_OPENING_GRACE_MS))
              if (!hasDockTerminal(sessionId) || isTerminalPopoutOpen()) return
              session = await probe(0)
            }
          } catch (error) {
            // Keep on probe failure: removing a possibly-live shell and its
            // scrollback is irreversible. The tab is user-closable, and the
            // backend orphan reaper backstops the PTY. The kept tab is
            // otherwise unexplained, so say so through the required surface --
            // and keep the probe's own transport error out of that copy, since
            // the user asked to run a command, not to list terminal sessions.
            // The console keeps it for whoever debugs the probe.
            // eslint-disable-next-line no-console -- a failed liveness probe is invisible in dev otherwise
            console.warn('run-in-terminal: liveness probe failed:', errMessage(error))
            showActionError(
              i18nT('pages.chatPage.run_in_terminal_liveness_probe_failed_error'),
            )
            return
          }

          // The user may close the tab or pop the panel out while the probe is
          // in flight. In either case this dispatch no longer owns it.
          if (!hasDockTerminal(sessionId) || isTerminalPopoutOpen()) return
          if (session?.alive === true) {
            // A profile that replaces the readiness hook (#7657) lands here on
            // EVERY click, so this is the routine outcome rather than an edge:
            // the terminal opens, the command never runs, and a 2s button flash
            // is too small to carry that. The shell is confirmed live, so the
            // tab is worth keeping and the silence is worth breaking.
            showActionError(i18nT('pages.chatPage.run_in_terminal_shell_alive_error'))
            return
          }

          // Same teardown, same order, as the tab-close paths: end the backend
          // PTY, drop the local WS + cached xterm, then remove the store entry.
          // A session the probe did not list is already gone from the backend
          // registry, so skip the DELETE -- it would 404 and surface a spurious
          // close failure for a session that needs no closing.
          if (session) deleteTerminalSessionRef.current.mutate(sessionId)
          disposeTerminalSession(sessionId)
          removeDockTerminal(sessionId)
          // Closing a tab the user watched open is the ROUTINE outcome here, so
          // it cannot be the quiet one: say what happened to the command.
          showActionError(i18nT('pages.chatPage.run_in_terminal_dispatch_rolled_back_error'))
        })()
      }, RUN_IN_TERMINAL_READY_DEADLINE_MS)
    }
    window.addEventListener('mc:run-in-terminal', handler)
    return () => window.removeEventListener('mc:run-in-terminal', handler)
    // All are stable for the provider's / component's lifetime (a context
    // client, a []-dep useCallback and refs), so the listener still installs once.
  }, [queryClient, showActionError, currentProjectRef, deleteTerminalSessionRef, terminalReuseRef, terminalCfgUnsettledRef, terminalCfgReadFailedRef])
  // A redaction card's "Open in Terminal": open a dock terminal and TYPE the
  // command without submitting it, so the reader reviews it before it runs.
  // The typing tier refuses a newline, which is what keeps this from running.
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail || {}
      const command: unknown = detail.command
      const reqId: unknown = detail.reqId
      const emit = (ok: boolean) =>
        window.dispatchEvent(new CustomEvent('mc:prefill-terminal-result', { detail: { reqId, ok } }))
      if (typeof command !== 'string' || !command || /[\r\n]/.test(command)) { emit(false); return }
      const sessionId = addDockTerminal(currentProjectRef.current ?? undefined)
      if (!sessionId) { emit(false); return }
      let settled = false
      const unsub = onTerminalReady(sessionId, () => {
        settled = true
        emit(sendRawToTerminalSession(sessionId, command))
      })
      setTimeout(() => { if (!settled) { unsub(); emit(false) } }, RUN_IN_TERMINAL_READY_DEADLINE_MS)
    }
    window.addEventListener('mc:prefill-terminal', handler)
    return () => window.removeEventListener('mc:prefill-terminal', handler)
  }, [currentProjectRef])
  // A redaction card's "Pre-fill request": the text lands in the composer for
  // the reader to review; nothing is sent. It appends to unsent text rather
  // than replacing it, because the pending-input path persists what it sets.
  useEffect(() => {
    const handler = (e: Event) => {
      const text: unknown = (e as CustomEvent).detail?.text
      if (typeof text === 'string' && text) dispatch(setPendingInput(mergeIntoDraft(inputRef.current, text)))
    }
    window.addEventListener('mc:prefill-composer', handler)
    return () => window.removeEventListener('mc:prefill-composer', handler)
  }, [dispatch, inputRef])
  // A redaction card's "Allow for this host" (or its Undo): the reply was saved
  // with the link removed, and the server shows allowed hosts' links again when
  // it serves the slot, so reload it to show the link in place of the chip.
  // The status frame carries the same change to every tab a few seconds later;
  // adopting the write's token here keeps this tab from handling it twice.
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as { slot?: unknown; gen?: unknown } | undefined
      const slot = detail?.slot
      if (typeof slot === 'string' && slot) {
        // The allow is workspace-wide, so every loaded session's rows, this
        // one's included, are now served differently: the store marks them
        // all and re-reads the open one (a background pane showing this card
        // re-reads itself once marked), unless a status frame already carried
        // this same change here.
        // As a thunk, so it reads the state of the store it is dispatched to.
        void dispatch(((d: AppDispatch, getState: () => RootState) => {
          noteRedactionHostsGen(d as (action: unknown) => unknown, getState, detail?.gen, 'write')
        }) as never)
      }
    }
    window.addEventListener('mc:redaction-hosts-changed', handler)
    return () => window.removeEventListener('mc:redaction-hosts-changed', handler)
  }, [dispatch])
}

/**
 * Cold file tabs: restored panel tabs carry a path but no content, and their
 * reads run here, deduped with the page's own file opens by query key.
 */
export function useColdFileTabHydration({ tabsCtl, showActionError }: {
  tabsCtl: PanelTabs
  showActionError: (message: string, title?: string) => void
}) {
  // Cold-tab hydration: after a reload (or when restoring a slot's strip from
  // the persisted panel-tabs store), file tabs come back as lightweight
  // references with their heavy content stripped (content === undefined). Read
  // it back declaratively with useQueries — one ['file-read', path] query per
  // cold file tab (same key/shape as handleFileOpen so the cache dedupes).
  // Once a tab's content is patched in it drops out of coldFileTabs and its
  // query unsubscribes. Diff tabs are transient (not persisted — a restored
  // diff can't reconstruct the original turn snapshot); artifact tabs
  // self-hydrate via ArtifactPanel's own ['artifact', slug] query.
  const coldFileTabs = useMemo(
    () => tabsCtl.tabs.filter(t => t.kind === 'file' && t.path && t.content === undefined),
    [tabsCtl.tabs],
  )
  const coldFileResults = useQueries({
    queries: coldFileTabs.map(t => ({
      queryKey: fileReadQueryKey(t.path!),
      // Same fetch (and so the same cache shape) as handleFileOpen: the binary
      // verdict rides with the text. A 404 is a real answer and keeps its
      // placeholder; any other failure is reported as an error, never as text.
      queryFn: ({ signal }) => fetchFileRead(t.path!, signal),
      staleTime: FILE_READ_STALE_MS,
    })),
  })
  // Mirror settled reads into the tab strip. useQueries owns the fetch
  // lifecycle (error/retry/dedupe); this effect only writes results back, and
  // the content===undefined guard keeps it idempotent (a hydrated tab leaves
  // coldFileTabs, so it isn't re-patched).
  // Read failures already reported, by tab id. The effect below re-runs whenever
  // ANY cold query settles, so without this a failure the user dismissed would
  // come back each time an unrelated tab hydrated. Cleared when the tab's read
  // succeeds, so a retry that fails again is reported again.
  const reportedColdReadsRef = useRef(new Set<string>())
  useEffect(() => {
    coldFileResults.forEach((r, i) => {
      const t = coldFileTabs[i]
      if (!t || t.content !== undefined) return
      if (r.data && (r.data.ok || r.data.status === 404)) {
        reportedColdReadsRef.current.delete(t.id)
        const text = r.data.ok ? r.data.text : i18nT('pages.chatPage.file_not_found_on_disk_it_may_have_been_moved_or')
        // The verdict is re-established by the same read that refills the
        // buffer -- it was stripped from persistence alongside the content.
        tabsCtl.patchTab(t.id, { content: text, savedContent: text, binary: r.data.ok && r.data.binary, partial: r.data.ok && isPartialRead(r.data) })
      } else if ((r.data || r.isError) && !reportedColdReadsRef.current.has(t.id)) {
        // The tab stays cold (its buffer untouched, so the next chip/tree click
        // retries the read) and the failure is reported above the composer.
        // Writing the error sentence into the tab made it look like the file's
        // own text — and a clean, saveable one at that.
        reportedColdReadsRef.current.add(t.id)
        const reason = r.isError
          ? (errMessage(r.error) || i18nT('pages.chatPage.unknown_error'))
          : i18nT('pages.chatPage.http_status', { status: r.data!.status })
        showActionError(i18nT('pages.chatPage.could_not_read_file_reason', { path: t.path!, reason }))
      }
    })
  }, [coldFileResults, coldFileTabs, tabsCtl, showActionError])
}
