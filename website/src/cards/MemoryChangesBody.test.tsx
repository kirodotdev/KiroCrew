/**
 * A bot's memory card: one ticked row per delete or edit. Unticking re-previews
 * the card with that row in `skip`; the last ticked row stays ticked.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import type { Card, CardChange } from '../api/cards'
import { MemoryChangesBody } from './MemoryChangesBody'
import { CARD_REGISTRY, cardChanges, cardTitle } from './cardRegistry'

const row = (index: number, over: Partial<CardChange> = {}): CardChange => ({
  field: 'memory_change',
  index,
  memory_kind: 'fact',
  action: 'delete',
  label: 'CI moved off jenkins',
  before: 'jenkins',
  included: true,
  ...over,
})

const memoryCard = (changes: CardChange[], params: Record<string, unknown> = {}): Card => ({
  id: 'm1',
  slot_key: 'slot-A',
  kind: 'crewmate.memory',
  revision: 1,
  status: 'pending',
  risk: 'normal',
  title: 'Delete 1 of Scout\u2019s memories',
  changes,
  scope: { members: ['Scout'], count: 1, only_this_member: true },
  editable: ['skip'],
  params: { member: 'scout', store: 'member-scout', changes: [], ...params },
  plan: { apply: [], undo: null },
  created_at: 1_700_000_000,
  expires_at: 4_000_000_000_000,
})

const twoRows = () => [
  row(0),
  row(1, { memory_kind: 'episode', action: 'edit', before: 'old text', after: 'new text', label: 'the year is known' }),
]

describe('MemoryChangesBody', () => {
  it('draws each change with what it does, its text and the reason', () => {
    render(<MemoryChangesBody card={memoryCard(twoRows())} disabled={false} preview={vi.fn()} />)
    const rows = screen.getAllByTestId('change-card-memory-row')
    expect(rows).toHaveLength(2)
    expect(rows[0].textContent).toContain('Delete fact')
    expect(rows[0].textContent).toContain('jenkins')
    expect(rows[1].textContent).toContain('Edit experience')
    expect(rows[1].textContent).toContain('old text')
    expect(rows[1].textContent).toContain('new text')
    expect(screen.getAllByTestId('change-card-memory-reason').map(r => r.textContent)).toEqual([
      'CI moved off jenkins',
      'the year is known',
    ])
  })

  it('unticking a row re-previews with it skipped, and ticking it back drops the skip', () => {
    const preview = vi.fn()
    const { rerender } = render(<MemoryChangesBody card={memoryCard(twoRows())} disabled={false} preview={preview} />)
    fireEvent.click(screen.getAllByTestId('change-card-memory-box')[0])
    expect(preview).toHaveBeenLastCalledWith({ member: 'scout', store: 'member-scout', changes: [], skip: [0] })
    const skipped = memoryCard([row(0, { included: false }), twoRows()[1]], { skip: [0] })
    rerender(<MemoryChangesBody card={skipped} disabled={false} preview={preview} />)
    const boxes = screen.getAllByTestId('change-card-memory-box') as HTMLInputElement[]
    expect(boxes[0].checked).toBe(false)
    // The one row still ticked cannot be unticked: Decline is how to change nothing.
    expect(boxes[1].disabled).toBe(true)
    fireEvent.click(boxes[0])
    expect(preview).toHaveBeenLastCalledWith({ member: 'scout', store: 'member-scout', changes: [], skip: [] })
  })

  it('strikes a memory through only while its row is ticked', () => {
    const struck = () => Array.from(document.querySelectorAll('.line-through')).map(n => n.textContent)
    const { rerender } = render(<MemoryChangesBody card={memoryCard(twoRows())} disabled={false} preview={vi.fn()} />)
    expect(struck()).toEqual(['jenkins', 'old text'])
    const unticked = memoryCard([row(0, { included: false }), { ...twoRows()[1], included: false }])
    rerender(<MemoryChangesBody card={unticked} disabled={false} preview={vi.fn()} />)
    expect(struck()).toEqual([])
  })

  it('says why the last ticked row is locked, on that row only', () => {
    const { rerender } = render(<MemoryChangesBody card={memoryCard(twoRows())} disabled={false} preview={vi.fn()} />)
    expect(screen.queryByTestId('change-card-memory-last')).toBeNull()
    rerender(<MemoryChangesBody card={memoryCard([row(0, { included: false }), twoRows()[1]])} disabled={false} preview={vi.fn()} />)
    const hints = screen.getAllByTestId('change-card-memory-last')
    expect(hints).toHaveLength(1)
    expect(hints[0].textContent).toBe('Decline the card to keep this one too.')
    expect(screen.getAllByTestId('change-card-memory-row')[1].contains(hints[0])).toBe(true)
  })

  it('the press note names only what the ticked rows do', () => {
    const note = CARD_REGISTRY['crewmate.memory'].pressNote!
    const del = "Deletes can't be undone."
    const edit = 'An edited memory keeps its old version on the Memory page.'
    expect(note(memoryCard(twoRows()))).toBe(`${del} ${edit}`)
    expect(note(memoryCard([row(0, { included: false }), twoRows()[1]]))).toBe(edit)
    expect(note(memoryCard([row(0), { ...twoRows()[1], included: false }]))).toBe(del)
  })

  it('a card that is no longer pending cannot be edited', () => {
    const card = { ...memoryCard(twoRows()), status: 'applying' as const }
    render(<MemoryChangesBody card={card} disabled={false} preview={vi.fn()} />)
    for (const box of screen.getAllByTestId('change-card-memory-box') as HTMLInputElement[]) {
      expect(box.disabled).toBe(true)
    }
  })

  it('the rows are drawn by the body only, and the title counts the ticked rows', () => {
    const card = memoryCard(twoRows())
    expect(cardChanges(card)).toEqual([])
    expect(cardTitle(card)).toBe('Delete 1 and edit 1 of Scout\u2019s memories'.replace('\u2019', "'"))
    const editOnly = memoryCard([row(0, { included: false }), twoRows()[1]])
    expect(cardTitle(editOnly)).toBe("Edit 1 of Scout's memories")
  })
})
