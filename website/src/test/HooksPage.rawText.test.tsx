/**
 * The hooks table shows a hook's name, command and matcher exactly as typed.
 * React escapes text children itself, so a pre-escaped string would show its
 * entities (`&gt;&gt;`, `&amp;`) literally.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const NAME = 'log & notify <prompt>'
const COMMAND = 'echo prompt >> /tmp/log.txt & wait'
const MATCHER = 'a&b>c'

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'hooks') {
        return vi.fn(async () => ({
          hooks: [{
            id: 'h1', name: NAME, event: 'Stop', matcher: MATCHER, matcher_mode: 'glob',
            command: COMMAND, skills: [], timeout: 30, enabled: true,
            last_run: 0, last_status: '', run_count: 0,
          }],
        }))
      }
      return vi.fn().mockResolvedValue({})
    },
  }),
}))

vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp',
    capabilities: { hooks: false },
    labels: { hooksSection: 'Provider hooks' },
    fetchProviderHooks: () => Promise.resolve({}),
  }),
}))

import HooksPage from '../pages/HooksPage'

describe('hooks table — raw hook text', () => {
  it('shows name, command and matcher with >> and & exactly as typed', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <HooksPage />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    const nameCell = await screen.findByText(NAME)
    const row = within(nameCell.closest('tr')!)
    const commandCell = row.getByText(COMMAND)
    expect(nameCell.textContent).toBe(NAME)
    expect(commandCell.textContent).toBe(COMMAND)
    expect(commandCell).toHaveAttribute('title', COMMAND)
    expect(row.getByText(MATCHER).textContent).toBe(MATCHER)
    expect(nameCell.closest('tr')!.textContent).not.toMatch(/&(gt|lt|amp);/)
  })
})
