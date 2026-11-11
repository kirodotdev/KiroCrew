/**
 * The hooks table renders a hook's name, command and matcher as plain JSX text.
 * React escapes text children itself, so pre-escaping them shows entities such
 * as `&gt;` literally. The cell must show the raw characters and must still not
 * parse markup in them.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const HOOK = {
  id: 'h1', name: 'a & b', event: 'Stop', matcher: '<x>&y', matcher_mode: 'glob',
  command: 'echo prompt >> /tmp/log.txt <b>bold</b>', skills: [], timeout: 30, enabled: true,
  last_run: 0, last_status: '', run_count: 0,
}

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'hooks') return vi.fn(async () => ({ hooks: [HOOK] }))
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

describe('hooks table — cell text', () => {
  it('shows name, command and matcher once-escaped, without parsing markup', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter><HooksPage /></MemoryRouter>
      </QueryClientProvider>,
    )
    const nameCell = await screen.findByText('a & b')
    const row = within(nameCell.closest('tr')!)
    const cmd = row.getByText(HOOK.command)
    expect(cmd.getAttribute('title')).toBe(HOOK.command)
    expect(cmd.querySelector('b')).toBeNull()
    expect(row.getByText('<x>&y')).toBeTruthy()
    expect(nameCell.closest('tr')!.textContent).not.toContain('&amp;')
    expect(nameCell.closest('tr')!.textContent).not.toContain('&gt;')
  })
})
