// A poll that brings an agent's revision while the user is typing in the same
// task field must not throw away what they typed, and a field they only
// focused must not write a stale value back over that revision.

import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import TaskSidebar from './TaskSidebar'
import type { Task } from '../api'
import EN_CATALOG from '../../../i18n/locales/en.json'

const TS = EN_CATALOG.apps.meetings.taskSidebar

const task = (over: Partial<Task> = {}): Task => ({
  id: 't1',
  description: 'Send the deck',
  assignee: 'alice',
  priority: 'medium',
  status: 'open',
  context: '',
  labels: [],
  review_status: 'pending',
  filed_ref: null,
  ...over,
})

function mount(initial: Task) {
  const fns = { onClose: vi.fn(), onAdd: vi.fn(), onUpdate: vi.fn(), onDelete: vi.fn() }
  const view = render(<TaskSidebar tasks={[initial]} {...fns} />)
  const poll = (next: Task) => view.rerender(<TaskSidebar tasks={[next]} {...fns} />)
  return { fns, poll }
}

const field = (label: string) => screen.getByLabelText(label) as HTMLInputElement

describe('TaskSidebar while a poll lands during typing', () => {
  it('keeps the description the user is typing and writes it on blur', () => {
    const { fns, poll } = mount(task())
    fireEvent.focus(field(TS.descriptionLabel))
    fireEvent.change(field(TS.descriptionLabel), { target: { value: 'Send the final deck' } })

    poll(task({ description: 'Send the deck by Friday' }))

    expect(field(TS.descriptionLabel).value).toBe('Send the final deck')
    fireEvent.blur(field(TS.descriptionLabel))
    expect(fns.onUpdate).toHaveBeenCalledWith('t1', { description: 'Send the final deck' })
  })

  it('keeps the assignee the user is typing', () => {
    const { poll } = mount(task())
    fireEvent.focus(field(TS.assigneeLabel))
    fireEvent.change(field(TS.assigneeLabel), { target: { value: 'bob' } })

    poll(task({ assignee: 'carol' }))

    expect(field(TS.assigneeLabel).value).toBe('bob')
  })

  it('takes the revision without writing when the user only focused the field', () => {
    const { fns, poll } = mount(task())
    fireEvent.focus(field(TS.descriptionLabel))

    poll(task({ description: 'Send the deck by Friday' }))
    fireEvent.blur(field(TS.descriptionLabel))

    expect(fns.onUpdate).not.toHaveBeenCalled()
    expect(field(TS.descriptionLabel).value).toBe('Send the deck by Friday')
  })

  it('shows a revision at once when the field is not focused', () => {
    const { fns, poll } = mount(task())

    poll(task({ description: 'Send the deck by Friday', assignee: 'carol' }))

    expect(field(TS.descriptionLabel).value).toBe('Send the deck by Friday')
    expect(field(TS.assigneeLabel).value).toBe('carol')
    expect(fns.onUpdate).not.toHaveBeenCalled()
  })
})
