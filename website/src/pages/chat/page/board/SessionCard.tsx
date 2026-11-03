/**
 * A session card on the board (PoC): a board element that stands for another
 * session (a chat, a doc, another board…). It draws the session's kind icon on a
 * tinted tile, its live title, what kind it is and whether it is working, and its
 * latest message. A double-click, or Open in its toolbar, opens the session.
 *
 * Also the picker the Link session tool opens where the person clicked: a search
 * field over the doc's session list. Labels are English like the rest of the board.
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Unlink } from 'lucide-react'
import { ICON, KIND_LABEL, MentionMenu, type SessionKind, type SessionRef } from '../SessionMention'
import { matchSessions } from '../docMentions'
import type { Shape } from './boardModel'

/** Each kind's tile colour, from the theme. */
const TINT: Record<SessionKind, string> = {
  chat: 'var(--info)', doc: 'var(--accent)', board: 'var(--ok)', dashboard: 'var(--warn)', codebase: 'var(--muted-strong)',
}

export function SessionCardView({ shape: s, session }: { shape: Shape; session: SessionRef | null }) {
  const Icon = session ? ICON[session.kind] : Unlink
  const tint = session ? TINT[session.kind] : 'var(--muted)'
  return (
    <div data-shape-id={s.id} data-testid="board-shape" data-kind="session" data-session={s.session} data-live={session ? 'true' : 'false'}
      style={{
        position: 'absolute', left: s.x, top: s.y, width: s.w, height: s.h, boxSizing: 'border-box',
        display: 'flex', flexDirection: 'column', gap: 10, padding: '14px 16px', overflow: 'hidden',
        borderRadius: 14, border: '1px solid var(--border)', background: 'var(--bg-elevated)', color: 'var(--text)',
        boxShadow: '0 1px 2px rgba(0,0,0,.08), 0 8px 22px -10px rgba(0,0,0,.28)',
      }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, minWidth: 0 }}>
        <span aria-hidden style={{
          display: 'flex', alignItems: 'center', justifyContent: 'center', width: 40, height: 40, flexShrink: 0, borderRadius: 11,
          background: `color-mix(in srgb, ${tint} 16%, transparent)`, color: tint,
        }}><Icon size={20} strokeWidth={2} /></span>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div data-testid="session-card-title" style={{
            fontSize: 15, fontWeight: 600, lineHeight: 1.3, color: session ? 'var(--text-strong)' : 'var(--muted)',
            textDecoration: session ? undefined : 'line-through', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          }}>{session ? session.title : s.text || 'Deleted session'}</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 2, fontSize: 12, color: 'var(--muted)' }} data-testid="session-card-meta">
            {session ? KIND_LABEL[session.kind] : 'This session no longer exists'}
            {session?.running && (
              <>
                <span aria-hidden>·</span>
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, color: 'var(--accent)' }}>
                  <span aria-hidden style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--accent)', animation: 'cm-ask-pulse 1.2s ease-in-out infinite' }} />
                  Working
                </span>
              </>
            )}
          </div>
        </div>
      </div>
      {session?.preview && (
        <div data-testid="session-card-preview" style={{
          fontSize: 12.5, lineHeight: 1.45, color: 'var(--muted-strong)',
          display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden',
        }}>{session.preview}</div>
      )}
    </div>
  )
}

/** The Link session tool's picker, at (x, y) in the canvas's own pixels. */
export function SessionPicker({ x, y, sessions, self, onPick, onClose }: {
  x: number
  y: number
  sessions: SessionRef[]
  self: string | null
  onPick: (s: SessionRef) => void
  onClose: () => void
}) {
  const [query, setQuery] = useState('')
  const [index, setIndex] = useState(0)
  const root = useRef<HTMLDivElement>(null)
  const field = useRef<HTMLInputElement>(null)
  const items = matchSessions({ sessions, self }, query)
  // A press elsewhere puts it away. Listening starts once the press that opened it
  // is over: React can run this effect while that press is still bubbling, and it
  // would count as a press outside and close the picker at once.
  const close = useRef(onClose)
  close.current = onClose
  useEffect(() => {
    const away = (e: PointerEvent) => { if (root.current && !root.current.contains(e.target as Node)) close.current() }
    const start = setTimeout(() => document.addEventListener('pointerdown', away), 0)
    return () => { clearTimeout(start); document.removeEventListener('pointerdown', away) }
  }, [])
  const search = (
    <div style={{ padding: '8px 8px 2px' }}>
      <input ref={field} value={query} placeholder="Search sessions…" aria-label="Search sessions" data-testid="session-picker-search"
        onChange={e => { setQuery(e.target.value); setIndex(0) }}
        onKeyDown={e => {
          // The board's own shortcuts stay out of the field.
          e.stopPropagation()
          const n = items.length
          if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && n) { e.preventDefault(); setIndex(i => (i + (e.key === 'ArrowDown' ? 1 : -1) + n) % n) }
          else if (e.key === 'Enter' && n) { e.preventDefault(); onPick(items[Math.min(index, n - 1)]) }
          else if (e.key === 'Escape') { e.preventDefault(); onClose() }
        }}
        style={{
          width: '100%', boxSizing: 'border-box', padding: '7px 10px', borderRadius: 8, border: '1px solid var(--border)',
          background: 'var(--bg)', color: 'var(--text)', font: 'inherit', fontSize: 14, outline: 'none',
        }} />
    </div>
  )
  // Kept inside the board: near its right or bottom edge it opens left of or above
  // the click instead of running off it.
  const [spot, setSpot] = useState({ left: x, top: y })
  useLayoutEffect(() => {
    const el = root.current
    const box = el?.offsetParent as HTMLElement | null
    if (!el || !box) return
    const w = el.offsetWidth
    const h = el.offsetHeight
    const left = Math.max(8, Math.min(x, box.clientWidth - w - 8))
    const top = y + h + 8 > box.clientHeight ? Math.max(8, y - h) : y
    setSpot(s => (s.left === left && s.top === top ? s : { left, top }))
  }, [x, y, items.length])
  // Focused without scrolling: the board clips its content, and a scrolled board
  // would jump sideways under the pointer.
  useEffect(() => { field.current?.focus({ preventScroll: true }) }, [])
  return (
    <div ref={root} data-board-ui data-testid="session-picker" onPointerDown={e => e.stopPropagation()}
      style={{ position: 'absolute', left: spot.left, top: spot.top, zIndex: 30 }}>
      <MentionMenu items={items} index={Math.min(index, Math.max(0, items.length - 1))} query={query} head={search}
        onPick={i => onPick(items[i])} onHover={setIndex} />
    </div>
  )
}
