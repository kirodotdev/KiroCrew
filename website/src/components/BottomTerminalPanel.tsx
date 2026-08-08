import { useCallback, useEffect, useRef, useState } from 'react'
import { motion, AnimatePresence, Reorder } from 'framer-motion'
import { usePointerDrag } from '../hooks/usePointerDrag'
import { useLocation } from 'react-router-dom'
import { TerminalSquare, Plus, X, ChevronDown, PictureInPicture2 } from 'lucide-react'
import { PanelRightSolid } from './icons/panels'
import CliPanel, { disposeTerminalSession, useDeleteTerminalSession } from './CliPanel'
import { useTerminalTitle, disposeTerminalConnection } from '../utils/terminalRegistry'
import { usePanelTabs } from '../hooks/usePanelTabs'
import { useAppSelector, useAppDispatch } from '../store'
import { openActivityPanel } from '../store/chatSlice'
import { openPopout as openTerminalPopout, isPopoutOpen as isTerminalPopoutOpen, focusPopout as focusTerminalPopout, bringBack as bringBackTerminalPopout, returnSelfToMain } from '../utils/terminalPopout'
import {
  useBottomTerminal, addTab, removeTab, setActiveTab, setTabsOrder,
  closeBottomTerminal, setBottomTerminalHeight, MAX_TERMINALS,
  type TermTab,
} from '../hooks/useBottomTerminal'

import { i18nT } from '../i18n/t'
/** Fraction of the viewport the panel may grow to via the resize grip. */
const MAX_VH = 0.72

/** Live terminal tab title — the running command / cwd basename pushed by the
 *  backend poller; falls back to "Terminal" until the first frame arrives. */
function TerminalTitle({ sessionId }: { sessionId: string }) {
  const live = useTerminalTitle(sessionId)
  return <>{live || i18nT('components.bottomTerminalPanel.terminal')}</>
}

/** A terminal tab chip — mirrors the activity-bar SidePanel TabChip design */
function TabChip({ tab, active, onSelect, onClose, onTransfer, canTransfer, showTransfer }: {
  tab: TermTab; active: boolean; onSelect: () => void; onClose: () => void
  onTransfer: () => void; canTransfer: boolean; showTransfer: boolean
}) {
  const transferCls = canTransfer
    ? `pi-morph shrink-0 flex items-center justify-center w-[18px] h-[18px] rounded-full transition-all bg-transparent border-none cursor-pointer text-muted hover:text-text hover:bg-bg-hover ${active ? 'opacity-70' : 'opacity-0 group-hover:opacity-70'}`
    : 'shrink-0 flex items-center justify-center w-[18px] h-[18px] rounded-full bg-transparent border-none text-muted opacity-30 cursor-not-allowed'
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
      <span className="shrink-0 opacity-80"><TerminalSquare size={13} /></span>
      <span className="min-w-0 text-[12.5px] truncate text-left">
        <TerminalTitle sessionId={tab.id} />
      </span>
      <div className="flex items-center gap-0.5 shrink-0">
        {showTransfer && (
          <button
            onClick={(e) => { e.stopPropagation(); if (canTransfer) onTransfer() }}
            disabled={!canTransfer}
            className={transferCls}
            title={canTransfer ? i18nT('components.bottomTerminalPanel.move_to_side_panel') : i18nT('components.bottomTerminalPanel.open_a_chat_page_to_move_this_terminal_there')}
            aria-label={i18nT('components.bottomTerminalPanel.move_to_side_panel')}
          >
            <PanelRightSolid size={12} />
          </button>
        )}
        <button
          onClick={(e) => { e.stopPropagation(); onClose() }}
          className={`shrink-0 -ml-0.5 flex items-center justify-center w-[18px] h-[18px] rounded-full transition-all bg-transparent border-none cursor-pointer text-muted hover:text-text hover:bg-bg-hover ${active ? 'opacity-70' : 'opacity-0 group-hover:opacity-70'}`}
          title={i18nT('components.bottomTerminalPanel.close_terminal')}
          aria-label={i18nT('components.bottomTerminalPanel.close_terminal')}
        >
          <X size={12} />
        </button>
      </div>
    </div>
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
export function TerminalTabsView({ variant }: { variant: 'dock' | 'popout' }) {
  const { tabs, activeId } = useBottomTerminal()
  const del = useDeleteTerminalSession()

  // Move-to-chat is only offered while the user is actually ON the chat page
  // with a slot active
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const chatTabs = usePanelTabs(activeSlot)
  const dispatch = useAppDispatch()
  const location = useLocation()
  const canTransferToChat = variant === 'dock' && activeSlot != null && location.pathname.startsWith('/chat')

  /** Close a tab: kill its backend PTY (best-effort), tear down local WS +
   *  xterm, then drop it from the store (which hides the panel if it was last). */
  const closeTab = useCallback((id: string) => {
    del.mutate(id)
    disposeTerminalSession(id)
    removeTab(id)
  }, [del])

  // Move a terminal OUT of the app-wide panel into the active chat's activity
  // bar. Non-disposing (mirror of the chat→bottom transfer): the PTY/xterm live
  // in terminalRegistry/termCache keyed by session id and re-attach in the
  // chat. Open the chat's activity panel so the moved tab is visible; only drop
  // it from the bottom panel once the chat accepts it.
  const transferToChat = useCallback((id: string, cwd?: string) => {
    if (!canTransferToChat) return
    if (chatTabs.adoptTerminal(id, cwd)) {
      dispatch(openActivityPanel())
      removeTab(id)
    }
  }, [canTransferToChat, chatTabs, dispatch])

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
            <Reorder.Item
              key={t.id}
              value={t}
              className="relative shrink-0 list-none"
              transition={{ type: 'spring', stiffness: 700, damping: 45 }}
            >
              {/* Hairline between adjacent chips, suppressed on both edges of
                  the active tab (its pill already delineates it). */}
              {i > 0 && t.id !== activeId && tabs[i - 1].id !== activeId && (
                <span aria-hidden="true" className="absolute -left-[4.5px] top-1/2 -translate-y-1/2 w-px h-4 bg-border" />
              )}
              <TabChip
                tab={t}
                active={t.id === activeId}
                onSelect={() => setActiveTab(t.id)}
                onClose={() => closeTab(t.id)}
                onTransfer={() => transferToChat(t.id, t.cwd)}
                canTransfer={canTransferToChat}
                showTransfer={variant === 'dock'}
              />
            </Reorder.Item>
          ))}
        </Reorder.Group>
        {/* + opens a new terminal tab instantly (no menu). */}
        <button
          className="flex items-center justify-center w-7 h-7 rounded-md text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 disabled:opacity-40 disabled:cursor-not-allowed"
          onClick={() => addTab()}
          disabled={atCap}
          title={atCap ? i18nT('components.bottomTerminalPanel.maximum_terminals', { n: MAX_TERMINALS }) : i18nT('components.bottomTerminalPanel.new_terminal')}
          aria-label={i18nT('components.bottomTerminalPanel.new_terminal')}
        >
          <Plus size={15} />
        </button>
        {variant === 'dock' ? (
          <div className="flex items-center gap-0.5 ml-auto shrink-0">
            <button
              className="flex items-center justify-center w-7 h-7 rounded-md text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0"
              onClick={popOut}
              title={i18nT('components.bottomTerminalPanel.pop_out_to_window')}
              aria-label={i18nT('components.bottomTerminalPanel.pop_out_to_window')}
            >
              <PictureInPicture2 size={14} />
            </button>
            <button
              className="flex items-center justify-center w-7 h-7 rounded-md text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0"
              onClick={() => closeBottomTerminal()}
              title={i18nT('components.bottomTerminalPanel.hide_terminal_panel')}
              aria-label={i18nT('components.bottomTerminalPanel.hide_terminal_panel')}
            >
              <ChevronDown size={16} />
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
 * (App.tsx), it spans the whole app (below the routed <main>) rather than
 * living inside a single chat's activity bar. A terminals-only tab view: each
 * tab is a single-session CliPanel bound to a PTY in terminalRegistry — so
 * hiding/reopening the panel, switching tabs, or navigating routes keeps every
 * shell warm. Only closing an individual tab kills its PTY.
 */
export default function BottomTerminalPanel() {
  const { open, height } = useBottomTerminal()
  const [dragging, setDragging] = useState(false)

  /* ── Top grip resize (drag up → taller) ── */
  const startHRef = useRef(0)
  const gripResize = usePointerDrag({
    threshold: 0,
    onStart: () => {
      startHRef.current = height
      setDragging(true)
      document.body.style.userSelect = 'none'
      document.body.style.cursor = 'row-resize'
    },
    onMove: ({ dy }) => {
      // Grip is at the panel TOP, so dragging UP (dy < 0) grows the panel.
      const maxH = Math.round(window.innerHeight * MAX_VH)
      setBottomTerminalHeight(Math.min(maxH, startHRef.current - dy))
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

  return (
    <AnimatePresence initial={false}>
      {open && (
        <motion.div
          key="bottom-terminal"
          className="shrink-0 overflow-hidden border-t border-border bg-bg"
          initial={{ height: 0 }}
          animate={{ height }}
          exit={{ height: 0 }}
          transition={{ duration: dragging ? 0 : 0.22, ease: 'easeOut' }}
          style={{ willChange: 'height' }}
        >
          <div className="flex flex-col" style={{ height }}>
          <div
            {...gripResize}
            className="relative shrink-0 h-[6px] cursor-row-resize group/drag"
            style={{ touchAction: 'none' }}
            role="separator"
            aria-orientation="horizontal"
            aria-label={i18nT('components.bottomTerminalPanel.resize_terminal_panel')}
          >
            <div className={`absolute inset-x-0 top-0 h-[2px] transition-colors duration-200 ${dragging ? 'bg-accent' : 'bg-transparent group-hover/drag:bg-accent'}`} />
          </div>
          <div className="flex-1 min-h-0">
            <TerminalTabsView variant="dock" />
          </div>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
