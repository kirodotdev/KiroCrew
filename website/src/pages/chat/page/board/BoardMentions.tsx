/**
 * Session links on the board (PoC): the doc's `[title](session:<key>)` links, drawn
 * as the same chips in a shape's text, and the same "@" picker while a shape's text
 * is being edited. The chip and picker markup are the doc's (`SessionMention`); the
 * board pane mounts their styles (`MENTION_STYLE`, `MENU_STYLE`).
 */
import { Fragment, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode, type RefObject } from 'react'
import { MentionChip, MentionMenu, type SessionRef } from '../SessionMention'
import { SESSION_LINK, matchSessions, mentionMarkdown } from '../docMentions'

export interface BoardLinks {
  sessions: SessionRef[]
  /** The board's own session, which the picker leaves out. */
  self: string | null
  open: (key: string) => void
}

/** A shape's text with its session links drawn as chips. */
export function ShapeLinkText({ text, links }: { text: string; links: BoardLinks }): ReactNode {
  const byKey = new Map(links.sessions.map(s => [s.key, s]))
  const out: ReactNode[] = []
  let at = 0
  for (const m of text.matchAll(SESSION_LINK)) {
    const from = m.index ?? 0
    if (from > at) out.push(<Fragment key={`t${at}`}>{text.slice(at, from)}</Fragment>)
    const session = byKey.get(m[2]) ?? null
    out.push(
      <button key={`m${from}`} type="button" data-mention="" data-testid="session-mention" data-key={m[2]} data-kind={session?.kind ?? 'missing'}
        aria-label={session ? `Open session ${session.title}` : `Deleted session ${m[1]}`}
        title={session ? `Open ${session.title}` : 'This session no longer exists'}
        // Not a drag or a selection of the shape: only the click that opens it.
        onPointerDown={e => e.stopPropagation()} onDoubleClick={e => e.stopPropagation()}
        onClick={e => { e.stopPropagation(); if (session) links.open(session.key) }}>
        <MentionChip session={session} fallback={m[1]} />
      </button>,
    )
    at = from + m[0].length
  }
  if (at < text.length) out.push(<Fragment key={`t${at}`}>{text.slice(at)}</Fragment>)
  return out
}

interface Open { from: number; query: string; index: number }

/**
 * The "@" picker for a shape's textarea. Hand `onChange` and `onKeyDown` to the
 * textarea (the latter first, before the shape's own keys) and render `menu`
 * inside the shape; *zoom* keeps the menu its normal size on a zoomed board.
 */
export function useMentionPicker({ value, onText, links, field, zoom }: {
  value: string
  onText: (next: string) => void
  links: BoardLinks
  field: RefObject<HTMLTextAreaElement | null>
  zoom: number
}) {
  const [open, setOpen] = useState<Open | null>(null)
  const data = { sessions: links.sessions, self: links.self }
  // Where the caret goes once a picked link is in the text: set as soon as the
  // new value is drawn, before another key can land, not a frame later.
  const pendingCaret = useRef<number | null>(null)
  useLayoutEffect(() => {
    const at = pendingCaret.current
    if (at === null || !field.current) return
    pendingCaret.current = null
    field.current.focus()
    field.current.setSelectionRange(at, at)
  }, [value, field])
  const items = open ? matchSessions(data, open.query) : []

  /** *typed*: what the keystroke inserted (InputEvent.data), so "@" typed over a
   *  selection opens the picker too; without it, a one-character growth counts. */
  const onChange = (next: string, caret: number, typed?: string | null) => {
    const inserted = typed !== undefined ? typed : next.length === value.length + 1 ? next[caret - 1] : null
    let o = open
    if (o) {
      const query = next.slice(o.from + 1, caret)
      const gone = next[o.from] !== '@' || caret <= o.from || query.includes('\n') || query.length > 60
      // Prose that only looks like a mention ("@home") closes it by matching nothing.
      o = gone || (query && !matchSessions(data, query).length) ? null : { ...o, query, index: 0 }
    } else if (inserted === '@' && next[caret - 1] === '@' && (caret === 1 || /[\s([{]/.test(next[caret - 2]))) {
      o = { from: caret - 1, query: '', index: 0 }
    }
    setOpen(o)
    onText(next)
  }

  const pick = (s: SessionRef) => {
    if (!open) return
    const el = field.current
    const caret = el?.selectionStart ?? value.length
    const insert = `${mentionMarkdown(s)} `
    const next = value.slice(0, open.from) + insert + value.slice(caret)
    pendingCaret.current = open.from + insert.length
    setOpen(null)
    onText(next)
  }

  /** True when the picker took the key. */
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>): boolean => {
    if (!open) return false
    const n = items.length
    if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && n) {
      e.preventDefault()
      setOpen({ ...open, index: (open.index + (e.key === 'ArrowDown' ? 1 : -1) + n) % n })
      return true
    }
    if ((e.key === 'Enter' || e.key === 'Tab') && n) {
      e.preventDefault()
      pick(items[Math.min(open.index, n - 1)])
      return true
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      setOpen(null)
      return true
    }
    return false
  }

  const menu = open ? (
    <div data-testid="board-mention-picker" onPointerDown={e => e.stopPropagation()}
      style={{ position: 'absolute', top: '100%', left: 0, zIndex: 40, transform: `scale(${1 / zoom})`, transformOrigin: 'top left' }}>
      <MentionMenu items={items} query={open.query} index={Math.min(open.index, Math.max(0, items.length - 1))}
        onPick={i => pick(items[i])} onHover={i => setOpen({ ...open, index: i })} />
    </div>
  ) : null

  return { onChange, onKeyDown, menu, close: () => setOpen(null) }
}
