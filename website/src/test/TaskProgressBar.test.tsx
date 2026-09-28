import { describe, it, expect } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import TaskProgressBar from '../pages/chat/TaskProgressBar'
import dashboardReducer, { sseTodoUpdate, sseSlots } from '../store/dashboardSlice'
import type { ChatSlot, TodoList } from '../types'

const todo = (tasks: Array<[string, boolean]>, description = 'Config workflow'): TodoList => {
  const list = tasks.map(([text, completed], i) => ({ id: String(i + 1), text, completed }))
  const completed = list.filter(t => t.completed).length
  return {
    description,
    tasks: list,
    completed,
    total: list.length,
    current: list.find(t => !t.completed)?.text ?? '',
  }
}

const slot = (key: string, t: TodoList | null): ChatSlot =>
  ({ key, messages: 0, running: false, todo: t }) as ChatSlot

function renderBar(slots: ChatSlot[], activeKey: string | null = 'slot-1') {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer },
    preloadedState: { dashboard: { slots } } as never,
  })
  const utils = render(
    <Provider store={store}>
      <TaskProgressBar slot={activeKey} />
    </Provider>,
  )
  return { store, ...utils }
}

describe('TaskProgressBar', () => {
  it('renders nothing when the slot has no todo list', () => {
    renderBar([slot('slot-1', null)])
    expect(screen.queryByTestId('todo-pill')).toBeNull()
  })

  it('renders nothing when the list is present but empty', () => {
    renderBar([slot('slot-1', todo([]))])
    expect(screen.queryByTestId('todo-pill')).toBeNull()
  })

  it('renders nothing when no slot is active', () => {
    renderBar([slot('slot-1', todo([['a', false]]))], null)
    expect(screen.queryByTestId('todo-pill')).toBeNull()
  })

  it('shows the server-derived count as "N of M"', () => {
    renderBar([slot('slot-1', todo([['a', true], ['b', false], ['c', false]]))])
    expect(screen.getByTestId('todo-count').textContent).toBe('1 of 3')
  })

  it('shows the first incomplete task as the current task', () => {
    renderBar([slot('slot-1', todo([['done it', true], ['do this next', false]]))])
    expect(screen.getByTestId('todo-current').textContent).toBe('do this next')
  })

  it('reports completion instead of a current task when all are done', () => {
    renderBar([slot('slot-1', todo([['a', true], ['b', true]]))])
    expect(screen.getByTestId('todo-count').textContent).toBe('2 of 2')
    expect(screen.getByTestId('todo-current').textContent).toBe('All tasks complete')
  })

  it('stays visible once every task is complete', () => {
    // Deliberate UX choice: the finished list is the payoff, not noise to hide.
    renderBar([slot('slot-1', todo([['a', true]]))])
    expect(screen.getByTestId('todo-pill')).toBeTruthy()
  })

  it('is collapsed initially and expands the full list on click', async () => {
    const user = userEvent.setup()
    renderBar([slot('slot-1', todo([['alpha', true], ['beta', false]]))])
    expect(screen.queryByTestId('todo-list')).toBeNull()
    const pill = screen.getByTestId('todo-pill')
    expect(pill.getAttribute('aria-expanded')).toBe('false')

    await user.click(pill)
    expect(screen.getByTestId('todo-list')).toBeTruthy()
    expect(pill.getAttribute('aria-expanded')).toBe('true')
    const rows = screen.getAllByTestId('todo-row')
    expect(rows).toHaveLength(2)
    // Scoped to the rows: 'beta' also appears in the pill's current-task label,
    // so a document-wide text query would be ambiguous by design.
    expect(rows.map(r => r.textContent)).toEqual(['alpha', 'beta'])
  })

  it('collapses again on a second click', async () => {
    const user = userEvent.setup()
    renderBar([slot('slot-1', todo([['alpha', false]]))])
    const pill = screen.getByTestId('todo-pill')
    await user.click(pill)
    expect(screen.getByTestId('todo-list')).toBeTruthy()
    await user.click(pill)
    expect(screen.queryByTestId('todo-list')).toBeNull()
  })

  it('reads only the active slot\'s list', () => {
    renderBar(
      [slot('slot-1', todo([['mine', false]])), slot('slot-2', todo([['theirs', false], ['other', false]]))],
      'slot-1',
    )
    expect(screen.getByTestId('todo-count').textContent).toBe('0 of 1')
    expect(screen.getByTestId('todo-current').textContent).toBe('mine')
  })

  it('exposes an accessible progressbar matching the count', () => {
    renderBar([slot('slot-1', todo([['a', true], ['b', false], ['c', false], ['d', false]]))])
    const bar = screen.getByRole('progressbar')
    expect(bar.getAttribute('aria-valuenow')).toBe('1')
    expect(bar.getAttribute('aria-valuemax')).toBe('4')
  })

  it('tolerates a partial state slice with no slots key', () => {
    // Fixtures across the suite build partial preloaded state; an undefined
    // slots array must not throw.
    const store = configureStore({
      reducer: { dashboard: dashboardReducer },
      preloadedState: { dashboard: {} } as never,
    })
    expect(() =>
      render(
        <Provider store={store}>
          <TaskProgressBar slot="slot-1" />
        </Provider>,
      ),
    ).not.toThrow()
  })

  it('updates live when a todo_update delta arrives', () => {
    const { store } = renderBar([slot('slot-1', todo([['a', false], ['b', false]]))])
    expect(screen.getByTestId('todo-count').textContent).toBe('0 of 2')
    act(() => { store.dispatch(sseTodoUpdate({ slot: 'slot-1', todo: todo([['a', true], ['b', false]]) })) })
    expect(screen.getByTestId('todo-count').textContent).toBe('1 of 2')
    expect(screen.getByTestId('todo-current').textContent).toBe('b')
  })

  it('rehydrates from a slots snapshot after reconnect', () => {
    // The snapshot path is what makes the pill survive a refresh mid-turn.
    const { store } = renderBar([slot('slot-1', null)])
    expect(screen.queryByTestId('todo-pill')).toBeNull()
    act(() => { store.dispatch(sseSlots([slot('slot-1', todo([['a', true], ['b', false]]))])) })
    expect(screen.getByTestId('todo-count').textContent).toBe('1 of 2')
  })
})

describe('sseTodoUpdate reducer', () => {
  const initial = { slots: [slot('slot-1', null), slot('slot-2', null)] } as never

  it('patches the addressed slot only', () => {
    const next = dashboardReducer(initial, sseTodoUpdate({ slot: 'slot-2', todo: todo([['x', false]]) }))
    expect(next.slots[0].todo).toBeNull()
    expect(next.slots[1].todo?.tasks[0].text).toBe('x')
  })

  it('clears a list when the delta carries null', () => {
    const withTodo = { slots: [slot('slot-1', todo([['x', false]]))] } as never
    const next = dashboardReducer(withTodo, sseTodoUpdate({ slot: 'slot-1', todo: null }))
    expect(next.slots[0].todo).toBeNull()
  })

  it('ignores a delta for an unknown slot', () => {
    const next = dashboardReducer(initial, sseTodoUpdate({ slot: 'ghost', todo: todo([['x', false]]) }))
    expect(next.slots).toHaveLength(2)
    expect(next.slots.every(s => s.todo === null)).toBe(true)
  })
})
