/**
 * The conductor conversations on a page, as one horizontal bar (PoC), shared by
 * docs and boards: one chip per conversation in page order, with its state, and
 * ⌃⌥← / ⌃⌥→ to step through them. The host decides what "focus" means (a doc
 * scrolls to the highlight and opens its answer; a board pans to the shape).
 *
 * ⌃⌥ rather than ⌥ or ⌘⌥: ⌥+arrow moves the caret by word in the doc editor, and
 * ⌘⌥+arrow switches browser tabs.
 */
import { useEffect, useRef } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'

export type ConversationState = 'pending' | 'unread' | 'read'

export interface ConversationItem {
  id: string
  /** A few words from what the conversation is about. */
  label: string
  state: ConversationState
  /** Workers it started: how many are running, of how many. */
  workers?: { running: number; total: number }
}

/** -1 or 1 for the step-through shortcut (⌃⌥← / ⌃⌥→), else 0. */
export function cycleDirection(e: Pick<KeyboardEvent, 'key' | 'ctrlKey' | 'altKey' | 'metaKey' | 'shiftKey'>): -1 | 0 | 1 {
  if (!e.ctrlKey || !e.altKey || e.metaKey || e.shiftKey) return 0
  return e.key === 'ArrowLeft' ? -1 : e.key === 'ArrowRight' ? 1 : 0
}

/**
 * The id one step from *active* in *ids*. "None" is a stop of its own, between
 * the last and the first: none -> first -> ... -> last -> none. `null` is none.
 */
export function stepId(ids: string[], active: string | null, dir: -1 | 1): string | null {
  if (!ids.length) return null
  const stops: (string | null)[] = [null, ...ids]
  const at = Math.max(0, stops.indexOf(active))
  return stops[(at + dir + stops.length) % stops.length]
}

/** A short label from the text a conversation is about: its first line, markup stripped. */
export function shortLabel(text: string, max = 32): string {
  const line = text.split('\n').map(l => l.replace(/<!--[\s\S]*?-->/g, '').replace(/[#>*_`|[\]]/g, ' ').replace(/\s+/g, ' ').trim()).find(Boolean) ?? ''
  if (!line) return 'Untitled'
  return line.length > max ? `${line.slice(0, max - 1).trimEnd()}…` : line
}

const DOT: Record<ConversationState, { cls: string; color: string }> = {
  pending: { cls: 'animate-pulse', color: 'var(--accent)' },
  unread: { cls: '', color: 'var(--accent)' },
  read: { cls: '', color: 'color-mix(in srgb, var(--muted) 55%, transparent)' },
}

export default function ConversationBar({ items, activeId, onSelect }: {
  items: ConversationItem[]
  activeId: string | null
  /** `null` is "no conversation": the page goes back to plain writing. */
  onSelect: (id: string | null) => void
}) {
  const strip = useRef<HTMLDivElement>(null)
  // Keep the active chip in view as the shortcut walks past the bar's edge.
  useEffect(() => {
    const el = strip.current?.querySelector<HTMLElement>('[data-active="true"]')
    el?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
  }, [activeId])
  if (!items.length) return null
  const ids = items.map(i => i.id)
  const at = activeId ? ids.indexOf(activeId) : -1
  const step = (dir: -1 | 1) => onSelect(stepId(ids, activeId, dir))
  const arrow = 'flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-muted hover:bg-bg-hover hover:text-text'
  return (
    <div
      role="toolbar"
      aria-label="Conductor conversations"
      data-testid="conversation-bar"
      className="pointer-events-auto flex max-w-full items-center gap-1 rounded-xl border border-border bg-bg-elevated p-1 text-text shadow-md"
    >
      <button type="button" className={arrow} onClick={() => step(-1)} title="Previous conversation (⌃⌥←)" aria-label="Previous conversation" aria-keyshortcuts="Control+Alt+ArrowLeft">
        <ChevronLeft size={15} />
      </button>
      <div ref={strip} className="flex min-w-0 items-center gap-1 overflow-x-auto" style={{ scrollbarWidth: 'none' }}>
        {items.map(item => {
          const active = item.id === activeId
          return (
            <button
              key={item.id}
              type="button"
              data-testid="conversation-chip"
              data-active={active ? 'true' : 'false'}
              data-state={item.state}
              aria-pressed={active}
              title={item.label}
              // Clicking the current one again puts it away.
              onClick={() => onSelect(active ? null : item.id)}
              className={`flex h-7 max-w-[220px] shrink-0 items-center gap-1.5 rounded-lg px-2.5 text-[12.5px] transition-colors ${
                active ? 'font-medium text-text' : 'text-muted hover:bg-bg-hover hover:text-text'
              }`}
              style={active ? { background: 'color-mix(in srgb, var(--accent) 16%, transparent)' } : undefined}
            >
              <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${DOT[item.state].cls}`} style={{ background: DOT[item.state].color }} aria-hidden />
              <span className="truncate">{item.label}</span>
              {item.workers && item.workers.total > 0 && (
                <span className="shrink-0 rounded-md bg-bg-hover px-1 text-[10.5px] text-muted">{`${item.workers.running}/${item.workers.total}`}</span>
              )}
            </button>
          )
        })}
      </div>
      <button type="button" className={arrow} onClick={() => step(1)} title="Next conversation (⌃⌥→)" aria-label="Next conversation" aria-keyshortcuts="Control+Alt+ArrowRight">
        <ChevronRight size={15} />
      </button>
      <span className="shrink-0 pl-1 pr-2 text-[11px] tabular-nums text-muted" aria-live="polite">
        {at >= 0 ? `${at + 1} / ${items.length}` : `– / ${items.length}`}
      </span>
    </div>
  )
}
