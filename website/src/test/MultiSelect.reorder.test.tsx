/**
 * MultiSelect's optional reorder mode (Rev 3 of the model-order feature:
 * ordering rides the same Settings rows as visibility — PR #9969).
 *
 * Pinned here: grips render only for non-locked rows and only when the
 * caller passes onReorder; the search filter suspends reordering (a filtered
 * list is a subsequence, so a drop has no well-defined position in the full
 * order); reorderDisabled hides the sortable path while rows stay
 * toggleable; and grip key events bypass the popover's list navigation so
 * dnd-kit's keyboard sensor owns the arrows.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import '../i18n/all'
import MultiSelect from '../components/MultiSelect'

// Capture the real onDragEnd from a stubbed DndContext (children pass through) —
// jsdom cannot deliver real PointerEvents + layout measurement, so drop tests
// invoke the handler directly. Same pattern as ChatSidebar.dragFreezeOrder.
const dnd = vi.hoisted(() => ({ onDragEnd: undefined as ((e: unknown) => void) | undefined }))
vi.mock('@dnd-kit/core', async importOriginal => {
  const actual = await importOriginal<typeof import('@dnd-kit/core')>()
  return {
    ...actual,
    DndContext: (props: { children?: React.ReactNode; onDragEnd?: (e: unknown) => void }) => {
      dnd.onDragEnd = props.onDragEnd
      return <>{props.children}</>
    },
  }
})

const options = [
  { value: 'auto', label: 'auto', locked: true },
  { value: 'opus', label: 'opus' },
  { value: 'sonnet', label: 'sonnet' },
]

function open(ui: ReturnType<typeof render>) {
  fireEvent.click(ui.getByRole('button', { name: 'Models' }))
}

function mount(extra: Partial<React.ComponentProps<typeof MultiSelect>> = {}) {
  const onToggle = vi.fn()
  const onReorder = vi.fn()
  const ui = render(
    <MultiSelect
      label="Models"
      summary="2 of 3"
      options={options}
      selected={new Set(['auto', 'opus', 'sonnet'])}
      onToggle={onToggle}
      onReorder={onReorder}
      reorderRowLabel={v => `Reorder ${v}`}
      {...extra}
    />,
  )
  return { ui, onToggle, onReorder }
}

describe('MultiSelect reorder mode', () => {
  it('renders grips for non-locked rows only', () => {
    const { ui } = mount()
    open(ui)
    expect(screen.getByLabelText('Reorder opus')).toBeInTheDocument()
    expect(screen.getByLabelText('Reorder sonnet')).toBeInTheDocument()
    expect(screen.queryByLabelText('Reorder auto')).not.toBeInTheDocument()
  })

  it('suspends reordering while the filter is active', () => {
    const { ui } = mount()
    open(ui)
    fireEvent.change(screen.getByLabelText(/search/i), { target: { value: 'opus' } })
    expect(screen.queryByLabelText('Reorder opus')).not.toBeInTheDocument()
  })

  it('renders no grips when reorderDisabled, rows stay toggleable', () => {
    const { ui, onToggle } = mount({ reorderDisabled: true })
    open(ui)
    expect(screen.queryByLabelText('Reorder opus')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox', { name: 'opus' }))
    expect(onToggle).toHaveBeenCalledWith('opus', false)
  })

  it('a drop reports the full non-locked order (captured onDragEnd — jsdom cannot drag)', () => {
    const { ui, onReorder } = mount()
    open(ui)
    expect(dnd.onDragEnd).toBeDefined()
    dnd.onDragEnd?.({ active: { id: 'sonnet' }, over: { id: 'opus' } })
    expect(onReorder).toHaveBeenCalledWith(['sonnet', 'opus'])
  })

  it('a self-drop or missing target reorders nothing', () => {
    const { ui, onReorder } = mount()
    open(ui)
    dnd.onDragEnd?.({ active: { id: 'opus' }, over: { id: 'opus' } })
    dnd.onDragEnd?.({ active: { id: 'opus' }, over: null })
    expect(onReorder).not.toHaveBeenCalled()
  })

  it('footerAction renders on its own row and honors disabled', () => {
    const onSelect = vi.fn()
    const { ui } = mount({ footerAction: { label: 'Reset to default order', onSelect, disabled: true } })
    open(ui)
    const reset = screen.getByRole('button', { name: 'Reset to default order' })
    // Its own row under the list, not a third chip in the bulk-actions row
    // (max-two-buttons-per-row): the header row holds only the search box
    // here (no bulkActions passed), and the footer sits after the group.
    expect(reset.closest('[role="group"]')).toBeNull()
    fireEvent.click(reset)
    expect(onSelect).not.toHaveBeenCalled()
  })
})
