import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { X } from 'lucide-react'
import MarkdownRenderer from '../MarkdownRenderer'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import type { BindingCardRow, BindingCardView } from '../../apps/editorBindingContributions'

/**
 * The result card over the side panel's editor for an app's `editorBindings`
 * answer. The host owns it: an app supplies locations (one row each, with its own
 * label or `file:line`) or markdown, and the reader filters, moves with the arrow
 * keys, opens with Enter and closes with Escape. Shown as loading from the moment
 * the binding fires, so a slow app has a visible owner.
 */
export default function EditorBindingCard({ view, onOpen, onClose }: {
  view: BindingCardView
  onOpen: (location: { path: string; line: number; character?: number }, sameFile: boolean) => void
  onClose: () => void
}) {
  useLanguageGeneration()
  const [filter, setFilter] = useState('')
  const [active, setActive] = useState(0)
  const rootRef = useRef<HTMLDivElement>(null)
  const filterRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const ready = view.status === 'ready' ? view : null
  const rows = useMemo<BindingCardRow[]>(() => {
    if (!ready) return []
    const needle = filter.trim().toLowerCase()
    return needle
      ? ready.rows.filter(r => r.primary.toLowerCase().includes(needle) || r.secondary.toLowerCase().includes(needle) || r.path.toLowerCase().includes(needle))
      : ready.rows
  }, [ready, filter])
  const listId = useId()
  const rowId = (i: number) => `${listId}-${i}`
  useEffect(() => { setFilter(''); setActive(0) }, [view])
  useEffect(() => { setActive(0) }, [filter])
  // Focus follows the card in every state, so Escape closes it while it loads.
  useEffect(() => {
    if (ready && ready.rows.length > 1) filterRef.current?.focus()
    else if (ready && listRef.current) listRef.current.focus()
    else rootRef.current?.focus()
  }, [ready, view])
  useEffect(() => {
    rootRef.current?.querySelector(`[data-row-index="${active}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [active])

  const open = (row: BindingCardRow) => onOpen({ path: row.path, line: row.line, character: row.character }, row.sameFile)
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); onClose(); return }
    if (!rows.length) return
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive(i => Math.min(rows.length - 1, i + 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(i => Math.max(0, i - 1)) }
    else if (e.key === 'Enter') {
      e.preventDefault()
      const row = rows[active]
      if (row) open(row)
    }
  }
  const showFilter = !!ready && ready.rows.length > 1

  return (
    // The card is a dialog whose keys (arrows, Enter, Escape) drive the list
    // inside it; the focusable targets are the buttons and the filter input.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <div
      ref={rootRef}
      data-mc-mdpanel
      data-testid="editor-binding-card"
      role="dialog"
      aria-label={view.title}
      tabIndex={-1}
      onKeyDown={onKeyDown}
      className="absolute top-2 right-3 z-30 flex flex-col w-[min(440px,calc(100%-24px))] max-h-[60%] bg-bg-elevated text-text border border-border rounded-lg shadow-md outline-hidden text-[12.5px]"
    >
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border shrink-0">
        <span className="font-semibold text-text-strong truncate">{view.title}</span>
        {ready && ready.rows.length > 0 && <span className="text-muted tabular-nums">{ready.rows.length}</span>}
        <span className="flex-1" />
        <button onClick={onClose} className="p-0.5 rounded text-muted hover:text-text cursor-pointer border-none bg-transparent" title={i18nT('components.editorBindings.close')} aria-label={i18nT('components.editorBindings.close')}><X size={14} /></button>
      </div>
      {view.status === 'loading' && <div role="status" className="px-3 py-2 text-muted">{i18nT('components.editorBindings.waiting', { app: view.title })}</div>}
      {ready && ready.markdown && (
        <div className="px-3 py-2 overflow-auto msg-content text-sm leading-relaxed">
          <MarkdownRenderer content={ready.markdown} readOnlyCode />
        </div>
      )}
      {showFilter && (
        <input
          ref={filterRef}
          type="text"
          value={filter}
          onChange={e => setFilter(e.target.value)}
          placeholder={i18nT('components.editorBindings.filter')}
          aria-label={i18nT('components.editorBindings.filter')}
          role="combobox"
          aria-expanded={rows.length > 0}
          aria-controls={listId}
          aria-activedescendant={rows.length ? rowId(active) : undefined}
          className="mx-2 mt-2 px-2 h-[26px] rounded-md bg-bg text-text border border-border focus:border-accent outline-hidden placeholder:text-muted shrink-0"
        />
      )}
      {ready && ready.rows.length > 0 && (
        rows.length === 0
          ? <div className="px-3 py-2 text-muted">{i18nT('components.editorBindings.nothing_here')}</div>
          : (
            <div
              id={listId}
              ref={listRef}
              role="listbox"
              tabIndex={-1}
              aria-label={view.title}
              aria-activedescendant={rowId(active)}
              className="overflow-auto py-1 m-0 outline-hidden"
            >
              {rows.map((row, i) => (
                <div key={row.key} data-row-index={i} id={rowId(i)} role="option" aria-selected={i === active}>
                  <button
                    type="button"
                    tabIndex={-1}
                    aria-current={i === active ? 'true' : undefined}
                    onMouseEnter={() => setActive(i)}
                    onClick={() => open(row)}
                    title={row.path}
                    className={`w-full flex items-baseline gap-2 py-1 px-3 text-left border-none cursor-pointer ${i === active ? 'bg-accent/15 text-text' : 'bg-transparent text-text'}`}
                  >
                    <span className="font-medium shrink-0 font-mono">{row.primary}</span>
                    {row.secondary && <span className="truncate text-muted font-mono">{row.secondary}</span>}
                  </button>
                </div>
              ))}
            </div>
          )
      )}
    </div>
  )
}
