import { useState, useRef, useEffect, useCallback, type ReactNode } from 'react'
import { useIsMobile } from '../../hooks/useIsMobile'
import { Reorder } from 'framer-motion'
import { FileText, Bot, Workflow, ScrollText, MessageSquare, TerminalSquare, GitCompare, GitPullRequest, Package, Plus, X, Hash, Pen, Columns2, PanelRightClose, Component, PanelBottom } from 'lucide-react'
import ActivityViewer from './ActivityViewer'
import DiffPanel from '../../components/DiffPanel'
import DetailPanel from '../../components/DetailPanel'
import MarkdownPanel from '../../components/MarkdownPanel'
import ArtifactPanel from '../../components/ArtifactPanel'
import CliPanel, { disposeTerminalSession, useDeleteTerminalSession } from '../../components/CliPanel'
import { countLines } from '../../components/FileChangeChips'
import { useTerminalEnabled, useTerminalTitle } from '../../utils/terminalRegistry'
import { adoptTab as adoptBottomTerminal } from '../../hooks/useBottomTerminal'
import type { usePanelTabs, ViewKind, PanelTab, TabKind } from '../../hooks/usePanelTabs'
import { usePersistedBool } from '../../hooks/usePersistedBool'
import { useListboxKeyboard } from '../../hooks/useListboxKeyboard'
import { safeSetItem } from '../../utils/safeStorage'
import type { SubagentActivity, ToolActivity } from '../../types'
import type { TouchedFile } from '../../hooks/useTouchedFiles'
import type { ExtractedLink } from '../../utils/extractChatLinks'
import type { PullRequestLink } from '../../utils/pullRequestLinks'

const KIND_ICON: Record<TabKind, ReactNode> = {
  changes: <GitPullRequest size={13} />, files: <FileText size={13} />, artifacts: <Component size={13} />, subagents: <Bot size={13} />, workflows: <Workflow size={13} />,
  logs: <ScrollText size={13} />, side: <MessageSquare size={13} />, terminal: <TerminalSquare size={13} />,
  file: <FileText size={13} />, diff: <GitCompare size={13} />, artifact: <Package size={13} />,
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
}: SidePanelProps) {
  const { tabs, activeId, openView, openTerminal, setActive, closeTab, patchTab, setOrder } = tabsCtl
  const terminalEnabled = useTerminalEnabled()
  // The + menu / empty-state launcher hide Terminal when the feature is
  // disabled server-side and hide Changes when no PR/MR source is present.
  const menuItems = NEW_MENU.filter(item =>
    (terminalEnabled || item.kind !== 'terminal')
    && (sources?.length || item.kind !== 'changes')
  )
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
  const effectiveWidth = isMobile ? '100%' : Math.max(MIN_W, Math.min(width, maxW))
  // While the user drags the resize handle, every mousemove shifts the whole
  // panel's viewport position (the handle is on the LEFT edge; the right edge
  // is pinned to the window). Framer's layout projection on each Reorder.Item
  // sees the chips' screen positions change and spring-animates them toward
  // the new spot each frame — so tabs visibly lag the resize and "catch up"
  // when it stops. During a resize we make the layout transition instant.
  const [resizing, setResizing] = useState(false)
  const onDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    const startX = e.clientX; const startW = widthRef.current
    const max = () => Math.min(Math.round(window.innerWidth * 0.7), window.innerWidth - measureSidePanelReservedW())
    setResizing(true)
    const onMove = (ev: MouseEvent) => setWidth(Math.max(MIN_W, Math.min(startW + (startX - ev.clientX), max())))
    const onUp = () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      setResizing(false)
      safeSetItem(WIDTH_KEY, String(widthRef.current))
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }, [])

  useEffect(() => {
    if (!menuOpen) return
    const onDown = (e: MouseEvent) => { if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false) }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [menuOpen])

  return (
    <div className="h-full shrink-0 flex flex-col bg-bg border-l border-border overflow-hidden relative" style={{ width: effectiveWidth, maxWidth: '100vw' }}>
      {/* Left-edge resize handle */}
      <div className="absolute left-0 top-0 bottom-0 w-[6px] cursor-col-resize z-30 group/drag" onMouseDown={onDragStart}>
        <div className="absolute left-0 top-0 bottom-0 w-[2px] transition-colors duration-200 bg-transparent group-hover/drag:bg-accent resize-accent" />
      </div>
      {/* Tab strip — drag chips horizontally to reorder (framer Reorder).
          h-[42px] matches the app header row so the two bars align.
          side-panel-strip punches the strip out of the Electron window-drag
          region (see index.css) so chips receive pointer events. */}
      {/* border-b gives the strip a tab-bar baseline; chips stay centered
          pills (bordered when active) floating above it. */}
      <div className="side-panel-strip flex items-center gap-1.5 h-[42px] shrink-0 pl-2 pr-1.5 border-b border-border">
        <Reorder.Group
          axis="x"
          values={tabs}
          onReorder={setOrder}
          role="tablist"
          className="flex items-center gap-2 flex-1 min-w-0 overflow-x-auto scrollbar-none list-none m-0 p-0"
        >
          {tabs.map((t, i) => (
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
              {i > 0 && t.id !== activeId && tabs[i - 1].id !== activeId && (
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
            title="Open side panel tab"
            aria-label="Open side panel tab"
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
        <button
          className="flex items-center justify-center w-7 h-7 rounded-md text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0"
          onClick={onClose}
          title="Close panel"
          aria-label="Close panel"
        >
          <PanelRightClose size={15} />
        </button>
      </div>

      {/* Body — render every doc/terminal tab mounted (hidden when inactive) so
          xterm sessions and editor scroll state survive tab switches; category
          views mount only when active (cheap + query-driven). */}
      <div className="flex-1 min-h-0 relative">
        {tabs.length === 0 && (
          /* Empty state: launcher — the available views themselves, roomy and
             clickable, instead of a hint pointing at the + menu. */
          <div className="flex items-center justify-center h-full px-6">
            <div className="flex flex-col items-center gap-4 w-full max-w-[420px]">
              <div className="text-[22px] text-muted font-semibold">Pick a panel to view</div>
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
          // Category views: mount only the active one.
          if (VIEW_KINDS.has(t.kind)) {
            return isActive ? (
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
                  // Files opened FROM the Files tab replace it in place (open
                  // another Files tab from + for a second file). Opens from
                  // other views keep the default open-or-focus behavior.
                  onFileOpen={t.kind === 'files' ? (p) => onFileOpen?.(p, { replaceId: t.id }) : onFileOpen}
                  onFileRemove={onFileRemove} onFilesClear={onFilesClear}
                  projectDir={projectDir} navLinks={navLinks} navResolving={navResolving}
                />
              </div>
            ) : null
          }
          // Terminal + documents: keep mounted, toggle visibility.
          return (
            <div key={t.id} className="absolute inset-0" style={{ display: isActive ? 'block' : 'none' }}>
              <TabBody
                tab={t} active={isActive}
                onClose={() => handleCloseTab(t.id)}
                onContentChange={(c) => patchTab(t.id, { content: c })}
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
function TabBody({ tab, active, onClose, onContentChange, onFileSave, onFileOpen, onSubmitComments, onTerminalSendToChat, diffLineNumbers, setDiffLineNumbers, diffSideBySide, setDiffSideBySide }: {
  tab: PanelTab; active: boolean; onClose: () => void
  onContentChange: (c: string) => void
  onFileSave: (fp: string, c: string) => Promise<void>
  onFileOpen?: (p: string) => void
  onSubmitComments?: (m: string) => void
  onTerminalSendToChat?: (text: string) => void
  diffLineNumbers: boolean; setDiffLineNumbers: (fn: (v: boolean) => boolean) => void
  diffSideBySide: boolean; setDiffSideBySide: (fn: (v: boolean) => boolean) => void
}) {
  if (tab.kind === 'terminal') return <CliPanel sessionId={tab.sessionId ?? ''} cwd={tab.cwd} visible={active} onSendToChat={onTerminalSendToChat} />
  if (tab.kind === 'file') {
    return (
      <MarkdownPanel
        embedded
        filePath={tab.path || ''}
        content={tab.content || ''}
        onContentChange={onContentChange}
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
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-accent/15 text-accent font-medium shrink-0">Turn Diff</span>
            {(added > 0 || removed > 0) && <span className="text-[11px] font-mono font-semibold shrink-0">{added > 0 && <span className="text-ok">+{added}</span>}{removed > 0 && <span className="text-danger ml-1.5">-{removed}</span>}</span>}
            <span className="flex-1" />
            <button onClick={() => onFileOpen?.(tab.path || '')} className="flex items-center justify-center w-[26px] h-[26px] rounded-md cursor-pointer transition-colors text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none" title="Open in Editor" aria-label="Open in Editor"><Pen size={14} /></button>
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

function TabChip({ tab, active, onSelect, onClose, onTransfer }: { tab: PanelTab; active: boolean; onSelect: () => void; onClose: () => void; onTransfer?: () => void }) {
  return (
    <div
      role="tab"
      aria-selected={active}
      tabIndex={0}
      onClick={onSelect}
      // Guard on e.target so Enter/Space on the nested transfer/close buttons
      // activates them natively instead of also selecting the tab.
      onKeyDown={(e) => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); onSelect() } }}
      onAuxClick={(e) => { if (e.button === 1) { e.preventDefault(); onClose() } }}
      className={`group relative flex items-center gap-1.5 h-8 pl-3 pr-1.5 rounded-full cursor-pointer shrink-0 max-w-[240px] select-none border transition-colors ${
        active ? 'bg-bg-elevated border-border text-text-strong shadow-sm' : 'bg-transparent border-transparent text-muted hover:text-text hover:bg-bg-hover'
      }`}
    >
      <span className="shrink-0 opacity-80">{KIND_ICON[tab.kind]}</span>
      <span className="min-w-0 text-[12.5px] truncate text-left">
        {tab.kind === 'terminal' && tab.sessionId
          ? <TerminalTabTitle sessionId={tab.sessionId} fallback={tab.title} />
          : tab.title}
      </span>
      <div className="flex items-center gap-0.5 shrink-0">
        {onTransfer && (
          <button
            onClick={(e) => { e.stopPropagation(); onTransfer() }}
            className={`shrink-0 flex items-center justify-center w-[18px] h-[18px] rounded-full transition-all bg-transparent border-none cursor-pointer text-muted hover:text-text hover:bg-bg-hover ${active ? 'opacity-70' : 'opacity-0 group-hover:opacity-70'}`}
            title="Move to bottom panel"
            aria-label="Move to bottom panel"
          >
            <PanelBottom size={12} />
          </button>
        )}
        <button
          onClick={(e) => { e.stopPropagation(); onClose() }}
          className={`shrink-0 -ml-0.5 flex items-center justify-center w-[18px] h-[18px] rounded-full transition-all bg-transparent border-none cursor-pointer text-muted hover:text-text hover:bg-bg-hover ${active ? 'opacity-70' : 'opacity-0 group-hover:opacity-70'}`}
          title="Close tab"
          aria-label="Close tab"
        >
          <X size={12} />
        </button>
      </div>
    </div>
  )
}
