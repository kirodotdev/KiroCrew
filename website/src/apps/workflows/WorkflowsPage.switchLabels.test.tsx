/**
 * The Workflows page's Author/Runs switch reads translated labels, never the
 * English words, in every language.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../api/client', () => ({ api: { promoteWorkflowRun: vi.fn() } }))

import WorkflowsPage from './WorkflowsPage'
import { i18next } from '../../i18n/all'

const jsonResponse = (body: unknown) =>
  Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) }) as Promise<Response>

function withClient(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/workflows/runs') return jsonResponse({ runs: [] })
      if (url === '/apps/workflows/api/examples') return jsonResponse([])
      throw new Error(`Unexpected request: ${url}`)
    }),
  )
})

afterEach(async () => {
  vi.unstubAllGlobals()
  await i18next.changeLanguage('en')
})

describe('Workflows Author/Runs switch', () => {
  it('labels both views in the selected language', async () => {
    await i18next.changeLanguage('de')
    withClient(<WorkflowsPage />)
    expect(screen.getByText('Erstellen')).toBeInTheDocument()
    // A narrow viewport collapses the switch to its selected segment; open it.
    if (!screen.queryByText('Ausführungen')) fireEvent.click(screen.getByText('Erstellen'))
    expect(screen.getByText('Ausführungen')).toBeInTheDocument()
    expect(screen.queryByText('Author')).not.toBeInTheDocument()
  })
})
