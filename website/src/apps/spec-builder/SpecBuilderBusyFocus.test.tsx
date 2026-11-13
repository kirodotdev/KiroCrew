// #32999: the remaining Spec Builder write-action buttons dropped keyboard
// focus while their own request ran. A native `disabled` on a focused button
// takes it out of the tab order, so a keyboard or screen-reader user who
// pressed it landed on the page body. The fix is the `busy` shape #33001 added
// to the app's pill `Btn` (components/shared.tsx): aria-disabled, no click, the
// same dimmed look, but natively enabled so the press keeps focus.
//
// These tests fail on main (the buttons render native `disabled` while busy and
// `toHaveFocus` fails after the press) and pass with the fix. One case per
// component file the fix touches; NewSpecView's Start uses the identical `Btn`
// `busy` mechanism SettingsModal's Save is tested through here.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import React from 'react'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('./components/ChatColumn', () => ({
  default: () => <div data-testid="chat-column" />,
}))

import SpecDetail from './components/SpecDetail'
import SettingsModal from './components/SettingsModal'
import TaskList from './components/TaskList'

let queryClient: QueryClient

const DETAIL = {
  name: 'ready',
  status: 'planning',
  phase: 'tasks',
  running: false,
  spec_dir: '/p/.kiro/specs/ready',
  working_dir: '/p',
  slot_key: 'spec-builder-ready-abcd1234',
  files: { 'requirements.md': '# r', 'design.md': '# d', 'tasks.md': '- [ ] one' },
  context: { turns: 0, tool_calls: 0 },
}

function withClient(ui: React.ReactElement) {
  return (
    <QueryClientProvider client={queryClient}>
      {ui}
    </QueryClientProvider>
  )
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  localStorage.clear()
})

afterEach(() => { vi.restoreAllMocks() })

describe('TaskList run button (TaskList.tsx)', () => {
  it('keeps the run button focusable while a build is in flight', () => {
    const tasks = [{ text: 'do the thing', done: false }]

    const idle = render(withClient(
      <TaskList tasks={tasks as never} pendingIndex={null} busy={false} onRun={() => {}} />,
    ))
    const run = screen.getByRole('button', { name: /Run task: do the thing/i })
    expect(run).not.toHaveAttribute('aria-disabled', 'true')
    expect(run).not.toBeDisabled()
    idle.unmount()

    // busy=true models the whole-list build running. A single-task run is
    // refused then, so the control is inert -- but a keyboard user who had it
    // focused must keep their place rather than drop to the page.
    render(withClient(
      <TaskList tasks={tasks as never} pendingIndex={null} busy onRun={() => {}} />,
    ))
    const busyRun = screen.getByRole('button', { name: /Run task: do the thing/i })
    busyRun.focus()
    expect(busyRun).toHaveAttribute('aria-disabled', 'true')
    expect(busyRun).not.toBeDisabled()
    expect(busyRun).toHaveFocus()
  })
})

describe('SettingsModal save button (SettingsModal.tsx)', () => {
  it('keeps Save focusable while its write is in flight', async () => {
    let releaseSave: (() => void) | undefined
    vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const method = (init?.method || 'GET').toUpperCase()
      if (method !== 'GET' && String(url).includes('/settings')) {
        return new Promise((res) => {
          releaseSave = () => res({ ok: true, status: 200, text: async () => '{"ok":true}' })
        })
      }
      // Both the settings read and the models read: a loaded base_path so Save
      // is enabled (the unloaded guard stays a native disable, not our concern).
      return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify({ base_path: '/p', model: 'auto', models: ['auto'] }) })
    }))

    render(withClient(<SettingsModal onClose={() => {}} />))

    const save = await waitFor(() => {
      const b = screen.getByRole('button', { name: /^Save$/i })
      expect(b).not.toBeDisabled()
      return b
    })

    save.focus()
    fireEvent.click(save)

    const saving = await screen.findByRole('button', { name: /Saving/i })
    expect(saving).toHaveAttribute('aria-disabled', 'true')
    expect(saving).not.toBeDisabled()
    expect(saving).toHaveFocus()

    releaseSave?.()
  })
})

describe('SpecDetail delete dialog (SpecDetail.tsx)', () => {
  it('keeps the Remove button focusable while the delete is in flight', async () => {
    let releaseDelete: (() => void) | undefined
    const deleteCalls: string[] = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const method = (init?.method || 'GET').toUpperCase()
      if (method === 'DELETE') {
        deleteCalls.push(url)
        return new Promise((res) => {
          releaseDelete = () => res({ ok: true, status: 200, text: async () => '{"ok":true}' })
        })
      }
      return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify(DETAIL) })
    }))

    const user = userEvent.setup()
    render(withClient(<SpecDetail name="ready" setErr={() => {}} />))

    // Open the overflow menu -> Remove -> the confirm dialog. Radix opens on a
    // real pointer sequence, so drive it with userEvent, not fireEvent.click.
    const more = await waitFor(() => screen.getByRole('button', { name: /More actions/i }))
    await user.click(more)
    const removeItem = await screen.findByRole('menuitem', { name: /Remove spec ready/i })
    await user.click(removeItem)

    const confirm = await waitFor(() => {
      const b = screen.getByRole('button', { name: /^Remove this spec$/i })
      expect(b).not.toBeDisabled()
      return b
    })

    confirm.focus()
    fireEvent.click(confirm)

    const removing = await screen.findByRole('button', { name: /Removing/i })
    expect(removing).toHaveAttribute('aria-disabled', 'true')
    expect(removing).not.toBeDisabled()
    expect(removing).toHaveFocus()
    // Second press while busy must not fire a second delete.
    fireEvent.click(removing)
    expect(deleteCalls).toHaveLength(1)

    releaseDelete?.()
    await waitFor(() => expect(deleteCalls).toHaveLength(1))
  })
})
