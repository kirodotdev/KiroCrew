import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
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
      <GitReposPanel slotKey="chat-1" />
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

  it('says no repositories yet and how the list fills, instead of asking for a project folder', async () => {
    H.api.projectGitRepos.mockResolvedValue({ repos: [], omitted: 0, limit: 24 })
    mount()
    const empty = await screen.findByText(
      'No Git repositories yet. They appear here when the agent edits files or runs commands in one.',
    )
    expect(empty).toHaveAttribute('role', 'status')
    // Prospective only: after a restart a repository the chat only ran commands
    // in is not rebuilt, so the copy says how a row APPEARS, never what the
    // chat did or did not do.
    expect(empty).not.toHaveTextContent(/hasn't|has not|didn't|did not/)
    expect(screen.queryByTestId('git-repos-caption')).toBeNull()
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
    // The tag alone says "Project"; the tooltip has to say what that means AND
    // what the untagged rows are, or it only repeats the one word it explains.
    expect(badge).toHaveAttribute(
      'title',
      'The folder this chat was started in. Other repositories are ones the agent worked in.',
    )
    expect(within(sections[1]).queryByTestId('git-repo-badge')).toBeNull()
  })

  it('captions the list so the Project tag is explained without hovering', async () => {
    H.api.projectGitRepos.mockResolvedValue({
      repos: [
        { path: PROJECT, source: 'project' },
        { path: OTHER, source: 'agent' },
      ],
      omitted: 0,
      limit: 24,
    })
    mount()
    await screen.findAllByTestId('git-repo-section')
    const caption = screen.getByTestId('git-repos-caption')
    expect(caption).toHaveTextContent(
      "This chat's project folder (Project) and the repositories the agent worked in.",
    )
    // Above the rows, not among them.
    const panel = screen.getByTestId('git-repos-panel')
    expect(panel.firstElementChild).toBe(caption)
  })

  it('captions a list with no Project row as the repositories the agent worked in', async () => {
    H.api.projectGitRepos.mockResolvedValue({
      repos: [{ path: OTHER, source: 'agent' }],
      omitted: 0,
      limit: 24,
    })
    mount()
    await screen.findByTestId('git-repo-section')
    expect(screen.getByTestId('git-repos-caption')).toHaveTextContent(
      'Repositories the agent worked in during this chat.',
    )
    expect(screen.getByTestId('git-repos-caption')).not.toHaveTextContent('Project')
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
    [1, "Showing the 24 most recently used repositories. 1 more isn't listed."],
    [3, "Showing the 24 most recently used repositories. 3 more aren't listed."],
  ])('states the cap as a rule and %i unlisted repositories as information below the list', async (omitted, text) => {
    H.api.projectGitRepos.mockResolvedValue({
      repos: [{ path: OTHER, source: 'agent' }],
      omitted,
      limit: 24,
    })
    mount()
    const notice = await screen.findByTestId('git-repos-omitted')
    expect(notice).toHaveTextContent(text)
    // "Older" collides with the sidebar's Older Sessions and reads as something
    // to expand; the server keeps nothing more, so the line names no action.
    expect(notice).not.toHaveTextContent(/older|show (more|all)|\bshow\b/i)
    expect(within(notice).queryByRole('button')).toBeNull()
    expect(notice).toHaveAttribute('role', 'status')
    const panel = screen.getByTestId('git-repos-panel')
    expect(panel.lastElementChild).toBe(notice)
    expect(screen.queryByTestId('git-repos-error')).toBeNull()
  })

  it('states the cap the server sent, not a literal', async () => {
    H.api.projectGitRepos.mockResolvedValue({
      repos: [{ path: OTHER, source: 'agent' }],
      omitted: 2,
      limit: 8,
    })
    mount()
    expect(await screen.findByTestId('git-repos-omitted')).toHaveTextContent(
      "Showing the 8 most recently used repositories. 2 more aren't listed.",
    )
  })

  it('omits the capacity notice at zero', async () => {
    H.api.projectGitRepos.mockResolvedValue({
      repos: [{ path: OTHER, source: 'agent' }],
      omitted: 0,
      limit: 24,
    })
    mount()
    await screen.findByTestId('git-repo-section')
    expect(screen.queryByTestId('git-repos-omitted')).toBeNull()
  })

  it('reports a failed listing through ErrorNotice with a retry beside the hand-off', async () => {
    H.api.projectGitRepos.mockRejectedValue(new Error(''))
    mount()
    const notice = await screen.findByTestId('git-repos-error')
    expect(notice).toHaveTextContent("Couldn't list this chat's repositories.")
    expect(within(notice).getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(within(notice).getByRole('button', { name: /agent/i })).toBeInTheDocument()
    expect(screen.queryByTestId('git-repos-caption')).toBeNull()
  })

  it('retry refetches the listing and clears the error once it succeeds', async () => {
    H.api.projectGitRepos.mockRejectedValue(new Error('listing failed'))
    mount()
    const notice = await screen.findByTestId('git-repos-error')
    const calls = H.api.projectGitRepos.mock.calls.length
    H.api.projectGitRepos.mockResolvedValue({
      repos: [{ path: OTHER, source: 'agent' }],
      omitted: 0,
      limit: 24,
    })
    await userEvent.click(within(notice).getByTestId('git-repos-retry'))
    await waitFor(() => expect(H.api.projectGitRepos.mock.calls.length).toBeGreaterThan(calls))
    await screen.findByTestId('git-repo-section')
    expect(screen.queryByTestId('git-repos-error')).toBeNull()
    expect(screen.getByTestId('git-repos-caption')).toBeInTheDocument()
  })

  it('shows the localized failure, never the server sentence, and hands the agent the report', async () => {
    // The route's error text is English in every language the dashboard
    // renders, so it is never the visible message. It stays the journal lookup
    // key: the hand-off needs the structured report (endpoint, status) that a
    // localized message cannot match.
    const serverMessage = 'server-only repository listing detail'
    recordError({
      source: 'api',
      message: serverMessage,
      status: 500,
      endpoint: '/api/project/git/repos',
    })
    H.api.projectGitRepos.mockRejectedValue(new Error(serverMessage))
    await i18next.changeLanguage('de')
    mount()
    const notice = await screen.findByTestId('git-repos-error')
    expect(notice).toHaveTextContent(i18next.t('components.gitPanel.repos_failed'))
    expect(notice).not.toHaveTextContent(serverMessage)
    expect(notice).not.toHaveTextContent("Couldn't list")
    await userEvent.click(within(notice).getByRole('button', { name: /agent/i }))
    const prompt = consumeChatHandoff()
    expect(prompt).toContain('- Request: /api/project/git/repos -> HTTP 500')
    expect(prompt).toContain(`- Message: ${serverMessage}`)
    await i18next.changeLanguage('en')
  })
})
