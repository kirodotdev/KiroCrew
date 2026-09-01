import {
  type Dispatch,
  type SetStateAction,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { type QueryClient, useQuery } from '@tanstack/react-query'

import { api } from '../../api/client'
import { useChatFileDrop } from '../../components/ChatDropOverlay'
import { makeRelative } from '../../components/FilePickerMenu'
import { PREVIEW_SNIP_EVENT } from '../../components/WebPreviewPanel'
import { useMessageSearch } from '../../hooks/useMessageSearch'
import {
  clearInlineDraft,
  getInlineDraft,
  useAnyLiveAppTab,
  usePanelTabs,
} from '../../hooks/usePanelTabs'
import {
  captureScreen,
  currentTabCaptureDeps,
  screenSnipSupported,
} from '../../hooks/useScreenSnip'
import { useTheme } from '../../hooks/useTheme'
import { i18nT } from '../../i18n/t'
import type { AppDispatch } from '../../store'
import { openActivityPanel } from '../../store/chatSlice'
import type { Artifact, ChatMessage } from '../../types'
import { setDraft } from '../../utils/chatDrafts'
import { setFileDraft } from '../../utils/chatFileDrafts'
import { classifyDrop } from '../../utils/dropClassify'
import { fileReadUrl } from '../../utils/fileReadUrl'
import { spliceDirTokens, VIDEO_EXT } from '../../utils/fileTokens'
import {
  adoptSourceSelections,
  commitSourceSelection,
  isSourceSelectionKey,
  loadRevealedSources,
  loadSeenPullRequestLinks,
  loadSourceSelections,
  partitionSourceLinks,
  persistSeenPullRequestLinks,
  PullRequestLinkIndex,
  recordNewPullRequestLinks,
  sourceSelection,
  type RevealedSources,
  type SourceLinkKind,
  withSourceSelection,
} from '../../utils/pullRequestLinks'
import type { ResizeInfo } from '../../utils/resizeImage'
import { fileLandingSlot } from '../../utils/uploadRouting'
import { optsForReplace } from './replaceGuard'

type MutableRef<T> = { current: T }
const COMPOSER_PARAGRAPH_BREAK = String.fromCharCode(10).repeat(2)

export interface ChatPageFileOpenOptions {
  replaceId?: string
  line?: number
  endLine?: number
  diffMode?: boolean
  canReplace?: () => boolean
}

export interface ChatPageResourcesComposerPorts {
  inputRef: MutableRef<string>
  setInput: Dispatch<SetStateAction<string>>
  drafts: MutableRef<Record<string, string>>
  fileDrafts: MutableRef<Record<string, string[]>>
  setPendingFiles: Dispatch<SetStateAction<string[]>>
  currentProjectRef: MutableRef<string | undefined>
  voiceCaretRef: MutableRef<{ start: number; end: number } | null>
  voicePendingCaretRef: MutableRef<number | null>
  saveDrafts: () => void
}

export interface ChatPageResourcesCapturePorts {
  setUploading: Dispatch<SetStateAction<boolean>>
  setUploadError: Dispatch<SetStateAction<string>>
  setResizedInfo: Dispatch<SetStateAction<Record<string, ResizeInfo>>>
  snipSlotRef: MutableRef<string | null>
  setSnipFrame: Dispatch<SetStateAction<HTMLCanvasElement | null>>
}

export interface UseChatPageResourcesControllerOptions {
  activeSlot: string | null
  activeSlotRef: MutableRef<string | null>
  messages: ChatMessage[]
  slotLoading: boolean
  dispatch: AppDispatch
  queryClient: QueryClient
  composer: ChatPageResourcesComposerPorts
  capture: ChatPageResourcesCapturePorts
}

/**
 * Owns the page's right-dock resources and composer attachment ingress.
 *
 * The option groups are intentionally state/ref ports rather than a context:
 * session identity changes synchronously during render, while async resource
 * completions must address the slot that initiated them. Keeping those refs
 * explicit makes that ordering contract visible in this signature.
 */
export function useChatPageResourcesController({
  activeSlot,
  activeSlotRef,
  messages,
  slotLoading,
  dispatch,
  queryClient,
  composer,
  capture,
}: UseChatPageResourcesControllerOptions) {
  const {
    inputRef,
    setInput,
    drafts,
    fileDrafts,
    setPendingFiles,
    currentProjectRef,
    voiceCaretRef,
    voicePendingCaretRef,
    saveDrafts,
  } = composer
  const {
    setUploading,
    setUploadError,
    setResizedInfo,
    snipSlotRef,
    setSnipFrame,
  } = capture

  const tabsCtl = usePanelTabs(activeSlot)
  // An MCP App tab hosts a null-origin iframe with no storage: unmounting it
  // reloads the app and destroys whatever the user has drawn (see
  // docs/dashboard-iframe-hosts.md). The whole SidePanel subtree is normally
  // gated on `activityOpen`, so closing the panel would unmount it. While an app
  // tab is live we therefore keep the subtree MOUNTED and hide it instead — the
  // same hide-not-unmount rule SidePanel already applies to its own tab bodies.
  // With no app tab, behaviour is unchanged (the panel still unmounts on close,
  // preserving the existing exit animation).
  // Across ALL slots, not just the active one: with cross-slot hosting a frame
  // belonging to another chat lives in this panel subtree, so deciding to unmount
  // on the active slot's (possibly empty) tab list would destroy that canvas.
  const hasLiveAppTab = useAnyLiveAppTab()
  // Current slot only — unlike app tabs (hosted cross-slot via `allAppTabs`), a
  // Browser tab renders solely from the active slot's strip, and a background
  // slot's browser view already unmounts (its WebContentsView released) on the
  // slot switch. So keep-mounted follows THIS slot's tabs, not every slot's.
  const hasBrowserTab = tabsCtl.tabs.some(tab => tab.kind === 'browser')
  // Find/search pane state. Declared above handleFileOpen / handleOpenDiff so
  // those handlers can call search.close() directly when opening a dock panel
  // (the right-hand dock is a single slot and the file/diff panes are
  // render-gated behind !search.isOpen).
  const search = useMessageSearch(messages, activeSlot)
  const closeSearch = search.close
  const sourceLinkIndex = useRef(new PullRequestLinkIndex())
  // Self-managed GitLab hosts the operator authorized (config-only, read-only
  // here). Without them a pasted self-hosted MR link is not a Changes source.
  // No refetchInterval: polling this shared ['dashboardConfig'] key turned every
  // same-key observer into a poller and wrote a dashboard_config_read SEL entry
  // on each tick. Instead the WS 'slots' push carries the allowlist generation
  // (see useWebSocket), which invalidates this query only when the allowlist
  // actually changes — an edit on disk still propagates, without the churn.
  const { data: sourceHostCfg } = useQuery<{ gitlab_hosts?: string[]; jira_hosts?: string[] }>({
    queryKey: ['dashboardConfig'],
    queryFn: () => api.dashboardConfig(),
    staleTime: 30_000,
  })
  const sourceHosts = sourceHostCfg?.gitlab_hosts ?? []
  const jiraSourceHosts = sourceHostCfg?.jira_hosts ?? []
  // Read through refs by callbacks that must stay identity-stable (they are
  // handed to the sidebar, which re-renders every session row).
  const sourceHostsRef = useRef(sourceHosts)
  sourceHostsRef.current = sourceHosts
  const jiraSourceHostsRef = useRef(jiraSourceHosts)
  jiraSourceHostsRef.current = jiraSourceHosts
  const indexedSourceLinks = sourceLinkIndex.current.update(
    activeSlot,
    messages,
    sourceHosts,
    jiraSourceHosts,
  )
  // One scan, one dedup map, two panels: the extractor returns pull requests and
  // issues together (they share the per-role cap), and the two side-panel tabs
  // consume the halves. useMemo keyed on the index's own result identity — the
  // index returns the SAME array reference until the transcript actually changes,
  // so the halves stay reference-stable and don't retrigger the reconciliation
  // effects below on every render.
  const { changes: sourceLinks, issues: issueLinks } = useMemo(
    () => partitionSourceLinks(indexedSourceLinks),
    [indexedSourceLinks],
  )
  // Which Change / Issue tab is focused, PER SLOT and persisted (see
  // pullRequestLinks.SourceSelections). Per-slot because a single shared value
  // reconciles to the first link of whichever transcript is active, so switching
  // A→B→A dropped A's selection; persisted because the panel tab strip itself
  // survives reloads (mc-panel-tabs:<slot>) and a strip that comes back focused
  // on a tab the user never chose is the bug this closes.
  //
  // React state holds this window's view for rendering; commitSourceSelection
  // does the durable write, merging ONE slot into a freshly read snapshot so a
  // second chat window (a popped-out session shares this localStorage) cannot
  // publish its stale view of the slots it is not looking at. That means this
  // window's map can lag another window's writes to OTHER slots — harmless,
  // since only the active slot is ever read, and far better than losing them.
  const [sourceSelections, setSourceSelections] = useState(loadSourceSelections)
  const selectedSourceUrl = sourceSelection(sourceSelections, activeSlot, 'change')
  const selectedIssueUrl = sourceSelection(sourceSelections, activeSlot, 'issue')
  // The links sidebar chips asked to see, per slot and per kind.
  //
  // The chips and these panels do NOT scan for links the same way: the backend
  // chip scan (state.py) keeps every provider url in the transcript, while the
  // panel's extractor emits only links the AGENT surfaced — a pull request the
  // USER pasted is deliberately a Resource, not a Change. A chip is also drawn
  // from the whole server-side transcript, while the extractor sees only the
  // messages this window has loaded. Either gap would make the chip a dead end
  // (the panel would normalise straight back to the first link it does know), so
  // the clicked link is injected into the list for the session it belongs to.
  //
  // Keyed by slot AND kind, matching the two selection ledgers below. A single
  // last-one-wins record could not hold a revealed pull request and a revealed
  // issue at the same time: revealing an issue evicted the pull request, its
  // injection vanished from `panelSources`, and the Changes reconciliation then
  // normalised the selection onto a DIFFERENT pull request behind the user's back.
  //
  // Durable, for the same reason. The SELECTION pointing at a revealed link is
  // already persisted; without persisting the link too, a reload remembered the
  // url but could no longer produce it, and reconciliation performed that same
  // silent swap one page load later.
  const [revealedSources, setRevealedSources] = useState<RevealedSources>(loadRevealedSources)
  const revealedForSlot = activeSlot ? revealedSources[activeSlot] : undefined
  const revealedChange = revealedForSlot?.change ?? null
  const revealedIssue = revealedForSlot?.issue ?? null
  const panelSources = useMemo(() => (
    revealedChange && !sourceLinks.some(link => link.url === revealedChange.url)
      ? [revealedChange, ...sourceLinks]
      : sourceLinks
  ), [sourceLinks, revealedChange])
  const panelIssues = useMemo(() => (
    revealedIssue && !issueLinks.some(link => link.url === revealedIssue.url)
      ? [revealedIssue, ...issueLinks]
      : issueLinks
  ), [issueLinks, revealedIssue])
  // Fields whose durable write storage REFUSED, per slot. Storage then holds an
  // older url than the user's live choice, so adoption must not take it back
  // (see adoptSourceSelections). A ref, not state: it changes nothing on screen
  // and must not re-render.
  const unpersistedSelectionsRef = useRef<Record<string, Partial<Record<SourceLinkKind, boolean>>>>({})
  // Fields whose on-screen value is a provisional fallback rather than a real
  // choice. The value is the link count seen when the fallback was taken, so the
  // storage re-read below can retry only once the transcript has actually GROWN
  // rather than on every render. Cleared by an explicit pick or a successful
  // restore.
  const provisionalFallbackRef = useRef<Record<string, Partial<Record<SourceLinkKind, number>>>>({})
  const selectSource = useCallback((kind: SourceLinkKind, url: string, forSlot?: string) => {
    // `forSlot` is for a pick made on a session that is not on screen yet — a
    // sidebar chip switches sessions and selects in one gesture, and
    // activeSlotRef is assigned during RENDER, so at call time it still names the
    // chat being left.
    const slot = forSlot ?? activeSlotRef.current
    setSourceSelections(previous => withSourceSelection(previous, slot, kind, url))
    const outcome = commitSourceSelection(slot, kind, url)
    if (!slot) return
    // An explicit choice supersedes any provisional fallback for this field.
    const provisional = { ...provisionalFallbackRef.current[slot] }
    delete provisional[kind]
    provisionalFallbackRef.current = { ...provisionalFallbackRef.current, [slot]: provisional }
    const failed = { ...unpersistedSelectionsRef.current[slot] }
    // 'failed' means storage refused the write and still holds an older url;
    // 'unchanged' means storage already agrees. Both are explicit writes, so the
    // ledger records exactly whether this selection reached storage.
    if (outcome === 'failed') failed[kind] = true
    else delete failed[kind]
    unpersistedSelectionsRef.current = { ...unpersistedSelectionsRef.current, [slot]: failed }
  }, [activeSlotRef])
  const selectSourceUrl = useCallback((url: string) => selectSource('change', url), [selectSource])
  const selectIssueUrl = useCallback((url: string) => selectSource('issue', url), [selectSource])
  // A RECONCILED pick is derived from the transcript, not chosen by the user, and
  // is deliberately IN-MEMORY ONLY — it never writes to storage.
  //
  // Persisting it bought nothing and cost correctness. The fallback is
  // deterministic (`sourceLinks[0]`), so a session where the user never picked a
  // tab recomputes the same answer on return without any stored value; the only
  // case persistence changes is a choice that DIFFERS from the first link, which
  // is exactly what an explicit click already records. Meanwhile every write from
  // here could destroy a real choice, because the fallback also fires whenever the
  // transcript on screen is provisional — `switchSlot.pending` serves a cached
  // transcript with `slotLoading` already false while the fetch is still in
  // flight, and a transcript missing a url is not proof the url is gone.
  //
  // The slot is marked provisional so the reconciliation effects know to look in
  // storage once for a better answer (see the effects below).
  const reconcileSelection = useCallback((kind: SourceLinkKind, url: string, seen = 0) => {
    const slot = activeSlotRef.current
    setSourceSelections(previous => withSourceSelection(previous, slot, kind, url))
    if (!slot) return
    provisionalFallbackRef.current = {
      ...provisionalFallbackRef.current,
      [slot]: { ...provisionalFallbackRef.current[slot], [kind]: seen },
    }
  }, [activeSlotRef])
  // The panels normalize their own selection when the remembered url is not among
  // the tabs they render, and that is NOT a user choice — route it to the
  // in-memory path so it cannot overwrite storage. Before this split the panels
  // were handed the persisting callback, which made their normalize a durable
  // write and defeated the whole in-memory-only rule.
  const reconcileSourceUrl = useCallback(
    (url: string) => reconcileSelection('change', url, panelSources.length),
    [reconcileSelection, panelSources.length],
  )
  const reconcileIssueUrl = useCallback(
    (url: string) => reconcileSelection('issue', url, panelIssues.length),
    [reconcileSelection, panelIssues.length],
  )

  // Re-read storage for a slot whose on-screen value is a provisional fallback.
  //
  // Without this the fallback would stick for the life of the document: nothing
  // else re-reads storage in the window that wrote it — loadSourceSelections runs
  // only in the useState initializer, and the `storage` event never fires in the
  // writing document — so the user would keep seeing the fallback instead of the
  // tab they left open until a reload.
  //
  // Retried only when the transcript has GROWN since the fallback was taken. A
  // transcript is append-only within a slot, so growth is the only way a
  // previously-absent url can appear, and gating on it keeps this off the
  // per-render (and per-streaming-chunk) path. Membership in `links` is the
  // "the fetch proved it still exists" condition.
  const restoreFromStorage = useCallback((
    kind: SourceLinkKind,
    links: readonly { url: string }[],
  ): boolean => {
    const slot = activeSlotRef.current
    if (!slot) return false
    const seen = provisionalFallbackRef.current[slot]?.[kind]
    if (seen === undefined || links.length <= seen) return false

    const stored = sourceSelection(loadSourceSelections(), slot, kind)
    if (stored && links.some(link => link.url === stored)) {
      const provisional = { ...provisionalFallbackRef.current[slot] }
      delete provisional[kind]
      provisionalFallbackRef.current = { ...provisionalFallbackRef.current, [slot]: provisional }
      setSourceSelections(previous => withSourceSelection(previous, slot, kind, stored))
      return true
    }
    // Not there yet — wait for further growth rather than re-reading every render.
    provisionalFallbackRef.current = {
      ...provisionalFallbackRef.current,
      [slot]: { ...provisionalFallbackRef.current[slot], [kind]: links.length },
    }
    return false
  }, [activeSlotRef])

  // Adopt a sibling window's writes. `storage` fires in every OTHER document on
  // this origin, so the window that did NOT write is the one that needs to
  // re-read. Without this, a window carries its mount-time view until reload and
  // two windows focused on the same session would each show their own last
  // choice. The event's newValue is ignored in favour of a full re-read, so the
  // loader's own validation and bounds apply to whatever a sibling wrote.
  //
  // The urls THIS window can actually SHOW go in with the read: adoption is
  // conditional on them for the active slot, which is what keeps two windows
  // with divergent transcripts from overwriting each other in a loop (see
  // adoptSourceSelections). The panel lists rather than the raw scan, so a link
  // revealed from a sidebar chip is not taken back by a sibling's write. Read
  // through a ref because the listener is registered once and must see the
  // current lists at event time.
  const availableSourceUrls = useMemo(() => ({
    change: panelSources.map(source => source.url),
    issue: panelIssues.map(issue => issue.url),
  }), [panelSources, panelIssues])
  const availableSourceUrlsRef = useRef(availableSourceUrls)
  availableSourceUrlsRef.current = availableSourceUrls
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.storageArea && event.storageArea !== localStorage) return
      // key === null is a storage.clear(), which does concern us. Otherwise
      // match the store's key prefix — the selection lives in one key per
      // (slot, kind), so there is no single literal to compare against.
      if (event.key !== null && !isSourceSelectionKey(event.key)) return
      setSourceSelections(previous => adoptSourceSelections(
        previous,
        activeSlotRef.current,
        availableSourceUrlsRef.current,
        unpersistedSelectionsRef.current,
      ))
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [activeSlotRef])

  // Add and focus the per-slot Changes / Issues tabs for newly detected URLs,
  // but leave panel visibility under explicit user control. Both kinds share one
  // seen-url bookkeeping set (it is keyed by url, and the cap is a per-slot
  // budget), so each kind is recorded separately only to learn WHICH tab to open.
  const [seenSourceUrls] = useState(loadSeenPullRequestLinks)
  useEffect(() => {
    const newChanges = recordNewPullRequestLinks(seenSourceUrls, activeSlot, sourceLinks)
    const newIssues = recordNewPullRequestLinks(seenSourceUrls, activeSlot, issueLinks)
    if (!newChanges && !newIssues) return
    persistSeenPullRequestLinks(seenSourceUrls)
    if (newChanges) tabsCtl.openView('changes')
    if (newIssues) tabsCtl.openView('issues')
    // tabsCtl is intentionally not a dependency: this effect reacts only to
    // source discovery, not tab focus or panel visibility changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSlot, sourceLinks, issueLinks, seenSourceUrls])

  useEffect(() => {
    // An uncached slot temporarily has no messages while its history hydrates.
    // Preserve the persisted strip until that source-of-truth load settles.
    if (slotLoading) return
    // Reconciled against the list the PANEL renders, not the raw transcript scan:
    // a link revealed from a sidebar chip is a real, user-chosen tab, and judging
    // it against the scan alone would normalise the selection straight off it.
    // A previous provisional render may have fallen back in memory while storage
    // still holds the tab the user chose; look there first once links appear.
    if (restoreFromStorage('change', panelSources)) return
    if (panelSources.length === 0) {
      // Changes is a permanently pinned tab (SidePanel.syncPinned) — never
      // auto-close it here. Just clear the source selection; the tab stays put
      // and renders its empty state until sources are detected again.
      //
      // Two guards, both load-bearing:
      //  - transcript LOADED, not merely empty. switchSlot.rejected (a dropped
      //    history fetch) empties `messages` AND drops slotLoading in one reducer
      //    pass, so the guard above does not hold; since the selection is durable,
      //    clearing there would outlive the failure and lose the tab on retry.
      //  - something to clear. commitSourceSelection enumerates storage to decide
      //    whether the value already matches, and these effects re-run on every
      //    streaming chunk (the link index hands back a fresh array per chunk), so
      //    an unconditional clear costs a full enumeration per chunk for every
      //    session that never mentions a pull request — the common case.
      if (messages.length && selectedSourceUrl) reconcileSelection('change', '')
      return
    }
    // First-wins fallback ONLY when the remembered url is gone from the
    // transcript: while it is still present, selectedSourceUrl already carries
    // the restored per-slot choice and this reconciliation leaves it alone.
    if (!panelSources.some(source => source.url === selectedSourceUrl)) {
      // Storage may still hold the tab the user actually chose — absent from an
      // earlier PROVISIONAL transcript but present now that the fetch landed.
      // Look there once before falling back, gated on the url being in THIS
      // transcript (that gate IS the "the fetch proved it exists" condition).
      reconcileSelection('change', panelSources[0].url, panelSources.length)
    }
    // reconcileSourceUrl reads the active slot through a ref, so it is stable and
    // this effect reacts only to sources, selection, and hydration state.
  }, [panelSources, selectedSourceUrl, slotLoading, messages.length, reconcileSelection, restoreFromStorage])

  useEffect(() => {
    // Same first-wins / clear-on-empty reconciliation as the Changes selection
    // above, including the loaded-transcript guard on the clear.
    if (slotLoading) return
    if (restoreFromStorage('issue', panelIssues)) return
    if (panelIssues.length === 0) {
      if (messages.length && selectedIssueUrl) reconcileSelection('issue', '')
      return
    }
    if (!panelIssues.some(issue => issue.url === selectedIssueUrl)) {
      reconcileSelection('issue', panelIssues[0].url, panelIssues.length)
    }
  }, [panelIssues, selectedIssueUrl, slotLoading, messages.length, reconcileSelection, restoreFromStorage])

  const addSourceCommentToChat = useCallback((text: string) => {
    setInput(previous => previous.trim() ? `${previous.trimEnd()}${COMPOSER_PARAGRAPH_BREAK}${text}` : text)
  }, [setInput])

  const { colorTheme } = useTheme()
  // Mirror colorTheme into a ref so the `send` callback (which does not depend
  // on colorTheme, to avoid re-creating on every theme switch) can always read
  // the current theme without going stale — otherwise a theme change with no
  // activeSlot change sends the previous theme's color_theme to the backend,
  // mis-injecting the persona.
  const colorThemeRef = useRef(colorTheme)
  useEffect(() => { colorThemeRef.current = colorTheme }, [colorTheme])
  // Read file content via queryClient.fetchQuery so we get React Query's
  // caching/deduplication on repeated opens (re-opening the same file is
  // instant for ~10s) AND proper error semantics (queryFn throws → catch
  // block runs). useMutation was the wrong tool for a read operation.
  // The `ok` flag gates whether the file is recorded in history — 404s and
  // other HTTP failures show a placeholder in the panel but should NOT
  // pollute the history list with files that don't exist on disk.
  const handleFileOpen = useCallback(async (filePath: string, opts?: ChatPageFileOpenOptions) => {
    // Plugin host integration: notify the IntelliJ plugin (if active) so
    // it can open the file natively in the IDE editor. If the plugin
    // handles file opens, skip the dashboard's DiffPanel — the user wanted
    // IDE-native, not in-dashboard.
    try { window.dispatchEvent(new CustomEvent('kirocrew-file-open', { detail: { path: filePath } })) } catch { /* ignore */ }
    if ((window as unknown as { __kirocrewPluginHandlesFiles?: boolean }).__kirocrewPluginHandlesFiles) return
    try {
      const [{ text }] = await Promise.all([
        queryClient.fetchQuery({
          queryKey: ['file-read', filePath],
          queryFn: async () => {
            const url = fileReadUrl(filePath)
            const res = await fetch(url)
            const text = res.ok
              ? await res.text()
              : res.status === 404 ? i18nT('pages.chatPage.file_not_found_on_disk_it_may_have_been_moved_or')
              : i18nT('pages.chatPage.unable_to_read_file')
            return { text, ok: res.ok }
          },
          staleTime: 10_000,
        }),
        queryClient.prefetchQuery({
          queryKey: ['file-diff', filePath],
          queryFn: () => api.fileDiff(filePath),
        }),
      ])
      tabsCtl.openFile(filePath, text, activeSlotRef.current ?? null, optsForReplace(opts))
      dispatch(openActivityPanel())
      // The right-hand dock is a single slot; the file viewer is render-gated
      // behind !search.isOpen. Close the find pane so the opened file actually
      // shows instead of being silently suppressed.
      closeSearch()
    } catch {
      tabsCtl.openFile(filePath, i18nT('pages.chatPage.error_reading_file'), activeSlotRef.current ?? null, optsForReplace(opts))
      dispatch(openActivityPanel())
      closeSearch()
    }
    // Depend on the stable member, not the whole hook object: `closeSearch` is a
    // useCallback([]) in useMessageSearch, while the `search` object changes
    // identity on every search-state change (isOpen/term/matches), which would
    // churn this callback and the onFileOpen prop on every row. (tabsCtl still
    // churns on tab changes, but those are user actions, not per-chunk.)
  }, [queryClient, tabsCtl, dispatch, closeSearch, activeSlotRef])

  /** Open a DIRECTORY as a panel tab.
   *
   *  The folder twin of handleFileOpen, and deliberately much thinner: there is
   *  no content to prefetch (FolderPanel owns its own ['browse-files', path]
   *  query). Only reachable for paths the backend already
   *  confirmed are directories, so there is no not-found branch to handle. */
  const handleFolderOpen = useCallback((dirPath: string) => {
    tabsCtl.openFolder(dirPath, activeSlotRef.current ?? null)
    dispatch(openActivityPanel())
    closeSearch()
  }, [tabsCtl, dispatch, closeSearch, activeSlotRef])

  // Open an artifact as a side-panel tab — the artifact twin of
  // handleFileOpen, and the single entry point every in-chat artifact
  // affordance routes through (the Artifacts tab's rows and `/artifacts/<slug>`
  // links inside messages). Routing them here renders the document inline in the
  // panel instead of hard-navigating to the standalone detail page, which would
  // tear down the chat and make artifacts the only panel-capable content that
  // could not be flipped between like files.
  const handleArtifactOpen = useCallback(async (slug: string) => {
    if (!slug) return
    const slot = activeSlotRef.current ?? null
    // Opening an artifact is an act of session involvement: record the
    // `referenced` breadcrumb so a merely-read (or merely-linked) artifact
    // joins "This session" instead of sitting in the library section forever.
    // Deliberately fire-and-forget and deliberately NOT awaited — the panel
    // must open at click speed, and the store already enforces
    // one-breadcrumb-per-session so a double click cannot spam the event log.
    // The 403 an incognito slot returns is expected, not an error to surface.
    if (slot) {
      api.recordArtifactReference(slug, slot)
        .then(() => {
          // Re-run the involvement scan so the row moves sections live.
          queryClient.invalidateQueries({ queryKey: ['session-artifact-records', slot] })
        })
        .catch(() => { /* best-effort breadcrumb */ })
    }
    // Seed the tab from the artifact list cache when it is already warm so the
    // body paints immediately; ArtifactPanel's own query is authoritative and
    // overrides kind/content once it resolves, so a miss here costs a spinner,
    // not correctness.
    let kind: Artifact['kind'] = 'markdown'
    let content = ''
    try {
      const art = await queryClient.fetchQuery<Artifact>({
        queryKey: ['artifact', slug],
        queryFn: () => api.artifact(slug),
        staleTime: 10_000,
      })
      kind = art.kind
      content = art.content ?? ''
    } catch { /* fall through — the panel's own query renders the error state */ }
    tabsCtl.openArtifact({ slug, kind }, content, slot)
    dispatch(openActivityPanel())
    // Same single-slot constraint as handleFileOpen: the right-hand dock is
    // render-gated behind !search.isOpen, so an open find pane would silently
    // swallow the tab we just focused.
    closeSearch()
  }, [queryClient, tabsCtl, dispatch, closeSearch, activeSlotRef])

  // Open the diff panel from a file-change chip click. Closes the
  // markdown viewer and the activity panel so panels stay mutually exclusive.
  const handleOpenDiff = useCallback((filePath: string, modified: string, original: string) => {
    // If the IntelliJ plugin's file bridge is active, dispatch the event
    // with before/after content so the plugin can show a native IntelliJ
    // diff viewer (with syntax highlighting). Skip the dashboard's
    // own DiffPanel in that case — the plugin sets the flag on page load.
    try {
      window.dispatchEvent(new CustomEvent('kirocrew-file-open', {
        detail: { path: filePath, before: original, after: modified },
      }))
    } catch { /* ignore */ }
    if ((window as unknown as { __kirocrewPluginHandlesFiles?: boolean }).__kirocrewPluginHandlesFiles) return
    // Brand-new file (no prior content): a diff would render as one big green
    // all-additions block, which hurts readability. Open the normal readable
    // file view instead — there's no meaningful "before" to compare against.
    // Identical content (no-op): the diff editor shows two identical panes with
    // zero signal — fall through to the readable file view as well.
    if (!original || !original.trim() || original === modified) { handleFileOpen(filePath); return }
    tabsCtl.openDiff(filePath, modified, original)
    dispatch(openActivityPanel())
    // Diff pane is render-gated behind !search.isOpen (single right-dock slot);
    // close the find pane so the diff shows instead of opening underneath it.
    closeSearch()
  }, [tabsCtl, dispatch, closeSearch, handleFileOpen])

  const handleFileSave = useCallback(async (filePath: string, content: string) => {
    // Capture the slot BEFORE awaiting: if the user switches chats mid-save, the
    // draft we reconcile must be the one that owned this save, not whatever slot
    // is active when the write resolves.
    const requestSlot = activeSlotRef.current ?? ''
    const res = await fetch('/api/file-write', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: filePath, content }),
    })
    if (!res.ok) throw new Error(`Save failed: ${res.status}`)
    // The saved bytes become the tab's dirty baseline, so a later re-open of
    // the same path refreshes the buffer instead of (needlessly) preserving it
    // as if it still held unsaved work. Best-effort: a tab that is not open
    // right now is simply not found by id.
    tabsCtl.patchTab(`file:${filePath}`, { savedContent: content })
    // Reconcile the inline-preview draft for the SAVING slot (drafts are
    // slot+path keyed). Clear it ONLY if it still equals what we just saved -
    // if the user typed more while the write was in flight, the draft now holds
    // newer content and must be preserved, not dropped.
    if (getInlineDraft(requestSlot, filePath) === content) clearInlineDraft(requestSlot, filePath)
  }, [tabsCtl, activeSlotRef])

  const takeScreenshot = useCallback(async () => {
    // Capture the slot at click-time. If the user switches away before the
    // screenshot promise resolves, we must land the file in the slot the user
    // was looking at when they clicked — not whatever slot is now active.
    const requestSlot = activeSlotRef.current
    setUploading(true)
    try {
      const { path } = await api.screenshot()
      if (path) {
        if (activeSlotRef.current === requestSlot) {
          setPendingFiles(previous => [...previous, path])
        } else if (requestSlot) {
          // Slot changed during the await — divert the file into the request
          // slot's persisted draft so it's waiting when the user goes back.
          const current = fileDrafts.current[requestSlot] ?? []
          setFileDraft(fileDrafts.current, requestSlot, [...current, path])
          saveDrafts()
        }
      }
    } catch { /* user cancelled */ }
    setUploading(false)
  }, [activeSlotRef, setUploading, setPendingFiles, fileDrafts, saveDrafts])

  /** Screen capture entry: cross-platform snip+crop when supported, else native macOS screenshot. */
  const handleCapture = useCallback(async () => {
    snipSlotRef.current = activeSlotRef.current
    if (!screenSnipSupported) { takeScreenshot(); return }
    const canvas = await captureScreen()
    if (canvas) setSnipFrame(canvas)
  }, [snipSlotRef, activeSlotRef, takeScreenshot, setSnipFrame])

  // The Web Preview tab's crop button asks for an area screenshot via a window
  // event. Same crop→attach pipeline as the composer button, but capture pre-
  // targets THIS tab (preferCurrentTab) so the browser prompt is a single
  // "Share this tab?" confirm instead of the full source picker. (Desktop app:
  // no prompt either way via setDisplayMediaRequestHandler.)
  useEffect(() => {
    const onSnip = async () => {
      snipSlotRef.current = activeSlotRef.current
      if (!screenSnipSupported) { takeScreenshot(); return }
      const canvas = await captureScreen(currentTabCaptureDeps())
      if (canvas) setSnipFrame(canvas)
    }
    window.addEventListener(PREVIEW_SNIP_EVENT, onSnip)
    return () => window.removeEventListener(PREVIEW_SNIP_EVENT, onSnip)
  }, [snipSlotRef, activeSlotRef, takeScreenshot, setSnipFrame])

  /** Upload files via browser File API (cross-platform) */
  const uploadFiles = useCallback(async (files: File[], targetSlot?: string | null) => {
    if (!files.length) return
    // Same slot-capture pattern as takeScreenshot — see note there. An explicit
    // targetSlot (e.g. the slot that initiated a snip) overrides the live slot
    // so an async capture lands where it started, not where the user switched to.
    const requestSlot = targetSlot !== undefined ? targetSlot : activeSlotRef.current
    setUploadError('')
    if (files.length > 20) { setUploadError(i18nT('pages.chatPage.too_many_files_max_20')); return }
    // Video is deliberately exempt from this pre-check: it has a much larger
    // server-side ceiling and streams to disk there, so the 50 MB figure this
    // message states would be a lie for a recording. Its own 413 carries the
    // real cap and surfaces through the `upload_failed_error` branch below,
    // the same route every other server-side rejection already takes.
    const big = files.find(file => !VIDEO_EXT.test(file.name) && file.size > 50 * 1024 * 1024)
    if (big) { setUploadError(i18nT('pages.chatPage.file_too_large', { name: big.name })); return }
    setUploading(true)
    try {
      const response = await api.uploadFiles(files)
      if (response.error) {
        setUploadError(i18nT('pages.chatPage.upload_failed_error', { error: response.error }))
      } else if (response.paths?.length) {
        const landing = fileLandingSlot(requestSlot, activeSlotRef.current)
        if (landing.target === 'pending') {
          setPendingFiles(previous => [...previous, ...response.paths])
        } else if (landing.target === 'draft') {
          const current = fileDrafts.current[landing.slot] ?? []
          setFileDraft(fileDrafts.current, landing.slot, [...current, ...response.paths])
          saveDrafts()
        }
      }
      if (!response.error && response.resizedByPath && Object.keys(response.resizedByPath).length) {
        setResizedInfo(previous => ({ ...previous, ...response.resizedByPath }))
      }
    } catch {
      setUploadError(i18nT('pages.chatPage.upload_failed_check_file_type_and_size_max_50_mb'))
    }
    setUploading(false)
  }, [
    activeSlotRef,
    setUploadError,
    setUploading,
    setPendingFiles,
    fileDrafts,
    saveDrafts,
    setResizedInfo,
  ])

  // Deliver an optimize result to the session that started it when the user
  // navigated away before the request settled. ChatInput only calls this for
  // the cross-session case (it writes the result itself when the originating
  // session is still on screen). Same slot-capture pattern as uploadFiles /
  // the send-failure draft restore: persist into the originating slot's draft
  // unconditionally (recoverable on disk + shown when the user returns), and
  // only splice into the live input when that slot is what's currently on
  // screen — compared against activeSlotRef.current, never the stale closure.
  const handleOptimizeResult = useCallback((slot: string | null, optimized: string) => {
    if (!slot) return
    setDraft(drafts.current, slot, optimized)
    saveDrafts()
    if (slot === activeSlotRef.current) setInput(optimized)
  }, [drafts, saveDrafts, activeSlotRef, setInput])

  const handleDrop = useCallback((dataTransfer: DataTransfer) => {
    // Classify BEFORE acting (issue #743): a dropped folder inserts its path
    // into the composer as an `@rel/` token — the same reference the @-picker
    // stages — instead of taking the upload route, which cannot ingest a
    // directory. Files keep uploading; a mixed drop takes both routes. In a
    // plain browser no real path is visible, so classifyDrop leaves folders
    // on the upload route there (today's behaviour) rather than inserting a
    // misleading bare name.
    const { files, dirPaths } = classifyDrop(dataTransfer)
    if (dirPaths.length) {
      // Short relative form when the folder lies inside the project root,
      // absolute otherwise — exactly the picker's own fallback convention.
      const rels = dirPaths.map(path => makeRelative(path, currentProjectRef.current || ''))
      const spliced = spliceDirTokens(inputRef.current, voiceCaretRef.current?.start ?? null, rels)
      if (spliced.changed) {
        // Arm the caret restore the same way the dictation splice does, so the
        // cursor lands just past the inserted tokens once the value commits.
        // Only on a real change: an all-duplicates drop leaves the value
        // identical, React bails out of the no-op setInput, the restore effect
        // never fires, and the armed offset would fire stale on the next
        // unrelated edit, yanking the user's cursor.
        voicePendingCaretRef.current = spliced.caret
        setInput(spliced.value)
      }
    }
    if (files.length) uploadFiles(files)
  }, [currentProjectRef, inputRef, voiceCaretRef, voicePendingCaretRef, setInput, uploadFiles])
  const { active: dragOver, dropTargetProps } = useChatFileDrop(handleDrop)

  return {
    tabsCtl,
    hasLiveAppTab,
    hasBrowserTab,
    search,
    sourceHostsRef,
    jiraSourceHosts,
    jiraSourceHostsRef,
    panelSources,
    panelIssues,
    selectedSourceUrl,
    selectedIssueUrl,
    selectSource,
    selectSourceUrl,
    selectIssueUrl,
    reconcileSourceUrl,
    reconcileIssueUrl,
    setRevealedSources,
    addSourceCommentToChat,
    colorThemeRef,
    handleFileOpen,
    handleFolderOpen,
    handleArtifactOpen,
    handleOpenDiff,
    handleFileSave,
    handleCapture,
    uploadFiles,
    handleOptimizeResult,
    dragOver,
    dropTargetProps,
  }
}

export type ChatPageResourcesController = ReturnType<typeof useChatPageResourcesController>
