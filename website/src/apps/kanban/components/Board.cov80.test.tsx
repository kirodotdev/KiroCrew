/**
 * The board's lane layout and its drag-validation rules.
 *
 * `handleDragEnd` is where an illegal move is refused, and every branch there
 * protects something real: dropping onto `running` would let a user fake an
 * execution that no agent is performing, and dragging a running task anywhere
 * but done/failed would abandon a live run. Those rules are invisible from the
 * DOM, so DndContext is stubbed to hand back its own handlers and they are
 * driven directly with synthetic drag events.
 */
import { act, render, screen } from '@testing-library/react'
import React from 'react'
import { describe, expect, it, vi } from 'vitest'

import type { TaskRecord, TaskStatus } from '../types'

/** Captured DndContext handlers, so drag validation can be exercised. */
const captured: {
  onDragStart?: (e: unknown) => void
  onDragEnd?: (e: unknown) => void
} = {}

vi.mock('@dnd-kit/core', async (importOriginal) => {
  const actual = (await importOriginal()) as Record<string, unknown>
  return {
    ...actual,
    DndContext: ({ children, onDragStart, onDragEnd }: any) => {
      captured.onDragStart = onDragStart
      captured.onDragEnd = onDragEnd
      return <div>{children}</div>
    },
    DragOverlay: ({ children }: any) => <div>{children}</div>,
    useDroppable: () => ({ setNodeRef: () => {}, isOver: false }),
  }
})

vi.mock('@dnd-kit/sortable', async (importOriginal) => {
  const actual = (await importOriginal()) as Record<string, unknown>
  return {
    ...actual,
    SortableContext: ({ children }: any) => <div>{children}</div>,
    useSortable: () => ({
      attributes: {},
      listeners: {},
      setNodeRef: () => {},
      transform: null,
      transition: undefined,
      isDragging: false,
    }),
  }
})

const { Board } = await import('./Board')

function task(over: Partial<TaskRecord> = {}): TaskRecord {
  return {
    id: 't1',
    title: 'Task one',
    description: '',
    prompt: '',
    status: 'todo',
    created_at: 1,
    updated_at: 1,
    executions: [],
    tags: [],
    priority: 'medium',
    ...over,
  }
}

function drag(over: string | null, active = 't1') {
  captured.onDragEnd?.({ active: { id: active }, over: over ? { id: over } : null })
}

function mount(tasks: TaskRecord[], onMove = vi.fn()) {
  render(
    <Board
      tasks={tasks}
      onMove={onMove}
      onTaskClick={() => {}}
      onTaskRun={() => {}}
      onOpenSession={() => {}}
    />,
  )
  return onMove
}

describe('the lanes', () => {
  it('renders every lane, empty', () => {
    mount([])
    // Each lane shows its own empty state.
    expect(screen.getAllByText(/no tasks/i).length).toBe(5)
  })

  it('places a task in its own lane and counts it', () => {
    mount([task({ title: 'Only task', status: 'todo' })])
    expect(screen.getByText('Only task')).toBeInTheDocument()
    expect(screen.getAllByText(/no tasks/i).length).toBe(4)
  })

  it('orders a lane newest-first', () => {
    mount([
      task({ id: 'a', title: 'older', updated_at: 10 }),
      task({ id: 'b', title: 'newer', updated_at: 99 }),
    ])
    const html = document.body.innerHTML
    expect(html.indexOf('newer')).toBeLessThan(html.indexOf('older'))
  })
})

describe('drag validation', () => {
  it('moves a task dropped on a lane', () => {
    const onMove = mount([task({ status: 'todo' })])
    drag('done')
    expect(onMove).toHaveBeenCalledWith('t1', 'done')
  })

  it('adopts the lane of the card it was dropped on', () => {
    const onMove = mount([
      task({ id: 't1', status: 'todo' }),
      task({ id: 't2', status: 'done', updated_at: 5 }),
    ])
    drag('t2')
    expect(onMove).toHaveBeenCalledWith('t1', 'done')
  })

  it('ignores a drop outside any lane', () => {
    const onMove = mount([task()])
    drag(null)
    expect(onMove).not.toHaveBeenCalled()
  })

  it('ignores a drop onto the lane the task is already in', () => {
    const onMove = mount([task({ status: 'todo' })])
    drag('todo')
    expect(onMove).not.toHaveBeenCalled()
  })

  it('refuses a manual drop onto running', () => {
    const onMove = mount([task({ status: 'todo' })])
    drag('running')
    expect(onMove).not.toHaveBeenCalled()
  })

  it('refuses to drag a running task back to todo', () => {
    const onMove = mount([task({ status: 'running' })])
    drag('todo')
    expect(onMove).not.toHaveBeenCalled()
  })

  it('allows a running task to be settled as done', () => {
    const onMove = mount([task({ status: 'running' })])
    drag('done')
    expect(onMove).toHaveBeenCalledWith('t1', 'done')
  })

  it('allows a running task to be settled as failed', () => {
    const onMove = mount([task({ status: 'running' })])
    drag('failed')
    expect(onMove).toHaveBeenCalledWith('t1', 'failed')
  })

  it('ignores a drag whose task is unknown', () => {
    const onMove = mount([task({ id: 't1' })])
    drag('done', 'ghost')
    expect(onMove).not.toHaveBeenCalled()
  })

  it('ignores a drop onto an unknown target', () => {
    const onMove = mount([task()])
    drag('not-a-lane-or-card')
    expect(onMove).not.toHaveBeenCalled()
  })
})

describe('the drag overlay', () => {
  it('shows the dragged card while a drag is active', () => {
    mount([task({ title: 'Dragging me' })])
    // The state update must be flushed, or the overlay has not re-rendered yet.
    act(() => captured.onDragStart?.({ active: { id: 't1' } }))
    // The overlay renders a second copy of the card.
    expect(screen.getAllByText('Dragging me').length).toBeGreaterThan(1)
  })

  it('ignores a drag start for an unknown task', () => {
    mount([task()])
    act(() => captured.onDragStart?.({ active: { id: 'ghost' } }))
    expect(screen.getAllByText('Task one').length).toBe(1)
  })
})
