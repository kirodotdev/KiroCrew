import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { i18next, initI18n } from '../i18n/all'

const H = vi.hoisted(() => ({
  api: {
    projectGitRepos: vi.fn(),
    projectGitStatus: vi.fn(),
    projectGitLog: vi.fn(),
  },
}))

vi.mock('../api/client', () => ({ api: H.api }))

import GitReposPanel from '../components/GitReposPanel'

const PROJECT = '/workspace/project'
const OTHER = '/home/me/src/other-repo'

function mount() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <GitReposPanel slotKey="chat-1" onClose={vi.fn()} />
    </QueryClientProvider>,
  )
}

beforeEach(async () => {
  await initI18n()
  await i18next.changeLanguage('en')
  H.api.projectGitRepos.mockReset()
  H.api.projectGitStatus.mockReset().mockImplementation((path: string) =>
    Promise.resolve({ repo: true, repoRoot: path, branch: 'main', files: [] }),
  )
  H.api.projectGitLog.mockReset().mockResolvedValue({ repo: true, commits: [] })
})

describe('GitReposPanel', () => {
  it('lists every repository by path, the project first and tagged', async () => {
    H.api.projectGitRepos.mockResolvedValue({
      repos: [
        { path: PROJECT, source: 'project' },
        { path: OTHER, source: 'agent' },
      ],
    })
    mount()
    const sections = await screen.findAllByTestId('git-repo-section')
    expect(sections.map(s => s.getAttribute('data-path'))).toEqual([PROJECT, OTHER])
    expect(within(sections[0]).getByText('Project')).toBeInTheDocument()
    expect(within(sections[1]).queryByText('Project')).toBeNull()
    expect(within(sections[1]).getByText('other-repo')).toBeInTheDocument()
    expect(H.api.projectGitRepos).toHaveBeenCalledWith('chat-1')
    await waitFor(() => expect(H.api.projectGitStatus).toHaveBeenCalledWith(OTHER))
  })

  it('opens the first section and a section with changes; others stay collapsed until toggled', async () => {
    const third = '/home/me/src/dirty'
    H.api.projectGitRepos.mockResolvedValue({
      repos: [
        { path: PROJECT, source: 'project' },
        { path: OTHER, source: 'agent' },
        { path: third, source: 'agent' },
      ],
    })
    H.api.projectGitStatus.mockImplementation((path: string) =>
      Promise.resolve({
        repo: true,
        repoRoot: path,
        branch: 'main',
        files: path === third ? [{ path: 'a.txt', status: 'M', staged: false }] : [],
      }),
    )
    mount()
    const toggles = await screen.findAllByTestId('git-repo-toggle')
    await waitFor(() => expect(toggles[2]).toHaveAttribute('aria-expanded', 'true'))
    expect(toggles[0]).toHaveAttribute('aria-expanded', 'true')
    expect(toggles[1]).toHaveAttribute('aria-expanded', 'false')
    // A collapsed section fetches no history.
    expect(H.api.projectGitLog).not.toHaveBeenCalledWith(OTHER)
    await userEvent.click(toggles[1])
    expect(toggles[1]).toHaveAttribute('aria-expanded', 'true')
    await waitFor(() => expect(H.api.projectGitLog).toHaveBeenCalledWith(OTHER))
  })

  it('says no repositories were found instead of asking for a project folder', async () => {
    H.api.projectGitRepos.mockResolvedValue({ repos: [] })
    mount()
    const empty = await screen.findByText('No Git repositories found for this chat yet.')
    expect(empty).toHaveAttribute('role', 'status')
    // The copy claims only what the gateway found: after a restart a repository
    // the chat only ran commands in is not rebuilt, so "nothing touched yet"
    // would be false.
    expect(empty).not.toHaveTextContent(/edits files|runs commands/)
    expect(screen.queryByTestId('git-repos-loading')).toBeNull()
    expect(H.api.projectGitStatus).not.toHaveBeenCalled()
  })

  it('shows an accessible loading state until the first listing arrives, never the empty state', async () => {
    let resolve!: (value: { repos: { path: string; source: 'project' | 'agent' }[] }) => void
    H.api.projectGitRepos.mockReturnValue(new Promise(r => { resolve = r }))
    mount()
    const loading = screen.getByTestId('git-repos-loading')
    expect(loading).toHaveAttribute('role', 'status')
    expect(loading).toHaveAttribute('aria-busy', 'true')
    expect(loading).toHaveTextContent("Loading this chat's repositories...")
    expect(screen.queryByText(/No Git repositories/)).toBeNull()
    resolve({ repos: [{ path: OTHER, source: 'agent' }] })
    await screen.findByTestId('git-repo-section')
    expect(screen.queryByTestId('git-repos-loading')).toBeNull()
  })

  it('explains the Project tag with a tooltip', async () => {
    H.api.projectGitRepos.mockResolvedValue({
      repos: [
        { path: PROJECT, source: 'project' },
        { path: OTHER, source: 'agent' },
      ],
    })
    mount()
    const sections = await screen.findAllByTestId('git-repo-section')
    const badge = within(sections[0]).getByTestId('git-repo-badge')
    expect(badge).toHaveTextContent('Project')
    expect(badge).toHaveAttribute('title', "This chat's project folder")
    expect(within(sections[1]).queryByTestId('git-repo-badge')).toBeNull()
  })

  it('opens a later section whose status failed, so its ErrorNotice is shown', async () => {
    H.api.projectGitRepos.mockResolvedValue({
      repos: [
        { path: PROJECT, source: 'project' },
        { path: OTHER, source: 'agent' },
      ],
    })
    H.api.projectGitStatus.mockImplementation((path: string) =>
      path === OTHER
        ? Promise.reject(new Error('git status failed'))
        : Promise.resolve({ repo: true, repoRoot: path, branch: 'main', files: [] }),
    )
    mount()
    const sections = await screen.findAllByTestId('git-repo-section')
    await waitFor(() =>
      expect(within(sections[1]).getByTestId('git-repo-toggle')).toHaveAttribute('aria-expanded', 'true'),
    )
    expect(within(sections[1]).getByTestId('git-panel-status-error')).toBeInTheDocument()
  })

  it.each([
    [1, "1 older repository isn't shown."],
    [3, "3 older repositories aren't shown."],
  ])('reports %i hidden older repositories as information below the list', async (omitted, text) => {
    H.api.projectGitRepos.mockResolvedValue({
      repos: [{ path: OTHER, source: 'agent' }],
      omitted,
    })
    mount()
    const notice = await screen.findByTestId('git-repos-omitted')
    expect(notice).toHaveTextContent(text)
    expect(notice).toHaveAttribute('role', 'status')
    const panel = screen.getByTestId('git-repos-panel')
    expect(panel.lastElementChild).toBe(notice)
    expect(screen.queryByTestId('git-repos-error')).toBeNull()
  })

  it('omits the capacity notice at zero', async () => {
    H.api.projectGitRepos.mockResolvedValue({
      repos: [{ path: OTHER, source: 'agent' }],
      omitted: 0,
    })
    mount()
    await screen.findByTestId('git-repo-section')
    expect(screen.queryByTestId('git-repos-omitted')).toBeNull()
  })

  it('reports a failed listing through ErrorNotice', async () => {
    H.api.projectGitRepos.mockRejectedValue(new Error(''))
    mount()
    expect(await screen.findByTestId('git-repos-error')).toHaveTextContent(
      "Couldn't list this chat's repositories.",
    )
  })
})
