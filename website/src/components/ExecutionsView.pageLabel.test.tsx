import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ExecutionsView from './ExecutionsView'
import { i18next } from '../i18n/all'

vi.mock('../api/client', () => ({
  api: {
    crons: vi.fn().mockResolvedValue({ jobs: [{ id: 'j1' }] }),
    cronHistoryAll: vi.fn().mockResolvedValue({
      runs: [{
        job_id: 'j1', job_name: 'Job One', run_id: 'r1', started_at: 1_700_000_000_000,
        duration_ms: 1_000, status: 'success', trigger: 'schedule',
      }],
    }),
    cronRunDetail: vi.fn(),
  },
}))

describe('ExecutionsView page label', () => {
  it('renders the page number inside one catalog sentence', async () => {
    // en-XA brackets every catalog value, so "Page" + n reads `[Page] 1`.
    await i18next.changeLanguage('en-XA')
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <ExecutionsView />
      </QueryClientProvider>,
    )
    await screen.findByText('Job One')
    expect(screen.getByText(/^\[[^[\]]* 1[^[\]]*\]$/)).toBeInTheDocument()
  })
})
