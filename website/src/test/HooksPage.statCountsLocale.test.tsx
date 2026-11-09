/**
 * The hooks page stat cards (Total, Enabled, Total runs, Errors) print their
 * counts through the locale's number formatter. They rendered the raw JS
 * number, so Bengali read Latin digits beside translated labels (#33237).
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { i18next } from '../i18n/all'

const HOOK = {
  id: 'h1', name: 'backup', event: 'Stop', matcher: '', matcher_mode: 'glob',
  command: 'true', skills: [], timeout: 30, enabled: true,
  last_run: 0, last_status: '', run_count: 7,
}

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      // Two hooks: one enabled, 12 runs between them, one last run failed.
      if (prop === 'hooks') {
        return vi.fn(async () => ({
          hooks: [HOOK, { ...HOOK, id: 'h2', name: 'lint', enabled: false, last_status: 'error', run_count: 5 }],
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

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <HooksPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

// In act: the page is still mounted, and a language switch re-renders it.
afterEach(async () => { await act(async () => { await i18next.changeLanguage('en') }) })

describe('HooksPage stat counts', () => {
  it('formats the counts in the reader\'s numerals', async () => {
    await i18next.changeLanguage('bn')
    renderPage()
    await screen.findByText('backup')
    expect(screen.getAllByTestId('stat-card-value').map(v => v.textContent)).toEqual(['২', '১', '১২', '১'])
  })
})
