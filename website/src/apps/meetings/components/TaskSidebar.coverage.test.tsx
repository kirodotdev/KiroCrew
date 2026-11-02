// Behaviour of the live-meeting action-item sidebar: what it counts, how a
// quick-add reaches the caller, and which field edits are written back on blur.

import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'

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

const handlers = () => ({
  onClose: vi.fn(),
  onAdd: vi.fn(),
  onUpdate: vi.fn(),
  onDelete: vi.fn(),
})

const renderSidebar = (tasks: Task[], fns = handlers()) => {
  render(<TaskSidebar tasks={tasks} {...fns} />)
  return fns
}

describe('TaskSidebar heading and empty state', () => {
  it('counts only tasks that are not archived', () => {
    renderSidebar([
      task({ id: 'a' }),
      task({ id: 'b', review_status: 'archived' }),
      task({ id: 'c', description: 'Book room' }),
    ])
    expect(screen.getByText('2 action items')).toBeTruthy()
  })

  it('shows the empty message when every task is archived', () => {
    renderSidebar([task({ review_status: 'archived' })])
    expect(screen.getByText(TS.empty)).toBeTruthy()
    expect(screen.queryAllByLabelText(TS.descriptionLabel)).toHaveLength(0)
  })

  it('calls onClose from the close button', () => {
    const fns = renderSidebar([])
    fireEvent.click(screen.getByRole('button', { name: TS.close }))
    expect(fns.onClose).toHaveBeenCalledTimes(1)
  })
})

describe('TaskSidebar quick add', () => {
  it('passes the trimmed text to onAdd and clears the field', () => {
    const fns = renderSidebar([])
    const input = screen.getByRole('textbox', { name: TS.quickAddPlaceholder }) as HTMLInputElement
    fireEvent.change(input, { target: { value: '  Follow up with legal  ' } })
    fireEvent.click(screen.getByRole('button', { name: TS.add }))
    expect(fns.onAdd).toHaveBeenCalledWith('Follow up with legal')
    expect(input.value).toBe('')
  })

  it('ignores a blank entry', () => {
    const fns = renderSidebar([])
    const input = screen.getByRole('textbox', { name: TS.quickAddPlaceholder })
    fireEvent.change(input, { target: { value: '    ' } })
    fireEvent.click(screen.getByRole('button', { name: TS.add }))
    expect(fns.onAdd).not.toHaveBeenCalled()
  })

  it('adds on Enter as well as on the Add button', () => {
    const fns = renderSidebar([])
    const input = screen.getByRole('textbox', { name: TS.quickAddPlaceholder })
    fireEvent.change(input, { target: { value: 'Ship the fix' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(fns.onAdd).toHaveBeenCalledWith('Ship the fix')
  })
})

describe('TaskSidebar in-place edits', () => {
  it('writes a changed description on blur, trimmed', () => {
    const fns = renderSidebar([task()])
    const input = screen.getByLabelText(TS.descriptionLabel)
    fireEvent.change(input, { target: { value: '  Send the final deck ' } })
    fireEvent.blur(input)
    expect(fns.onUpdate).toHaveBeenCalledWith('t1', { description: 'Send the final deck' })
  })

  it('does not write a description that is unchanged or emptied', () => {
    const fns = renderSidebar([task()])
    const input = screen.getByLabelText(TS.descriptionLabel)
    fireEvent.blur(input)
    fireEvent.change(input, { target: { value: '   ' } })
    fireEvent.blur(input)
    expect(fns.onUpdate).not.toHaveBeenCalled()
  })

  it('writes a changed assignee on blur', () => {
    const fns = renderSidebar([task()])
    const input = screen.getByLabelText(TS.assigneeLabel)
    fireEvent.change(input, { target: { value: 'bob' } })
    fireEvent.blur(input)
    expect(fns.onUpdate).toHaveBeenCalledWith('t1', { assignee: 'bob' })
  })

  it('does not write an unchanged assignee', () => {
    const fns = renderSidebar([task()])
    fireEvent.blur(screen.getByLabelText(TS.assigneeLabel))
    expect(fns.onUpdate).not.toHaveBeenCalled()
  })

  it('deletes the task with its id from the delete button', () => {
    const fns = renderSidebar([task({ id: 'task-42' })])
    fireEvent.click(screen.getByRole('button', { name: TS.deleteTask }))
    expect(fns.onDelete).toHaveBeenCalledWith('task-42')
  })
})

describe('TaskSidebar task badges', () => {
  it('marks a filed task with the Filed badge', () => {
    renderSidebar([task({ review_status: 'pushed' })])
    expect(screen.getByText(TS.filed)).toBeTruthy()
  })

  it('does not mark an unfiled task as filed', () => {
    renderSidebar([task({ review_status: 'pending' })])
    expect(screen.queryByText(TS.filed)).toBeNull()
  })

  it('labels each task with its own priority', () => {
    renderSidebar([
      task({ id: 'h', priority: 'high' }),
      task({ id: 'l', priority: 'low' }),
    ])
    const P = EN_CATALOG.apps.meetings.priority
    const rows = screen
      .getAllByRole('button', { name: TS.deleteTask })
      .map(b => within(b.parentElement as HTMLElement))
    expect(rows[0].getByText(P.high)).toBeTruthy()
    expect(rows[0].queryByText(P.low)).toBeNull()
    expect(rows[1].getByText(P.low)).toBeTruthy()
    expect(rows[1].queryByText(P.high)).toBeNull()
  })
})
