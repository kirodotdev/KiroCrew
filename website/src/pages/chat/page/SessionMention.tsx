/**
 * A link to another session inside a doc (PoC), after Notion's page mentions: an
 * inline chip with the session's kind icon and its live title, and the "@" picker
 * that inserts one. Drawn by `docMentions` inside the editor; styled there through
 * `data-mn` parts. Labels are English like the rest of the doc conductor layer.
 */
import { useEffect, useMemo, useRef, type ReactNode } from 'react'
import { Boxes, FileText, LayoutDashboard, MessageSquare, Shapes, type LucideIcon } from 'lucide-react'
import type { ChatSlot } from '../../../types'
import { isBoardId } from './board/boardModel'
import { isCodebaseId } from './codebase/codebaseModel'
import { isDashboardId } from './dashboard/dashboardModel'

export type SessionKind = 'chat' | 'doc' | 'board' | 'dashboard' | 'codebase'

export interface SessionRef {
  key: string
  title: string
  kind: SessionKind
  running: boolean
  /** The session's latest message, one line, for a board's session card. */
  preview?: string
}

export const ICON: Record<SessionKind, LucideIcon> = {
  chat: MessageSquare, doc: FileText, board: Shapes, dashboard: LayoutDashboard, codebase: Boxes,
}
export const KIND_LABEL: Record<SessionKind, string> = {
  chat: 'Chat', doc: 'Doc', board: 'Board', dashboard: 'Dashboard', codebase: 'Codebase',
}

export function sessionKind(slot: Pick<ChatSlot, 'doc'>): SessionKind {
  const doc = slot.doc
  if (!doc) return 'chat'
  if (isBoardId(doc)) return 'board'
  if (isDashboardId(doc)) return 'dashboard'
  if (isCodebaseId(doc)) return 'codebase'
  return 'doc'
}

export function toSessionRef(slot: ChatSlot): SessionRef {
  const preview = slot.last_message?.replace(/\s+/g, ' ').trim().slice(0, 160)
  return { key: slot.key, title: slot.title?.trim() || 'Untitled', kind: sessionKind(slot), running: !!slot.running, ...(preview ? { preview } : {}) }
}

/** What a session link can point at, most recently active first. The same list
 *  comes back until something a chip shows (title, kind, running) changes, so a
 *  slot update that only moves a timestamp does not redraw every chip. */
export function useSessionRefs(slots: ChatSlot[]): SessionRef[] {
  const raw = useMemo(() => [...slots]
    .sort((a, b) => (b.last_ts ?? b.created ?? '').localeCompare(a.last_ts ?? a.created ?? ''))
    .map(toSessionRef), [slots])
  const key = JSON.stringify(raw)
  // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed by content on purpose
  return useMemo(() => raw, [key])
}

/** The chip. *session* is the live row, or null when the linked session is gone. */
export function MentionChip({ session, fallback }: { session: SessionRef | null; fallback: string }) {
  const Icon = ICON[session?.kind ?? 'chat']
  return (
    <>
      <span data-mn="icon" aria-hidden><Icon size={14} strokeWidth={2} /></span>
      <span data-mn="title">{session ? session.title : fallback || 'Deleted session'}</span>
      {session?.running && <span data-mn="running" aria-hidden />}
    </>
  )
}

export function MentionMenu({ items, index, query, onPick, onHover, head }: {
  items: SessionRef[]
  index: number
  query: string
  onPick: (i: number) => void
  onHover: (i: number) => void
  /** Replaces the heading line: the board's picker puts its search field there. */
  head?: ReactNode
}) {
  const list = useRef<HTMLDivElement>(null)
  useEffect(() => {
    list.current?.querySelector<HTMLElement>(`[data-i="${index}"]`)?.scrollIntoView?.({ block: 'nearest' })
  }, [index])
  return (
    <div data-sm="root" data-testid="mention-menu" role="listbox" aria-label="Link to a session">
      {head ?? <div data-mn="menu-head">{query ? `Sessions matching “${query}”` : 'Link to a session'}</div>}
      <div data-sm="list" ref={list}>
        {items.length === 0 && <div data-mn="empty">No sessions match</div>}
        {items.map((s, i) => {
          const Icon = ICON[s.kind]
          return (
            // The editor keeps focus and drives the list; a focused option still answers Enter.
            <div key={s.key} role="option" tabIndex={-1} aria-selected={i === index} data-sm="item" data-i={i} data-testid="mention-item" data-key={s.key}
              onMouseDown={e => e.preventDefault()} onMouseMove={() => { if (i !== index) onHover(i) }} onClick={() => onPick(i)}
              onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); onPick(i) } }}>
              <span data-sm="icon"><Icon size={16} aria-hidden /></span>
              <span data-sm="label">{s.title}</span>
              <span data-mn="kind">{KIND_LABEL[s.kind]}</span>
            </div>
          )
        })}
      </div>
      <div data-sm="foot"><kbd>↑</kbd><kbd>↓</kbd> choose · <kbd>Enter</kbd> link · <kbd>Esc</kbd> close</div>
    </div>
  )
}
