/** GitPanel: a refused repo must stay visible even when nothing is dirty.
 *
 * The `skipped` label lives inside the CHANGES section, and that section is gated
 * on there being something to show. A workspace whose only signal IS the refusal —
 * every other repo clean, no truncation — would otherwise render nothing, and the
 * refused repo reads as clean: the exact harm the label exists to prevent. */
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import GitPanel from '../components/GitPanel'

vi.mock('../api/client', () => ({
  api: { projectGitStatus: vi.fn(), projectGitLog: vi.fn() },
}))

type StatusPayload = Awaited<ReturnType<typeof api.projectGitStatus>>

const WS = '/ws'

function renderPanel(status: StatusPayload) {
  vi.mocked(api.projectGitStatus).mockResolvedValue(status)
  vi.mocked(api.projectGitLog).mockResolvedValue({ repo: false, commits: [] })
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <GitPanel projectDir={WS} onClose={() => {}} />
    </QueryClientProvider>,
  )
}

describe('GitPanel — a refused repo in an otherwise clean workspace', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('renders the skipped label when the refusal is the only signal', async () => {
    renderPanel({
      repo: true,
      files: [],
      repos: [
        { root: `${WS}/src/PkgA`, name: 'src/PkgA', branch: 'trunk', files: [] },
        { root: `${WS}/src/PkgB`, name: 'src/PkgB', branch: 'trunk', refused: true, files: [] },
      ],
    } as StatusPayload)

    await waitFor(() => expect(screen.getByText('src/PkgB')).toBeInTheDocument())
    expect(screen.getByText('skipped')).toBeInTheDocument()
  })

  it('shows a dash, not a zero, for a group whose rows the budget dropped', async () => {
    // A count of 0 asserts the repo is clean — about the one repo the shared row
    // budget stopped us from reading.
    renderPanel({
      repo: true,
      truncated: true,
      files: [{ path: 'a.txt', status: 'M', staged: false, repoRoot: `${WS}/src/PkgA` }],
      repos: [
        {
          root: `${WS}/src/PkgA`, name: 'src/PkgA', branch: 'trunk',
          files: [{ path: 'a.txt', status: 'M', staged: false, repoRoot: `${WS}/src/PkgA` }],
        },
        { root: `${WS}/src/PkgB`, name: 'src/PkgB', branch: 'trunk', truncated: true, files: [] },
      ],
    } as StatusPayload)

    await waitFor(() => expect(screen.getByText('src/PkgB')).toBeInTheDocument())
    const starved = screen.getByText('src/PkgB').closest('div')!
    expect(starved.textContent).toContain('\u2014')
    expect(starved.textContent).not.toMatch(/\b0\b/)
  })

  it('still collapses when every repo is genuinely clean', async () => {
    renderPanel({
      repo: true,
      files: [],
      repos: [{ root: `${WS}/src/PkgA`, name: 'src/PkgA', branch: 'trunk', files: [] }],
    } as StatusPayload)

    await waitFor(() => expect(screen.queryByText('skipped')).not.toBeInTheDocument())
    expect(screen.queryByText('src/PkgA')).not.toBeInTheDocument()
  })
})
