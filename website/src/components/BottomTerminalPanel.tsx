import { useCallback, useEffect, useRef, useState } from 'react'
import { motion, AnimatePresence, Reorder } from 'framer-motion'
import { useLocation } from 'react-router-dom'
import { TerminalSquare, Plus, X, ChevronDown, PanelRight } from 'lucide-react'
import CliPanel, { disposeTerminalSession, useDeleteTerminalSession } from './CliPanel'
import { useTerminalTitle } from '../utils/terminalRegistry'
import { usePanelTabs } from '../hooks/usePanelTabs'
import { useAppSelector, useAppDispatch } from '../store'
import { openActivityPanel } from '../store/chatSlice'
import {
  useBottomTerminal, addTab, removeTab, setActiveTab, setTabsOrder,
  closeBottomTerminal, setBottomTerminalHeight, MAX_TERMINALS,
  type TermTab,
} from '../hooks/useBottomTerminal'

/** Fraction of the viewport the panel may grow to via the resize grip. */
const MAX_VH = 0.72

/** Live terminal tab title — the running command / cwd basename pushed by the
 *  backend poller; falls back to "Terminal" until the first frame arrives. */
function TerminalTitle({ sessionId }: { sessionId: string }) {
  const live = useTerminalTitle(sessionId)
  return <>{live || 'Terminal'}</>
}

/** A terminal tab chip — mirrors the activity-bar SidePanel TabChip design */
function TabChip({ tab, active, onSelect, onClose, onTransfer, canTransfer }: {
  tab: TermTab; active: boolean; onSelect: () => void; onClose: () => void
  onTransfer: () => void; canTransfer: boolean
}) {
  const transferCls = canTransfer
    ? `shrink-0 flex items-center justify-center w-[18px] h-[18px] rounded-full transition-all bg-transparent border-none cursor-pointer text-muted hover:text-text hover:bg-bg-hover ${active ? 'opacity-70' : 'opacity-0 group-hover:opacity-70'}`
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
        <button
          onClick={(e) => { e.stopPropagation(); if (canTransfer) onTransfer() }}
          disabled={!canTransfer}
          className={transferCls}
          title={canTransfer ? 'Move to side panel' : 'Open a chat page to move this terminal there'}
          aria-label="Move to side panel"
        >
          <PanelRight size={12} />
        </button>
        <button
          onClick={(e) => { e.stopPropagation(); onClose() }}
          className={`shrink-0 -ml-0.5 flex items-center justify-center w-[18px] h-[18px] rounded-full transition-all bg-transparent border-none cursor-pointer text-muted hover:text-text hover:bg-bg-hover ${active ? 'opacity-70' : 'opacity-0 group-hover:opacity-70'}`}
          title="Close terminal"
          aria-label="Close terminal"
        >
          <X size={12} />
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
  const { open, height, tabs, activeId } = useBottomTerminal()
  const del = useDeleteTerminalSession()
  const [dragging, setDragging] = useState(false)

  // Move-to-chat is only offered while the user is actually ON the chat page
  // with a slot active
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const chatTabs = usePanelTabs(activeSlot)
  const dispatch = useAppDispatch()
  const location = useLocation()
  const canTransferToChat = activeSlot != null && location.pathname.startsWith('/chat')

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

  /* ── Top grip resize (drag up → taller) ── */
  const startRef = useRef<{ y: number; h: number } | null>(null)
  const onGripDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    startRef.current = { y: e.clientY, h: height }
    setDragging(true)
    const onMove = (ev: MouseEvent) => {
      if (!startRef.current) return
      const maxH = Math.round(window.innerHeight * MAX_VH)
      setBottomTerminalHeight(Math.min(maxH, startRef.current.h + (startRef.current.y - ev.clientY)))
    }
    const onUp = () => {
      startRef.current = null
      setDragging(false)
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      document.body.style.userSelect = ''
      document.body.style.cursor = ''
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    document.body.style.userSelect = 'none'
    document.body.style.cursor = 'row-resize'
  }, [height])

  // Safety: restore body styles if unmounted mid-drag.
  useEffect(() => () => {
    document.body.style.userSelect = ''
    document.body.style.cursor = ''
  }, [])

  const atCap = tabs.length >= MAX_TERMINALS

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
            onMouseDown={onGripDown}
            className="relative shrink-0 h-[6px] cursor-row-resize group/drag"
            role="separator"
            aria-orientation="horizontal"
            aria-label="Resize terminal panel"
          >
            <div className={`absolute inset-x-0 top-0 h-[2px] transition-colors duration-200 ${dragging ? 'bg-accent' : 'bg-transparent group-hover/drag:bg-accent'}`} />
          </div>
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
                  />
                </Reorder.Item>
              ))}
            </Reorder.Group>
            {/* + opens a new terminal tab instantly (no menu). */}
            <button
              className="flex items-center justify-center w-7 h-7 rounded-md text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 disabled:opacity-40 disabled:cursor-not-allowed"
              onClick={() => addTab()}
              disabled={atCap}
              title={atCap ? `Maximum ${MAX_TERMINALS} terminals` : 'New terminal'}
              aria-label="New terminal"
            >
              <Plus size={15} />
            </button>
            <button
              className="flex items-center justify-center w-7 h-7 rounded-md text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 ml-auto"
              onClick={() => closeBottomTerminal()}
              title="Hide terminal panel"
              aria-label="Hide terminal panel"
            >
              <ChevronDown size={16} />
            </button>
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
        </motion.div>
      )}
    </AnimatePresence>
  )
}
