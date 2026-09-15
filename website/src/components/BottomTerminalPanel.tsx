import { useCallback, useEffect, useRef, useState } from 'react'
import { motion, AnimatePresence, Reorder } from 'framer-motion'
import { usePointerDrag } from '../hooks/usePointerDrag'
import { useLongPressReorder } from '../hooks/useLongPressReorder'
import { TerminalSquare, Plus, X, ChevronDown, ChevronRight, PictureInPicture2, MoreHorizontal, PanelRight, PanelBottom, Loader2 } from 'lucide-react'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from './ui/dropdown-menu'
import CliPanel, { disposeTerminalSession, useDeleteTerminalSession } from './CliPanel'
import ErrorNotice from './ErrorNotice'
import { useTerminalTitle, disposeTerminalConnection } from '../utils/terminalRegistry'
import { useAppSelector } from '../store'
import { selectActiveSlotProject } from '../store/chatSlice'
import { openPopout as openTerminalPopout, isPopoutOpen as isTerminalPopoutOpen, focusPopout as focusTerminalPopout, bringBack as bringBackTerminalPopout, returnSelfToMain } from '../utils/terminalPopout'
import {
  useBottomTerminal, addTab, removeTab, setActiveTab, setTabsOrder,
  closeBottomTerminal, setBottomTerminalHeight, setBottomTerminalWidth,
  toggleTerminalPosition, MAX_TERMINALS, MIN_WIDTH, MAX_VH, MAX_VW,
  setTerminalCloseFailed, useTerminalCloseFailed,
  type TermTab,
} from '../hooks/useBottomTerminal'

import { i18nT } from '../i18n/t'

/** Live terminal tab title — the running command / cwd basename pushed by the
 *  backend poller; falls back to "Terminal" until the first frame arrives. */
function TerminalTitle({ sessionId }: { sessionId: string }) {
  const live = useTerminalTitle(sessionId)
  return <>{live || i18nT('components.bottomTerminalPanel.terminal')}</>
}

/** A terminal tab chip — mirrors the activity-bar SidePanel TabChip design */
function TabChip({ tab, active, closing = false, onSelect, onClose }: {
  tab: TermTab; active: boolean; closing?: boolean; onSelect: () => void; onClose: () => void
}) {
  return (
    <div
      role="tab"
      aria-selected={active}
      aria-busy={closing || undefined}
      tabIndex={0}
      onClick={onSelect}
      // Guard on e.target so Enter/Space on the nested close button
      // activates it natively instead of also selecting the tab.
      onKeyDown={(e) => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); onSelect() } }}
      onAuxClick={(e) => { if (e.button === 1 && !closing) { e.preventDefault(); onClose() } }}
      className={`group relative flex items-center gap-1.5 h-8 pl-3 pr-1.5 rounded-full cursor-pointer shrink-0 max-w-[240px] select-none border transition-colors ${
        active ? 'bg-bg-elevated border-border text-text-strong shadow-sm' : 'bg-transparent border-transparent text-muted hover:text-text hover:bg-bg-hover'
      } ${closing ? 'opacity-60' : ''}`}
    >
      <span className="shrink-0 opacity-80"><TerminalSquare size={13} /></span>
      <span className="min-w-0 text-[12.5px] truncate text-left">
        <TerminalTitle sessionId={tab.id} />
      </span>
      <div className="flex items-center gap-0.5 shrink-0">
        {/* While the popout's last tab waits for its PTY DELETE to settle the
            close control shows progress and stays inert, so the wait reads as
            "closing" rather than a click that did nothing. */}
        <button
          onClick={(e) => { e.stopPropagation(); if (!closing) onClose() }}
          disabled={closing}
          className={`shrink-0 -ml-0.5 flex items-center justify-center w-[18px] h-[18px] rounded-full transition-all bg-transparent border-none cursor-pointer text-muted hover:text-text hover:bg-bg-hover ${active || closing ? 'opacity-70' : 'opacity-0 group-hover:opacity-70'}`}
          title={closing ? i18nT('components.bottomTerminalPanel.closing_terminal') : i18nT('components.bottomTerminalPanel.close_terminal')}
          aria-label={closing ? i18nT('components.bottomTerminalPanel.closing_terminal') : i18nT('components.bottomTerminalPanel.close_terminal')}
        >
          {closing ? <Loader2 size={12} className="animate-spin" /> : <X size={12} />}
        </button>
      </div>
    </div>
  )
}

/** One reorderable chip in the strip. A component rather than inline JSX inside
 *  the map: each chip owns its own long-press drag state, and a hook cannot be
 *  called from a loop. */
function DraggableTermTab({ tab, active, closing, separator, onSelect, onClose }: {
  tab: TermTab; active: boolean; closing: boolean; separator: boolean; onSelect: () => void; onClose: () => void
}) {
  const { itemProps, dragging } = useLongPressReorder()
  return (
    <Reorder.Item
      value={tab}
      {...itemProps}
      // The ring is the only feedback a press-and-hold gets before the finger
      // moves; without it an armed drag looks identical to a missed one.
      className={`relative shrink-0 list-none rounded-full ${dragging ? 'ring-1 ring-accent' : ''}`}
      transition={{ type: 'spring', stiffness: 700, damping: 45 }}
    >
      {separator && (
        <span aria-hidden="true" className="absolute -left-[4.5px] top-1/2 -translate-y-1/2 w-px h-4 bg-border" />
      )}
      <TabChip tab={tab} active={active} closing={closing} onSelect={onSelect} onClose={onClose} />
    </Reorder.Item>
  )
}

/**
 * The tabbed terminal view (strip + per-tab CliPanel bodies), shared by the
 * docked bottom panel and the popped-out terminal window
 * (`TerminalPopoutFrame`). Every terminal stays mounted (hidden when
 * inactive) so the xterm session + scrollback survive tab switches.
 *
 * `variant` picks the host-specific chrome:
 *  - `dock`: chips offer move-to-chat; the strip ends with pop-out + hide.
 *  - `popout`: no move-to-chat (there is no chat in that window); the strip
 *    ends with a "Return" control that re-docks the panel in the main window.
 */
/** The PTY kill is best-effort (a server-side reaper backstops it), but a
 *  rejected DELETE still must not vanish: the tab is already gone locally, so a
 *  silent failure would leave the user unaware a shell is still running.
 *  Nothing here is a draft, so the hand-off is on. */
function TerminalCloseErrorNotice() {
  const closeFailed = useTerminalCloseFailed()
  return (
    <ErrorNotice
      variant="inline"
      askAgent
      testId="terminal-close-error"
      className="mx-2 my-1"
      message={closeFailed ? i18nT('components.bottomTerminalPanel.close_failed') : ''}
      onDismiss={() => setTerminalCloseFailed(false)}
    />
  )
}

export function TerminalTabsView({ variant }: { variant: 'dock' | 'popout' }) {
  const { tabs, activeId, position } = useBottomTerminal()
  // A rejected PTY delete lands in the close-failed flag (set by the hook), which
  // the always-mounted panel root renders (see BottomTerminalPanel below) —
  // closing the LAST tab unmounts this strip before a delayed rejection arrives.
  const del = useDeleteTerminalSession()
  // The popout's last tab, while its DELETE is in flight (see closeTab).
  const [closingId, setClosingId] = useState<string | null>(null)

  // New tabs spawn in the selected session's project directory when one is
  // set; otherwise the backend's default cwd applies.
  const activeSlotProject = useAppSelector(selectActiveSlotProject)

  /** Close a tab: kill its backend PTY (best-effort), tear down local WS +
   *  xterm, then drop it from the store (which hides the panel if it was last).
   *
   *  In the POPOUT the last tab is special: emptying `tabs` makes the frame
   *  return itself to the main window, which tears this JS context down — so a
   *  rejection that arrives after that point has no callback left to record it.
   *  Let the DELETE settle first (the request itself is `keepalive`, so a hung
   *  one is bounded by the browser and cannot leak the shell), showing the chip
   *  as closing so the wait never reads as a dead click; the rejection then lands
   *  in the cross-window close-failed flag while this window still exists, and
   *  the main window's panel root renders it after the return. */
  const closeTab = useCallback((id: string) => {
    if (variant === 'popout' && tabs.length === 1) {
      if (closingId) return // already on its way out
      setClosingId(id)
      void del.mutateAsync(id).catch(() => { /* recorded by the hook's onError */ }).finally(() => {
        disposeTerminalSession(id)
        removeTab(id)
      })
      return
    }
    del.mutate(id)
    disposeTerminalSession(id)
    removeTab(id)
  }, [del, variant, tabs.length, closingId])

  /** Detach the WHOLE panel into its own browser window. Order matters, twice
   *  over: `openPopout` must run synchronously in the click (window.open needs
   *  the user activation), and this window's WebSockets are released only
   *  AFTER the popout window actually opened — a vetoed popup (blocker /
   *  browser policy) must leave every docked terminal connected. On success
   *  the release is still synchronous, well before the popout's JS context
   *  boots and reconnects (PTYs stay alive server-side; the backend replays
   *  each session's scrollback to the new window). */
  const popOut = useCallback(() => {
    openTerminalPopout()
    if (!isTerminalPopoutOpen()) return // window.open vetoed — keep the dock live
    for (const t of tabs) disposeTerminalConnection(t.id)
  }, [tabs])

  const atCap = tabs.length >= MAX_TERMINALS

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* The popout window has no BottomTerminalPanel root, so the strip hosts
          the notice there for a non-last tab; the last tab's rejection is
          settled before teardown (closeTab) and crosses to the main window
          through the persisted store. In the dock the root renders it. */}
      {variant === 'popout' && <TerminalCloseErrorNotice />}
      {/* Tab strip — same aesthetics as the activity-bar strip; drag chips
          horizontally to reorder (framer Reorder). */}
      <div className="flex items-center gap-1.5 h-9 shrink-0 pl-2 pr-1.5">
        <Reorder.Group
          axis="x"
          values={tabs}
          onReorder={setTabsOrder}
          role="tablist"
          className="flex items-center gap-2 min-w-0 overflow-x-auto scrollbar-none list-none m-0 p-0"
        >
          {tabs.map((t, i) => (
            <DraggableTermTab
              key={t.id}
              tab={t}
              active={t.id === activeId}
              closing={closingId === t.id}
              // Hairline between adjacent chips, suppressed on both edges of
              // the active tab (its pill already delineates it).
              separator={i > 0 && t.id !== activeId && tabs[i - 1].id !== activeId}
              onSelect={() => setActiveTab(t.id)}
              onClose={() => closeTab(t.id)}
            />
          ))}
        </Reorder.Group>
        {/* + opens a new terminal tab instantly (no menu). */}
        <button
          className="flex items-center justify-center w-7 h-7 rounded-md text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 disabled:opacity-40 disabled:cursor-not-allowed"
          onClick={() => addTab(activeSlotProject)}
          disabled={atCap}
          title={atCap ? i18nT('components.bottomTerminalPanel.maximum_terminals', { n: MAX_TERMINALS }) : i18nT('components.bottomTerminalPanel.new_terminal')}
          aria-label={i18nT('components.bottomTerminalPanel.new_terminal')}
        >
          <Plus size={15} />
        </button>
        {variant === 'dock' ? (
          <div className="flex items-center gap-0.5 ml-auto shrink-0">
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  className="flex items-center justify-center w-7 h-7 rounded-md text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0"
                  aria-label={i18nT('components.bottomTerminalPanel.more_actions')}
                  title={i18nT('components.bottomTerminalPanel.more_actions')}
                >
                  <MoreHorizontal size={14} />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="min-w-[180px]">
                <DropdownMenuItem onSelect={toggleTerminalPosition}>
                  {position === 'bottom' ? <PanelRight size={13} className="shrink-0" /> : <PanelBottom size={13} className="shrink-0" />}
                  {position === 'bottom' ? i18nT('components.bottomTerminalPanel.move_panel_to_right') : i18nT('components.bottomTerminalPanel.move_panel_to_bottom')}
                </DropdownMenuItem>
                <DropdownMenuItem onSelect={popOut}>
                  <PictureInPicture2 size={13} className="shrink-0" />
                  {i18nT('components.bottomTerminalPanel.pop_out_to_window')}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
            <button
              className="flex items-center justify-center w-7 h-7 rounded-md text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0"
              onClick={() => closeBottomTerminal()}
              title={i18nT('components.bottomTerminalPanel.hide_terminal_panel')}
              aria-label={i18nT('components.bottomTerminalPanel.hide_terminal_panel')}
            >
              {position === 'bottom' ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
            </button>
          </div>
        ) : (
          <button
            className="flex items-center gap-1.5 h-7 px-2.5 ml-auto rounded-md text-[12px] text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0"
            onClick={returnSelfToMain}
            title={i18nT('pages.terminalPopoutFrame.return_to_main_window_and_close_this_popout')}
            aria-label={i18nT('pages.terminalPopoutFrame.return_to_main_window_and_close_this_popout')}
          >
            <PictureInPicture2 size={13} /> {i18nT('pages.terminalPopoutFrame.return')}
          </button>
        )}
      </div>
      {/* Body — every terminal stays mounted (hidden when inactive) so the
          xterm session + scrollback survive tab switches. */}
      <div className="flex-1 min-h-0 relative">
        {tabs.map(t => (
          <div key={t.id} className="absolute inset-0" style={{ display: t.id === activeId ? 'block' : 'none' }}>
            <CliPanel sessionId={t.id} cwd={t.cwd} visible={t.id === activeId} />
          </div>
        ))}
      </div>
    </div>
  )
}

/**
 * Main-window stand-in while the panel lives in the popout window — a slim
 * docked bar making the detached state visible with EXPLICIT controls
 * (mirroring the chat-popout bring-back affordance): focus the popout, or
 * close it and re-dock the panel here. No timing heuristics — a refused
 * programmatic focus is a silent no-op, never a destructive re-dock.
 *
 * It also keeps this window's hands off the PTY sockets the popout owns:
 * release runs on every tab-list change (a chat terminal adopted into the
 * popped-out panel would otherwise leave its old socket held here) — release
 * is idempotent, and the PTYs themselves stay alive server-side.
 */
export function TerminalDetachedBar() {
  const { tabs } = useBottomTerminal()
  useEffect(() => {
    for (const t of tabs) disposeTerminalConnection(t.id)
  }, [tabs])
  return (
    <div className="shrink-0 flex items-center gap-2 h-9 px-3 border-t border-border bg-bg text-[12.5px] text-muted">
      <TerminalSquare size={13} className="shrink-0 opacity-80" />
      <span className="min-w-0 truncate">{i18nT('components.bottomTerminalPanel.terminal_is_in_its_own_window')}</span>
      <div className="flex items-center gap-1.5 ml-auto shrink-0">
        <button
          className="h-6 px-2 rounded-md text-[12px] text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer"
          onClick={() => focusTerminalPopout()}
        >
          {i18nT('components.bottomTerminalPanel.focus_popout')}
        </button>
        <button
          className="h-6 px-2 rounded-md text-[12px] text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer"
          onClick={() => bringBackTerminalPopout()}
        >
          {i18nT('pages.terminalPopoutFrame.return')}
        </button>
      </div>
    </div>
  )
}

/**
 * App-wide docked terminal panel. Toggled from the sidebar Terminal icon
 * (App.tsx), it spans the whole app (below or beside the routed <main>)
 * depending on `position`. A terminals-only tab view: each tab is a
 * single-session CliPanel bound to a PTY in terminalRegistry — so
 * hiding/reopening the panel, switching tabs, or navigating routes keeps every
 * shell warm. Only closing an individual tab kills its PTY.
 */
export default function BottomTerminalPanel() {
  const { open, height, width, position } = useBottomTerminal()
  const [dragging, setDragging] = useState(false)

  const isRight = position === 'right'

  /* ── Top grip resize (drag up → taller, for bottom position) ── */
  const startHRef = useRef(0)
  const gripResizeBottom = usePointerDrag({
    threshold: 0,
    onStart: () => {
      startHRef.current = height
      setDragging(true)
      document.body.style.userSelect = 'none'
      document.body.style.cursor = 'row-resize'
    },
    onMove: ({ dy }) => {
      const maxH = Math.round(window.innerHeight * MAX_VH)
      setBottomTerminalHeight(Math.min(maxH, startHRef.current - dy))
    },
    onEnd: () => {
      setDragging(false)
      document.body.style.userSelect = ''
      document.body.style.cursor = ''
    },
  })

  /* ── Left grip resize (drag left → wider, for right position) ── */
  const startWRef = useRef(0)
  const gripResizeRight = usePointerDrag({
    threshold: 0,
    onStart: () => {
      startWRef.current = width
      setDragging(true)
      document.body.style.userSelect = 'none'
      document.body.style.cursor = 'col-resize'
    },
    onMove: ({ dx }) => {
      // Grip is at the panel LEFT, so dragging LEFT (dx < 0) grows the panel.
      const maxW = Math.round(window.innerWidth * MAX_VW)
      setBottomTerminalWidth(Math.min(maxW, Math.max(MIN_WIDTH, startWRef.current - dx)))
    },
    onEnd: () => {
      setDragging(false)
      document.body.style.userSelect = ''
      document.body.style.cursor = ''
    },
  })

  // Safety: restore body styles if unmounted mid-drag.
  useEffect(() => () => {
    document.body.style.userSelect = ''
    document.body.style.cursor = ''
  }, [])

  // Orientation-parameterized layout: one tree, axis-driven props.
  const grip = isRight ? gripResizeRight : gripResizeBottom
  const dimension = isRight ? width : height
  const motionProp = isRight ? { width: dimension } : { height: dimension }
  const motionKey = isRight ? 'right-terminal' : 'bottom-terminal'

  return (
    <>
      {/* Outside the `open` guard on purpose: this root stays mounted while the
          panel is hidden, so a delete rejected AFTER the last tab closed still
          has a surface to land on. */}
      <TerminalCloseErrorNotice />
      <AnimatePresence initial={false}>
      {open && (
        <motion.div
          key={motionKey}
          className={`shrink-0 overflow-hidden bg-bg ${isRight ? 'border-l border-border' : 'border-t border-border'}`}
          initial={isRight ? { width: 0 } : { height: 0 }}
          animate={motionProp}
          exit={isRight ? { width: 0 } : { height: 0 }}
          transition={{ duration: dragging ? 0 : 0.22, ease: 'easeOut' }}
          style={{ willChange: isRight ? 'width' : 'height' }}
        >
          <div className={isRight ? 'flex flex-row h-full' : 'flex flex-col'} style={motionProp}>
            <div
              {...grip}
              className={`relative shrink-0 group/drag ${isRight ? 'w-[6px] cursor-col-resize' : 'h-[6px] cursor-row-resize'}`}
              style={{ touchAction: 'none' }}
              role="separator"
              aria-orientation={isRight ? 'vertical' : 'horizontal'}
              aria-label={i18nT('components.bottomTerminalPanel.resize_terminal_panel')}
            >
              <div className={`absolute transition-colors duration-200 ${
                isRight
                  ? `inset-y-0 left-0 w-[2px] ${dragging ? 'bg-accent' : 'bg-transparent group-hover/drag:bg-accent'}`
                  : `inset-x-0 top-0 h-[2px] ${dragging ? 'bg-accent' : 'bg-transparent group-hover/drag:bg-accent'}`
              }`} />
            </div>
            <div className={isRight ? 'flex-1 min-w-0 min-h-0' : 'flex-1 min-h-0'}>
              <TerminalTabsView variant="dock" />
            </div>
          </div>
        </motion.div>
      )}
      </AnimatePresence>
    </>
  )
}
