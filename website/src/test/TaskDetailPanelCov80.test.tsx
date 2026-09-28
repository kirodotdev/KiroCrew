import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import TaskDetailPanel from '../pages/aidlc/TaskDetailPanel'
import type { TaskDetail } from '../types'

/**
 * The task inspector's EDITING half: the draft it holds, the pending-edit
 * handshake with its owner, the save round-trip, and the approval gates.
 *
 * The draft is deliberately local, so two things have to hold or edits get lost:
 * switching to another task must reset the fields (otherwise task 4 shows task
 * 3's text and saving writes it), and editing back to the ORIGINAL values must
 * withdraw the pending edit rather than leave a no-op change staged.
 */
function task(overrides: Partial<TaskDetail> = {}): TaskDetail {
  return {
    index: 3,
    title: 'zzz deploy',
    description: 'zzz deploy the thing',
    status: 'pending',
    error: '',
    result: '',
    attempts: 1,
    depends_on: [],
    requires_approval: false,
    ...overrides,
  }
}

const OTHERS: TaskDetail[] = [
  task({ index: 1, title: 'zzz setup', status: 'passed' }),
  task({ index: 2, title: 'zzz build', status: 'in_progress' }),
]

describe('TaskDetailPanel timestamps', () => {
  it('renders an em dash when there is no timestamp at all', () => {
    render(<TaskDetailPanel task={task()} onClose={() => {}} />)
    // Created and Duration both fall back — the task has not started.
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2)
    expect(screen.queryByText('Started')).not.toBeInTheDocument()
    expect(screen.queryByText('Finished')).not.toBeInTheDocument()
  })

  it('formats a sub-minute duration in seconds', () => {
    render(<TaskDetailPanel
      task={task({ started_at: 1_786_000_000, finished_at: 1_786_000_042 })}
      onClose={() => {}}
    />)
    expect(screen.getByText('42s')).toBeInTheDocument()
    expect(screen.getByText('Started')).toBeInTheDocument()
    expect(screen.getByText('Finished')).toBeInTheDocument()
  })

  it('formats minutes and seconds under an hour', () => {
    render(<TaskDetailPanel
      task={task({ created_at: 1_786_000_000, started_at: 1_786_000_000, finished_at: 1_786_000_090 })}
      onClose={() => {}}
    />)
    expect(screen.getByText(/^1m\b/)).toBeInTheDocument()
  })

  it('formats hours and minutes past an hour', () => {
    render(<TaskDetailPanel
      task={task({ started_at: 1_786_000_000, finished_at: 1_786_000_000 + 3 * 3600 + 300 })}
      onClose={() => {}}
    />)
    expect(screen.getByText(/^3h\b/)).toBeInTheDocument()
  })

  it('measures an unfinished task against now', () => {
    const now = Math.floor(Date.now() / 1000)
    render(<TaskDetailPanel task={task({ started_at: now - 5, status: 'in_progress' })} onClose={() => {}} />)
    expect(screen.getByText(/^\d+s$/)).toBeInTheDocument()
  })
})

describe('TaskDetailPanel editing', () => {
  it('reports an edit to the owner and offers Save', async () => {
    const onEdit = vi.fn()
    render(<TaskDetailPanel task={task()} allTasks={OTHERS} onClose={() => {}} editable onEdit={onEdit} onSave={vi.fn()} />)
    expect(screen.queryByRole('button', { name: /Save/ })).not.toBeInTheDocument()

    await userEvent.type(screen.getByLabelText('Task title'), 'X')
    expect(onEdit).toHaveBeenLastCalledWith(3, {
      title: 'zzz deployX', description: 'zzz deploy the thing', depends_on: [],
    })
    expect(screen.getByRole('button', { name: /Save/ })).toBeInTheDocument()
  })

  it('withdraws the pending edit when the values are typed back to the original', async () => {
    const onEdit = vi.fn()
    render(
      <TaskDetailPanel
        task={task()}
        allTasks={OTHERS}
        onClose={() => {}}
        editable
        onEdit={onEdit}
        onSave={vi.fn()}
        pendingEdits={{ 3: { title: 'zzz deployX', description: 'zzz deploy the thing', depends_on: [] } }}
      />,
    )
    // Seeded dirty from the pending edit.
    expect(screen.getByRole('button', { name: /Save/ })).toBeInTheDocument()

    const title = screen.getByLabelText('Task title')
    fireEvent.change(title, { target: { value: 'zzz deploy' } })
    expect(onEdit).toHaveBeenLastCalledWith(3, undefined)
    expect(screen.queryByRole('button', { name: /Save/ })).not.toBeInTheDocument()
  })

  it('reports a description edit as well', async () => {
    const onEdit = vi.fn()
    render(<TaskDetailPanel task={task()} allTasks={OTHERS} onClose={() => {}} editable onEdit={onEdit} onSave={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Task description'), { target: { value: 'zzz new body' } })
    expect(onEdit).toHaveBeenLastCalledWith(3, {
      title: 'zzz deploy', description: 'zzz new body', depends_on: [],
    })
  })

  it('toggles a dependency on and back off, offering only EARLIER tasks', async () => {
    const onEdit = vi.fn()
    render(<TaskDetailPanel task={task()} allTasks={[...OTHERS, task()]} onClose={() => {}} editable onEdit={onEdit} onSave={vi.fn()} />)
    // Its own row and any later task are not selectable as a dependency.
    expect(screen.getAllByRole('checkbox')).toHaveLength(2)

    const dep = screen.getByLabelText(/Task 2: zzz build/)
    await userEvent.click(dep)
    expect(onEdit).toHaveBeenLastCalledWith(3, expect.objectContaining({ depends_on: [2] }))

    await userEvent.click(dep)
    expect(onEdit).toHaveBeenLastCalledWith(3, undefined)
  })

  it('resets the draft when a different task is selected', () => {
    const { rerender } = render(<TaskDetailPanel task={task()} allTasks={OTHERS} onClose={() => {}} editable onSave={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Task title'), { target: { value: 'zzz half typed' } })
    expect(screen.getByLabelText('Task title')).toHaveValue('zzz half typed')

    rerender(<TaskDetailPanel task={task({ index: 4, title: 'zzz other' })} allTasks={OTHERS} onClose={() => {}} editable onSave={vi.fn()} />)
    expect(screen.getByLabelText('Task title')).toHaveValue('zzz other')
    expect(screen.queryByRole('button', { name: /Save/ })).not.toBeInTheDocument()
  })
})

describe('TaskDetailPanel saving', () => {
  it('sends the whole draft and adopts the values the server returns', async () => {
    const onSave = vi.fn(async () => ({
      title: 'zzz normalised', description: 'zzz normalised body', depends_on: [1],
    }))
    render(<TaskDetailPanel task={task()} allTasks={OTHERS} onClose={() => {}} editable onSave={onSave} />)
    fireEvent.change(screen.getByLabelText('Task title'), { target: { value: 'zzz edited' } })
    await userEvent.click(screen.getByRole('button', { name: /Save/ }))

    expect(onSave).toHaveBeenCalledWith(3, {
      title: 'zzz edited', description: 'zzz deploy the thing', depends_on: [],
    })
    await waitFor(() => expect(screen.getByLabelText('Task title')).toHaveValue('zzz normalised'))
    expect(screen.getByLabelText('Task description')).toHaveValue('zzz normalised body')
    // Clean again — the Save affordance goes away.
    await waitFor(() => expect(screen.queryByRole('button', { name: /Save/ })).not.toBeInTheDocument())
  })

  it('keeps the draft when the server returns nothing to normalise', async () => {
    const onSave = vi.fn(async () => undefined)
    render(<TaskDetailPanel task={task()} allTasks={OTHERS} onClose={() => {}} editable onSave={onSave} />)
    fireEvent.change(screen.getByLabelText('Task title'), { target: { value: 'zzz kept' } })
    await userEvent.click(screen.getByRole('button', { name: /Save/ }))
    await waitFor(() => expect(screen.queryByRole('button', { name: /Save/ })).not.toBeInTheDocument())
    expect(screen.getByLabelText('Task title')).toHaveValue('zzz kept')
  })

  it('locks the form while the save is in flight and refuses a second click', async () => {
    let release: () => void = () => {}
    const onSave = vi.fn(() => new Promise<void>(res => { release = () => res() }))
    render(<TaskDetailPanel task={task()} allTasks={OTHERS} onClose={() => {}} editable onSave={onSave} />)
    fireEvent.change(screen.getByLabelText('Task title'), { target: { value: 'zzz edited' } })

    const save = screen.getByRole('button', { name: /Save/ })
    fireEvent.click(save)
    expect(screen.getByLabelText('Task title')).toBeDisabled()
    fireEvent.click(save)
    expect(onSave).toHaveBeenCalledTimes(1)

    release()
    await waitFor(() => expect(screen.queryByRole('button', { name: /Save/ })).not.toBeInTheDocument())
  })

  it('keeps the draft and says so when the save fails', async () => {
    const onSave = vi.fn(async () => { throw new Error('zzz refused') })
    render(<TaskDetailPanel task={task()} allTasks={OTHERS} onClose={() => {}} editable onSave={onSave} />)
    fireEvent.change(screen.getByLabelText('Task title'), { target: { value: 'zzz edited' } })
    await userEvent.click(screen.getByRole('button', { name: /Save/ }))

    expect(await screen.findByText(/Save failed/i)).toBeInTheDocument()
    // Still dirty, so the edit can be retried rather than silently dropped.
    expect(screen.getByRole('button', { name: /Save/ })).toBeInTheDocument()
    expect(screen.getByLabelText('Task title')).toHaveValue('zzz edited')
  })

  it('does nothing without an onSave handler', () => {
    render(<TaskDetailPanel task={task()} allTasks={OTHERS} onClose={() => {}} editable />)
    fireEvent.change(screen.getByLabelText('Task title'), { target: { value: 'zzz edited' } })
    // The button is offered by the dirty state, but there is nothing to call.
    fireEvent.click(screen.getByRole('button', { name: /Save/ }))
    expect(screen.getByRole('button', { name: /Save/ })).toBeInTheDocument()
  })
})

describe('TaskDetailPanel approval gates', () => {
  it('turns requires-approval on, then reveals the YOLO-mode gate', async () => {
    const onToggleApproval = vi.fn(async () => true)
    render(<TaskDetailPanel task={task()} allTasks={OTHERS} onClose={() => {}} editable onToggleApproval={onToggleApproval} />)

    expect(screen.queryByLabelText('Block in YOLO mode')).not.toBeInTheDocument()
    await userEvent.click(screen.getByLabelText('Requires approval'))
    expect(onToggleApproval).toHaveBeenCalledWith(3, 'requires_approval', true)
    expect(await screen.findByLabelText('Block in YOLO mode')).toBeInTheDocument()
  })

  it('reverts the checkbox when the write is refused', async () => {
    const onToggleApproval = vi.fn(async () => false)
    render(<TaskDetailPanel task={task()} allTasks={OTHERS} onClose={() => {}} editable onToggleApproval={onToggleApproval} />)
    const box = screen.getByLabelText('Requires approval')
    await userEvent.click(box)
    await waitFor(() => expect(box).not.toBeChecked())
  })

  it('clears the YOLO gate when approval itself is turned off', async () => {
    const onToggleApproval = vi.fn(async () => true)
    render(
      <TaskDetailPanel
        task={task({ requires_approval: true, force_approval: true })}
        allTasks={OTHERS}
        onClose={() => {}}
        editable
        onToggleApproval={onToggleApproval}
      />,
    )
    await userEvent.click(screen.getByLabelText('Requires approval'))
    await waitFor(() => expect(screen.queryByLabelText('Block in YOLO mode')).not.toBeInTheDocument())
  })

  it('toggles the YOLO gate on its own, reverting a refused write', async () => {
    const onToggleApproval = vi.fn(async () => false)
    render(
      <TaskDetailPanel
        task={task({ requires_approval: true })}
        allTasks={OTHERS}
        onClose={() => {}}
        editable
        onToggleApproval={onToggleApproval}
      />,
    )
    const yolo = screen.getByLabelText('Block in YOLO mode')
    await userEvent.click(yolo)
    expect(onToggleApproval).toHaveBeenCalledWith(3, 'force_approval', true)
    await waitFor(() => expect(yolo).not.toBeChecked())
  })

  it('offers approve / deny only while the gated task is actually running', async () => {
    const onApprove = vi.fn()
    const { unmount } = render(
      <TaskDetailPanel task={task({ requires_approval: true, status: 'in_progress' })} allTasks={OTHERS} onClose={() => {}} onApprove={onApprove} />,
    )
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    expect(onApprove).toHaveBeenCalledWith('approve')
    await userEvent.click(screen.getByRole('button', { name: /Deny/ }))
    expect(onApprove).toHaveBeenCalledWith('reject')
    unmount()

    render(<TaskDetailPanel task={task({ requires_approval: true })} allTasks={OTHERS} onClose={() => {}} onApprove={onApprove} />)
    expect(screen.queryByRole('button', { name: /Approve/ })).not.toBeInTheDocument()
    expect(screen.getByText('Requires approval before execution')).toBeInTheDocument()
  })

  it('names force-approval as the harder gate when it is the one set', () => {
    render(
      <TaskDetailPanel
        task={task({ force_approval: true, status: 'in_progress' })}
        allTasks={OTHERS}
        onClose={() => {}}
        onApprove={vi.fn()}
      />,
    )
    expect(screen.getByRole('button', { name: /Approve/ })).toBeInTheDocument()
  })
})

describe('TaskDetailPanel read-only surfaces', () => {
  it('shows the type icon, error and result blocks', () => {
    const { container } = render(
      <TaskDetailPanel
        task={task({ task_type: 'fix', status: 'failed', error: 'zzz it broke', result: 'zzz partial output' })}
        allTasks={OTHERS}
        onClose={() => {}}
        onRetry={vi.fn()}
      />,
    )
    expect(container.querySelector('svg.lucide-wrench')).toBeInTheDocument()
    expect(screen.getByText('zzz it broke')).toBeInTheDocument()
    expect(screen.getByText('zzz partial output')).toBeInTheDocument()
  })

  it('marks a checkpoint task with its own icon', () => {
    const { container } = render(
      <TaskDetailPanel task={task({ task_type: 'checkpoint' })} allTasks={OTHERS} onClose={() => {}} />,
    )
    expect(container.querySelector('svg.lucide-shield')).toBeInTheDocument()
  })

  it('retries a failed task from the footer', async () => {
    const onRetry = vi.fn()
    render(<TaskDetailPanel task={task({ status: 'failed' })} allTasks={OTHERS} onClose={() => {}} onRetry={onRetry} />)
    await userEvent.click(screen.getByRole('button', { name: /Retry Task/ }))
    expect(onRetry).toHaveBeenCalledWith(3)
  })

  it('lists resolved dependencies when nothing is blocking', () => {
    render(
      <TaskDetailPanel
        task={task({ depends_on: [1], status: 'passed' })}
        allTasks={OTHERS}
        onClose={() => {}}
      />,
    )
    expect(screen.getByText(/Depends on:/)).toBeInTheDocument()
    expect(screen.getByText(/zzz setup/)).toBeInTheDocument()
  })

  it('names what is blocking a pending task', () => {
    render(
      <TaskDetailPanel task={task({ depends_on: [1, 2] })} allTasks={OTHERS} onClose={() => {}} />,
    )
    expect(screen.getByText(/Blocked/)).toBeInTheDocument()
    expect(screen.getByText(/zzz build/)).toBeInTheDocument()
  })

  it('closes on request', async () => {
    const onClose = vi.fn()
    render(<TaskDetailPanel task={task()} allTasks={OTHERS} onClose={onClose} />)
    await userEvent.click(screen.getByRole('button', { name: /close/i }))
    expect(onClose).toHaveBeenCalled()
  })
})
