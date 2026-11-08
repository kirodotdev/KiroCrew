// Start building must not be clickable twice.
//
// Two clicks queued TWO handoffs. Pause halts the running turn and removes the
// nudge loop, but a handoff already queued behind the first one survives it — so
// execution resumed by itself and kept editing the user's files after they had
// stopped it. The button is inert while the handoff request is in flight, and
// stays focusable so a keyboard user keeps their place.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import React from 'react'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('./components/ChatColumn', () => ({
  default: () => <div data-testid="chat-column" />,
}))

import SpecDetail from './components/SpecDetail'

let queryClient: QueryClient

const DETAIL = {
  name: 'ready',
  status: 'planning',
  phase: 'tasks',
  running: false,
  spec_dir: '/p/.kiro/specs/ready',
  working_dir: '/p',
  slot_key: 'spec-builder-ready-abcd1234',
  // hasTasks is driven by the presence of tasks.md.
  files: { 'requirements.md': '# r', 'design.md': '# d', 'tasks.md': '- [ ] one' },
  context: { turns: 0, tool_calls: 0 },
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  localStorage.clear()
})

afterEach(() => { vi.restoreAllMocks() })

describe('SpecDetail execute button', () => {
  it('keeps Start building focused and inert while the handoff is in flight', async () => {
    let releaseExecute: (() => void) | undefined
    const executeCalls: string[] = []

    vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if ((init?.method || 'GET') === 'POST' && url.includes('/execute')) {
        executeCalls.push(url)
        return new Promise((res) => {
          // req() reads the body with text() and JSON.parses it.
          releaseExecute = () => res({ ok: true, status: 200, text: async () => '{"ok":true}' })
        })
      }
      return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify(DETAIL) })
    }))

    render(
      <QueryClientProvider client={queryClient}>
        <SpecDetail name="ready" setErr={() => {}} />
      </QueryClientProvider>,
    )

    const button = await waitFor(() => screen.getByRole('button', { name: /Start building/i }))
    expect(button).not.toBeDisabled()

    button.focus()
    fireEvent.click(button)

    // In flight: the control is inert and a second click cannot queue a second
    // handoff. It stays natively enabled, so the keyboard user who pressed it
    // keeps their place instead of landing on the page.
    const starting = await screen.findByRole('button', { name: /Starting/i })
    expect(starting).toHaveAttribute('aria-disabled', 'true')
    expect(starting).not.toBeDisabled()
    expect(starting).toHaveFocus()
    fireEvent.click(starting)
    expect(executeCalls).toHaveLength(1)

    releaseExecute?.()
    await waitFor(() => expect(executeCalls).toHaveLength(1))
  })
})
