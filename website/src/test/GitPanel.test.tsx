import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { i18next, initI18n } from '../i18n/all'
import {
  consumeChatHandoff,
  installSoftNavigate,
  recordError,
  __resetErrorJournalForTests,
  __resetNavSeamForTests,
} from '../utils/errorReport'

const H = vi.hoisted(() => ({
  api: {
    projectGitStatus: vi.fn(),
    projectGitLog: vi.fn(),
  },
}))

vi.mock('../api/client', () => ({ api: H.api }))

import GitPanel from '../components/GitPanel'

const PROJECT = '/workspace/project'

function mount() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <GitPanel projectDir={PROJECT} onClose={vi.fn()} />
    </QueryClientProvider>,
  )
}

beforeEach(async () => {
  await initI18n()
  await i18next.changeLanguage('en')
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  sessionStorage.clear()
  installSoftNavigate(() => {})
  H.api.projectGitStatus.mockReset().mockResolvedValue({
    repo: true,
    repoRoot: PROJECT,
    branch: 'main',
    files: [],
  })
  H.api.projectGitLog.mockReset().mockResolvedValue({ repo: true, commits: [] })
})

afterEach(async () => {
  await i18next.changeLanguage('en')
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  sessionStorage.clear()
})

describe('GitPanel repository state', () => {
  it('renders a terminal no-repository state without claiming the tree is clean', async () => {
    H.api.projectGitStatus.mockResolvedValue({ repo: false, files: [] })
    H.api.projectGitLog.mockResolvedValue({ repo: false, commits: [] })

    mount()

    expect(await screen.findByText('Not a Git repository')).toBeInTheDocument()
    expect(screen.getByText(
      'This project folder is not a Git repository. If the repository is in a subfolder, click the folder name below the message box.',
    )).toBeInTheDocument()
    expect(screen.queryByText('loading...')).toBeNull()
    expect(screen.queryByText('clean')).toBeNull()
    expect(screen.queryByText(/uncommitted/)).toBeNull()
    expect(screen.queryByText('No changes or commits to display.')).toBeNull()
  })

  it('renders an operational status failure through ErrorNotice without a health claim', async () => {
    H.api.projectGitStatus.mockRejectedValue(new Error('status unavailable'))

    mount()

    // GitPanel retries a failed status read once before React Query exposes
    // statusError, so wait for that bounded request chain rather than the
    // default one-second query timeout.
    expect(await screen.findByTestId('git-panel-status-error', {}, { timeout: 5000 })).toBeInTheDocument()
    expect(screen.queryByText('loading...')).toBeNull()
    expect(screen.queryByText('clean')).toBeNull()
    expect(screen.queryByText('Not a Git repository')).toBeNull()
    expect(screen.queryByText('No changes or commits to display.')).toBeNull()
  })

  it('does not render stale status-derived header data beside a failed refresh', async () => {
    const serverMessage = 'server-only repository status detail'
    const codedFailure = Object.assign(new Error(serverMessage), {
      body: JSON.stringify({ error: serverMessage, code: 'git_status_unavailable' }),
    })
    H.api.projectGitStatus
      .mockResolvedValueOnce({
        repo: true,
        repoRoot: PROJECT,
        branch: 'main',
        ahead: 2,
        behind: 1,
        files: [],
      })
      .mockRejectedValue(codedFailure)

    mount()

    expect(await screen.findByText('No changes or commits to display.')).toBeInTheDocument()
    expect(screen.getByText(/↑2\s*↓1/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    const notice = await screen.findByTestId('git-panel-status-error', {}, { timeout: 5000 })
    const localizedStatus = 'Couldn’t read the repository status. Commit history may be out of date.'
    expect(notice).toHaveTextContent(localizedStatus)
    expect(notice).not.toHaveTextContent(serverMessage)
    expect(screen.getAllByText(localizedStatus)).toHaveLength(1)
    expect(screen.getByText('Branch unavailable')).toHaveClass('text-muted')
    expect(screen.queryByText('main')).toBeNull()
    expect(screen.queryByText(/↑2\s*↓1/)).toBeNull()
    expect(screen.queryByText('clean')).toBeNull()
    expect(screen.queryByText('No changes or commits to display.')).toBeNull()
  })

  it('keeps the structured status report when the visible error is localized', async () => {
    const serverMessage = 'server-only repository status detail'
    const codedFailure = Object.assign(new Error(serverMessage), {
      body: JSON.stringify({ error: serverMessage, code: 'git_status_unavailable' }),
    })
    recordError({
      source: 'api',
      message: serverMessage,
      status: 503,
      code: 'git_status_unavailable',
      endpoint: '/api/project/git/status',
    })
    H.api.projectGitStatus.mockRejectedValue(codedFailure)
    await i18next.changeLanguage('zh-CN')

    mount()

    const notice = await screen.findByTestId('git-panel-status-error', {}, { timeout: 5000 })
    expect(notice).toHaveTextContent('无法读取仓库状态。提交历史可能已过时。')
    expect(notice).not.toHaveTextContent(serverMessage)
    await userEvent.click(within(notice).getByRole('button', { name: '询问代理' }))

    const prompt = consumeChatHandoff()
    expect(prompt).toContain('- Request: /api/project/git/status -> HTTP 503')
    expect(prompt).toContain('- Code: git_status_unavailable')
    expect(prompt).toContain(`- Message: ${serverMessage}`)
  })

  it('does not render stale no-repository data beside a failed refresh', async () => {
    H.api.projectGitStatus
      .mockResolvedValueOnce({ repo: false, files: [] })
      .mockRejectedValue(new Error('refresh unavailable'))
    H.api.projectGitLog.mockResolvedValue({ repo: false, commits: [] })

    mount()

    expect(await screen.findByText('Not a Git repository')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    expect(await screen.findByTestId('git-panel-status-error', {}, { timeout: 5000 })).toBeInTheDocument()
    expect(screen.queryByText('Not a Git repository')).toBeNull()
    expect(screen.queryByText(/This project folder is not a Git repository/)).toBeNull()
    expect(screen.queryByText('clean')).toBeNull()
  })

  it('does not render stale changed-file rows beside a failed refresh', async () => {
    H.api.projectGitStatus
      .mockResolvedValueOnce({
        repo: true,
        repoRoot: PROJECT,
        branch: 'feature',
        files: [{ path: 'src/a.ts', status: 'M', staged: false }],
      })
      .mockRejectedValue(new Error('refresh unavailable'))

    mount()

    expect(await screen.findByText('Changes')).toBeInTheDocument()
    expect(screen.getByTitle('src/a.ts')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    expect(await screen.findByTestId('git-panel-status-error', {}, { timeout: 5000 })).toBeInTheDocument()
    expect(screen.queryByText('Changes')).toBeNull()
    expect(screen.queryByTitle('src/a.ts')).toBeNull()
  })

  it('keeps the clean-repository label, badge, and empty state', async () => {
    mount()

    expect(await screen.findByText('main')).toBeInTheDocument()
    expect(screen.getByText('clean')).toBeInTheDocument()
    expect(screen.getByText('No changes or commits to display.')).toBeInTheDocument()
    expect(screen.queryByText('Not a Git repository')).toBeNull()
  })

  it('keeps the dirty-repository label, count, and changed-file row', async () => {
    H.api.projectGitStatus.mockResolvedValue({
      repo: true,
      repoRoot: PROJECT,
      branch: 'feature',
      files: [{ path: 'src/a.ts', status: 'M', staged: false }],
    })

    mount()

    expect(await screen.findByText('feature')).toBeInTheDocument()
    expect(screen.getByText('1 uncommitted')).toBeInTheDocument()
    expect(screen.getByText('Changes')).toBeInTheDocument()
    expect(screen.getByTitle('src/a.ts')).toBeInTheDocument()
    expect(screen.queryByText('No changes or commits to display.')).toBeNull()
    expect(screen.queryByText('Not a Git repository')).toBeNull()
  })
})
