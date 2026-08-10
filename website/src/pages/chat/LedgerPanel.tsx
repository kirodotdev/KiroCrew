import { useState, useRef, useCallback, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { NotebookPen, Pin, PinOff, Pencil, Eye, Trash2, ChevronDown, Plus, Link } from 'lucide-react'
import {
  listLedgers, createLedger, getLedger, updateLedger, deleteLedger,
  toggleLedgerLine, pinLedgerToSlot, LedgerApiError,
  type LedgerSummary, type Ledger,
} from '../../api/ledgerApi'
import { isCheckboxLine } from './ledgerHelpers'
import { i18nT } from '../../i18n/t'
import MarkdownRenderer, { TaskToggleCtx } from '../../components/MarkdownRenderer'

interface LedgerPanelProps {
  slot: string
  ledgerId?: string | null
  /** Opens a path in the Files tab (same handler chat messages use). Without
   *  it, clicking a `code` path falls back to api.revealPath (host-side
   *  reveal), which is invisible inside the dashboard. */
  onFileOpen?: (path: string) => void
}

export default function LedgerPanel({ slot, ledgerId, onFileOpen }: LedgerPanelProps) {
  const qc = useQueryClient()
  const [activeLedgerId, setActiveLedgerId] = useState<string | null>(ledgerId ?? null)
  const [editMode, setEditMode] = useState(false)
  const [editBuffer, setEditBuffer] = useState('')
  const [dirty, setDirty] = useState(false)
  const [conflictBanner, setConflictBanner] = useState<{ theirContent: string; theirVersion: number } | null>(null)
  const [showSwitcher, setShowSwitcher] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [renameValue, setRenameValue] = useState('')
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const baseVersionRef = useRef<number>(0)

  // Sync activeLedgerId when prop changes
  useEffect(() => { if (ledgerId) setActiveLedgerId(ledgerId) }, [ledgerId])

  // ── Queries ──
  const { data: allLedgers = [] } = useQuery<LedgerSummary[]>({
    queryKey: ['ledgers'],
    queryFn: listLedgers,
    refetchInterval: 10000,
  })

  // No explicit ledgerId prop: derive this slot's pin from pinned_by in the
  // list — self-contained, no ChatPage state threading needed.
  useEffect(() => {
    if (!activeLedgerId && !ledgerId) {
      const pinned = allLedgers.find(l => l.pinned_by?.includes(slot))
      if (pinned) setActiveLedgerId(pinned.id)
    }
  }, [allLedgers, activeLedgerId, ledgerId, slot])

  const { data: ledger } = useQuery<Ledger>({
    queryKey: ['ledger', activeLedgerId],
    queryFn: () => getLedger(activeLedgerId!),
    enabled: !!activeLedgerId,
    refetchInterval: 10000,
  })

  // Track version
  useEffect(() => {
    if (ledger) {
      baseVersionRef.current = ledger.version
      if (!dirty) setEditBuffer(ledger.content)
    }
  }, [ledger, dirty])

  // Live update: if server version > local and not dirty, adopt
  useEffect(() => {
    if (ledger && !dirty && ledger.version > baseVersionRef.current) {
      setEditBuffer(ledger.content)
      baseVersionRef.current = ledger.version
    }
  }, [ledger, dirty])

  // ── Mutations ──
  const createMut = useMutation({
    mutationFn: (title?: string) => createLedger(title),
    onSuccess: async (newLedger) => {
      setActiveLedgerId(newLedger.id)
      await pinLedgerToSlot(slot, newLedger.id)
      qc.invalidateQueries({ queryKey: ['ledgers'] })
    },
  })

  const saveMut = useMutation({
    mutationFn: (args: { content: string; version: number }) =>
      updateLedger(activeLedgerId!, { content: args.content, base_version: args.version }),
    onSuccess: (res) => {
      baseVersionRef.current = res.version
      setDirty(false)
      setConflictBanner(null)
      qc.invalidateQueries({ queryKey: ['ledger', activeLedgerId] })
      qc.invalidateQueries({ queryKey: ['ledgers'] })
    },
    onError: (err) => {
      if (err instanceof LedgerApiError && err.status === 409) {
        const conflict = err.body as { current: { content: string; version: number } }
        if (!dirty) {
          // silently adopt
          baseVersionRef.current = conflict.current.version
          setEditBuffer(conflict.current.content)
        } else {
          setConflictBanner({ theirContent: conflict.current.content, theirVersion: conflict.current.version })
        }
      }
    },
  })

  const toggleMut = useMutation({
    mutationFn: (args: { line: number; expected: string }) =>
      toggleLedgerLine(activeLedgerId!, args.line, args.expected),
    onSuccess: (res) => {
      baseVersionRef.current = res.version
      setEditBuffer(res.content)
      setDirty(false)
      qc.invalidateQueries({ queryKey: ['ledger', activeLedgerId] })
      qc.invalidateQueries({ queryKey: ['ledgers'] })
    },
  })

  const deleteMut = useMutation({
    mutationFn: () => deleteLedger(activeLedgerId!),
    onSuccess: () => {
      setActiveLedgerId(null)
      qc.invalidateQueries({ queryKey: ['ledgers'] })
    },
  })

  const renameMut = useMutation({
    mutationFn: (title: string) => updateLedger(activeLedgerId!, { title }),
    onSuccess: () => {
      setRenaming(false)
      qc.invalidateQueries({ queryKey: ['ledgers'] })
      qc.invalidateQueries({ queryKey: ['ledger', activeLedgerId] })
    },
  })

  // ── Autosave ──
  const scheduleSave = useCallback((content: string) => {
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current)
    saveTimerRef.current = setTimeout(() => {
      saveMut.mutate({ content, version: baseVersionRef.current })
    }, 1500)
  }, [saveMut])

  const handleEditChange = (value: string) => {
    setEditBuffer(value)
    setDirty(true)
    scheduleSave(value)
  }

  const handleBlur = () => {
    if (dirty && saveTimerRef.current) {
      clearTimeout(saveTimerRef.current)
      saveMut.mutate({ content: editBuffer, version: baseVersionRef.current })
    }
  }

  // ── Conflict resolution ──
  const takeTheirs = () => {
    if (!conflictBanner) return
    baseVersionRef.current = conflictBanner.theirVersion
    setEditBuffer(conflictBanner.theirContent)
    setDirty(false)
    setConflictBanner(null)
  }

  const keepMine = () => {
    if (!conflictBanner) return
    saveMut.mutate({ content: editBuffer, version: conflictBanner.theirVersion })
  }

  // ── Checkbox click in view mode ──
  // Receives the ABSOLUTE 0-based source line index from the renderer's
  // TaskToggleCtx (data-sourcepos mapping). The exact current line text is
  // sent as the API's expected-text staleness guard, so a mismapped index
  // can only 409 — never toggle the wrong line.
  const handleCheckboxToggle = (lineIndex: number) => {
    const lineText = editBuffer.split('\n')[lineIndex]
    if (lineText === undefined || !isCheckboxLine(lineText)) return
    toggleMut.mutate({ line: lineIndex, expected: lineText })
  }

  // ── Pin / unpin ──
  const handlePin = async (id: string) => {
    await pinLedgerToSlot(slot, id)
    setActiveLedgerId(id)
    setShowSwitcher(false)
    qc.invalidateQueries({ queryKey: ['ledgers'] })
  }

  const handleUnpin = async () => {
    await pinLedgerToSlot(slot, '')
    qc.invalidateQueries({ queryKey: ['ledgers'] })
  }

  // ── Empty state ──
  if (!activeLedgerId) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-4 p-6">
        <NotebookPen size={40} className="text-muted opacity-50" />
        <p className="text-[13px] text-muted text-center">
          No ledger attached to this session.
        </p>
        <div className="flex gap-2">
          <button
            onClick={() => createMut.mutate(undefined)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[12px] font-medium bg-accent text-accent-fg border-none cursor-pointer hover:opacity-90 transition-opacity"
          >
            <Plus size={13} /> Create ledger
          </button>
          {allLedgers.length > 0 && (
            <button
              onClick={() => setShowSwitcher(true)}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[12px] font-medium border border-border bg-transparent text-text cursor-pointer hover:bg-bg-hover transition-colors"
            >
              <Link size={13} /> Attach existing
            </button>
          )}
        </div>
        {showSwitcher && (
          <div className="w-full max-w-[280px] mt-2 rounded-md border border-border bg-bg shadow-lg overflow-hidden">
            {allLedgers.map(l => (
              <button
                key={l.id}
                onClick={() => handlePin(l.id)}
                className="w-full flex items-center gap-2 px-3 py-2 text-left text-[12px] text-text bg-transparent border-none cursor-pointer hover:bg-bg-hover transition-colors"
              >
                <NotebookPen size={12} className="text-muted shrink-0" />
                <span className="truncate flex-1">{l.title}</span>
                {l.pinned_by.length > 0 && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-accent/12 text-accent shrink-0">
                    {l.pinned_by.length} pinned
                  </span>
                )}
              </button>
            ))}
          </div>
        )}
      </div>
    )
  }

  // ── Active ledger view ──
  const isPinned = ledger?.pinned_by?.includes(slot)
  const title = ledger?.title ?? 'Untitled'

  return (
    <div className="flex flex-col h-full">
      {/* HEADER */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border shrink-0 min-h-[40px]">
        {renaming ? (
          <input
            autoFocus
            value={renameValue}
            onChange={e => setRenameValue(e.target.value)}
            onBlur={() => { if (renameValue.trim()) renameMut.mutate(renameValue.trim()); else setRenaming(false) }}
            onKeyDown={e => { if (e.key === 'Enter' && renameValue.trim()) renameMut.mutate(renameValue.trim()); if (e.key === 'Escape') setRenaming(false) }}
            className="flex-1 text-[13px] font-medium bg-transparent border border-border rounded px-1.5 py-0.5 text-text outline-none"
          />
        ) : (
          <button
            onClick={() => { setRenaming(true); setRenameValue(title) }}
            className="flex-1 text-left text-[13px] font-medium text-text truncate bg-transparent border-none cursor-pointer p-0 hover:text-accent transition-colors"
            title="Click to rename"
          >
            {title}
          </button>
        )}

        {/* Pinned badge */}
        {ledger && ledger.pinned_by.length > 0 && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-accent/12 text-accent font-medium shrink-0">
            {ledger.pinned_by.length}
          </span>
        )}

        {/* Switcher dropdown */}
        <div className="relative">
          <button
            onClick={() => setShowSwitcher(v => !v)}
            className="flex items-center justify-center w-[26px] h-[26px] rounded-md cursor-pointer transition-colors text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none"
            title="Switch ledger"
          >
            <ChevronDown size={14} />
          </button>
          {showSwitcher && (
            <div className="absolute right-0 top-full mt-1 z-50 w-[220px] rounded-md border border-border bg-bg shadow-lg overflow-hidden">
              {allLedgers.map(l => (
                <button
                  key={l.id}
                  onClick={() => { setActiveLedgerId(l.id); setShowSwitcher(false); setDirty(false) }}
                  className={`w-full flex items-center gap-2 px-3 py-2 text-left text-[12px] bg-transparent border-none cursor-pointer hover:bg-bg-hover transition-colors ${l.id === activeLedgerId ? 'text-accent font-medium' : 'text-text'}`}
                >
                  <NotebookPen size={11} className="shrink-0 opacity-60" />
                  <span className="truncate flex-1">{l.title}</span>
                  {l.pinned_by.includes(slot) && <Pin size={10} className="text-accent shrink-0" />}
                </button>
              ))}
              <button
                onClick={() => createMut.mutate(undefined)}
                className="w-full flex items-center gap-2 px-3 py-2 text-left text-[12px] text-muted bg-transparent border-none cursor-pointer hover:bg-bg-hover transition-colors border-t border-border"
              >
                <Plus size={11} /> New ledger
              </button>
            </div>
          )}
        </div>

        {/* Pin/unpin */}
        {isPinned ? (
          <button onClick={handleUnpin} className="flex items-center justify-center w-[26px] h-[26px] rounded-md cursor-pointer transition-colors text-accent hover:bg-bg-hover bg-transparent border-none" title="Unpin from this session">
            <PinOff size={14} />
          </button>
        ) : (
          <button onClick={() => handlePin(activeLedgerId)} className="flex items-center justify-center w-[26px] h-[26px] rounded-md cursor-pointer transition-colors text-muted hover:text-accent hover:bg-bg-hover bg-transparent border-none" title="Pin to this session">
            <Pin size={14} />
          </button>
        )}

        {/* Edit/View toggle */}
        <button
          onClick={() => { setEditMode(v => !v); if (editMode) handleBlur() }}
          className={`flex items-center justify-center w-[26px] h-[26px] rounded-md cursor-pointer transition-colors border-none ${editMode ? 'text-accent bg-accent/10' : 'text-muted hover:text-text hover:bg-bg-hover bg-transparent'}`}
          title={editMode ? 'Switch to view mode' : 'Switch to edit mode'}
        >
          {editMode ? <Eye size={14} /> : <Pencil size={14} />}
        </button>

        {/* Delete */}
        <button
          onClick={() => { if (confirm(i18nT('pages.chat.ledgerPanel.delete_confirm'))) deleteMut.mutate() }}
          className="flex items-center justify-center w-[26px] h-[26px] rounded-md cursor-pointer transition-colors text-muted hover:text-danger hover:bg-bg-hover bg-transparent border-none"
          title="Delete ledger"
        >
          <Trash2 size={14} />
        </button>
      </div>

      {/* CONFLICT BANNER */}
      {conflictBanner && (
        <div className="flex items-center gap-2 px-3 py-2 bg-warn-subtle border-b border-border text-[12px]">
          <span className="text-warn flex-1">Version conflict — someone else edited this ledger.</span>
          <button onClick={takeTheirs} className="px-2 py-1 rounded text-[11px] font-medium border border-border bg-transparent cursor-pointer hover:bg-bg-hover text-text">Take theirs</button>
          <button onClick={keepMine} className="px-2 py-1 rounded text-[11px] font-medium bg-accent text-accent-fg border-none cursor-pointer hover:opacity-90">Keep mine</button>
        </div>
      )}

      {/* BODY */}
      <div className="flex-1 overflow-y-auto p-3">
        {editMode ? (
          <textarea
            value={editBuffer}
            onChange={e => handleEditChange(e.target.value)}
            onBlur={handleBlur}
            className="w-full h-full resize-none bg-transparent border-none text-[13px] leading-relaxed text-text font-mono outline-none p-0"
            placeholder="Write your notes, tasks, scratch..."
            spellCheck={false}
          />
        ) : (
          <TaskToggleCtx.Provider value={handleCheckboxToggle}>
            <MarkdownRenderer content={editBuffer} sourcePos onFileOpen={onFileOpen} />
          </TaskToggleCtx.Provider>
        )}
      </div>

      {/* FOOTER STATUS */}
      {dirty && (
        <div className="px-3 py-1.5 text-[10px] text-muted border-t border-border shrink-0">
          Saving…
        </div>
      )}
    </div>
  )
}
