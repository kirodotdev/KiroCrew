import { useEffect, useMemo, useRef, type MutableRefObject } from 'react'
import { type QueryClient, useQueries } from '@tanstack/react-query'

import type { useDeleteTerminalSession } from '../../../components/CliPanel'
import { normalizeUrl, setSessionPreviewPending } from '../../../components/WebPreviewPanel'
import { addTab as addDockTerminal } from '../../../hooks/useBottomTerminal'
import type { usePanelTabs } from '../../../hooks/usePanelTabs'
import { useRunInTerminalBridge } from '../../../hooks/useRunInTerminalBridge'
import { i18nT } from '../../../i18n/t'
import type { AppDispatch } from '../../../store'
import { openActivityPanel, refreshSlot, setPendingInput } from '../../../store/chatSlice'
import type { ChatMessage, ChatSlot } from '../../../types'
import { mergeIntoDraft } from '../../../utils/chatDrafts'
import { detectPreviewUrl, previewFeedDecision } from '../../../utils/detectPreviewUrl'
import { RUN_IN_TERMINAL_READY_DEADLINE_MS } from '../../../utils/fenceShell'
import { fetchFileRead, fileReadQueryKey, FILE_READ_STALE_MS, isPartialRead } from '../../../utils/fileReadQuery'
import { safeSetItem } from '../../../utils/safeStorage'
import { onTerminalReady, sendRawToTerminalSession } from '../../../utils/terminalRegistry'
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
 * Returns the page's Run-in-terminal scope, which the page provides to its
 * chats through `RunInTerminalScope` (see useRunInTerminalBridge).
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
  // "Run in terminal" from this page's code blocks (useRunInTerminalBridge).
  // The page also answers unscoped requests, as it always has.
  const runInTerminalScope = useRunInTerminalBridge({
    queryClient,
    showActionError,
    cwdRef: currentProjectRef,
    acceptUnscoped: true,
    deleteTerminalSessionRef,
    terminalReuseRef,
    terminalCfgUnsettledRef,
    terminalCfgReadFailedRef,
  })
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
  // `messageTs` names the card's reply so the reload re-serves that row even
  // when it sits above the newest page (#17023).
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail
      const slot: unknown = detail?.slot
      const messageTs: unknown = detail?.messageTs
      if (typeof slot !== 'string' || !slot) return
      void dispatch(refreshSlot(typeof messageTs === 'string' && messageTs
        ? { key: slot, reachTs: messageTs }
        : slot))
    }
    window.addEventListener('mc:redaction-hosts-changed', handler)
    return () => window.removeEventListener('mc:redaction-hosts-changed', handler)
  }, [dispatch])
  return { runInTerminalScope }
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
