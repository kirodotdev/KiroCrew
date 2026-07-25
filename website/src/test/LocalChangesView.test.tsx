import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { PullRequestLink } from '../utils/pullRequestLinks'
import { LOCAL_CHANGES_SOURCE_URL } from '../utils/pullRequestLinks'

vi.mock('../api/client', () => ({
  api: {
    gitChanges: vi.fn(),
    fileDiff: vi.fn(),
    // PullRequestPanel queries these when a PR source is selected; keep them
    // pending so the strip tests never depend on provider data.
    pullRequestSource: vi.fn().mockImplementation(() => new Promise(() => {})),
    pullRequestChecks: vi.fn().mockImplementation(() => new Promise(() => {})),
    pullRequestStatuses: vi.fn().mockImplementation(() => new Promise(() => {})),
  },
}))

import { api } from '../api/client'
import LocalChangesView, { __resetLocalChangesUi } from '../components/LocalChangesView'
import PullRequestPanel from '../components/PullRequestPanel'

const mockGitChanges = vi.mocked(api.gitChanges)
const mockFileDiff = vi.mocked(api.fileDiff)

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

const PR: PullRequestLink = {
  provider: 'github', repo: 'KiroCrew', number: 450,
  url: 'https://github.com/kirodotdev/KiroCrew/pull/450',
}

describe('PullRequestPanel Local tab', () => {
  beforeEach(() => {
    mockGitChanges.mockReset()
    mockFileDiff.mockReset()
    vi.mocked(api.pullRequestSource).mockClear()
    mockGitChanges.mockResolvedValue({ dir: '/proj', repos: [] })
  })

  it('is ever-present in the source strip and selected by default with no sources', async () => {
    wrap(
      <PullRequestPanel sources={[]} selectedUrl="" onSelect={() => {}} onAddToChat={() => {}} projectDir="/proj" />,
    )
    const local = screen.getByRole('tab', { name: 'Local' })
    expect(local).toHaveAttribute('aria-selected', 'true')
    await waitFor(() => {
      expect(screen.getByText(/No git repository found/)).toBeInTheDocument()
    })
  })

  it('sits alongside PR source tabs and routes selection through onSelect', () => {
    const onSelect = vi.fn()
    wrap(
      <PullRequestPanel sources={[PR]} selectedUrl={PR.url} onSelect={onSelect} onAddToChat={() => {}} projectDir="/proj" />,
    )
    const local = screen.getByRole('tab', { name: 'Local' })
    const pr = screen.getByRole('tab', { name: /PR #450/ })
    expect(local).toHaveAttribute('aria-selected', 'false')
    expect(pr).toHaveAttribute('aria-selected', 'true')
    fireEvent.click(local)
    expect(onSelect).toHaveBeenCalledWith(LOCAL_CHANGES_SOURCE_URL)
  })

  it('renders the local view (not provider data) when the sentinel is selected', () => {
    wrap(
      <PullRequestPanel sources={[PR]} selectedUrl={LOCAL_CHANGES_SOURCE_URL} onSelect={() => {}} onAddToChat={() => {}} projectDir="/proj" />,
    )
    expect(screen.getByRole('tab', { name: 'Local' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: /PR #450/ })).toHaveAttribute('aria-selected', 'false')
    // Provider payload must not be requested for the local tab.
    expect(vi.mocked(api.pullRequestSource)).not.toHaveBeenCalled()
    expect(screen.queryByText(/Loading source provider/)).not.toBeInTheDocument()
  })
})

describe('LocalChangesView', () => {
  beforeEach(() => {
    mockGitChanges.mockReset()
    mockFileDiff.mockReset()
    __resetLocalChangesUi()
  })

  it('shows a hint when no project dir is set', () => {
    wrap(<LocalChangesView />)
    expect(screen.getByText(/Pick a project directory for this chat to see its uncommitted git changes/)).toBeInTheDocument()
    expect(mockGitChanges).not.toHaveBeenCalled()
  })

  it('shows the clean state when repos exist but have no changes', async () => {
    mockGitChanges.mockResolvedValue({
      dir: '/proj',
      repos: [{ root: '/proj', name: 'proj', branch: 'main', files: [] }],
    })
    wrap(<LocalChangesView projectDir="/proj" />)
    await waitFor(() => {
      expect(screen.getByText(/Working tree clean/)).toBeInTheDocument()
    })
  })

  it('lists changed files grouped by repo and expands to a diff', async () => {
    mockGitChanges.mockResolvedValue({
      dir: '/proj',
      repos: [{
        root: '/proj', name: 'proj', branch: 'feature-x',
        files: [{ path: '/proj/a.ts', rel: 'a.ts', status: 'modified', staged: false, additions: 12, deletions: 3 }],
      }],
    })
    mockFileDiff.mockResolvedValue({
      diff: '--- a/a.ts\n+++ b/a.ts\n@@ -1,1 +1,1 @@\n-alpha_before\n+beta_after\n',
      original: 'alpha_before\n',
      status: 'modified',
    })
    wrap(<LocalChangesView projectDir="/proj" />)
    await waitFor(() => { expect(screen.getByText('a.ts')).toBeInTheDocument() })
    expect(screen.getByText('feature-x')).toBeInTheDocument()
    // Status renders as a compact letter badge; the full word stays on title/aria.
    const badge = screen.getByLabelText('modified')
    expect(badge).toHaveTextContent('M')
    // +/- line counts from the repo-wide numstat.
    expect(screen.getByText('+12')).toBeInTheDocument()
    expect(screen.getByText('-3')).toBeInTheDocument()
    // Diff is lazy — fetched only on expand.
    expect(mockFileDiff).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('a.ts'))
    await waitFor(() => { expect(screen.getByText('beta_after')).toBeInTheDocument() })
    expect(mockFileDiff).toHaveBeenCalledWith('/proj/a.ts')
    expect(screen.getByText('alpha_before')).toBeInTheDocument()
  })

  it('renders the bare file name followed by a de-emphasized directory path', async () => {
    mockGitChanges.mockResolvedValue({
      dir: '/proj',
      repos: [{
        root: '/proj', name: 'proj', branch: 'main',
        files: [{ path: '/proj/src/components/Deep.tsx', rel: 'src/components/Deep.tsx', status: 'modified', staged: false }],
      }],
    })
    wrap(<LocalChangesView projectDir="/proj" />)
    // Name and dir are SEPARATE spans: name first (emphasized), then the muted
    // dir path, which truncates first in narrow panels.
    const nameEl = await screen.findByText('Deep.tsx')
    const dirEl = screen.getByText('src/components')
    expect(nameEl).not.toBe(dirEl)
    expect(dirEl.className).toContain('text-muted')
    expect(nameEl.className).toContain('text-text')
    // Name precedes the dir in the row.
    expect(nameEl.compareDocumentPosition(dirEl) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('collapses a repo section when its band is clicked', async () => {
    mockGitChanges.mockResolvedValue({
      dir: '/proj',
      repos: [{
        root: '/proj', name: 'proj', branch: 'main',
        files: [{ path: '/proj/a.ts', rel: 'a.ts', status: 'modified', staged: false }],
      }],
    })
    const { unmount } = wrap(<LocalChangesView projectDir="/proj" />)
    await screen.findByText('a.ts')
    fireEvent.click(screen.getByText('proj'))
    expect(screen.queryByText('a.ts')).not.toBeInTheDocument()
    // Collapse state survives an unmount/remount (SidePanel unmounts inactive
    // views on tab switches) via the module-level store.
    unmount()
    wrap(<LocalChangesView projectDir="/proj" />)
    await screen.findByText('proj')
    expect(screen.queryByText('a.ts')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('proj'))
    expect(screen.getByText('a.ts')).toBeInTheDocument()
  })

  it('opens the file as a native tab on name click; anywhere else expands inline', async () => {
    mockGitChanges.mockResolvedValue({
      dir: '/proj',
      repos: [{
        root: '/proj', name: 'proj', branch: 'main',
        files: [{ path: '/proj/src/Deep.tsx', rel: 'src/Deep.tsx', status: 'modified', staged: false }],
      }],
    })
    mockFileDiff.mockResolvedValue({ diff: '', original: '', status: 'modified' })
    const onFileOpen = vi.fn()
    wrap(<LocalChangesView projectDir="/proj" onFileOpen={onFileOpen} />)
    // The open action is an explicit trailing icon button; the filename itself
    // is plain text (row click = inline diff toggle).
    const openBtn = await screen.findByRole('button', { name: 'Open src/Deep.tsx in editor' })
    fireEvent.click(openBtn)
    expect(onFileOpen).toHaveBeenCalledWith('/proj/src/Deep.tsx')
    // Name click must NOT toggle the inline diff (stopPropagation). Both the
    // repo band and the file row carry aria-expanded — pick the FILE row.
    const row = screen.getAllByRole('button')
      .find(b => b.hasAttribute('aria-expanded') && b.textContent?.includes('Deep.tsx'))!
    expect(row).toHaveAttribute('aria-expanded', 'false')
    // Clicking the dir path (part of the row, not the name button) expands.
    fireEvent.click(screen.getByText('src'))
    await waitFor(() => { expect(row).toHaveAttribute('aria-expanded', 'true') })
  })
})
