import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import TaskDetailPanel from '../pages/aidlc/TaskDetailPanel'
import type { TaskDetail } from '../types'

const task = (overrides: Partial<TaskDetail> = {}): TaskDetail => ({
  index: 3, title: 'Deploy', description: 'Deploy to prod', status: 'pending',
  error: '', result: '', attempts: 1, depends_on: [1, 2], requires_approval: false,
  ...overrides,
})

const allTasks: TaskDetail[] = [
  { index: 1, title: 'Setup', description: '', status: 'passed', error: '', result: '', attempts: 1, depends_on: [], requires_approval: false },
  { index: 2, title: 'Build', description: '', status: 'in_progress', error: '', result: '', attempts: 1, depends_on: [], requires_approval: false },
  task(),
]

describe('TaskDetailPanel', () => {
  it('shows blocked banner when pending with unfinished deps', () => {
    render(<TaskDetailPanel task={task()} allTasks={allTasks} onClose={() => {}} />)
    expect(screen.getByText(/Blocked/)).toBeInTheDocument()
    expect(screen.getByText(/Build/)).toBeInTheDocument()
    expect(screen.getByText(/in progress/)).toBeInTheDocument()
  })

  it('shows dependency list when not blocked', () => {
    const doneTasks = allTasks.map(t => ({ ...t, status: 'passed' }))
    render(<TaskDetailPanel task={task({ status: 'passed' })} allTasks={doneTasks} onClose={() => {}} />)
    expect(screen.queryByText(/Blocked/)).not.toBeInTheDocument()
    expect(screen.getByText(/Depends on/)).toBeInTheDocument()
  })

  it('shows approval notice when requires_approval', () => {
    render(<TaskDetailPanel task={task({ requires_approval: true })} allTasks={allTasks} onClose={() => {}} />)
    expect(screen.getByText(/Requires approval/)).toBeInTheDocument()
  })

  it('shows retry button for failed tasks', () => {
    const onRetry = () => {}
    render(<TaskDetailPanel task={task({ status: 'failed', error: 'timeout' })} allTasks={[]} onClose={() => {}} onRetry={onRetry} />)
    expect(screen.getByText(/Retry Task/)).toBeInTheDocument()
    expect(screen.getByText('timeout')).toBeInTheDocument()
  })

  it('renders input fields when editable', () => {
    render(<TaskDetailPanel task={task()} allTasks={allTasks} onClose={() => {}} editable />)
    expect(screen.getByDisplayValue('Deploy')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Deploy to prod')).toBeInTheDocument()
    // Dependency checkboxes for other tasks
    expect(screen.getByLabelText(/Task 1: Setup/)).toBeInTheDocument()
    expect(screen.getByLabelText(/Task 2: Build/)).toBeInTheDocument()
  })

  it('does not render input fields when not editable', () => {
    render(<TaskDetailPanel task={task()} allTasks={allTasks} onClose={() => {}} />)
    expect(screen.queryByDisplayValue('Deploy')).not.toBeInTheDocument()
    expect(screen.getByText('Deploy to prod')).toBeInTheDocument()
  })

  it('shows Save button only when dirty', () => {
    render(<TaskDetailPanel task={task()} allTasks={allTasks} onClose={() => {}} editable onSave={() => {}} />)
    expect(screen.queryByText(/Save/)).not.toBeInTheDocument()
    fireEvent.change(screen.getByDisplayValue('Deploy'), { target: { value: 'Deploy v2' } })
    expect(screen.getByText(/Save/)).toBeInTheDocument()
  })

  it('calls onSave with updated values', () => {
    const onSave = vi.fn()
    render(<TaskDetailPanel task={task()} allTasks={allTasks} onClose={() => {}} editable onSave={onSave} />)
    fireEvent.change(screen.getByDisplayValue('Deploy'), { target: { value: 'Deploy v2' } })
    fireEvent.click(screen.getByText(/Save/))
    expect(onSave).toHaveBeenCalledWith(3, expect.objectContaining({ title: 'Deploy v2' }))
  })

  it('restores from pendingEdits on mount', () => {
    const pending = { 3: { title: 'Edited Title', description: 'Edited desc', depends_on: [1] } }
    render(<TaskDetailPanel task={task()} allTasks={allTasks} onClose={() => {}} editable pendingEdits={pending} />)
    expect(screen.getByDisplayValue('Edited Title')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Edited desc')).toBeInTheDocument()
    // Save button visible immediately since pending edits = dirty
    expect(screen.getByText(/Save/)).toBeInTheDocument()
  })

  it('calls onEdit to persist changes to parent', () => {
    const onEdit = vi.fn()
    render(<TaskDetailPanel task={task()} allTasks={allTasks} onClose={() => {}} editable onEdit={onEdit} />)
    fireEvent.change(screen.getByDisplayValue('Deploy'), { target: { value: 'New' } })
    expect(onEdit).toHaveBeenCalledWith(3, expect.objectContaining({ title: 'New' }))
  })

  it('keeps dirty=true and shows error on save failure', async () => {
    const onSave = vi.fn().mockRejectedValue(new Error('fail'))
    render(<TaskDetailPanel task={task()} allTasks={allTasks} onClose={() => {}} editable onSave={onSave} />)
    fireEvent.change(screen.getByDisplayValue('Deploy'), { target: { value: 'Deploy v2' } })
    fireEvent.click(screen.getByText(/Save/))
    await vi.waitFor(() => expect(screen.getByText(/Save failed/)).toBeInTheDocument())
    expect(screen.getByText(/Save/, { selector: 'button' })).toBeInTheDocument()
  })

  it('disables inputs during save', async () => {
    let resolve: () => void
    const onSave = vi.fn().mockImplementation(() => new Promise<void>(r => { resolve = r }))
    render(<TaskDetailPanel task={task()} allTasks={allTasks} onClose={() => {}} editable onSave={onSave} />)
    fireEvent.change(screen.getByDisplayValue('Deploy'), { target: { value: 'Deploy v2' } })
    fireEvent.click(screen.getByText(/Save/))
    expect(screen.getByDisplayValue('Deploy v2')).toBeDisabled()
    resolve!()
    await vi.waitFor(() => expect(screen.getByDisplayValue('Deploy v2')).not.toBeDisabled())
  })
})
