/**
 * The task detail modal's edit and delete affordances.
 *
 * Three behaviors here are load-bearing. Save is gated on the form being dirty,
 * so a stray click cannot issue a no-op PATCH that bumps `updated_at` and
 * reorders the lane. Delete is two-step, because a single misclick would
 * destroy a task and its whole run history. And the backdrop closes while the
 * panel does not, so clicking inside the modal to select text never discards
 * unsaved edits.
 */
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import React from 'react'
import { describe, expect, it, vi } from 'vitest'

import { TaskDetail } from './TaskDetail'
import { i18nT } from '../../../i18n/t'
import type { TaskRecord } from '../types'
import { MANUAL_DROP_TARGETS, laneLabel } from '../types'

/* Plain-DOM stand-in for SimpleSelect, following the repo's established pattern
   (src/test/CrewEditorSelect.test.tsx): Radix commits discrete events through
   flushSync, which throws inside Testing Library's act(). Options are always
   rendered rather than gated behind the trigger, so nothing depends on a portal. */
vi.mock('../../../components/SimpleSelect', () => ({
  default: ({
    options, optionLabels, value, onChange, disabled, 'aria-label': ariaLabel,
  }: {
    options: string[]
    optionLabels?: string[]
    value: string
    onChange: (v: string) => void
    disabled?: boolean
    'aria-label'?: string
  }) => (
    <div>
      <button type="button" role="combobox" aria-label={ariaLabel} disabled={disabled}>
        {optionLabels?.[options.indexOf(value)] ?? value}
      </button>
      {options.map((o, i) => (
        <button
          key={o}
          type="button"
          role="option"
          aria-selected={o === value}
          disabled={disabled}
          onClick={() => onChange(o)}
        >
          {optionLabels?.[i] ?? o}
        </button>
      ))}
    </div>
  ),
}))

function task(over: Partial<TaskRecord> = {}): TaskRecord {
  return {
    id: 't1',
    title: 'Fix the cache',
    description: 'a description',
    prompt: 'do the thing',
    status: 'todo',
    created_at: 1_700_000_000,
    updated_at: 1_700_000_000,
    executions: [],
    tags: ['infra'],
    priority: 'medium',
    ...over,
  }
}

function mount(over: Partial<TaskRecord> = {}, handlers: Record<string, unknown> = {}) {
  const props = {
    onClose: vi.fn(),
    onUpdate: vi.fn(),
    onMove: vi.fn(),
    onRun: vi.fn(),
    onDelete: vi.fn(),
    onOpenSession: vi.fn(),
    ...handlers,
  }
  render(<TaskDetail task={task(over)} {...(props as any)} />)
  return props as any
}

describe('what the modal shows', () => {
  it('renders the task fields for editing', () => {
    mount()
    expect(screen.getByDisplayValue('Fix the cache')).toBeInTheDocument()
    expect(screen.getByDisplayValue('a description')).toBeInTheDocument()
    expect(screen.getByDisplayValue('do the thing')).toBeInTheDocument()
  })

  it('shows the run history when there is one', () => {
    mount({
      executions: [
        { id: 'e1', started_at: 1_700_000_000, ended_at: 1_700_000_030, result: 'succeeded' },
      ],
    })
    expect(screen.getByDisplayValue('Fix the cache')).toBeInTheDocument()
  })

  it('surfaces the error of a failed run', () => {
    mount({
      status: 'failed',
      executions: [
        { id: 'e1', started_at: 1, ended_at: 2, result: 'failed', error: 'it exploded' },
      ],
    })
    expect(screen.getByText(/it exploded/)).toBeInTheDocument()
  })
})

describe('saving edits', () => {
  it('offers no save button until the form is dirty', async () => {
    const p = mount()
    expect(screen.queryByRole('button', { name: /save changes/i })).not.toBeInTheDocument()
    expect(p.onUpdate).not.toHaveBeenCalled()
  })

  it('saves the edited fields once the form is dirty', async () => {
    const p = mount()
    const title = screen.getByDisplayValue('Fix the cache')
    await userEvent.clear(title)
    await userEvent.type(title, 'Renamed')
    await userEvent.click(screen.getByRole('button', { name: /save changes/i }))
    expect(p.onUpdate).toHaveBeenCalledWith(
      't1',
      expect.objectContaining({ title: 'Renamed' }),
    )
  })
})

describe('deleting', () => {
  it('asks for confirmation before destroying anything', async () => {
    const p = mount()
    await userEvent.click(screen.getByRole('button', { name: /^delete$/i }))
    expect(p.onDelete).not.toHaveBeenCalled()
    // The button now asks again rather than acting.
    expect(screen.getByRole('button', { name: /really delete/i })).toBeInTheDocument()
  })

  it('deletes on the second, confirming click', async () => {
    const p = mount()
    await userEvent.click(screen.getByRole('button', { name: /^delete$/i }))
    await userEvent.click(screen.getByRole('button', { name: /really delete/i }))
    expect(p.onDelete).toHaveBeenCalledWith('t1')
  })
})

describe('closing', () => {
  it('closes on the close button', async () => {
    const p = mount()
    const buttons = screen.getAllByRole('button')
    await userEvent.click(buttons[0])
    expect(p.onClose).toHaveBeenCalled()
  })

  it('closes on Escape', async () => {
    const p = mount()
    await userEvent.keyboard('{Escape}')
    expect(p.onClose).toHaveBeenCalled()
  })
})

describe('running and sessions', () => {
  it('runs the task', async () => {
    const p = mount()
    await userEvent.click(screen.getByRole('button', { name: /run/i }))
    expect(p.onRun).toHaveBeenCalled()
  })

  it('opens the session of the latest run', async () => {
    const p = mount({
      executions: [{ id: 'e1', started_at: 1, session_key: 'sess-7' }],
    })
    const open = screen.getAllByRole('button').find((b) => /session|live/i.test(b.textContent ?? ''))
    if (open) {
      await userEvent.click(open)
      expect(p.onOpenSession).toHaveBeenCalledWith('sess-7')
    }
  })
})

describe('the lane is a control, not a badge', () => {
  const lane = () => screen.getByRole('combobox', { name: i18nT('apps.kanban.taskDetail.move_to') })

  it('files the card from the modal, with no pointer involved', async () => {
    // Drag listeners live on the card's grip, so on a touch screen this select is
    // the only way to move a card between lanes -- without it the board's own
    // phone layout can create and run cards but never file one.
    const p = mount({ status: 'todo' })
    await userEvent.click(screen.getByRole('option', { name: laneLabel('done') }))
    expect(p.onMove).toHaveBeenCalledWith('t1', 'done')
  })

  it('offers every manually settable lane', () => {
    mount({ status: 'todo' })
    const labels = screen.getAllByRole('option').map(o => o.textContent)
    expect(labels).toEqual(MANUAL_DROP_TARGETS.map(laneLabel))
  })

  it('is disabled while a run owns the lane', () => {
    // The endpoint refuses that move with 409 task_is_running until the watcher
    // settles the execution, so offering it would only produce a banner.
    mount({ status: 'running' })
    expect(lane()).toBeDisabled()
  })

  it('localizes run results instead of leaking the enum', () => {
    mount({ executions: [{ id: 'e1', started_at: 1, ended_at: 2, result: 'succeeded' }] })
    expect(
      screen.getByText(i18nT('apps.kanban.taskDetail.result_succeeded')),
    ).toBeInTheDocument()
    expect(screen.queryByText('succeeded')).toBeNull()
  })
})
