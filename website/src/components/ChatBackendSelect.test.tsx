import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../test/helpers'
import type { ChatSlot } from '../types'
import ChatBackendSelect from './ChatBackendSelect'

// Exercise the real SimpleSelect and API transport using its native select path.
vi.mock('../hooks/useIsTouchDevice', () => ({ useIsTouchDevice: () => true }))
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('chat backend registry ids over the real API transport', () => {
  it.each([
    ['kiro', 'Kiro', ''], ['', 'Kiro', ''],
    ['kas', 'KAS', 'kas'], ['claude', 'Claude', 'claude'],
    ['codex', 'Codex', 'codex'], ['pi', 'Pi', 'pi'],
    ['opencode', 'OpenCode', 'opencode'], ['goose', 'Goose', 'goose'],
    ['deepseek', 'DeepSeek', 'deepseek'],
    ['__inherit__', 'Use global default', null],
  ])('selecting %s sends %s with the canonical runtime value', async (id, label, expected) => {
    const posts: { path: string; body: unknown }[] = []
    const json = (data: unknown) => new Response(JSON.stringify(data), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const path = String(input)
      if (path === '/api/acp-backends') return json({ backends: [
        { id, policy_id: label, selectable: true },
        { id: 'vibe', policy_id: 'Vibe', selectable: false },
      ].filter((row) => row.id !== '__inherit__') })
      if (init?.method === 'POST') {
        const body = JSON.parse(String(init.body))
        posts.push({ path, body })
        return json({ ok: true, acp_backend: body.acp_backend, model: 'auto' })
      }
      throw new Error(`Unexpected request: ${path}`)
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const slot = { key: 'chat-kiro', acp_backend: id === 'pi' ? 'codex' : 'pi', backend_selection_supported: true, running: false } as ChatSlot
    render(<Provider store={createTestStore()}><QueryClientProvider client={client}>
      <ChatBackendSelect slot={slot} />
    </QueryClientProvider></Provider>)
    const picker = screen.getByRole('combobox', { name: 'Backend' })
    await waitFor(() => expect(picker).not.toBeDisabled())
    expect(within(picker).queryByRole('option', { name: 'Vibe' })).toBeNull()
    const option = within(picker).getByRole('option', { name: label }) as HTMLOptionElement
    fireEvent.change(picker, { target: { value: option.value } })
    await waitFor(() => expect(posts).toEqual([
      { path: '/api/chat/slots/chat-kiro/backend', body: { acp_backend: expected } },
    ]))
    await waitFor(() => expect(picker).not.toBeDisabled())
  })
})
