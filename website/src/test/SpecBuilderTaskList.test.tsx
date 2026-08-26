// The task list: tasks.md as addressable work rather than static markdown.
//
// The app could previously only hand the WHOLE list to an autonudge loop — no way
// to run one task, no way to see which task a run was on, and no progress anywhere.
// The two behaviours worth pinning are that a run carries the task's HASH (so a
// list that moved under the user is refused server-side rather than dispatching
// whatever ended up at that index), and that the controls go quiet while a build
// owns the list.
import { describe, it, expect, vi } from 'vitest'
import React from 'react'
import { render, screen, fireEvent, act } from '@testing-library/react'
import TaskList from '../apps/spec-builder/components/TaskList'
import DocView, { type DocComposer } from '../apps/spec-builder/components/DocView'
import type { SpecDetail, SpecTask } from '../apps/spec-builder/api'

const idleComposer: DocComposer = {
  sel: null, setSel: () => {}, note: null, setNote: () => {}, draft: '', setDraft: () => {},
}

const tasks: SpecTask[] = [
  { index: 0, text: 'wire the endpoint', done: true, hash: 'a'.repeat(64) },
  { index: 1, text: 'add the tests', done: false, hash: 'b'.repeat(64) },
  { index: 2, text: 'update the docs', done: false, hash: 'c'.repeat(64) },
]

describe('TaskList', () => {
  it('shows derived progress and offers a run only for open tasks', () => {
    const onRun = vi.fn()
    render(
      <TaskList
        tasks={tasks}
        progress={{ done: 1, total: 3 }}
        pendingIndex={null}
        busy={false}
        onRun={onRun}
      />,
    )

    expect(screen.getByText(/1 of 3 tasks done/i)).toBeTruthy()
    const bar = screen.getByRole('progressbar', { name: /task progress/i })
    expect(bar.getAttribute('aria-valuenow')).toBe('1')
    expect(bar.getAttribute('aria-valuemax')).toBe('3')
    // Two open tasks, so two run controls; the finished one shows a state instead.
    expect(screen.getAllByRole('button', { name: /^run task:/i })).toHaveLength(2)
  })

  it('hands the run the task identity, not just its position', () => {
    const onRun = vi.fn()
    render(<TaskList tasks={tasks} pendingIndex={null} busy={false} onRun={onRun} />)

    act(() => { fireEvent.click(screen.getByRole('button', { name: /run task: add the tests/i })) })

    expect(onRun).toHaveBeenCalledTimes(1)
    // The hash travels with it: the agent rewrites tasks.md between polls, so the
    // index alone could name a different task by the time the request lands.
    expect(onRun.mock.calls[0][0]).toMatchObject({ index: 1, hash: 'b'.repeat(64) })
  })

  it('disables every run while a build owns the whole list', () => {
    // The backend refuses a single-task run during a whole-list build, because both
    // write the same files and check off the same boxes. The control explains that
    // instead of failing on click.
    render(<TaskList tasks={tasks} pendingIndex={null} busy onRun={vi.fn()} />)
    for (const btn of screen.getAllByRole('button', { name: /^run task:/i })) {
      expect(btn).toBeDisabled()
    }
  })

  it('disables the other runs while one is being dispatched', () => {
    render(<TaskList tasks={tasks} pendingIndex={1} busy={false} onRun={vi.fn()} />)
    for (const btn of screen.getAllByRole('button', { name: /^run task:/i })) {
      expect(btn).toBeDisabled()
    }
  })

  it('says the list is still coming rather than showing an empty grid', () => {
    render(<TaskList tasks={[]} pendingIndex={null} busy={false} onRun={vi.fn()} />)
    expect(screen.getByText(/no tasks yet/i)).toBeTruthy()
  })
})

describe('DocView tasks tab', () => {
  const detail = {
    name: 'thing',
    spec_dir: '/w/.kiro/specs/thing',
    phase: 'tasks',
    running: false,
    files: {
      'requirements.md': null,
      'design.md': null,
      'tasks.md': '# Delivery notes\nKeep this context visible.\n\n- [ ] add the tests\n',
    },
    docs: { 'tasks.md': { hash: 'd'.repeat(64) } },
    tasks: [{ index: 0, text: 'add the tests', done: false, hash: 'b'.repeat(64) }],
    task_progress: { done: 0, total: 1 },
    state: null,
    context: {},
  } as unknown as SpecDetail

  it('renders the checklist as work when a run handler is wired', () => {
    render(<DocView detail={detail} tab="tasks" addComment={vi.fn()} composer={idleComposer} runTask={vi.fn()} />)
    expect(screen.getByRole('button', { name: /run task: add the tests/i })).toBeTruthy()
  })

  it('keeps non-checklist task document content reachable', () => {
    render(<DocView detail={detail} tab="tasks" addComment={vi.fn()} composer={idleComposer} runTask={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: 'tasks.md' }))

    expect(screen.getByText('Delivery notes')).toBeTruthy()
    expect(screen.getByText('Keep this context visible.')).toBeTruthy()
  })

})
