import { useEffect, useRef, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { X, Plus, GitFork, Loader2, Circle } from 'lucide-react'
import { SplitGlyph } from './SplitGlyph'
import { api } from '../api/client'
import SessionGridLayout from './SessionGridLayout'
import ChatPane from './ChatPane'
import { useSessionGrid, type GridLeaf } from '../hooks/useSessionGrid'

import { i18nT } from '../i18n/t'
type Slot = {
  key: string
  title?: string
  running?: boolean
  pending_approval?: boolean
  messages?: number
  agent?: string
  last_activity_ts?: string
  forked_from?: string | null
}

/**
 * SessionGridView — native in-place "terminal split" surface (no app shell).
 *
 * Renders the recursive split tree from useSessionGrid directly in the chat area
 * (NOT an overlay, and with NO "Session Grid · Exit" chrome). Each leaf is a live
 * <ChatPane> (session) or a picker (placeholder); split the focused pane right (⌘D)
 * or down (⬓ button), drag the dividers to resize, close a pane to let siblings
 * reflow. The split exists ONLY while it is a real multi-cell layout: entering
 * always seeds [current session | empty placeholder] in place, and closing back
 * down to a single session hands that session to the native single-chat surface
 * (onCollapse) — so a lone pane is never rendered with grid chrome.
 */
export default function SessionGridView({
  onClose,
  onCollapse,
  seedSlot,
}: {
  /** Leave split mode entirely (everything closed, or a lone empty placeholder). */
  onClose: () => void
  /** Down to a single session pane → return to the native single-chat surface on
   *  that session (the grid never shows a 1-pane chrome). */
  onCollapse: (slot: string) => void
  seedSlot?: string | null
}) {
  const grid = useSessionGrid(seedSlot)

  // On entry, restore this anchor's persisted layout if one exists (useSessionGrid
  // loaded it from splitLayoutStore); otherwise seed [current session | placeholder]
  // in place. Either way ⌘D opens the split of the session you're looking at.
  const seededRef = useRef(false)
  useEffect(() => {
    if (!seededRef.current) {
      seededRef.current = true
      if (grid.isEmpty) grid.seedFromSession(seedSlot ?? null)
      return
    }
    // Collapse rules — the grid only lives while it's a real (multi-cell) split:
    //  • 0 leaves            → user closed everything → leave split.
    //  • 1 leaf, a session   → return to native single chat on that session.
    //  • 1 leaf, placeholder → no session left → leave split.
    const ls = grid.leaves
    if (ls.length === 0) {
      onClose()
    } else if (ls.length === 1) {
      const only = ls[0]
      if (only.kind === 'session' && only.slot) onCollapse(only.slot)
      else onClose()
    }
  }, [grid.leaves]) // eslint-disable-line react-hooks/exhaustive-deps

  // ⌘D / Ctrl+D splits the focused pane RIGHT. ⌘⇧D is not page-cancelable in a
  // browser tab (Chrome reserves it), so split-down is the on-pane ⬓ button only.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || e.shiftKey || e.altKey || e.key.toLowerCase() !== 'd') return
      const ids = grid.leaves.map((l) => l.id)
      const target = grid.focusedId && ids.includes(grid.focusedId) ? grid.focusedId : ids[0]
      if (!target) return
      e.preventDefault()
      grid.splitLeaf(target, 'right')
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [grid.focusedId, grid.leaves, grid.splitLeaf])

  const { data: slots = [] } = useQuery<Slot[]>({
    queryKey: ['session-grid-slots'],
    queryFn: () => api.chatSlots(),
    refetchInterval: 3000,
  })

  // Once the slot list loads, heal a restored layout: drop panes whose session was
  // deleted/archived while away. Runs once (the first non-empty slots payload).
  const prunedRef = useRef(false)
  useEffect(() => {
    if (prunedRef.current || slots.length === 0) return
    prunedRef.current = true
    grid.pruneAgainst(slots.map((s) => s.key))
    // Depend on the stable pruneAgainst callback (useCallback []), not the whole grid
    // object (new literal each render) — avoids re-scheduling the effect every render.
  }, [slots, grid.pruneAgainst])

  // Fork source = the focused session pane, else the first session pane in the grid.
  const focusedLeaf = grid.leaves.find((l) => l.id === grid.focusedId)
  const forkSourceSlot =
    focusedLeaf?.kind === 'session' && focusedLeaf.slot
      ? focusedLeaf.slot
      : grid.leaves.find((l) => l.kind === 'session' && l.slot)?.slot
  const forkSourceTitle = slots.find((s) => s.key === forkSourceSlot)?.title

  const renderLeaf = (leaf: GridLeaf) => {
    if (leaf.kind === 'session' && leaf.slot) {
      return (
        <ChatPane
          slotKey={leaf.slot}
          focused={grid.focusedId === leaf.id}
          onFocus={() => grid.setFocused(leaf.id)}
          onRemove={() => grid.closeLeaf(leaf.id)}
          onSplitRight={() => grid.splitLeaf(leaf.id, 'right')}
          onSplitDown={() => grid.splitLeaf(leaf.id, 'down')}
        />
      )
    }
    if (leaf.kind === 'terminal') {
      // Phase 2 — terminal panes (xterm/PTY) not wired yet.
      return (
        <div className="h-full flex items-center justify-center text-muted text-[12px] border border-border rounded-lg m-1">
          {i18nT('components.sessionGridView.terminal_pane_coming_in_phase_2')}
        </div>
      )
    }
    return (
      <PlaceholderPane
        slots={slots}
        occupied={grid.occupiedSlots}
        forkSourceSlot={forkSourceSlot}
        forkSourceTitle={forkSourceTitle}
        focused={grid.focusedId === leaf.id}
        onFocus={() => grid.setFocused(leaf.id)}
        onPick={(slot) => grid.fillLeaf(leaf.id, { kind: 'session', slot })}
        onCancel={() => grid.closeLeaf(leaf.id)}
        onSplitRight={() => grid.splitLeaf(leaf.id, 'right')}
        onSplitDown={() => grid.splitLeaf(leaf.id, 'down')}
      />
    )
  }

  return (
    <div className="flex flex-col flex-1 min-h-0 bg-bg">
      {/* No app chrome — the split tree IS the chat surface. Per-pane headers carry
          the split/close controls; closing down to one session returns to native
          single chat (onCollapse). */}
      {grid.tree ? (
        <div className="flex-1 min-h-0 p-0.5">
          <SessionGridLayout node={grid.tree} renderLeaf={renderLeaf} onResize={grid.resize} />
        </div>
      ) : (
        <div className="flex-1 flex items-center justify-center text-muted text-sm">{i18nT('components.sessionGridView.loading')}</div>
      )}
    </div>
  )
}

function PlaceholderPane({
  slots,
  occupied,
  forkSourceSlot,
  forkSourceTitle,
  focused,
  onFocus,
  onPick,
  onCancel,
  onSplitRight,
  onSplitDown,
}: {
  slots: Slot[]
  occupied: string[]
  forkSourceSlot?: string
  forkSourceTitle?: string
  focused?: boolean
  onFocus: () => void
  onPick: (key: string) => void
  onCancel: () => void
  onSplitRight: () => void
  onSplitDown: () => void
}) {
  const [search, setSearch] = useState('')
  const queryClient = useQueryClient()
  const createSession = useMutation({
    mutationFn: () => api.createChatSlot(),
    onSuccess: (r: { key?: string }) => {
      queryClient.invalidateQueries({ queryKey: ['session-grid-slots'] })
      if (r?.key) onPick(r.key)
    },
  })
  const forkSession = useMutation({
    mutationFn: () => api.forkChatSlot(forkSourceSlot as string),
    onSuccess: (r: { ok?: boolean; key?: string }) => {
      queryClient.invalidateQueries({ queryKey: ['session-grid-slots'] })
      if (r?.ok && r.key) onPick(r.key)
    },
  })

  const available = slots
    .filter((s) => !occupied.includes(s.key))
    .filter(
      (s) =>
        !search || ((s.title || '') + s.key + (s.agent || '')).toLowerCase().includes(search.toLowerCase()),
    )
    .sort((a, b) => {
      if (!!a.pending_approval !== !!b.pending_approval) return a.pending_approval ? -1 : 1
      if (!!a.running !== !!b.running) return a.running ? -1 : 1
      return (b.last_activity_ts || '').localeCompare(a.last_activity_ts || '')
    })

  const ctrlBtn =
    'shrink-0 p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent border-none transition-colors'

  return (
    <div
      onMouseDownCapture={onFocus}
      className={`flex flex-col h-full border-[1.5px] border-dashed rounded-lg bg-bg overflow-hidden m-1 ${focused ? 'border-accent' : 'border-border'}`}
    >
      <div className="flex items-center gap-1 p-2 border-b border-border">
        <input
          autoFocus={focused}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={i18nT('components.sessionGridView.search_sessions')}
          className="flex-1 min-w-0 bg-bg-elevated border border-border rounded px-2 py-1 text-[13px] text-text placeholder:text-muted outline-none focus:border-accent"
        />
        <button onClick={onSplitRight} title={i18nT('components.sessionGridView.split_right_d')} aria-label={i18nT('components.sessionGridView.split_right')} className={ctrlBtn}>
          <SplitGlyph />
        </button>
        <button onClick={onSplitDown} title={i18nT('components.sessionGridView.split_down')} aria-label={i18nT('components.sessionGridView.split_down')} className={ctrlBtn}>
          <SplitGlyph down />
        </button>
        <button
          onClick={onCancel}
          title={i18nT('components.sessionGridView.close_cell')}
          aria-label={i18nT('components.sessionGridView.close_cell')}
          className="shrink-0 p-1 rounded text-muted hover:text-danger hover:bg-danger/10 cursor-pointer bg-transparent border-none transition-colors"
        >
          <X size={14} />
        </button>
      </div>

      {/* Three creation entry points (Terminal arrives in Phase 2). */}
      <div className="flex gap-1.5 p-2 border-b border-border">
        <button
          disabled={createSession.isPending}
          onClick={() => createSession.mutate()}
          className="flex-1 inline-flex items-center justify-center gap-1 text-[12px] font-semibold text-accent bg-accent/10 rounded px-2 py-1.5 cursor-pointer border-none hover:bg-accent/20 disabled:opacity-50 transition-colors"
        >
          {createSession.isPending ? <Loader2 size={13} className="animate-spin" /> : <Plus size={13} />} {i18nT('components.sessionGridView.new_session')}
        </button>
        <button
          disabled={!forkSourceSlot || forkSession.isPending}
          onClick={() => forkSession.mutate()}
          title={forkSourceSlot ? i18nT('components.sessionGridView.fork_child_session', { name: forkSourceTitle || forkSourceSlot }) : i18nT('components.sessionGridView.no_session_to_fork_yet')}
          className="flex-1 inline-flex items-center justify-center gap-1 text-[12px] font-semibold text-text border border-border rounded px-2 py-1.5 cursor-pointer bg-transparent hover:bg-bg-hover disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          {forkSession.isPending ? <Loader2 size={13} className="animate-spin" /> : <GitFork size={13} />} {i18nT('components.sessionGridView.fork')}
        </button>
      </div>

      <div className="flex-1 overflow-y-auto">
        {available.length === 0 ? (
          <div className="text-[12px] text-muted p-3 text-center">{i18nT('components.sessionGridView.no_matching_sessions')}</div>
        ) : (
          available.map((s) => (
            <button
              key={s.key}
              onClick={() => onPick(s.key)}
              className="w-full text-left px-3 py-2 hover:bg-bg-elevated text-[13px] cursor-pointer flex items-center gap-2 border-none bg-transparent text-text"
            >
              <Circle
                size={10}
                className={`shrink-0 ${s.pending_approval ? 'fill-warn text-warn' : s.running ? 'fill-ok text-ok' : 'fill-muted text-muted'}`}
              />
              <span className="truncate flex-1">{s.title || s.key}</span>
              <span className="text-[11px] text-muted shrink-0">{s.messages ?? 0} {i18nT('components.sessionGridView.msgs')}</span>
            </button>
          ))
        )}
      </div>
    </div>
  )
}
