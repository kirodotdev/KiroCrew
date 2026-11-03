/**
 * The doc's "/" command menu (PoC), after Notion's: a short list of blocks to
 * start, filtered by what follows the slash. Drawn by `docSlash` in an editor
 * tooltip; the keys stay with the editor, so this only shows the list and takes
 * clicks. Styled through `data-sm` parts by the editor theme. Labels are English
 * like the rest of the doc conductor layer.
 */
import { useEffect, useRef } from 'react'
import {
  Code, CodeXml, Heading1, Heading2, Heading3, Link2, List, ListOrdered, ListTodo, Minus, Quote, Sparkles, Table, Type,
  type LucideIcon,
} from 'lucide-react'

export type SlashIcon = 'ai' | 'text' | 'h1' | 'h2' | 'h3' | 'bullet' | 'number' | 'todo' | 'quote' | 'table' | 'code' | 'divider' | 'link' | 'html'

const ICONS: Record<SlashIcon, LucideIcon> = {
  ai: Sparkles, text: Type, h1: Heading1, h2: Heading2, h3: Heading3, bullet: List, number: ListOrdered,
  todo: ListTodo, quote: Quote, table: Table, code: Code, divider: Minus, link: Link2, html: CodeXml,
}

export interface SlashItem { id: string; label: string; hint: string; icon: SlashIcon }

export default function SlashMenu({ items, index, onPick, onHover }: {
  items: SlashItem[]
  index: number
  onPick: (i: number) => void
  onHover: (i: number) => void
}) {
  const list = useRef<HTMLDivElement>(null)
  // The chosen row stays in view as the arrows move past the menu's edge.
  useEffect(() => {
    list.current?.querySelector<HTMLElement>(`[data-i="${index}"]`)?.scrollIntoView?.({ block: 'nearest' })
  }, [index])
  return (
    <div data-sm="root" data-testid="slash-menu" role="listbox" aria-label="Blocks">
      <div data-sm="list" ref={list}>
        {items.map((it, i) => {
          const Icon = ICONS[it.icon]
          return (
            // The editor keeps focus and drives the list with its own keys; an
            // option that is focused anyway still answers Enter.
            <div key={it.id} role="option" tabIndex={-1} aria-selected={i === index} data-sm="item" data-i={i} data-id={it.id} data-testid="slash-item"
              onMouseDown={e => e.preventDefault()} onMouseMove={() => { if (i !== index) onHover(i) }} onClick={() => onPick(i)}
              onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); onPick(i) } }}>
              <span data-sm="icon" data-ai={it.icon === 'ai' ? 'true' : undefined}><Icon size={16} aria-hidden /></span>
              <span data-sm="label">{it.label}</span>
              <span data-sm="hint">{it.hint}</span>
            </div>
          )
        })}
      </div>
      <div data-sm="foot"><kbd>↑</kbd><kbd>↓</kbd> choose · <kbd>Enter</kbd> insert · <kbd>Esc</kbd> close</div>
    </div>
  )
}
