import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ExecutionsView from './ExecutionsView'

const { cronHistoryAll, cronRunDetail } = vi.hoisted(() => ({
  cronHistoryAll: vi.fn(),
  cronRunDetail: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
    crons: vi.fn().mockResolvedValue({ jobs: [{ id: 'j1' }] }),
    cronHistoryAll,
    cronRunDetail,
  },
}))

beforeEach(() => {
  cronHistoryAll.mockResolvedValue({
    runs: [{
      job_id: 'j1',
      job_name: 'Job One',
      run_id: 'r1',
      started_at: 1_700_000_000_000,
      duration_ms: 1200,
      status: 'success',
      trigger: 'schedule',
      summary: 'did a thing',
    }],
  })
  cronRunDetail.mockResolvedValue({ trace: 'TRACE BODY' })
})

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <ExecutionsView />
    </QueryClientProvider>,
  )
}

describe('ExecutionsView run row keyboard access', () => {
  it('gives each run a collapsed toggle button pointing at its trace row', async () => {
    renderView()
    const toggle = await screen.findByRole('button', { name: 'Job One' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(toggle).toHaveAttribute('aria-controls', 'executions-trace-r1')
  })

  it.each(['{Enter}', ' '])('opens and closes the trace with %s', async key => {
    const user = userEvent.setup()
    renderView()
    const toggle = await screen.findByRole('button', { name: 'Job One' })
    toggle.focus()
    await user.keyboard(key)
    expect(await screen.findByText('TRACE BODY')).toBeInTheDocument()
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(document.getElementById('executions-trace-r1')).not.toBeNull()
    await user.keyboard(key)
    expect(screen.queryByText('TRACE BODY')).toBeNull()
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
  })

  it('toggles once per button click, not twice through the row', async () => {
    renderView()
    const toggle = await screen.findByRole('button', { name: 'Job One' })
    fireEvent.click(toggle)
    expect(await screen.findByText('TRACE BODY')).toBeInTheDocument()
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
  })

  it('still opens the trace from a click anywhere on the row', async () => {
    renderView()
    fireEvent.click(await screen.findByText('did a thing'))
    expect(await screen.findByText('TRACE BODY')).toBeInTheDocument()
  })

  it('nests no button inside another button', async () => {
    renderView()
    await screen.findByRole('button', { name: 'Job One' })
    expect(document.querySelector('button button')).toBeNull()
  })
})
