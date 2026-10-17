import { useEffect, useState, useRef, useMemo, useCallback, type Dispatch, type SetStateAction, type MutableRefObject } from 'react'
import type { HoverIntent } from '../../../hooks/useHoverIntent'
import type { AppDispatch } from '../../../store'
import type { ChatSlot, ChatFolder } from '../../../types'
import { openActivityPanel, createSlot, deleteSlot } from '../../../store/chatSlice'
import { safeSetItem } from '../../../utils/safeStorage'
import type { useChatPageResourcesController } from '../useChatPageResourcesController'
import type { useComposerChips } from './composerChips'
import type { MemoryMode } from '../../../components/MemoryModeChip'
import { anchorForSlot, sessionSlots, loadLayout } from '../../../hooks/splitLayoutStore'
import type { SplitDir } from '../../../hooks/useSessionGrid'
import { CHAT_PANE_MIN_W, sidePanelFillWidth } from '../SidePanel'
import { useSidePanelDock } from '../../../hooks/useSidePanelDock'
import { parseStoredSidebarWidth, SIDEBAR_MAX } from '../sidebarWidth'
import { focusComposerAfter, focusComposer } from '../composerFocus'
import { resolveFolderAgent, resolveFolderProjectDir, isStaleProjectDirError } from '../../../utils/folderAgent'
import { PanelLeftLight, PanelLeftSolid } from '../../../components/icons/panels'
import { sidePanelDockMotion } from '../sidePanelMount'
import { errMessage } from '../../../utils/thunkError'
import { memorySwitchTimeoutMs } from '../../../utils/memorySwitchTimeout'
import type { MemorySwitchContext } from '../memoryModeSwitch'
import { crewWindowShown } from '../crew-window/crewWindowStore'
import { i18nT } from '../../../i18n/t'
import { uiLocation } from '../../../uiLocations/uiLocation'

interface PaneLayoutStateOptions {
  embedMode?: 'chat' | 'sessions'
  dispatch: AppDispatch
  slots: ChatSlot[]
  activeSlot: string | null
  autoOpenGitPanel: boolean
  autoOpenGitPanelKnown: boolean
  setSplitMode: Dispatch<SetStateAction<boolean>>
  splitMode: boolean
  setSplitAnchor: Dispatch<SetStateAction<string | null>>
  tabsCtl: ReturnType<typeof useChatPageResourcesController>['tabsCtl']
  splitFeatureEnabled: boolean
  _slotProject: string
  projectGit: ReturnType<typeof useComposerChips>['projectGit']
  projectGitError: boolean
}

/**
 * The pane's layout state: the opt-in Git panel auto-open, the sessions sidebar's
 * pinned / width / drag state and the side panel's dock, the title rename editor, and
 * split view (enter, the persisted split the active session belongs to, auto-enter on
 * its anchor, and the Cmd/Ctrl+D shortcut).
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function usePaneLayoutState({
  embedMode, dispatch, slots, activeSlot, autoOpenGitPanel, autoOpenGitPanelKnown, setSplitMode, splitMode, setSplitAnchor, tabsCtl, splitFeatureEnabled, _slotProject, projectGit, projectGitError,
}: PaneLayoutStateOptions) {
  // Auto-open the Git panel when the slot has a project dir that is a git repo.
  // OPT-IN (dashboard.auto_open_git_panel, default off) because the marker below
  // cannot make this the once-per-project nudge it reads like: a new slot inherits
  // `dashboard.default_project`, so keying on slot+path re-fires for every new
  // chat in the same repo — forever. The Git TAB is still created unconditionally
  // (same as the folder tab below), so the panel is one click away when off.
  useEffect(() => {
    if (!activeSlot || !_slotProject || projectGitError) return
    if (!projectGit?.repo) return
    // Do not consume the marker before the opt-in's value is known — see
    // `autoOpenGitPanelKnown`.
    if (!autoOpenGitPanelKnown) return
    const key = `mc-git-panel-opened:${activeSlot}:${_slotProject}`
    if (localStorage.getItem(key)) return
    // If the marker cannot be persisted, skip the auto-open entirely: opening
    // changes tabsCtl, which re-runs this effect, and an absent marker would
    // make it open again forever. safeSetItem reports whether the write landed
    // (after reclaiming a disposable tier if the quota was full), so the guard
    // is the return value rather than a caught throw.
    if (!safeSetItem(key, '1')) return
    tabsCtl.openView('git')
    if (autoOpenGitPanel) dispatch(openActivityPanel())
  }, [activeSlot, _slotProject, projectGit?.repo, projectGitError, tabsCtl, dispatch, autoOpenGitPanel, autoOpenGitPanelKnown])

  const [sidebarPinned, setSidebarPinned] = useState(() => localStorage.getItem('mc-sidebar-pinned') !== 'false')
  const sidebarPinnedRef = useRef(sidebarPinned)
  sidebarPinnedRef.current = sidebarPinned
  // Pre-focus session-list state while the Web Preview expand mode auto-hides
  // it, so exiting focus mode restores what the user had. null = focus mode is
  // not the reason the list is hidden (the user owns the state).
  const sidebarAutoHidden = useRef<boolean | null>(null)
  const [sidePanelDock] = useSidePanelDock()
  // Recomputed on every dock flip: the wrapper keeps one React key across the
  // flip, so both axes have to stay named or the flipped-away one gets driven
  // back to its base (see sidePanelDockMotion).
  const sidePanelDockAnim = useMemo(() => sidePanelDockMotion(sidePanelDock), [sidePanelDock])
  // The width ChatSidebar reports it paints at (see effectiveSidebarWidth).
  // Until it reports, read the stored width as the list views cap it: the
  // sidebar's first layout effect corrects it before the frame paints.
  const [sidebarWidth, setSidebarWidth] = useState(() =>
    Math.min(parseStoredSidebarWidth(localStorage.getItem('mc-sidebar-width')) ?? 260, SIDEBAR_MAX))
  const [sidebarDragging, setSidebarDragging] = useState(false)
  // Pinned to the slot the rename opened on: activeSlot moves the instant the user
  // switches sessions, and a live-resolved commit would rename the wrong session.
  const [editingTitleSlot, setEditingTitleSlot] = useState<string | null>(null)
  const editingTitle = editingTitleSlot !== null && editingTitleSlot === activeSlot
  // Leaving abandons the draft. The pin alone closes the editor but keeps it, so a
  // return would revive stale text and a blur could overwrite a newer title.
  useEffect(() => { setEditingTitleSlot(null) }, [activeSlot])
  // Which side of the anchor the next split view seeds its new pane on. Every
  // enterSplit sets it, so one entry never carries into the next.
  const [splitSeedDir, setSplitSeedDir] = useState<SplitDir>('right')
  // Native session grid "split mode": an in-place tiling of the chat surface (NOT an
  // overlay). The flag is EPHEMERAL per mount — nav/refresh lands on single chat —
  // but the LAYOUT persists per anchor slot (splitLayoutStore). So a split is
  // preserved across navigation, and a member session opened on its own shows single
  // chat plus an "in split" badge that re-enters it (β model). `splitAnchor` is the
  // slot whose split we're showing (the one ⌘D'd from, or the badge's target).
  // enterSplit opens Split View for `anchor`: SessionGridView restores anchor's saved
  // layout if one exists, else seeds [anchor | placeholder] with the placeholder
  // beside `anchor` ('right', the default) or below it ('down'). Closing back down to a
  // single session dissolves the layout and collapses to native chat (onCollapse).
  const enterSplit = useCallback((anchor: string | null, dir: SplitDir = 'right') => {
    setSplitAnchor(anchor)
    setSplitSeedDir(dir)
    setSplitMode(true)
  }, [setSplitAnchor, setSplitMode])
  // Anchor of the persisted split the active session belongs to (>= 2 live sessions),
  // or null — drives the "in split" badge in single chat. Validated against live
  // slots so a stale layout (a member was deleted) never shows a dead badge.
  const splitAnchorForActive = useMemo(() => {
    if (!splitFeatureEnabled || splitMode || !activeSlot) return null
    const anchor = anchorForSlot(activeSlot)
    if (!anchor) return null
    const liveKeys = new Set(slots.map((s) => s.key))
    return sessionSlots(loadLayout(anchor)).filter((k) => liveKeys.has(k)).length >= 2 ? anchor : null
  }, [splitFeatureEnabled, splitMode, activeSlot, slots])
  // True when the active session IS the anchor of its live persisted split (the slot
  // ⌘D was originally pressed from). The anchor's natural view IS its split, so we
  // auto-open it (no badge, no extra click); non-anchor members stay single chat + badge.
  const activeIsSplitAnchor = splitAnchorForActive !== null && splitAnchorForActive === activeSlot
  // Auto-enter split when you land on its anchor. Gated on splitMode being off (so we
  // don't fight an in-progress exit) and on a resolved activeSlot + real >=2-member live
  // layout (so a fresh refresh never seeds an orphan pane).
  // Members never auto-enter; closing a split to 1 dissolves the layout so there's no loop.
  useEffect(() => {
    if (embedMode || splitMode || !activeIsSplitAnchor) return
    enterSplit(splitAnchorForActive)
  }, [embedMode, splitMode, activeIsSplitAnchor, splitAnchorForActive, enterSplit])
  // ⌘D / Ctrl+D enters split mode from single chat (splitting the current session).
  // Inside split mode the grid (SessionGridView) owns ⌘D = split the focused pane.
  useEffect(() => {
    if (embedMode) return
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'd') {
        // A crew window covers the pane: never split the hidden local session.
        if (!splitFeatureEnabled || splitMode || !activeSlot || crewWindowShown()) return
        e.preventDefault()
        enterSplit(activeSlot)
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [embedMode, splitMode, enterSplit, splitFeatureEnabled, activeSlot])
  return {
    sidebarPinned, setSidebarPinned, sidebarPinnedRef, sidebarAutoHidden, sidePanelDock, sidePanelDockAnim, sidebarWidth, setSidebarWidth, sidebarDragging, setSidebarDragging, setEditingTitleSlot,
    editingTitle, enterSplit, splitAnchorForActive, activeIsSplitAnchor, splitSeedDir,
  }
}

interface PanelLayoutOptions {
  mode?: string
  dispatch: AppDispatch
  isMobile: boolean
  filteredSlots: ChatSlot[]
  activeSlot: string | null
  isWelcomeState: boolean
  showActionError: (message: string, title?: string) => void
  currentSlot: ChatSlot | undefined
  chatFolders: ChatFolder[]
  chatFoldersError: Error | null
  chatFoldersLoaded: boolean
  mobileNewSessionBusy: boolean
  mobileNewSessionInFlightRef: MutableRefObject<boolean>
  setMobileNewSessionBusy: Dispatch<SetStateAction<boolean>>
  setSidebarPinned: Dispatch<SetStateAction<boolean>>
  sidebarPinned: boolean
  railWidth: number
  winW: number
  effectiveSidebarWidth: number
  mobileSessions: boolean
  openSidebar: () => void
  closeSidebar: () => void
  previewExpanded: boolean
  sidebarOpen: boolean
  flyout: HoverIntent
  /** The composer state a memory-mode switch carries into the replacement session. */
  memorySwitch: Omit<MemorySwitchContext, 'dispatch' | 'slot' | 'mode' | 'showActionError'> & {
    /** Held true for the whole switch; the upload paths read it synchronously. */
    lockRef: MutableRefObject<boolean>
  }
}

/**
 * The flyout's New action, the side panel's reserve / maximised / fill widths, the
 * phone's sessions toggles and its "new chat here" item, and the composer's
 * memory-mode chip.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function usePanelLayout({
  mode, dispatch, isMobile, filteredSlots, activeSlot, isWelcomeState, showActionError, currentSlot, chatFolders, chatFoldersError, chatFoldersLoaded, mobileNewSessionBusy,
  mobileNewSessionInFlightRef, setMobileNewSessionBusy, setSidebarPinned, sidebarPinned, railWidth, winW, effectiveSidebarWidth, mobileSessions, openSidebar, closeSidebar, previewExpanded,
  sidebarOpen, flyout, memorySwitch,
}: PanelLayoutOptions) {
  const flyoutNew = useCallback(() => {
    flyout.close()
    // `focusComposerAfter`, not a bare dispatch + rAF: there is one composer and
    // it is bound to the ACTIVE slot, so focusing before creation fulfils puts
    // the caret on the old session and loses whatever is typed. See the module.
    // Agent-less: the server stamps the default custom agent (a template).
    focusComposerAfter(dispatch(createSlot({ mode: mode || '' })).unwrap())
  }, [dispatch, mode, flyout])

  // Force the list open when there is nothing in it, so a user with no sessions
  // still has the surface that creates one. Skipped while expand mode owns the
  // hidden state: re-pinning there would fight the auto-hide and, worse, persist
  // 'true' over the user's stored preference, which the restore on exit then
  // contradicts in the live state.
  useEffect(() => {
    if (filteredSlots.length === 0 && !sidebarPinned && !previewExpanded) {
      setSidebarPinned(true)
      safeSetItem('mc-sidebar-pinned', 'true')
    }
  }, [filteredSlots.length, sidebarPinned, previewExpanded, setSidebarPinned])

  // Horizontal space (px) the detail panel must keep clear so it never grows
  // past its flex row and collapses the chat pane: the open sidebar's width
  // plus a usable chat-pane minimum. On mobile the panel is full-screen (no
  // shared row), so no reserve applies.
  const CHAT_PANE_MIN = CHAT_PANE_MIN_W
  const panelReserve = isMobile ? undefined : (sidebarOpen ? effectiveSidebarWidth : 0) + CHAT_PANE_MIN
  // The panel takes its maximum only while the session list is actually hidden.
  // That maximum is measured against the header's reserve, which knows nothing
  // about the session list's width — so keeping it while the user reopens the
  // list inside expand mode pushes the chat pane below CHAT_PANE_MIN and clips
  // its content. Reverting to the normal width maths there costs the preview a
  // few hundred px in a state the user asked for by reopening the list.
  const panelMaximized = previewExpanded && !sidebarOpen

  // FILL vs BESIDE for the activity panel, decided from the width left for the
  // CHAT once the shell's hideable chrome is subtracted — the nav rail track and
  // the session sidebar (a shrink-0 flex sibling of exactly sidebarWidth; on
  // mobile its drawer is fixed-position and consumes no row width). Undefined =
  // beside. A px width = fill the chat column, squeezing the chat pane to zero
  // while the rail and sidebar stay exactly where they are.
  //
  // The panel's render PATH is unchanged either way, so crossing the threshold
  // never remounts it (no terminal re-attach, no Virtuoso churn) — only its
  // width changes. See sidePanelFillWidth for why this is loop-free.
  const panelFillWidth = sidePanelFillWidth({
    winW,
    railW: railWidth,
    sidebarW: !isMobile && sidebarOpen ? effectiveSidebarWidth : 0,
    isMobile,
  })

  // The mobile sessions toggle, rendered inline by whichever header owns the
  // surface's top-left: the single-chat title row, or in split view the grid's
  // top-left pane (SessionGridView `leading`). Mirrors the desktop toggle
  // exactly, state included: solid while the panel is hidden, light while it
  // is showing.
  const mobileSessionsToggle = (
    <button className="mc-touch-hit p-1 rounded-md text-muted hover:text-text cursor-pointer bg-transparent border-none pointer-events-auto shrink-0" onClick={() => mobileSessions ? closeSidebar() : openSidebar()} aria-label={i18nT('pages.chatPage.toggle_sessions')}>
      {mobileSessions ? <PanelLeftLight size={16} /> : <PanelLeftSolid size={16} />}
    </button>
  )
  /**
   * The same toggle for the shell's bar. `p-2` puts its 32px box at the bar's
   * 8px inset, so the glyph's ink lands on the 16px page gutter the nav logo
   * held before it (page-layout.md, "The title belongs to the content column").
   */
  const topbarSessionsToggle = (
    <button className="mc-touch-hit p-2 rounded-md text-text hover:text-text-strong hover:bg-bg-hover cursor-pointer bg-transparent border-none shrink-0" onClick={() => mobileSessions ? closeSidebar() : openSidebar()} aria-label={i18nT('pages.chatPage.toggle_sessions')} aria-expanded={mobileSessions} data-testid="mobile-topbar-sessions-toggle" {...uiLocation('chat.mobile-sessions-toggle')}>
      {mobileSessions ? <PanelLeftLight size={18} /> : <PanelLeftSolid size={18} />}
    </button>
  )

  // Mobile "new session here" (session menu item): creates a session in the SAME folder as
  // the one on screen, so starting a sibling chat does not require opening the
  // sessions drawer. Resolves agent and project like the drawer's folder "+"
  // (createChatInFolderMutation): nearest folder default_agent, then the global
  // default; nearest folder project_dir; a stale folder project dir gets the drawer's own message
  // (isStaleProjectDirError). The create itself stays here rather than in
  // createChatInFolderMutation on purpose: that mutation lives inside
  // ChatSidebar with board-column, tab and attempt state this button has none
  // of. An unfiled session creates an unfiled sibling, like the flyout's
  // New. A filed session waits for the folder list: resolving against a
  // pending or failed query would silently drop the folder's agent and project.
  const mobileNewFolderId = currentSlot?.folder_id || null
  const mobileNewFolder = mobileNewFolderId ? chatFolders.find(f => f.id === mobileNewFolderId) : undefined
  // Disabled only while the folder list is still loading, so a tap cannot
  // create the session before the folder's agent and project are known. Once
  // the list has settled the button is live: a failed load is reported on tap
  // (instead of leaving a dead button a touch user gets no tooltip for), and a
  // folder id that no longer names a folder is treated as unfiled, the way the
  // drawer files such a session.
  const mobileNewSessionReady = !mobileNewFolderId || chatFoldersLoaded || !!chatFoldersError
  const mobileNewSessionHere = async () => {
    if (!mobileNewSessionReady || mobileNewSessionInFlightRef.current) return
    // One notice for both failures below. The body stays the raw error text,
    // which is the key the error journal matches for the notice's ask-agent
    // hand-off; the title names what failed.
    const fail = (error: unknown, title = i18nT('pages.chatPage.could_not_start_a_new_session')) =>
      showActionError(errMessage(error) || i18nT('pages.chatPage.unknown_error'), title)
    if (mobileNewFolderId && !chatFoldersLoaded) return fail(chatFoldersError)
    const folderId = mobileNewFolder ? mobileNewFolderId : ''
    // Only a folder's own pin names an agent; otherwise the server stamps the
    // default custom agent (a template), never the default crewmate alias.
    const agent = folderId ? resolveFolderAgent(chatFolders, folderId, '') : undefined
    const project = folderId ? resolveFolderProjectDir(chatFolders, folderId) : undefined
    mobileNewSessionInFlightRef.current = true
    setMobileNewSessionBusy(true)
    try {
      await dispatch(createSlot({
        agent,
        mode: mode || '',
        ...(folderId ? { folder_id: folderId } : {}),
        ...(project ? { project } : {}),
      })).unwrap()
    } catch (error) {
      // Same stale-project-dir sentence as the drawer's folder `+`, as the
      // notice title: it is the one create failure the user can fix, so name
      // the missing path.
      return fail(error, folderId && isStaleProjectDirError(error)
        ? i18nT('pages.chatSidebar.folder_project_dir_missing', { path: project ?? '' })
        : undefined)
    } finally {
      mobileNewSessionInFlightRef.current = false
      setMobileNewSessionBusy(false)
    }
    // Touch devices skip focus by design (no keyboard pop); this only matters
    // for a narrow desktop window, where the caret should follow the create.
    focusComposer()
  }
  const mobileNewSessionLabel = mobileNewFolder
    ? i18nT('pages.chatSidebar.new_chat_in_name', { name: mobileNewFolder.name })
    : i18nT('pages.chat.sessionFlyout.new_chat')
  // The item leads the session (chevron) menu on the phone. It is not a third
  // control beside the sessions toggle: the bar's centre cell holds two
  // (AUTOSDE `max-two-buttons-per-row`, narrow-viewport.md).
  const mobileNewSessionItem = {
    label: mobileNewSessionLabel,
    disabled: !mobileNewSessionReady || mobileNewSessionBusy,
    onSelect: () => { void mobileNewSessionHere() },
  }

  // A memory-mode switch in flight. The composer is disabled meanwhile so the
  // draft and uploads it carries cannot change under it; the ref is the
  // synchronous read for upload paths and a second click on the chip.
  const [memorySwitching, setMemorySwitching] = useState(false)
  const memorySwitchingRef = memorySwitch.lockRef
  // An optimize in flight on the session on screen. A switch started now
  // would carry the pre-optimize text and deliver the rewrite to the session
  // it deletes, so the chip waits for the optimize to settle.
  const [composerOptimizing, setComposerOptimizing] = useState(false)
  const composerOptimizingRef = useRef(false)
  const handleOptimizingChange = useCallback((optimizing: boolean) => {
    composerOptimizingRef.current = optimizing
    setComposerOptimizing(optimizing)
  }, [])

  // Unchanged from the inline WelcomeView handler; shared with the composer memory chip.
  const switchMemoryMode = async (newMode: MemoryMode) => {
    if (!activeSlot || memorySwitchingRef.current || composerOptimizingRef.current) return
    const { lockRef: _lockRef, ...composer } = memorySwitch
    const { composerMountedRef } = composer
    // The composer is locked for the whole switch (see `memorySwitching`):
    // nothing can be typed, pasted or attached in either session until it
    // settles, so the draft and uploads are moved exactly once below.
    memorySwitchingRef.current = true
    setMemorySwitching(true)
    const unlock = () => {
      memorySwitchingRef.current = false
      if (composerMountedRef.current) setMemorySwitching(false)
    }
    // Bounded: a create or switch that never settles must not leave every
    // composer locked. On timeout the lock lifts and the switch, whenever it
    // settles, keeps both sessions and carries nothing.
    const attempt = { timedOut: false }
    const timer = setTimeout(() => {
      attempt.timedOut = true
      unlock()
      showActionError(i18nT('components.welcomeView.memory_switch_timed_out'))
    }, memorySwitchTimeoutMs())
    let handedOver = false
    try {
      const { runMemorySwitch } = await import('../memoryModeSwitch')
      handedOver = await runMemorySwitch({
        ...composer, dispatch, slot: currentSlot, mode,
        showActionError: message => showActionError(message),
      }, activeSlot, newMode, attempt)
    } catch (error) {
      // The module failed to load (an offline reload of a stale tab).
      if (!attempt.timedOut) showActionError(errMessage(error) || i18nT('pages.chatPage.unknown_error'))
    } finally {
      clearTimeout(timer)
      // A timed-out attempt already unlocked; a newer switch may hold the lock now.
      if (!attempt.timedOut) unlock()
    }
    // Deleted after the unlock: the user is already in the replacement.
    if (handedOver) {
      try { await dispatch(deleteSlot(activeSlot)).unwrap() } catch (error) {
        // A rejected delete leaves the moved uploads with the replacement,
        // where the user is, and the old slot keeps its own later work.
        showActionError(errMessage(error) || i18nT('pages.chatPage.unknown_error'))
      }
    }
  }
  // The welcome screen puts its memory chip directly above the composer.
  const showComposerMemoryChip = isWelcomeState
  return {
    flyoutNew, panelReserve, panelMaximized, panelFillWidth, mobileSessionsToggle, topbarSessionsToggle, mobileNewSessionItem, switchMemoryMode, showComposerMemoryChip,
    memorySwitching, composerOptimizing, handleOptimizingChange,
  }
}
