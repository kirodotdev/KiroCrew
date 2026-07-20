import { safeSetItem } from '../utils/safeStorage'
import React, { useState, useEffect, useCallback, useRef, memo } from 'react'
import { motion } from 'framer-motion'
import { X, ArrowUpDown, Plus, TerminalSquare } from 'lucide-react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'
import '@xterm/xterm/css/xterm.css'
import { useAppDispatch, useAppSelector } from '../store'
import {
  closeCliPanel,
  setCliPanelPosition,
  addSession,
  removeSession,
  setActiveSession,
  renameSession,
  setSessions,
  loadLabels,
  saveLabels,
  removeLabel,
  type TerminalSession,
} from '../store/terminalSlice'
import { useTerminalWs } from '../hooks/useTerminalWs'
import { setActiveTerminalSession } from '../utils/terminalRegistry'
import { usePointerDrag, rubberband } from '../hooks/usePointerDrag'

const HEIGHT_KEY = 'kirocrew-terminal-height'
const WIDTH_KEY = 'kirocrew-terminal-width'
const MIN_H = 120
const MIN_W = 300
const DEFAULT_H = 280
const DEFAULT_W = 420

/* ── Per-session xterm instance cache ── */
const termCache = new Map<string, { term: Terminal; fit: FitAddon }>()

/* ── Terminal theme from CSS custom properties ── */
function getTermTheme() {
  const style = getComputedStyle(document.documentElement)
  return {
    background:          style.getPropertyValue('--bg-elevated').trim()   || '#1e1e2e',
    foreground:          style.getPropertyValue('--text').trim()          || '#cdd6f4',
    cursor:              style.getPropertyValue('--accent').trim()        || '#89b4fa',
    selectionBackground: style.getPropertyValue('--accent-subtle').trim() || '#313244',
  }
}

/** Refresh theme on all cached terminals (called on theme change). */
function refreshTermThemes() {
  const theme = getTermTheme()
  for (const { term } of termCache.values()) {
    term.options.theme = theme
  }
}

function getOrCreateTerm(id: string): { term: Terminal; fit: FitAddon } {
  let entry = termCache.get(id)
  if (!entry) {
    const term = new Terminal({
      cursorBlink: true,
      fontSize: 13,
      fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",
      theme: getTermTheme(),
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.loadAddon(new WebLinksAddon())
    entry = { term, fit }
    termCache.set(id, entry)
  }
  return entry
}

function destroyTerm(id: string) {
  const entry = termCache.get(id)
  if (entry) {
    entry.term.dispose()
    termCache.delete(id)
  }
}

/** Refit all cached terminals (called after animation/resize). */
function refitAll() {
  for (const entry of termCache.values()) {
    entry.fit.fit()
  }
}

/* ── Session chip (memoized) ── */
const SessionChip = memo(function SessionChip({
  session,
  active,
  onSelect,
  onClose,
  onRename,
}: {
  session: TerminalSession
  active: boolean
  onSelect: () => void
  onClose: (e: React.MouseEvent | React.KeyboardEvent) => void
  onRename: (label: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(session.label)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { if (editing) inputRef.current?.select() }, [editing])

  const commit = () => {
    setEditing(false)
    const trimmed = draft.trim()
    if (trimmed && trimmed !== session.label) onRename(trimmed)
    else setDraft(session.label)
  }

  return (
    <button
      onClick={onSelect}
      onDoubleClick={() => { setDraft(session.label); setEditing(true) }}
      className={`flex items-center gap-1.5 px-2.5 py-1 rounded text-[13px] whitespace-nowrap shrink-0 transition-colors ${
        active
          ? 'bg-accent/20 text-accent border border-accent/40'
          : 'bg-bg-subtle text-text-muted border border-border hover:bg-bg-hover'
      }`}
    >
      <TerminalSquare size={12} />
      {editing ? (
        <input
          ref={inputRef}
          aria-label="Rename tab"
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={e => { if (e.key === 'Enter') e.currentTarget.blur(); if (e.key === 'Escape') { setDraft(session.label); setEditing(false) } }}
          className="w-[80px] bg-transparent border-none outline-none text-[13px] p-0"
          onClick={e => e.stopPropagation()}
        />
      ) : (
        <span className="max-w-[120px] truncate">{session.label}</span>
      )}
      <span
        role="button"
        tabIndex={0}
        aria-label="Close tab"
        onClick={onClose}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClose(e) } }}
        className="ml-0.5 hover:text-red-400 transition-colors"
      >
        <X size={10} />
      </span>
    </button>
  )
})

/**
 * Force xterm to re-measure the character cell, then refit. xterm measures the
 * cell at open()/fit() time with whatever font is resolvable then; the terminal
 * font ('JetBrains Mono') is a Google web font (display=swap) that can swap in
 * *after* the first measure, widening the cell while `cols` stays stale, so the
 * screen overflows the pane and the right edge is clipped (Mesh-2148, worst in
 * the narrow right sidebar). xterm exposes no public "re-measure now" API, so we
 * force CharSizeService.measure() via a transient fontFamily toggle (both sets
 * run synchronously, so nothing paints between them). Verified against xterm
 * 5.5.x, where CharSizeService re-measures on its
 * `onMultipleOptionChange(['fontFamily','fontSize'])` subscription;
 * CliPanel.fontRefit.test.ts pins that version so a future bump fails loudly.
 *
 * Skips detached / display:none panes (offsetParent === null), matching the
 * sibling refit effects (the ResizeObserver gates on offsetHeight, the focus
 * effect on `visible`): measuring a zero-size pane would cache a 0-width cell.
 * Such a pane is re-measured by the becoming-visible refit when next shown.
 */
export function remeasureAndFit(term: Terminal, fit: FitAddon): void {
  const el = term.element
  if (!el || !el.offsetParent) return // offsetParent === null when display:none / detached
  const ff = term.options.fontFamily
  term.options.fontFamily = 'monospace' // transient — forces CharSizeService.measure()
  term.options.fontFamily = ff          // restore — re-measures with the now-loaded font
  fit.fit()
}

/* ── Terminal view for one session ── */
function TerminalView({ sessionId, visible }: { sessionId: string; visible: boolean }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const entryRef = useRef<{ term: Terminal; fit: FitAddon } | null>(null)

  if (!entryRef.current) {
    entryRef.current = getOrCreateTerm(sessionId)
  }
  const { term, fit } = entryRef.current

  // Re-measure + refit, gated on pane visibility (see remeasureAndFit). Stable
  // per cached term/fit, so the effects below don't re-subscribe.
  const doRefit = useCallback(() => remeasureAndFit(term, fit), [term, fit])

  useTerminalWs(sessionId, term, fit)

  // Attach terminal to DOM
  useEffect(() => {
    if (!containerRef.current) return
    const el = term.element
    if (el) {
      containerRef.current.appendChild(el)
    } else {
      term.open(containerRef.current)
    }
    fit.fit()
  }, [term, fit])

  // Focus + refit when becoming visible. Re-measure here too so a pane that
  // gained the web font while it was hidden (display:none) picks up the correct
  // cell metrics the moment it is shown.
  useEffect(() => {
    if (!visible) return
    const raf = requestAnimationFrame(doRefit)
    term.focus()
    return () => cancelAnimationFrame(raf)
  }, [visible, term, doRefit])

  // Refit on container resize — always observe, not just when visible
  useEffect(() => {
    if (!containerRef.current) return
    const ro = new ResizeObserver(() => {
      if (containerRef.current?.offsetHeight) fit.fit()
    })
    ro.observe(containerRef.current)
    return () => ro.disconnect()
  }, [fit]) // stable — only depends on fit ref from cache

  // Refit after web fonts finish loading (see remeasureAndFit). `ready` resolves
  // once all pending @font-face loads settle; we also explicitly request the
  // terminal font in case it has not started loading when `ready` first fires.
  // doRefit no-ops on hidden panes — those are caught by the becoming-visible
  // refit above.
  useEffect(() => {
    const fonts = document.fonts
    if (!fonts) return
    let cancelled = false
    const onReady = () => { if (!cancelled) doRefit() }
    // `ready` resolves immediately (harmless no-op) if the font loaded before mount.
    fonts.ready.then(onReady)
    try {
      const px = term.options.fontSize ?? 13
      fonts.load(`${px}px "JetBrains Mono"`).then(onReady, () => {})
    } catch { /* invalid spec on some engines — `ready` handler covers it */ }
    return () => { cancelled = true }
  }, [term, doRefit]) // stable — term/doRefit come from the per-session cache

  // xterm instances persist in termCache across panel close/open — only destroyed on explicit tab close

  return (
    <div
      ref={containerRef}
      className="flex-1 min-h-0 h-full overflow-hidden"
      style={{ display: visible ? 'block' : 'none' }}
    />
  )
}

/* ── Main panel ── */
export default function CliPanel() {
  const dispatch = useAppDispatch()
  const { position, sessions, activeSessionId } = useAppSelector(s => s.terminal)
  const isBottom = position === 'bottom'

  function readSize(bottom: boolean): number {
    const key = bottom ? HEIGHT_KEY : WIDTH_KEY
    const min = bottom ? MIN_H : MIN_W
    const def = bottom ? DEFAULT_H : DEFAULT_W
    const v = parseInt(localStorage.getItem(key) || '', 10)
    return !isNaN(v) && v >= min ? v : def
  }

  const [size, setSize] = useState(() => readSize(isBottom))
  const [prevPosition, setPrevPosition] = useState(position)

  // Reset size when position changes (synchronous, no useEffect needed)
  if (position !== prevPosition) {
    setPrevPosition(position)
    setSize(readSize(isBottom))
  }

  const MAX_TABS = 3

  function createSession() {
    if (sessions.length >= MAX_TABS) {
      // Brief visual feedback — could be a toast, but console + title flash is zero-dep
      const btn = document.querySelector('[title="New terminal"]')
      if (btn) {
        btn.classList.add('text-red-400')
        setTimeout(() => btn.classList.remove('text-red-400'), 1000)
      }
      return
    }
    const id = Math.random().toString(36).slice(2, 10)
    dispatch(addSession({ id, label: 'bash' }))
  }

  useEffect(() => {
    let cancelled = false
    async function reconnect() {
      try {
        const r = await fetch('/api/terminal/sessions')
        if (cancelled) return
        if (!r.ok) { createSession(); return }
        const data = await r.json()
        const alive = (data.sessions ?? []).filter((s: { alive: boolean }) => s.alive)
        if (alive.length === 0) { createSession(); return }
        const labels = loadLabels()
        const restored: TerminalSession[] = alive.map((s: { session_id: string }) => ({
          id: s.session_id,
          label: labels[s.session_id] || 'bash',
        }))
        // Clean labels for dead sessions
        const aliveIds = new Set(alive.map((s: { session_id: string }) => s.session_id))
        for (const id of Object.keys(labels)) {
          if (!aliveIds.has(id)) removeLabel(id)
        }
        if (!cancelled) dispatch(setSessions(restored))
      } catch {
        if (!cancelled) createSession()
      }
    }
    if (sessions.length === 0) reconnect()
    return () => { cancelled = true }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Refresh terminal theme colors when the user switches themes
  useEffect(() => {
    const observer = new MutationObserver(() => refreshTermThemes())
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    setActiveTerminalSession(activeSessionId)
  }, [activeSessionId])

  // Stable callbacks for SessionChip (prevent re-renders)
  const handleSelect = useCallback((id: string) => dispatch(setActiveSession(id)), [dispatch])
  const handleClose = useCallback((id: string, e: React.MouseEvent | React.KeyboardEvent) => {
    e.stopPropagation()
    // Optimistic: instant UI cleanup, backend delete is fire-and-forget
    destroyTerm(id)
    removeLabel(id)
    dispatch(removeSession(id))
    fetch(`/api/terminal/sessions/${id}`, { method: 'DELETE' }).catch(() => {})
  }, [dispatch])
  const handleRename = useCallback((id: string, label: string) => {
    dispatch(renameSession({ id, label }))
    const labels = loadLabels()
    labels[id] = label
    saveLabels(labels)
  }, [dispatch])

  // ── Resize drag — use refs for size to avoid recreating on every pixel ──
  const sizeRef = useRef(size)
  sizeRef.current = size
  const isBottomRef = useRef(isBottom)
  isBottomRef.current = isBottom

  // Size captured at pointer-down so live moves are computed from a stable origin.
  const dragStartSize = useRef(size)

  const bounds = () => {
    const bottom = isBottomRef.current
    const min = bottom ? MIN_H : MIN_W
    // Cap the panel so it can't swallow the whole viewport; leave `min` for the rest of the UI.
    const max = Math.max(min, (bottom ? window.innerHeight : window.innerWidth) - min)
    return { min, max }
  }

  // Pointer Events (mouse + touch) via the shared hook. Grows the panel as the
  // splitter is dragged toward the panel's anchored edge; rubber-bands past max.
  const drag = usePointerDrag({
    threshold: 6,
    onStart: () => { dragStartSize.current = sizeRef.current },
    onMove: ({ dx, dy }) => {
      const { min, max } = bounds()
      // Bottom panel is anchored to the viewport bottom → dragging up (dy < 0) grows it;
      // right panel is anchored to the right edge → dragging left (dx < 0) grows it.
      const raw = dragStartSize.current - (isBottomRef.current ? dy : dx)
      let next = raw
      if (raw > max) next = max + rubberband(raw - max, max) // progressive resistance past the cap
      else if (raw < min) next = min                          // hard floor (matches prior behavior)
      setSize(next)
    },
    onEnd: () => {
      setSize(s => {
        const { min, max } = bounds()
        const clamped = Math.min(max, Math.max(min, s)) // settle back inside [min, max]
        safeSetItem(isBottomRef.current ? HEIGHT_KEY : WIDTH_KEY, String(clamped))
        return clamped
      })
    },
  })

  return (
    <motion.div
      initial={isBottom ? { height: 0, width: '100%' } : { width: 0, height: '100%' }}
      animate={isBottom ? { height: size, width: '100%' } : { width: size, height: '100%' }}
      exit={isBottom ? { height: 0, width: '100%' } : { width: 0, height: '100%' }}
      transition={{ type: 'spring', bounce: 0, duration: 0.3 }}
      onAnimationComplete={refitAll}
      className={`shrink-0 overflow-hidden border border-border rounded-lg bg-bg ${isBottom ? 'ml-0 mr-2 mb-2 mt-0' : 'ml-0 mr-2 my-2'}`}
      style={isBottom ? undefined : { minWidth: MIN_W }}
    >
      <div
        className="flex flex-col overflow-hidden relative w-full h-full"
      >
        {/* Resize handle — a Pointer-Events drag splitter (mouse + touch) carrying
            the correct role="separator"/aria-orientation semantics. The rule treats
            "separator" as non-interactive, so the drag handlers are flagged despite
            the role being the ARIA-correct choice for a resizer. */}
        <div
          role="separator"
          aria-orientation={isBottom ? 'horizontal' : 'vertical'}
          aria-label="Resize terminal panel"
          className={`absolute z-20 group/drag touch-none ${
            isBottom
              ? 'left-0 right-0 top-0 h-[6px] cursor-ns-resize'
              : 'left-0 top-0 bottom-0 w-[6px] cursor-col-resize'
          }`}
          {...drag}
        >
          <div
            className={`absolute transition-colors duration-200 bg-transparent group-hover/drag:bg-accent ${
              isBottom ? 'left-0 right-0 top-0 h-[2px]' : 'left-0 top-0 bottom-0 w-[2px]'
            }`}
          />
        </div>

        {/* Header */}
        <div className="flex items-center gap-1.5 px-3 h-9 shrink-0 border-b border-border">
          <div className="flex items-center gap-1 flex-1 min-w-0 overflow-x-auto scrollbar-none">
            {sessions.map(s => (
              <SessionChip
                key={s.id}
                session={s}
                active={s.id === activeSessionId}
                onSelect={() => handleSelect(s.id)}
                onClose={(e) => handleClose(s.id, e)}
                onRename={(label) => handleRename(s.id, label)}
              />
            ))}
            <button
              onClick={createSession}
              aria-label={sessions.length >= MAX_TABS ? `Max ${MAX_TABS} terminals` : 'New terminal'}
              className={`p-1 rounded transition-colors shrink-0 ${sessions.length >= MAX_TABS ? 'text-text-muted/40 cursor-not-allowed' : 'text-text-muted hover:text-text-strong hover:bg-bg-hover'}`}
              title={sessions.length >= MAX_TABS ? `Max ${MAX_TABS} terminals` : 'New terminal'}
            >
              <Plus size={14} />
            </button>
          </div>

          <button
            onClick={() => dispatch(setCliPanelPosition(isBottom ? 'right' : 'bottom'))}
            className="p-1 rounded text-text-muted hover:text-text-strong hover:bg-bg-hover transition-colors"
            title={`Move to ${isBottom ? 'right' : 'bottom'}`}
          >
            <ArrowUpDown size={14} />
          </button>
          <button
            onClick={() => dispatch(closeCliPanel())}
            className="p-1 rounded text-text-muted hover:text-text-strong hover:bg-bg-hover transition-colors"
            title="Close terminal"
          >
            <X size={14} />
          </button>
        </div>

        {/* Terminal views */}
        <div className="flex-1 min-h-0 relative overflow-hidden">
          {sessions.map(s => (
            <TerminalView key={s.id} sessionId={s.id} visible={s.id === activeSessionId} />
          ))}
          {sessions.length === 0 && (
            <div className="flex items-center justify-center h-full text-text-muted text-sm">
              <button onClick={createSession} className="hover:text-accent transition-colors">
                + New Terminal
              </button>
            </div>
          )}
        </div>
      </div>
    </motion.div>
  )
}
