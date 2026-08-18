/**
 * What a card surfaces, and which of its actions are reachable.
 *
 * The card's conditionals are the product decisions: a failed run shows its
 * error (otherwise the user must open the task to learn why), a running task
 * offers "watch live" but NOT "run" (a second run would race the first), and
 * the row-level buttons must stop propagation or every click also opens the
 * detail modal. Clicking through the card is the only thing that proves those.
 */
import { DndContext } from '@dnd-kit/core'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import React from 'react'
import { describe, expect, it, vi } from 'vitest'

import { TaskCard } from './TaskCard'
import { i18nT } from '../../../i18n/t'
import type { TaskRecord } from '../types'

function task(over: Partial<TaskRecord> = {}): TaskRecord {
  return {
    id: 't1',
    title: 'Fix the cache',
    description: '',
    prompt: '',
    status: 'todo',
    created_at: 1_700_000_000,
    updated_at: 1_700_000_000,
    executions: [],
    tags: [],
    priority: 'medium',
    ...over,
  }
}

/** dnd-kit's useSortable needs a DndContext ancestor. */
function mount(ui: React.ReactElement) {
  return render(<DndContext>{ui}</DndContext>)
}

describe('what the card shows', () => {
  it('shows the title', () => {
    mount(<TaskCard task={task()} onClick={() => {}} />)
    expect(screen.getByText('Fix the cache')).toBeInTheDocument()
  })

  it('says the name is still coming while the card is being named', () => {
    mount(<TaskCard task={task({ refining: true })} onClick={() => {}} />)
    // The provisional title is shown, but not presented as final: the indicator
    // is announced to a screen reader rather than being a bare spinner glyph.
    expect(screen.getByText('Refining...')).toBeInTheDocument()
  })

  it('shows no naming indicator once the card has its real name', () => {
    mount(<TaskCard task={task()} onClick={() => {}} />)
    expect(screen.queryByText('Refining...')).toBeNull()
  })

  it('falls back to the prompt when there is no description', () => {
    mount(<TaskCard task={task({ prompt: 'do the thing' })} onClick={() => {}} />)
    expect(screen.getByText('do the thing')).toBeInTheDocument()
  })

  it('prefers the description over the prompt', () => {
    mount(
      <TaskCard task={task({ description: 'the desc', prompt: 'the prompt' })} onClick={() => {}} />,
    )
    expect(screen.getByText('the desc')).toBeInTheDocument()
    expect(screen.queryByText('the prompt')).not.toBeInTheDocument()
  })

  it('marks a high-priority task', () => {
    // Through the catalog, not a literal: the badge renders on every card in a
    // 12-language dashboard, so a hardcoded "high" is untranslated everywhere.
    mount(<TaskCard task={task({ priority: 'high' })} onClick={() => {}} />)
    expect(screen.getByText(i18nT('apps.kanban.taskCard.priority_high'))).toBeInTheDocument()
  })

  it('pluralises the run count through the catalog', () => {
    const t = task({ executions: [{ id: 'e1', started_at: 1 }, { id: 'e2', started_at: 2 }] })
    mount(<TaskCard task={t} onClick={() => {}} />)
    expect(
      screen.getByTitle(i18nT('apps.kanban.taskCard.run_count', { count: 2 })),
    ).toBeInTheDocument()
  })

  it('surfaces the failure reason on a failed task', () => {
    const t = task({
      status: 'failed',
      executions: [
        { id: 'e1', started_at: 1, ended_at: 2, result: 'failed', error: 'it exploded' },
      ],
    })
    mount(<TaskCard task={t} onClick={() => {}} />)
    expect(screen.getByText('it exploded')).toBeInTheDocument()
  })

  it('shows the run count', () => {
    const t = task({ executions: [{ id: 'e1', started_at: 1 }] })
    mount(<TaskCard task={t} onClick={() => {}} />)
    expect(screen.getByTitle(/1 run/)).toBeInTheDocument()
  })

  it('shows at most two tags', () => {
    mount(<TaskCard task={task({ tags: ['a', 'b', 'c'] })} onClick={() => {}} />)
    expect(screen.getByText('a')).toBeInTheDocument()
    expect(screen.getByText('b')).toBeInTheDocument()
    expect(screen.queryByText('c')).not.toBeInTheDocument()
  })
})

describe('the card actions', () => {
  it('opens the task on click', async () => {
    const onClick = vi.fn()
    mount(<TaskCard task={task()} onClick={onClick} />)
    await userEvent.click(screen.getByText('Fix the cache'))
    expect(onClick).toHaveBeenCalled()
  })

  it('opens the task on Enter', async () => {
    const onClick = vi.fn()
    mount(<TaskCard task={task()} onClick={onClick} />)
    const card = screen.getAllByRole('button')[0]
    card.focus()
    await userEvent.keyboard('{Enter}')
    expect(onClick).toHaveBeenCalled()
  })

  it('offers no run button while the task is running', () => {
    const onRun = vi.fn()
    mount(<TaskCard task={task({ status: 'running' })} onClick={() => {}} onRun={onRun} />)
    expect(screen.queryByTitle(/run this task/i)).not.toBeInTheDocument()
  })

  it('runs without also opening the task', async () => {
    const onClick = vi.fn()
    const onRun = vi.fn()
    mount(<TaskCard task={task()} onClick={onClick} onRun={onRun} />)
    await userEvent.click(screen.getByTitle(/run this task/i))
    expect(onRun).toHaveBeenCalled()
    expect(onClick).not.toHaveBeenCalled()
  })

  it('opens the session without also opening the task', async () => {
    const onClick = vi.fn()
    const onOpenSession = vi.fn()
    const t = task({ executions: [{ id: 'e1', started_at: 1, session_key: 'sess-9' }] })
    mount(<TaskCard task={t} onClick={onClick} onOpenSession={onOpenSession} />)
    await userEvent.click(screen.getByText(/view session/i))
    expect(onOpenSession).toHaveBeenCalledWith('sess-9')
    expect(onClick).not.toHaveBeenCalled()
  })

  it('dragging the handle does not open the task', async () => {
    const onClick = vi.fn()
    mount(<TaskCard task={task()} onClick={onClick} />)
    await userEvent.click(screen.getByLabelText(/drag to move/i))
    expect(onClick).not.toHaveBeenCalled()
  })
})
