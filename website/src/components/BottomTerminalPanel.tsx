import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import { motion, AnimatePresence, Reorder } from 'framer-motion'
import { usePointerDrag } from '../hooks/usePointerDrag'
import { useLongPressReorder } from '../hooks/useLongPressReorder'
import { useImeGuard } from '../hooks/useImeGuard'
import { TerminalSquare, Plus, X, ChevronDown, ChevronRight, PictureInPicture2, MoreHorizontal, PanelRight, PanelBottom, Loader2 } from 'lucide-react'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from './ui/dropdown-menu'
import { ContextMenu, ContextMenuTrigger, ContextMenuContent, ContextMenuItem } from './ui/context-menu'
import { Input } from './ui'
import { useTranslation } from 'react-i18next'
import CliPanel, { disposeTerminalSession, useDeleteTerminalSession } from './CliPanel'
import ErrorNotice from './ErrorNotice'
import { useTerminalTitle, disposeTerminalConnection } from '../utils/terminalRegistry'
import { useAppSelector } from '../store'
import { selectActiveSlotProject } from '../store/chatSlice'
import { openPopout as openTerminalPopout, isPopoutOpen as isTerminalPopoutOpen, focusPopout as focusTerminalPopout, bringBack as bringBackTerminalPopout, returnSelfToMain } from '../utils/terminalPopout'
import {
  useBottomTerminal, useTerminalHydratePending, addTab, removeTab, hasTab, setActiveTab, setTabsOrder,
  renameTab, capTerminalName,
  closeBottomTerminal, setBottomTerminalHeight, setBottomTerminalWidth,
  toggleTerminalPosition, MAX_TERMINALS, MIN_WIDTH, MAX_VH, MAX_VW,
  setTerminalCloseFailed, useTerminalCloseFailed,
  type TermTab,
} from '../hooks/useBottomTerminal'

import { i18nT } from '../i18n/t'

/** A terminal tab chip — mirrors the activity-bar SidePanel TabChip design.
 *  `hintId` names the strip's visible editing helper (rendered outside the
 *  scrolling tablist by TerminalTabsView), which the editor is described by;
 *  `onEditingChange` tells the strip when to show it. */
function TabChip({ tab, active, closing = false, hintId, onSelect, onClose, onEditingChange }: {
  tab: TermTab; active: boolean; closing?: boolean; hintId: string
  onSelect: () => void; onClose: () => void; onEditingChange: (editing: boolean) => void
}) {
  const { t } = useTranslation()
  const ime = useImeGuard()
  const liveTitle = useTerminalTitle(tab.id)
  const title = tab.name || liveTitle || t('components.bottomTerminalPanel.terminal')
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const editingRef = useRef(false)
  const menuRenameRef = useRef(false)
  const chipRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const { tabs, activeId } = useBottomTerminal()
  // The first click selects immediately so the terminal can accept typing.
  // Keep its prior selection through autofocus blur for a matching dblclick.
  const priorActiveRef = useRef<string | null>(null)
  const clearPriorActive = useCallback(() => { priorActiveRef.current = null }, [])

  useEffect(clearPriorActive, [tabs, closing, clearPriorActive])
  useEffect(() => {
    if (activeId !== tab.id) clearPriorActive()
  }, [activeId, tab.id, clearPriorActive])

  useEffect(() => {
    if (editing) {
      inputRef.current?.focus()
      inputRef.current?.select()
    }
  }, [editing])

  // The helper lives outside the scrolling tablist (which would clip it), so
  // the strip has to be told; the cleanup also covers a tab closed mid-edit.
  useEffect(() => {
    if (!editing) return
    onEditingChange(true)
    return () => onEditingChange(false)
  }, [editing, onEditingChange])

  useLayoutEffect(() => {
    // Refocusing before the input unmounts scrolls its wider box into view.
    // Reconcile once the label is back so the strip retains the outline inset.
    if (!editing && document.activeElement === chipRef.current) {
      chipRef.current?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
    }
  }, [editing])

  const beginRename = () => {
    clearPriorActive()
    if (closing || editingRef.current) return
    // Renaming an inactive tab must not reveal its CliPanel: the terminal's
    // visibility autofocus would blur and immediately commit the editor.
    setDraft(capTerminalName(title))
    editingRef.current = true
    setEditing(true)
  }
  const finishRename = (save: boolean, restoreFocus: boolean) => {
    // Escape/Enter can unmount the focused input and trigger blur. Only the
    // first completion owns the edit, so cancellation can never become a save.
    if (!editingRef.current) return
    editingRef.current = false
    if (save) renameTab(tab.id, draft)
    setEditing(false)
    if (restoreFocus) chipRef.current?.focus()
  }

  return (
    <ContextMenu>
      <ContextMenuTrigger asChild disabled={editing || closing}>
        <div
          ref={chipRef}
          role="tab"
          aria-label={title}
          aria-selected={active}
          aria-busy={closing || undefined}
          aria-keyshortcuts="F2"
          tabIndex={0}
          onFocus={(e) => {
            if (e.target === e.currentTarget) {
              e.currentTarget.scrollIntoView({ block: 'nearest', inline: 'nearest' })
            }
          }}
          // A second primary press belongs to the same potential double-click;
          // close/context actions must not inherit that selection snapshot.
          onPointerDownCapture={(e) => {
            if (e.button !== 0 || (e.target as HTMLElement).closest('button, input')) clearPriorActive()
          }}
          onPointerCancel={clearPriorActive}
          onDragStartCapture={clearPriorActive}
          onContextMenuCapture={clearPriorActive}
          onClick={(e) => {
            if (closing || editingRef.current || e.detail > 1) return
            priorActiveRef.current = e.detail === 0 ? null : activeId
            onSelect()
          }}
          onDoubleClick={(e) => {
            if ((e.target as HTMLElement).closest('button, input') || closing || editingRef.current) return
            const priorActive = priorActiveRef.current
            if (priorActive && priorActive !== tab.id && activeId === tab.id && hasTab(priorActive)) {
              // Flush visibility AND its autofocus before beginRename mounts the
              // input; batching both lets the restored terminal blur-save it.
              flushSync(() => setActiveTab(priorActive))
            }
            beginRename()
          }}
          onKeyDown={(e) => {
            if (e.target !== e.currentTarget) return
            if (e.key === 'F2') { e.preventDefault(); e.stopPropagation(); beginRename() }
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault()
              clearPriorActive()
              if (!closing) onSelect()
            }
          }}
          onAuxClick={(e) => {
            if (e.button === 1 && !closing && !editing) {
              e.preventDefault()
              clearPriorActive()
              onClose()
            }
          }}
          // Three states, each in its own vocabulary: the selected tab is the
          // elevated pill; an inactive tab is bare text; an editing tab sheds
          // the pill entirely, leaving the filled text field as the only box —
          // so a field can never be mistaken for a focus ring around a pill.
          // Keyboard focus is split the same way. The selected pill keeps the
          // global 2px accent outline: ring and pill are one element there, so
          // the ring can only read as focus ON the selection. An inactive chip
          // paints the same 2px outline in NEUTRAL `--muted` instead. Focus
          // lands on an inactive chip after every rename of a tab that is not
          // selected, and an accent ring there stands beside the selected pill
          // as a second selection whatever its strength — the strip's selection
          // grammar is fill plus accent, so the cue differs in COLOUR, as the
          // swatch hover cue does (frontend-conventions § Animations). `--muted`
          // is the lightest neutral token clearing the 3:1 non-text floor on
          // `--bg` in both default themes; the utilities outspecify the bare
          // global `:focus-visible` rule, and the offset is restated so the
          // extent stays the 4px the strip's gutter reserves.
          className={`group relative flex items-center gap-1.5 h-8 pl-3 pr-1.5 rounded-full cursor-pointer shrink-0 max-w-[240px] select-none border transition-colors ${
            editing ? 'bg-transparent border-transparent text-text'
              : active ? 'bg-bg-elevated border-border text-text-strong shadow-sm' : 'bg-transparent border-transparent text-muted hover:text-text hover:bg-bg-hover focus-visible:outline-2 focus-visible:outline-muted focus-visible:outline-offset-2'
          } ${closing ? 'opacity-60' : ''}`}
        >
          <span className="shrink-0 opacity-80"><TerminalSquare size={13} /></span>
          {editing ? (
            <Input
              ref={inputRef}
              aria-label={t('terminalTab.name')}
              aria-describedby={hintId}
              value={draft}
              // A filled field, not a ring: the strip's hover wash is the one
              // fill that stands off the bare chip on every theme (the shared
              // Input's elevated fill is near-invisible on the panel's own
              // background). The helper it is described by stays visible
              // beneath the strip while typing (see TerminalTabsView).
              className="w-36 h-6 px-1 py-0 text-[12.5px] bg-bg-hover"
              onChange={(e) => setDraft(capTerminalName(e.target.value))}
              onPointerDown={(e) => e.stopPropagation()}
              onClick={(e) => e.stopPropagation()}
              onDoubleClick={(e) => e.stopPropagation()}
              onAuxClick={(e) => e.stopPropagation()}
              {...ime.bindComposition({ onBlur: () => finishRename(true, false) })}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  if (ime.claimEnter(e)) finishRename(true, true)
                } else if (e.key === 'Escape') {
                  if (ime.claimKey(e)) { e.preventDefault(); finishRename(false, true) }
                }
                // Editing keys belong to the editor, never to the strip's
                // shortcuts or the reorder gesture.
                e.stopPropagation()
              }}
            />
          ) : (
            <span className="min-w-0 text-[12.5px] truncate text-left">{title}</span>
          )}
          {/* The editor stands alone: with the close control beside it, a click
              meant to leave the field would blur-save and then kill the shell. */}
          {!editing && (
            <div className="flex items-center gap-0.5 shrink-0">
              {/* Closing remains visible while the popout's last PTY DELETE settles. */}
              <button
                onPointerDown={(e) => e.stopPropagation()}
                onClick={(e) => { e.stopPropagation(); clearPriorActive(); if (!closing) onClose() }}
                disabled={closing}
                className={`shrink-0 -ml-0.5 flex items-center justify-center w-[18px] h-[18px] rounded-full transition-all bg-transparent border-none cursor-pointer text-muted hover:text-text hover:bg-bg-hover ${active || closing ? 'opacity-70' : 'opacity-0 group-hover:opacity-70'}`}
                title={closing ? t('components.bottomTerminalPanel.closing_terminal') : t('components.bottomTerminalPanel.close_terminal')}
                aria-label={closing ? t('components.bottomTerminalPanel.closing_terminal') : t('components.bottomTerminalPanel.close_terminal')}
              >
                {closing ? <Loader2 size={12} className="animate-spin" /> : <X size={12} />}
              </button>
            </div>
          )}
        </div>
      </ContextMenuTrigger>
      <ContextMenuContent onCloseAutoFocus={(e) => {
        // Wait until the menu releases its focus trap before mounting the editor.
        if (menuRenameRef.current) {
          e.preventDefault()
          menuRenameRef.current = false
          beginRename()
        }
      }}>
        <ContextMenuItem disabled={closing} aria-keyshortcuts="F2" onSelect={() => { menuRenameRef.current = true }}>
          {t('terminalTab.rename')}
          {/* The shortcut a sighted user can otherwise never discover: F2 is
              declared on the chip (aria-keyshortcuts) but nothing shows it.
              Same shape as the nav rail's chord badge — a muted trailing span,
              aria-hidden so the item's accessible name stays "Rename" while the
              menuitem's own aria-keyshortcuts carries it to assistive tech.
              `data-i18n-opaque` marks it as keycap data, not copy, for the
              render-time i18n scan. Inline rather than a shared primitive:
              this is the one menu in the tree that shows a shortcut. */}
          <span aria-hidden="true" data-i18n-opaque="" data-testid="terminal-rename-shortcut" className="ml-auto pl-4 text-[11px] leading-none text-muted">
            F2
          </span>
        </ContextMenuItem>
        {tab.name && (
          <ContextMenuItem disabled={closing} onSelect={() => renameTab(tab.id, '')}>
            {t('terminalTab.automatic_name')}
          </ContextMenuItem>
        )}
      </ContextMenuContent>
    </ContextMenu>
  )
}

/** One reorderable chip in the strip. A component rather than inline JSX inside
 *  the map: each chip owns its own long-press drag state, and a hook cannot be
 *  called from a loop. */
function DraggableTermTab({ tab, active, closing, separator, hintId, onSelect, onClose, onEditingChange }: {
  tab: TermTab; active: boolean; closing: boolean; separator: boolean; hintId: string
  onSelect: () => void; onClose: () => void; onEditingChange: (editing: boolean) => void
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
      <TabChip tab={tab} active={active} closing={closing} hintId={hintId} onSelect={onSelect} onClose={onClose} onEditingChange={onEditingChange} />
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
  // Tabs restored from storage are unverified until the backend has said which
  // shells still exist. Nothing is drawn for them before that ruling — a
  // mounted CliPanel would reconnect and spawn a shell for a tab the probe is
  // about to drop, and a strip chip would offer to close a tab that may be
  // gone already. One probe round-trip, then the kept tabs mount as usual.
  const hydratePending = useTerminalHydratePending()
  // A rejected PTY delete lands in the close-failed flag (set by the hook), which
  // the always-mounted panel root renders (see BottomTerminalPanel below) —
  // closing the LAST tab unmounts this strip before a delayed rejection arrives.
  const del = useDeleteTerminalSession()
  // The popout's last tab, while its DELETE is in flight (see closeTab).
  const [closingId, setClosingId] = useState<string | null>(null)
  // Chips with an open name editor. A count rather than an id: a second
  // editor can open (F2 on another tab) before the first's blur-save lands.
  const [editingCount, setEditingCount] = useState(0)
  const onEditingChange = useCallback((editing: boolean) => {
    setEditingCount(n => n + (editing ? 1 : -1))
  }, [])
  const hintId = useId()

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

  if (hydratePending) return null

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* The popout window has no BottomTerminalPanel root, so the strip hosts
          the notice there for a non-last tab; the last tab's rejection is
          settled before teardown (closeTab) and crosses to the main window
          through the persisted store. In the dock the root renders it. */}
      {variant === 'popout' && <TerminalCloseErrorNotice />}
      {/* Tab strip — same aesthetics as the activity-bar strip; drag chips
          horizontally to reorder (framer Reorder). */}
      <div className="flex items-center gap-1.5 h-10 shrink-0 pl-1 pr-1.5">
        <Reorder.Group
          axis="x"
          values={tabs}
          onReorder={setTabsOrder}
          role="tablist"
          // Four pixels contain the global 2px outline plus its 2px offset,
          // including the first/last tab at either end of the scroll range.
          className="flex items-center gap-2 min-w-0 overflow-x-auto scrollbar-none scroll-px-1 list-none m-0 p-1"
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
              hintId={hintId}
              onSelect={() => setActiveTab(t.id)}
              onClose={() => closeTab(t.id)}
              onEditingChange={onEditingChange}
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
        {/* Visible save/cancel helper for the name editor, shown only while one
            is open. It sits here, not in the tablist: that strip is a
            horizontal scroller (so it would clip anything hung below a chip)
            and at 320px has no width to spare. Overlaid rather than stacked so
            the shell keeps its size — a rename must not refit or resize the
            PTY. Keyed by `hintId`, it is the editor's aria-describedby target. */}
        {editingCount > 0 && (
          <p
            id={hintId}
            data-testid="terminal-rename-hint"
            className="absolute inset-x-0 top-0 z-10 m-0 px-3 py-1 text-[11.5px] leading-snug text-muted bg-bg-elevated border-b border-border shadow-sm"
          >
            {i18nT('terminalTab.edit_hint')}
          </p>
        )}
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
