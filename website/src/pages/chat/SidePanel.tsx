import { useState, useRef, useEffect, useCallback, useMemo, type ReactNode } from 'react'
import { useIsMobile } from '../../hooks/useIsMobile'
import { usePointerDrag } from '../../hooks/usePointerDrag'
import { Reorder } from 'framer-motion'
import { FileText, Bot, Workflow, ScrollText, MessageSquare, TerminalSquare, GitCompare, GitPullRequest, Package, Plus, X, Hash, Pen, Columns2, PanelRightClose, Component, PanelBottom, Globe } from 'lucide-react'
import ActivityViewer from './ActivityViewer'
import DiffPanel from '../../components/DiffPanel'
import DetailPanel from '../../components/DetailPanel'
import MarkdownPanel from '../../components/MarkdownPanel'
import ArtifactPanel from '../../components/ArtifactPanel'
import WebPreviewPanel from '../../components/WebPreviewPanel'
import CliPanel, { disposeTerminalSession, useDeleteTerminalSession } from '../../components/CliPanel'
import { countLines } from '../../components/FileChangeChips'
import { useTerminalEnabled, useTerminalTitle } from '../../utils/terminalRegistry'
import { adoptTab as adoptBottomTerminal } from '../../hooks/useBottomTerminal'
import type { usePanelTabs, ViewKind, PanelTab, TabKind } from '../../hooks/usePanelTabs'
import { PINNED_VIEWS } from '../../hooks/usePanelTabs'
import { usePersistedBool } from '../../hooks/usePersistedBool'
import { useListboxKeyboard } from '../../hooks/useListboxKeyboard'
import { safeSetItem } from '../../utils/safeStorage'
import type { SubagentActivity, ToolActivity } from '../../types'
import type { TouchedFile } from '../../hooks/useTouchedFiles'
import type { ExtractedLink } from '../../utils/extractChatLinks'
import type { PullRequestLink } from '../../utils/pullRequestLinks'

import { i18nT } from '../../i18n/t'
const KIND_ICON: Record<TabKind, ReactNode> = {
  changes: <GitPullRequest size={16} />, files: <FileText size={16} />, artifacts: <Component size={16} />, subagents: <Bot size={16} />, workflows: <Workflow size={16} />,
  logs: <ScrollText size={16} />, side: <MessageSquare size={16} />, terminal: <TerminalSquare size={16} />, browser: <Globe size={16} />,
  file: <FileText size={16} />, diff: <GitCompare size={16} />, artifact: <Package size={16} />,
}

/** Views offered by the + menu. */
const NEW_MENU: { kind: ViewKind | 'terminal'; label: string; icon: ReactNode; desc: string }[] = [
  { kind: 'changes', label: 'Changes', icon: <GitPullRequest size={15} />, desc: 'Pull requests, checks & reviews' },
  { kind: 'files', label: 'Files', icon: <FileText size={15} />, desc: 'Browse & edit files' },
  { kind: 'artifacts', label: 'Artifacts', icon: <Component size={15} />, desc: 'In-session documents & stars' },
  { kind: 'subagents', label: 'Subagents', icon: <Bot size={15} />, desc: 'Live agent activity & transcripts' },
  { kind: 'workflows', label: 'Workflows', icon: <Workflow size={15} />, desc: 'Runs, phases & restartable steps' },
  { kind: 'logs', label: 'Logs', icon: <ScrollText size={15} />, desc: 'Gateway log stream' },
  { kind: 'side', label: 'Side', icon: <MessageSquare size={15} />, desc: 'Parallel chat, shared context' },
  { kind: 'browser', label: 'Browser', icon: <Globe size={15} />, desc: 'View a web page or a local dev server' },
  { kind: 'terminal', label: 'Terminal', icon: <TerminalSquare size={15} />, desc: 'Shell on the gateway host' },
]

const VIEW_KINDS = new Set<TabKind>(['changes', 'files', 'artifacts', 'subagents', 'workflows', 'logs', 'side'])

interface SidePanelProps {
  tabsCtl: ReturnType<typeof usePanelTabs>
  subagents: Record<string, SubagentActivity>
  toolLog: ToolActivity[]
  slot: string
  files?: TouchedFile[]
  onFileOpen?: (path: string, opts?: { replaceId?: string }) => void
  onFileRemove?: (path: string) => void
  onFilesClear?: (source: 'history' | 'tool') => void
  projectDir?: string
  navLinks?: ExtractedLink[]
  navResolving?: boolean
  sources?: PullRequestLink[]
  selectedSourceUrl?: string
  onSelectSource?: (url: string) => void
  onAddSourceToChat?: (text: string) => void
  onSubmitComments?: (message: string) => void
  onFileSave: (filePath: string, content: string) => Promise<void>
  /** Close the whole panel (hides the side column). */
  onClose: () => void
  /** Lifted Files-tab inline preview state (owned by ChatPage so it survives
   *  panel collapse and coordinates with document-tab opens). */
  inlinePreviewPath?: string | null
  onInlinePreviewChange?: (path: string | null) => void
  /** Preview "focus" mode: when true the panel takes its maximum width (chat
   *  shrinks to its minimum), driven by the Web Preview tab's expand toggle. */
  expanded?: boolean
  /** FILL mode (set by ChatPage): an explicit px width covering the whole chat
   *  column, used when the space left after the nav rail and session sidebar
   *  cannot seat the panel BESIDE a usable chat pane. Overrides the responsive
   *  clamp and the user's persisted width, and retires the resize handle —
   *  there is nothing to resize against. Undefined = beside mode. */
  fillWidth?: number
}

/**
 * Tabbed side panel. One strip holds singleton view tabs
 * (Changes / Files / Subagents / Workflows / Logs / Side / Terminal, opened
 * from +) and document tabs (file / diff / artifact, opened on demand — file
 * chips, the Files picker, artifact refs). Each tab renders its own body;
 * documents live as tabs instead of replacing the panel.
 */
/** Panel minimum width (also the resize handle's lower clamp). */
export const SIDE_PANEL_MIN_W = 320
/**
 * Space reserved to the panel's left so the chat column never collapses:
 * the app nav rail (up to 220px expanded) plus a working minimum for the
 * chat column itself. The panel's effective width shrinks before eating
 * into this; when even SIDE_PANEL_MIN_W no longer fits beside it, ChatPage
 * auto-collapses the panel (and reopens it when space returns).
 */
export const SIDE_PANEL_RESERVED_W = 560

/**
 * Live minimum space the panel must leave to its left. The static reserve
 * only budgets the content row (nav rail + chat minimum) — but the actbar
 * grid column shortens the header row too, and the header's clusters
 * (branding + Request a Feature on the left; readout capsule + bell on the
 * right) can need more than 560px when the capsule is expanded. Without
 * accounting for that, the panel overlapped the bell/capsule before it
 * started shrinking. Returns the larger of the two constraints; falls back
 * to the static reserve when there's no header (embed/popout frames).
 */
/** Usable minimum for the chat pane itself, beside the panel. */
export const CHAT_PANE_MIN_W = 320

/**
 * Decide the panel's mode from the width left for the CHAT, not from the
 * viewport: subtract the shell's hideable chrome (nav rail track, session
 * sidebar) and ask whether the remainder seats the panel at SIDE_PANEL_MIN_W
 * beside a CHAT_PANE_MIN_W chat pane.
 *
 * Returns `undefined` for BESIDE mode, or the px width the panel should take to
 * FILL the chat column. Mobile always fills (its viewport cannot seat both, and
 * its sidebar is a fixed-position drawer that consumes no row width).
 *
 * Pure and loop-free on purpose: every input is a shell-level fact that does
 * NOT change when the panel opens. Feeding it the chat container's painted
 * width instead would oscillate, since opening the panel shrinks that width.
 */
export function sidePanelFillWidth(
  { winW, railW, sidebarW, isMobile }:
  { winW: number; railW: number; sidebarW: number; isMobile: boolean },
): number | undefined {
  if (isMobile) return Math.max(SIDE_PANEL_MIN_W, winW)
  const chatAvail = winW - railW - sidebarW
  if (chatAvail >= SIDE_PANEL_MIN_W + CHAT_PANE_MIN_W) return undefined
  return Math.max(SIDE_PANEL_MIN_W, chatAvail)
}

/**
 * Resolve the panel's rendered width.
 *
 * Order matters. FILL (an explicit px width) wins over the mobile percentage:
 * `width: 100%` is unreliable because the inline (non-portal) render path wraps
 * the panel in a shrink-to-fit `width: auto` flex item, and a percentage child
 * cannot resolve against an auto containing block during intrinsic sizing — the
 * browser falls back to the panel's own max-content width, so the panel comes
 * out narrow and the chat pane keeps the rest. An explicit px width is
 * deterministic in BOTH paths. '100%' survives only as the fallback for a
 * mobile frame that somehow receives no fillWidth.
 */
export function sidePanelEffectiveWidth(
  { fillWidth, isMobile, expanded, width, maxW }:
  { fillWidth?: number; isMobile: boolean; expanded?: boolean; width: number; maxW: number },
): number | string {
  if (fillWidth != null) return fillWidth
  if (isMobile) return '100%'
  if (expanded) return Math.max(SIDE_PANEL_MIN_W, maxW)
  return Math.max(SIDE_PANEL_MIN_W, Math.min(width, maxW))
}

export function measureSidePanelReservedW(): number {
  const header = document.querySelector('header.topbar-glass')
  if (!header) return SIDE_PANEL_RESERVED_W
  const clusters = Array.from(header.children).filter(
    c => c.tagName !== 'A' && !c.hasAttribute('data-topbar-overlay'),
  ) as HTMLElement[]
  const content = clusters.reduce((sum, c) => sum + c.getBoundingClientRect().width, 0)
  const cs = getComputedStyle(header as HTMLElement)
  const pad = (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0)
  // +24: minimum breathing gap between the two clusters.
  return Math.max(SIDE_PANEL_RESERVED_W, Math.ceil(content + pad + 24))
}

export default function SidePanel({
  tabsCtl, subagents, toolLog, slot, files, onFileOpen, onFileRemove, onFilesClear,
  projectDir, navLinks, navResolving, sources, selectedSourceUrl, onSelectSource,
  onAddSourceToChat, onSubmitComments, onFileSave, onClose,
  inlinePreviewPath, onInlinePreviewChange, expanded, fillWidth,
}: SidePanelProps) {
  const { tabs, activeId, openView, openTerminal, setActive, closeTab, patchTab, setOrder, syncPinned } = tabsCtl
  const terminalEnabled = useTerminalEnabled()
  // The + menu / empty-state launcher hide Terminal when the feature is
  // disabled server-side, and never list the auto-managed pinned views
  // (Changes / Files / Artifacts) — those appear on their own when they have
  // content (see the syncPinned reconcile below).
  const menuItems = NEW_MENU.filter(item =>
    (terminalEnabled || item.kind !== 'terminal')
    && !(PINNED_VIEWS as string[]).includes(item.kind)
  )
  // Files / Artifacts / Changes are ALWAYS present — pinned to the front,
  // non-closable, and never in the + menu — regardless of whether they
  // currently have content.
  useEffect(() => { syncPinned(PINNED_VIEWS) }, [syncPinned])
  // Split the strip: pinned (fixed, non-closable) vs. dynamic (draggable).
  const pinnedTabs = useMemo(() => tabs.filter(t => (PINNED_VIEWS as string[]).includes(t.id)), [tabs])
  const dynamicTabs = useMemo(() => tabs.filter(t => !(PINNED_VIEWS as string[]).includes(t.id)), [tabs])
  // Paths already open as `file:` document tabs — passed to the Files view so
  // opening such a path inline routes to its existing tab (one editor per path).
  const openDocPaths = useMemo(() => new Set(tabs.filter(t => t.kind === 'file' && t.path).map(t => t.path as string)), [tabs])
  // Terminal opens a NEW tab (its own PTY session) starting in the chat's
  // working dir; every other menu item is a singleton view.
  const openMenuItem = useCallback((kind: ViewKind | 'terminal') => {
    if (kind === 'terminal') openTerminal({ cwd: projectDir })
    else openView(kind)
  }, [openTerminal, openView, projectDir])
  // Closing a terminal tab kills its PTY (server) and disposes local state. The
  // server delete goes through a React Query mutation (use-react-query
  // guideline); the synchronous WS + xterm teardown stays in disposeTerminalSession.
  const deleteTerminalSession = useDeleteTerminalSession()
  const handleCloseTab = useCallback((id: string) => {
    const t = tabs.find(x => x.id === id)
    if (t?.kind === 'terminal' && t.sessionId) {
      deleteTerminalSession.mutate(t.sessionId)
      disposeTerminalSession(t.sessionId)
    }
    closeTab(id)
  }, [tabs, closeTab, deleteTerminalSession])
  // Move a terminal tab OUT of this chat into the app-wide bottom panel. Unlike
  // handleCloseTab this must NOT dispose the session — the PTY + xterm live in
  // terminalRegistry/termCache keyed by session id and simply re-attach in the
  // bottom panel. Only drop it from this chat once the panel accepts it.
  const handleTransferToBottom = useCallback((id: string) => {
    const t = tabs.find(x => x.id === id)
    if (t?.kind !== 'terminal' || !t.sessionId) return
    if (adoptBottomTerminal(t.sessionId, t.cwd)) closeTab(id)
  }, [tabs, closeTab])
  const [menuOpen, setMenuOpen] = useState(false)
  // Diff view preferences — persisted; 'mc-diff-split' is shared with the
  // file view's git-diff toggle so split/unified is one app-wide preference.
  const [diffLineNumbers, setDiffLineNumbers] = usePersistedBool('mc-diff-linenums', false)
  const [diffSideBySide, setDiffSideBySide] = usePersistedBool('mc-diff-split', true)
  const menuRef = useRef<HTMLDivElement>(null)
  // Keyboard operability for the + menu (WAI-ARIA menu pattern): roving focus
  // across the items on open, ArrowUp/Down + Home/End, Escape/Tab closes and
  // returns focus to the trigger. Shared hook with StyledSelect/AgentSelector.
  const menuTriggerRef = useRef<HTMLButtonElement>(null)
  const menuListRef = useRef<HTMLDivElement>(null)
  const menuNoInputRef = useRef<HTMLElement>(null)
  const closeMenuToTrigger = useCallback(() => {
    setMenuOpen(false)
    menuTriggerRef.current?.focus()
  }, [])
  const { onListKeyDown: onMenuKeyDown } = useListboxKeyboard({
    open: menuOpen,
    dropdownRef: menuListRef,
    inputRef: menuNoInputRef,
    hasFilterInput: false,
    filteredCount: menuItems.length,
    onEnterSingleMatch: () => {},
    closeToTrigger: closeMenuToTrigger,
  })

  // Resizable width (the actbar grid column is auto-sized, so the panel owns
  // its own width — mirrors the old DetailPanel resize handle).
  const WIDTH_KEY = 'mc-side-panel-width'
  const MIN_W = SIDE_PANEL_MIN_W
  const [width, setWidth] = useState(() => {
    const v = parseInt(localStorage.getItem(WIDTH_KEY) || '', 10)
    return !isNaN(v) && v >= MIN_W ? v : 460
  })
  const widthRef = useRef(width); widthRef.current = width
  // Responsive clamp: the user's chosen width is persisted untouched, but the
  // rendered width yields to the window so the chat keeps its reserved
  // minimum. On mobile the panel simply takes the full width. Re-measured on
  // window resize AND when the header clusters change size (e.g. the readout
  // capsule expanding), since the header's content need is part of the reserve.
  const isMobile = useIsMobile()
  const [maxW, setMaxW] = useState(() => window.innerWidth - measureSidePanelReservedW())
  useEffect(() => {
    const recalc = () => setMaxW(window.innerWidth - measureSidePanelReservedW())
    recalc()
    window.addEventListener('resize', recalc)
    // Observe the header's clusters (their intrinsic width is independent of
    // the panel's own width, so this can't feed back into itself).
    const header = document.querySelector('header.topbar-glass')
    const ro = new ResizeObserver(recalc)
    if (header) Array.from(header.children)
      .filter(c => !c.hasAttribute('data-topbar-overlay'))
      .forEach(c => ro.observe(c))
    return () => { window.removeEventListener('resize', recalc); ro.disconnect() }
  }, [])
  const effectiveWidth = sidePanelEffectiveWidth({ fillWidth, isMobile, expanded, width, maxW })
  // While the user drags the resize handle, every mousemove shifts the whole
  // panel's viewport position (the handle is on the LEFT edge; the right edge
  // is pinned to the window). Framer's layout projection on each Reorder.Item
  // sees the chips' screen positions change and spring-animates them toward
  // the new spot each frame — so tabs visibly lag the resize and "catch up"
  // when it stops. During a resize we make the layout transition instant.
  const [resizing, setResizing] = useState(false)
  const startWRef = useRef(0)
  const panelResize = usePointerDrag({
    threshold: 0,
    onStart: () => { startWRef.current = widthRef.current; setResizing(true) },
    onMove: ({ dx }) => {
      // Left-edge handle with the right edge pinned: dragging left (dx < 0) widens.
      const max = Math.min(Math.round(window.innerWidth * 0.7), window.innerWidth - measureSidePanelReservedW())
      setWidth(Math.max(MIN_W, Math.min(startWRef.current - dx, max)))
    },
    onEnd: () => { setResizing(false); safeSetItem(WIDTH_KEY, String(widthRef.current)) },
  })

  useEffect(() => {
    if (!menuOpen) return
    const onDown = (e: MouseEvent) => { if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false) }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [menuOpen])

  return (
    <div className="shrink-0 min-h-0 my-2 flex flex-col bg-bg overflow-hidden relative border-l border-t border-b border-border rounded-l-xl" style={{ width: effectiveWidth, maxWidth: '100vw' }}>
      {/* Left-edge resize handle */}
      {fillWidth == null && <div role="separator" aria-orientation="vertical" aria-label={i18nT('pages.chat.sidePanel.resize_panel')} className="absolute left-0 top-0 bottom-0 w-[6px] cursor-col-resize z-30 group/drag" style={{ touchAction: 'none' }} {...panelResize}>
        <div className="absolute left-0 top-0 bottom-0 w-[2px] transition-colors duration-200 bg-transparent group-hover/drag:bg-accent resize-accent" />
      </div>}
      {/* Tab strip — drag chips horizontally to reorder (framer Reorder).
          Per Figma "left-nav" (7328:10637): the row is a rounded elevated card
          (bg-elevated, 12px radius, 8px padding) floating above the content,
          not a flat bordered bar. side-panel-strip punches the strip out of the
          Electron window-drag region (see index.css) so chips receive events. */}
      <div className="side-panel-strip flex items-center gap-1.5 shrink-0 p-2 rounded-tl-xl bg-bg-elevated">
        {/* Collapse the panel (far-left), separated from the tabs by a hairline. */}
        <button
          className="flex items-center justify-center w-7 h-7 rounded-md text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0"
          onClick={onClose}
          title={i18nT('pages.chat.sidePanel.close_panel')}
          aria-label={i18nT('pages.chat.sidePanel.close_panel')}
        >
          <PanelRightClose size={15} />
        </button>
        <span aria-hidden="true" className="w-px h-5 bg-border shrink-0" />
        {/* Pinned views (Changes / Files / Artifacts): always present, fixed at
            the front, non-closable, not draggable, compact. Wrapped in a
            tight-gap group so the three sit closer together than the strip's
            default spacing. */}
        <div className="flex items-center gap-1.5 shrink-0">
          {pinnedTabs.map(t => (
            <TabChip key={t.id} tab={t} active={t.id === activeId} closable={false} pinned onSelect={() => setActive(t.id)} onClose={() => {}} />
          ))}
        </div>
        {pinnedTabs.length > 0 && dynamicTabs.length > 0 && (
          <span aria-hidden="true" className="w-px h-5 bg-border shrink-0" />
        )}
        <Reorder.Group
          axis="x"
          values={dynamicTabs}
          onReorder={(next) => setOrder([...pinnedTabs, ...next])}
          role="tablist"
          className="flex items-center gap-2 flex-1 min-w-0 overflow-x-auto scrollbar-none list-none m-0 p-0"
        >
          {dynamicTabs.map((t, i) => (
            <Reorder.Item
              key={t.id}
              value={t}
              className="relative shrink-0 list-none"
              // Reorder.Item's layout prop can't be disabled (true | "position"
              // only) — instead make the layout correction instant while
              // resizing so chips track the panel edge 1:1. Otherwise use a
              // tight spring (high stiffness, near-critical damping) so the
              // reorder shuffle snaps into place instead of floating.
              transition={resizing ? { duration: 0 } : { type: 'spring', stiffness: 700, damping: 45 }}
            >
              {/* Chrome-style separator: hairline between adjacent chips,
                  suppressed on both edges of the selected tab (its pill
                  background already delineates it). Centered in the gap-2. */}
              {i > 0 && t.id !== activeId && dynamicTabs[i - 1].id !== activeId && (
                <span aria-hidden="true" className="absolute -left-[4.5px] top-1/2 -translate-y-1/2 w-px h-4 bg-border" />
              )}
              <TabChip tab={t} active={t.id === activeId} onSelect={() => setActive(t.id)} onClose={() => handleCloseTab(t.id)} onTransfer={t.kind === 'terminal' ? () => handleTransferToBottom(t.id) : undefined} />
            </Reorder.Item>
          ))}
        </Reorder.Group>
        {/* + menu */}
        <div className="relative shrink-0" ref={menuRef}>
          <button
            ref={menuTriggerRef}
            className="flex items-center justify-center w-7 h-7 rounded-md text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer"
            onClick={() => setMenuOpen(v => !v)}
            title={i18nT('pages.chat.sidePanel.open_side_panel_tab')}
            aria-label={i18nT('pages.chat.sidePanel.open_side_panel_tab')}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
          >
            <Plus size={15} />
          </button>
          {menuOpen && (
            <div
              ref={menuListRef}
              role="menu"
              onKeyDown={onMenuKeyDown}
              className="absolute top-9 right-0 z-50 min-w-[200px] py-1.5 rounded-xl bg-bg-elevated border border-border shadow-lg animate-rise"
            >
              {menuItems.map(item => (
                <button
                  key={item.kind}
                  role="menuitem"
                  data-option
                  tabIndex={-1}
                  className="flex items-center gap-2.5 w-full px-3 py-2 text-[13px] text-text hover:bg-bg-hover focus:bg-bg-hover focus:outline-none transition-colors bg-transparent border-none cursor-pointer text-left"
                  onClick={() => { openMenuItem(item.kind); closeMenuToTrigger() }}
                >
                  <span className="text-muted shrink-0">{item.icon}</span>
                  <span className="flex-1">{item.label}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Body — render every doc/terminal tab mounted (hidden when inactive) so
          xterm sessions and editor scroll state survive tab switches; category
          views mount only when active (cheap + query-driven). */}
      {/* Content area: left + top border (square corner) so the border wraps
          only the content, NOT the tab strip above (which stays borderless). */}
      <div className="flex-1 min-h-0 relative">
        {tabs.length === 0 && (
          /* Empty state: launcher — the available views themselves, roomy and
             clickable, instead of a hint pointing at the + menu. */
          <div className="flex items-center justify-center h-full px-6">
            <div className="flex flex-col items-center gap-4 w-full max-w-[420px]">
              <div className="text-[22px] text-muted font-semibold">{i18nT('pages.chat.sidePanel.pick_a_panel_to_view')}</div>
              <div className="grid grid-cols-2 gap-2.5 w-full">
              {menuItems.map(item => {
                // Live badges from data already flowing into the panel — a
                // quiet accent pill when non-zero, muted otherwise.
                const badge = item.kind === 'files' && files && files.length > 0
                  ? `${files.length} touched`
                  : item.kind === 'subagents' && Object.values(subagents).some(s => s.status === 'running' || s.status === 'tool')
                    ? `${Object.values(subagents).filter(s => s.status === 'running' || s.status === 'tool').length} running`
                    : item.kind === 'logs' && toolLog.length > 0
                      ? `${toolLog.length} calls`
                      : null
                return (
                  <button
                    key={item.kind}
                    className="flex flex-col items-start gap-1.5 px-3.5 py-3 rounded-xl border border-border bg-transparent hover:bg-bg-hover hover:border-border-strong text-left cursor-pointer transition-colors"
                    onClick={() => openMenuItem(item.kind)}
                  >
                    <div className="flex items-center gap-2.5 w-full text-text">
                      <span className="shrink-0 opacity-80">{item.icon}</span>
                      <span className="text-[13px] font-medium">{item.label}</span>
                      {badge && (
                        <span className="ml-auto text-[10px] px-2 py-0.5 rounded-full bg-accent/12 text-accent font-medium shrink-0">{badge}</span>
                      )}
                    </div>
                    <div className="text-[11px] text-muted leading-snug">{item.desc}</div>
                  </button>
                )
              })}
              </div>
            </div>
          </div>
        )}
        {tabs.map(t => {
          const isActive = t.id === activeId
          // Category views: mount only the active one. The Files tab hosts an
          // inline file editor, but it does NOT need to stay mounted while
          // inactive — an in-progress edit survives a tab switch via the
          // module-level draft store, and which file is open inline is owned by
          // ChatPage (both above the SidePanel subtree). Keeping it mounted-but-
          // hidden would leave its global Escape handler live, letting an
          // invisible editor swallow Escape and drop the draft; so unmount it.
          // Cross-slot safety: the inline-preview path is reset by ChatPage when
          // the active chat slot changes.
          if (VIEW_KINDS.has(t.kind)) {
            if (!isActive) return null
            return (
              <div key={t.id} className="absolute inset-0">
                <ActivityViewer
                  view={t.kind as 'changes' | 'files' | 'artifacts' | 'subagents' | 'workflows' | 'logs' | 'side'}
                  open onToggle={onClose} slot={slot}
                  subagents={subagents} toolLog={toolLog}
                  files={files}
                  sources={sources}
                  selectedSourceUrl={selectedSourceUrl}
                  onSelectSource={onSelectSource}
                  onAddToChat={onAddSourceToChat}
                  // The Files/Artifacts/Changes tabs are permanent (pinned).
                  // Files opens its file inline (kept in the Files tab, with a
                  // back button); the Artifacts tab still opens document tabs
                  // via onFileOpen.
                  onFileOpen={onFileOpen}
                  onFileRemove={onFileRemove} onFilesClear={onFilesClear}
                  onFileSave={onFileSave} onSubmitComments={onSubmitComments}
                  openDocPaths={openDocPaths}
                  previewPath={inlinePreviewPath ?? null} onPreviewPathChange={onInlinePreviewChange}
                  projectDir={projectDir} navLinks={navLinks} navResolving={navResolving}
                />
              </div>
            )
          }
          // Terminal + documents: keep mounted, toggle visibility.
          return (
            <div key={t.id} className="absolute inset-0" style={{ display: isActive ? 'block' : 'none' }}>
              <TabBody
                tab={t} active={isActive}
                slot={slot}
                onClose={() => handleCloseTab(t.id)}
                onContentChange={(c) => patchTab(t.id, { content: c })}
                onDiffModeChange={(diffMode) => patchTab(t.id, { diffMode })}
                onFileSave={onFileSave}
                onFileOpen={onFileOpen}
                onSubmitComments={onSubmitComments}
                onTerminalSendToChat={onAddSourceToChat}
                diffLineNumbers={diffLineNumbers}
                setDiffLineNumbers={setDiffLineNumbers}
                diffSideBySide={diffSideBySide}
                setDiffSideBySide={setDiffSideBySide}
              />
            </div>
          )
        })}
      </div>
    </div>
  )
}

/** Body renderer for terminal + document tabs. Module-scope (NOT nested inside
 *  SidePanel): a nested component definition would produce a new component
 *  type on every SidePanel render, forcing React to unmount/remount the whole
 *  subtree — which reset editor state and re-fired xterm's focus-on-visible
 *  effect, stealing focus from the chat input on every keystroke. */
function TabBody({ tab, active, slot, onClose, onContentChange, onDiffModeChange, onFileSave, onFileOpen, onSubmitComments, onTerminalSendToChat, diffLineNumbers, setDiffLineNumbers, diffSideBySide, setDiffSideBySide }: {
  tab: PanelTab; active: boolean; slot: string
  onClose: () => void
  onContentChange: (c: string) => void
  onDiffModeChange: (diffMode: boolean) => void
  onFileSave: (fp: string, c: string) => Promise<void>
  onFileOpen?: (p: string) => void
  onSubmitComments?: (m: string) => void
  onTerminalSendToChat?: (text: string) => void
  diffLineNumbers: boolean; setDiffLineNumbers: (fn: (v: boolean) => boolean) => void
  diffSideBySide: boolean; setDiffSideBySide: (fn: (v: boolean) => boolean) => void
}) {
  if (tab.kind === 'terminal') return <CliPanel sessionId={tab.sessionId ?? ''} cwd={tab.cwd} visible={active} onSendToChat={onTerminalSendToChat} />
  if (tab.kind === 'browser') return <WebPreviewPanel sessionKey={slot} active={active} />
  if (tab.kind === 'file') {
    return (
      <MarkdownPanel
        embedded
        filePath={tab.path || ''}
        content={tab.content || ''}
        onContentChange={onContentChange}
        initialDiffMode={tab.diffMode}
        onDiffModeChange={onDiffModeChange}
        onSave={onFileSave}
        onClose={onClose}
        liveWatch
        onSubmitComments={onSubmitComments}
      />
    )
  }
  if (tab.kind === 'artifact') {
    return (
      <ArtifactPanel
        embedded
        slug={tab.artifactSlug || ''}
        kind={tab.artifactKind || 'markdown'}
        content={tab.content || ''}
        onClose={onClose}
        onSubmitComments={onSubmitComments}
      />
    )
  }
  if (tab.kind === 'diff') {
    const { added, removed } = countLines(tab.original || '', tab.modified || '')
    return (
      <DetailPanel
        embedded
        title={tab.title}
        onClose={onClose}
        noPadding
        customHeader={
          // Minimal single-bar toolbar: the tab chip owns identity + close, so the
          // bar carries breadcrumb (click → open editor), change stats, and the
          // two view controls. Divider to content lives on the bar's border-b.
          <div className="flex items-center gap-2 h-[38px] px-3 shrink-0 border-b border-border">
            <button className="text-[12px] text-text-strong truncate hover:text-accent cursor-pointer transition-colors bg-transparent border-none p-0" onClick={() => { onFileOpen?.(tab.path || '') }} title={`Open ${tab.path || ''} in editor`}>
              {/* Bare filename: the tab title carries '- Diff', which would
                  read redundantly next to the Turn Diff badge here. */}
              {(tab.path || '').split('/').pop() || tab.title}
            </button>
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-accent/15 text-accent font-medium shrink-0">{i18nT('pages.chat.sidePanel.turn_diff')}</span>
            {(added > 0 || removed > 0) && <span className="text-[11px] font-mono font-semibold shrink-0">{added > 0 && <span className="text-ok">+{added}</span>}{removed > 0 && <span className="text-danger ml-1.5">-{removed}</span>}</span>}
            <span className="flex-1" />
            <button onClick={() => onFileOpen?.(tab.path || '')} className="flex items-center justify-center w-[26px] h-[26px] rounded-md cursor-pointer transition-colors text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none" title={i18nT('pages.chat.sidePanel.open_in_editor')} aria-label={i18nT('pages.chat.sidePanel.open_in_editor')}><Pen size={14} /></button>
            <button onClick={() => setDiffSideBySide(v => !v)} className={`flex items-center justify-center w-[26px] h-[26px] rounded-md cursor-pointer transition-colors border-none ${!diffSideBySide ? 'text-accent bg-accent-subtle' : 'text-muted hover:text-text hover:bg-bg-hover bg-transparent'}`} title={diffSideBySide ? 'Switch to unified view' : 'Switch to split view'} aria-label={diffSideBySide ? 'Switch to unified view' : 'Switch to split view'}><Columns2 size={14} /></button>
            <button onClick={() => setDiffLineNumbers(v => !v)} className={`flex items-center justify-center w-[26px] h-[26px] rounded-md cursor-pointer transition-colors border-none ${diffLineNumbers ? 'text-accent bg-accent-subtle' : 'text-muted hover:text-text hover:bg-bg-hover bg-transparent'}`} title={diffLineNumbers ? 'Hide line numbers' : 'Show line numbers'} aria-label={diffLineNumbers ? 'Hide line numbers' : 'Show line numbers'}><Hash size={14} /></button>
          </div>
        }
      >
        <DiffPanel filePath={tab.path || ''} original={tab.original || ''} modified={tab.modified || ''} lineNumbers={diffLineNumbers} sideBySide={diffSideBySide} />
      </DetailPanel>
    )
  }
  return null
}

/** Live terminal tab title — subscribes to the session's title (running command
 *  / cwd basename) pushed by the backend poller; falls back to the tab's default
 *  cwd title until the first frame arrives. Module-scope so it isn't redefined
 *  per render. */
function TerminalTabTitle({ sessionId, fallback }: { sessionId: string; fallback: string }) {
  const live = useTerminalTitle(sessionId)
  return <>{live || fallback}</>
}

function TabChip({ tab, active, onSelect, onClose, closable = true, onTransfer, pinned = false }: { tab: PanelTab; active: boolean; onSelect: () => void; onClose: () => void; closable?: boolean; onTransfer?: () => void; pinned?: boolean }) {
  // Pinned views (Changes / Files / Artifacts) are icon-only when inactive and
  // expand to icon + label when active — a hybrid that keeps the strip compact
  // while still naming the current view. Dynamic (document / terminal) tabs
  // always show their label. Icon-only chips MUST carry an accessible name.
  const showLabel = active || !pinned
  return (
    <div
      role="tab"
      aria-selected={active}
      tabIndex={0}
      onClick={onSelect}
      // Guard on e.target so Enter/Space on the nested transfer/close buttons
      // activates them natively instead of also selecting the tab.
      onKeyDown={(e) => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); onSelect() } }}
      onAuxClick={(e) => { if (closable && e.button === 1) { e.preventDefault(); onClose() } }}
      // Icon-only pinned chips have no visible text, so give them an explicit
      // accessible name + hover tooltip. Harmless (and a nice tooltip) when the
      // label is also shown.
      aria-label={pinned ? tab.title : undefined}
      title={pinned && !showLabel ? tab.title : undefined}
      // Figma "Side Navigation" chip: 28px tall, 6px corners (not a full pill),
      // 8px padding, 4px icon↔label gap. Active = neutral fill (--border) + accent
      // text; inactive = muted, brightening on hover. Icon-only (inactive pinned)
      // collapses to a square (w-7, centered) so the trio reads as an even set.
      className={`group relative flex items-center gap-1 h-7 rounded-md cursor-pointer shrink-0 select-none transition-colors ${
        showLabel ? `max-w-[240px] ${closable ? 'pl-2 pr-1' : 'px-2'}` : 'w-7 justify-center px-0'
      } ${
        active ? 'bg-border text-accent' : 'text-muted hover:text-text hover:bg-bg-elevated'
      }`}
    >
      <span className="shrink-0">{KIND_ICON[tab.kind]}</span>
      {showLabel && (
        <span className="min-w-0 text-[12px] truncate text-left">
          {tab.kind === 'terminal' && tab.sessionId
            ? <TerminalTabTitle sessionId={tab.sessionId} fallback={tab.title} />
            : tab.title}
        </span>
      )}
      {(onTransfer || closable) && (
        <div className="flex items-center gap-0.5 shrink-0">
          {onTransfer && (
            <button
              onClick={(e) => { e.stopPropagation(); onTransfer() }}
              className={`shrink-0 flex items-center justify-center w-[18px] h-[18px] rounded-full transition-all bg-transparent border-none cursor-pointer text-muted hover:text-text hover:bg-bg-hover ${active ? 'opacity-70' : 'opacity-0 group-hover:opacity-70'}`}
              title={i18nT('pages.chat.sidePanel.move_to_bottom_panel')}
              aria-label={i18nT('pages.chat.sidePanel.move_to_bottom_panel')}
            >
              <PanelBottom size={12} />
            </button>
          )}
          {closable && (
            <button
              onClick={(e) => { e.stopPropagation(); onClose() }}
              className={`shrink-0 -ml-0.5 flex items-center justify-center w-[18px] h-[18px] rounded-full transition-all bg-transparent border-none cursor-pointer text-muted hover:text-text hover:bg-bg-hover ${active ? 'opacity-70' : 'opacity-0 group-hover:opacity-70'}`}
              title={i18nT('pages.chat.sidePanel.close_tab')}
              aria-label={i18nT('pages.chat.sidePanel.close_tab')}
            >
              <X size={12} />
            </button>
          )}
        </div>
      )}
    </div>
  )
}
