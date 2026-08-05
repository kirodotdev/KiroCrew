import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ReportProblemCard from './ReportProblemCard'
import { api } from '../../api/client'

vi.mock('../../api/client', () => ({
  api: {
    collectDiagnostics: vi.fn(),
    revealPath: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}))

function renderCard() {
  const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ReportProblemCard />
    </QueryClientProvider>,
  )
}

const RESULT = {
  zip_path: '/Users/x/.kiro/crew/diagnostics/b.zip',
  filename: 'b.zip',
  included: ['versions.txt', 'gateway.log'],
  skipped: [],
  redaction_summary: { 'gateway.log': 3 },
  total_redactions: 3,
  github_issue_url: 'https://github.com/kirodotdev/KiroCrew/issues/new?title=x&body=y',
  download_url: '/api/diagnostics/download/b.zip',
}

describe('ReportProblemCard', () => {
  beforeEach(() => vi.clearAllMocks())

  it('opens the modal from the Report a Problem button', () => {
    renderCard()
    fireEvent.click(screen.getByRole('button', { name: /report a problem/i }))
    expect(screen.getByText(/what happened/i)).toBeInTheDocument()
  })

  it('collects diagnostics and surfaces the three delivery actions', async () => {
    ;(api.collectDiagnostics as ReturnType<typeof vi.fn>).mockResolvedValue(RESULT)
    renderCard()

    fireEvent.click(screen.getByRole('button', { name: /report a problem/i }))
    fireEvent.click(screen.getByRole('button', { name: /create report/i }))

    await waitFor(() =>
      expect(screen.getByText(/3 secret\(s\) redacted/i)).toBeInTheDocument(),
    )
    expect(api.collectDiagnostics).toHaveBeenCalledWith({ note: '', include_logs: true })

    // Finder + download + GitHub issue actions all present
    expect(screen.getByRole('button', { name: /show in finder/i })).toBeInTheDocument()
    const issueLink = screen.getByRole('link', { name: /open github issue/i })
    expect(issueLink).toHaveAttribute('href', RESULT.github_issue_url)
    const dl = screen.getByRole('link', { name: /download zip/i })
    expect(dl).toHaveAttribute('href', RESULT.download_url)

    fireEvent.click(screen.getByRole('button', { name: /show in finder/i }))
    expect(api.revealPath).toHaveBeenCalledWith(RESULT.zip_path)
  })

  it('shows an error when collection fails', async () => {
    ;(api.collectDiagnostics as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error('boom'),
    )
    renderCard()

    fireEvent.click(screen.getByRole('button', { name: /report a problem/i }))
    fireEvent.click(screen.getByRole('button', { name: /create report/i }))

    await waitFor(() =>
      expect(screen.getByText(/failed to collect diagnostics/i)).toBeInTheDocument(),
    )
  })
})
