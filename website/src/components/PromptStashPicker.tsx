import { useEffect, useId, useRef, useState } from 'react'
import type React from 'react'
import { Trash2 } from 'lucide-react'
import { i18nT } from '../i18n/t'
import type { PromptStashState } from '../hooks/usePromptStash'
import { isStashChord } from '../utils/promptStash'

/** The first line with something on it, the row's whole label. */
function firstLine(text: string): string {
  return text.split('\n').find(line => line.trim() !== '')?.trim() ?? ''
}

/**
 * The list Cmd/Ctrl+S opens in an empty composer when several drafts are
 * stashed: newest first, the newest highlighted. Up/Down move, Enter (or the
 * chord again) restores, Delete/Backspace discards, Escape closes. The list
 * takes focus on open and hands it back to the message input after a restore
 * or Escape; focus leaving it for anything else closes it where focus went.
 */
export default function PromptStashPicker({ stash, touch, onFocusInput }: {
  stash: Pick<PromptStashState, 'restoreEntry' | 'discardEntry' | 'closePicker'> & { pickerEntries: NonNullable<PromptStashState['pickerEntries']> }
  touch: boolean
  onFocusInput: () => void
}) {
  const entries = stash.pickerEntries
  const [active, setActive] = useState(0)
  const listRef = useRef<HTMLDivElement>(null)
  const baseId = useId()
  const hintId = useId()
  // A delete keeps the position; the last row going moves to the new last.
  const index = Math.min(active, entries.length - 1)
  const optionId = (id: string) => `${baseId}-${id}`
  const current = entries[index]
  const activeId = current ? optionId(current.id) : undefined

  useEffect(() => { listRef.current?.focus() }, [])
  useEffect(() => {
    if (activeId) document.getElementById(activeId)?.scrollIntoView?.({ block: 'nearest' })
  }, [activeId])

  const restore = (id: string) => {
    stash.restoreEntry(id)
    onFocusInput()
  }
  // The last row going closes the list: focus goes back to the input rather
  // than falling to the page.
  const discard = (id: string) => {
    stash.discardEntry(id)
    if (entries.length === 1) onFocusInput()
  }
  const onKeyDown = (e: React.KeyboardEvent) => {
    const chord = isStashChord(e.nativeEvent)
    const key = e.key
    const move = key === 'ArrowDown' ? 1 : key === 'ArrowUp' ? -1 : 0
    const remove = key === 'Delete' || key === 'Backspace'
    if (!chord && !move && !remove && key !== 'Enter' && key !== 'Escape') return
    e.preventDefault()
    e.stopPropagation()
    // A held key acts once: only the arrows repeat.
    if (e.nativeEvent.repeat && !move) return
    if (chord || key === 'Enter') { if (current) restore(current.id); return }
    if (move) setActive(Math.min(Math.max(index + move, 0), entries.length - 1))
    else if (key === 'Escape') { stash.closePicker(); onFocusInput() }
    else if (current) discard(current.id)
  }

  return (
    <div className="px-2.5 pt-1 text-left" data-testid="prompt-stash-picker">
      <div className="flex min-w-0 items-baseline justify-between gap-2 px-2 pb-0.5">
        <span className="shrink-0 text-[12px] font-medium text-text">{i18nT('components.promptStash.picker_label')}</span>
        {!touch && <span id={hintId} className="min-w-0 break-words text-[11px] text-muted">{i18nT('components.promptStash.picker_hint')}</span>}
      </div>
      <div
        ref={listRef}
        role="listbox"
        tabIndex={0}
        aria-label={i18nT('components.promptStash.picker_label')}
        aria-activedescendant={activeId}
        aria-describedby={touch ? undefined : hintId}
        data-testid="prompt-stash-picker-list"
        onKeyDown={onKeyDown}
        onBlur={e => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) stash.closePicker() }}
        className="flex max-h-48 flex-col gap-0.5 overflow-y-auto rounded-lg focus-visible:outline focus-visible:outline-2"
      >
        {entries.map((entry, i) => (
          // eslint-disable-next-line jsx-a11y/click-events-have-key-events -- the keyboard path is the listbox's own onKeyDown (aria-activedescendant); a row is a pointer target only
          <div
            key={entry.id}
            id={optionId(entry.id)}
            role="option"
            tabIndex={-1}
            aria-selected={i === index}
            data-testid="prompt-stash-picker-option"
            onClick={() => restore(entry.id)}
            onMouseMove={() => { if (i !== index) setActive(i) }}
            className={`flex min-w-0 cursor-pointer items-center gap-2 rounded-lg px-2 py-1 ${i === index ? 'bg-bg-hover' : ''}`}
          >
            <span className="min-w-0 flex-1 truncate text-[12px] text-text">{firstLine(entry.text)}</span>
            {/* Hidden from assistive tech, whose path is the Delete key the hint names: an option's children are presentational. */}
            <button
              type="button"
              tabIndex={-1}
              aria-hidden="true"
              aria-label={i18nT('components.promptStash.menu_discard')}
              title={i18nT('components.promptStash.menu_discard')}
              data-testid="prompt-stash-picker-delete"
              onMouseDown={e => e.preventDefault()}
              onClick={e => { e.stopPropagation(); setActive(i); discard(entry.id) }}
              className="shrink-0 rounded p-1.5 text-muted hover:text-text"
            >
              <Trash2 size={12} />
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}
