import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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

import GitReposPanel, { MAX_POLLING_SECTIONS } from '../components/GitReposPanel'

const PROJECT = '/workspace/project'
const OTHER = '/home/me/src/other-repo'

/** Waits on a CHAINED load: the repository-list query renders a section per
 *  repository, each section mounts a GitPanel that starts its own status query,
 *  and the status answer decides the badge, whether the section opens, and (once
 *  open) the log query. Every wait on state from the status or log hop is bounded
 *  by this rather than RTL's 1000 ms default, which a loaded coverage shard can
 *  exceed. */
const REPO_STATUS_CHAIN_TIMEOUT_MS = 5000
const REPO_STATUS_CHAIN = { timeout: REPO_STATUS_CHAIN_TIMEOUT_MS }
/** `n` agent-touched repository rows, as the repos route lists them. */
const agentRows = (n: number) =>
  Array.from({ length: n }, (_, i) => ({ path: `/home/me/src/repo-${i}`, source: 'agent' as const }))

function mount() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  const view = render(
    <QueryClientProvider client={queryClient}>
      <GitReposPanel slotKey="chat-1" />
    </QueryClientProvider>,
  )
  return { ...view, queryClient }
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
    await waitFor(() => expect(H.api.projectGitStatus).toHaveBeenCalledWith(OTHER), REPO_STATUS_CHAIN)
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
    await waitFor(() => expect(toggles[2]).toHaveAttribute('aria-expanded', 'true'), REPO_STATUS_CHAIN)
    expect(toggles[0]).toHaveAttribute('aria-expanded', 'true')
    expect(toggles[1]).toHaveAttribute('aria-expanded', 'false')
    // A collapsed section fetches no history.
    expect(H.api.projectGitLog).not.toHaveBeenCalledWith(OTHER)
    await userEvent.click(toggles[1])
    expect(toggles[1]).toHaveAttribute('aria-expanded', 'true')
    await waitFor(() => expect(H.api.projectGitLog).toHaveBeenCalledWith(OTHER), REPO_STATUS_CHAIN)
  })

  it('says there is nothing to show and how the list fills, instead of asking for a project folder', async () => {
    H.api.projectGitRepos.mockResolvedValue({ repos: [], omitted: 0, limit: 24 })
    mount()
    const empty = await screen.findByText(
      'No Git repositories to show. One appears here when the agent edits files or runs commands in it.',
    )
    expect(empty).toHaveAttribute('role', 'status')
    // Prospective only: the copy says how a row APPEARS, never what the chat
    // did or did not do -- and not "yet", which would claim there never was one.
    expect(empty).not.toHaveTextContent(/hasn't|has not|didn't|did not|\byet\b/)
    expect(screen.queryByTestId('git-repos-caption')).toBeNull()
    expect(screen.queryByTestId('git-repos-loading')).toBeNull()
    expect(H.api.projectGitStatus).not.toHaveBeenCalled()
  })

  it('draws the expand hint on the first collapsed section only, keeping it in every collapsed name', async () => {
    const paths = [PROJECT, OTHER, '/home/me/src/third', '/home/me/src/fourth']
    H.api.projectGitRepos.mockResolvedValue({
      repos: paths.map((path, i) => ({ path, source: i === 0 ? 'project' : 'agent' })),
    })
    mount()
    const toggles = await screen.findAllByTestId('git-repo-toggle')
    await waitFor(() => expect(toggles[0]).toHaveAttribute('aria-expanded', 'true'), REPO_STATUS_CHAIN)
    const visibleHints = () =>
      screen.getAllByTestId('git-repo-expand-hint').filter(h => !h.className.includes('sr-only'))
    // Three collapsed rows, one drawn hint, on the first of them; the open row
    // has none.
    await waitFor(() => expect(visibleHints()).toHaveLength(1), REPO_STATUS_CHAIN)
    expect(toggles[1]).toContainElement(visibleHints()[0])
    expect(within(toggles[0]).queryByTestId('git-repo-expand-hint')).toBeNull()
    for (const toggle of toggles.slice(2)) {
      const hint = within(toggle).getByTestId('git-repo-expand-hint')
      expect(hint.className).toMatch(/\bsr-only\b/)
      // Still in the accessible name: a screen reader hears what each row opens.
      expect(toggle).toHaveAccessibleName(/Show changes and history/)
    }
    expect(toggles[1]).toHaveAccessibleName(/Show changes and history/)

    // Opening the row that carries the hint moves it to the next collapsed row.
    await userEvent.click(toggles[1])
    expect(toggles[1]).toHaveAttribute('aria-expanded', 'true')
    await waitFor(() => expect(toggles[2]).toContainElement(visibleHints()[0]), REPO_STATUS_CHAIN)
    expect(visibleHints()).toHaveLength(1)
    expect(within(toggles[3]).getByTestId('git-repo-expand-hint').className).toMatch(/\bsr-only\b/)
  })

  it('keeps a later collapsed row\'s hint in the accessible name with no hover reveal, the first row\'s hint plainly visible', async () => {
    const paths = [PROJECT, OTHER, '/home/me/src/third']
    H.api.projectGitRepos.mockResolvedValue({
      repos: paths.map((path, i) => ({ path, source: i === 0 ? 'project' : 'agent' })),
    })
    mount()
    const toggles = await screen.findAllByTestId('git-repo-toggle')
    await waitFor(() => expect(toggles[0]).toHaveAttribute('aria-expanded', 'true'), REPO_STATUS_CHAIN)
    await waitFor(() => expect(toggles[1]).toHaveAttribute('aria-expanded', 'false'), REPO_STATUS_CHAIN)
    // The first collapsed row (toggles[1]) draws its hint plainly: visible at
    // rest, with no hover/focus gating and no sr-only.
    const firstHint = within(toggles[1]).getByTestId('git-repo-expand-hint')
    expect(firstHint.className).not.toMatch(/\bsr-only\b/)
    expect(firstHint.className).not.toContain('group-hover:not-sr-only')
    expect(firstHint.className).not.toContain('group-focus-visible:not-sr-only')
    // A later collapsed row keeps the hint in its accessible name (sr-only) and
    // does NOT reveal it on hover or focus -- a reveal that flipped sr-only off
    // pushed every row below it down. The toggle is not a Tailwind `group`.
    const laterToggle = toggles[2]
    expect(laterToggle).not.toHaveClass('group')
    const laterHint = within(laterToggle).getByTestId('git-repo-expand-hint')
    expect(laterHint.className).toMatch(/\bsr-only\b/)
    expect(laterHint.className).not.toContain('group-hover:not-sr-only')
    expect(laterHint.className).not.toContain('group-focus-visible:not-sr-only')
    expect(laterToggle).toHaveAccessibleName(/Show changes and history/)
    // Every collapsed toggle carries the hint in its native title too.
    expect(toggles[1]).toHaveAttribute('title', 'Show changes and history')
    expect(laterToggle).toHaveAttribute('title', 'Show changes and history')
  })

  it('mutes the clean badge so only a row with changes carries colour', async () => {
    const dirty = '/home/me/src/dirty'
    H.api.projectGitRepos.mockResolvedValue({
      repos: [
        { path: PROJECT, source: 'project' },
        { path: dirty, source: 'agent' },
      ],
    })
    H.api.projectGitStatus.mockImplementation((path: string) =>
      Promise.resolve({
        repo: true,
        repoRoot: path,
        branch: 'main',
        files: path === dirty ? [{ path: 'a.txt', status: 'M', staged: false }] : [],
      }),
    )
    mount()
    const clean = await screen.findByTestId('git-repo-clean-badge', {}, REPO_STATUS_CHAIN)
    expect(clean).toHaveTextContent('clean')
    expect(clean.className).toMatch(/\btext-muted\b/)
    expect(clean.className).not.toMatch(/\b(text-ok|bg-ok\/15)\b/)
    const changes = await screen.findByTestId('git-repo-changes-badge', {}, REPO_STATUS_CHAIN)
    expect(changes).toHaveTextContent('1 uncommitted')
    expect(changes.className).toMatch(/\btext-warn\b/)
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
      'The repository this chat was started in. The others are repositories the agent worked in.',
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
    // One caption sentence always, plus the project-first sentence because a
    // Project row is present.
    expect(caption).toHaveTextContent(
      "Repositories the agent edited files or ran commands in during this chat. The project's repository is listed first.",
    )
    expect(caption).toHaveTextContent("The project's repository is listed first.")
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
    const caption = screen.getByTestId('git-repos-caption')
    expect(caption).toHaveTextContent(
      'Repositories the agent edited files or ran commands in during this chat.',
    )
    expect(caption).not.toHaveTextContent('Project')
    // The project-first sentence is appended only when a Project row exists.
    expect(caption).not.toHaveTextContent("The project's repository is listed first.")
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
    await waitFor(
      () => expect(within(sections[1]).getByTestId('git-repo-toggle')).toHaveAttribute('aria-expanded', 'true'),
      REPO_STATUS_CHAIN,
    )
    expect(within(sections[1]).getByTestId('git-panel-status-error')).toBeInTheDocument()
  })

  it.each([
    [1, "The agent worked in 25 repositories; the 24 most recent are listed. The hidden one reappears once the agent works in it again."],
    [3, "The agent worked in 27 repositories; the 24 most recent are listed. Each hidden one reappears once the agent works in it again."],
  ])('leads with the total worked in, states how many it lists, and how a hidden one returns (%i omitted)', async (omitted, text) => {
    H.api.projectGitRepos.mockResolvedValue({
      repos: agentRows(24),
      omitted,
      listed: 24,
      limit: 24,
    })
    mount()
    const notice = await screen.findByTestId('git-repos-omitted')
    expect(notice).toHaveTextContent(text)
    // The count alone left the reader looking for the hidden repositories, so
    // the line also says how one comes back: the server re-lists a repository
    // the moment the agent works in it again (record_repo_roots), and nothing
    // the reader can click does the same.
    expect(notice).toHaveTextContent(/reappears once the agent works in it again/)
    // "Older" collides with the sidebar's Older Sessions and reads as something
    // to expand; the server keeps nothing more, so the line names no action.
    expect(notice).not.toHaveTextContent(/older|show (more|all)|\bshow\b/i)
    expect(within(notice).queryByRole('button')).toBeNull()
    expect(notice).toHaveAttribute('role', 'status')
    const panel = screen.getByTestId('git-repos-panel')
    expect(panel.lastElementChild).toBe(notice)
    expect(screen.queryByTestId('git-repos-error')).toBeNull()
  })

  it('states the listed count the server sent, not a literal', async () => {
    H.api.projectGitRepos.mockResolvedValue({
      repos: agentRows(8),
      omitted: 2,
      listed: 8,
      limit: 8,
    })
    mount()
    expect(await screen.findByTestId('git-repos-omitted')).toHaveTextContent(
      "The agent worked in 10 repositories; the 8 most recent are listed. Each hidden one reappears once the agent works in it again.",
    )
  })

  it('does not count an untouched project row among the repositories the agent worked in', async () => {
    // The project's own repository is listed first even when the agent never
    // worked in it, so the rows are one more than the agent's listed count:
    // reading the row count would say 28 and promise a 25th "most recent".
    H.api.projectGitRepos.mockResolvedValue({
      repos: [{ path: PROJECT, source: 'project' }, ...agentRows(24)],
      omitted: 3,
      listed: 24,
      limit: 24,
    })
    mount()
    const notice = await screen.findByTestId('git-repos-omitted')
    expect(notice).toHaveTextContent(
      "The agent worked in 27 repositories; the 24 most recent are listed. Each hidden one reappears once the agent works in it again.",
    )
    expect(screen.getAllByTestId('git-repo-section')).toHaveLength(25)
  })

  it('names the repositories actually listed when fewer than the cap are drawn', async () => {
    // A held repository whose .git was deleted still fills a slot of the cap
    // but is never listed, so the notice must name the 22 shown, not the cap.
    H.api.projectGitRepos.mockResolvedValue({
      repos: agentRows(22),
      omitted: 3,
      listed: 22,
      limit: 24,
    })
    mount()
    expect(await screen.findByTestId('git-repos-omitted')).toHaveTextContent(
      "The agent worked in 25 repositories; the 22 most recent are listed. Each hidden one reappears once the agent works in it again.",
    )
  })

  it.each([false, true])('shows the localized incomplete-list notice only when true (%s)', async incomplete => {
    await i18next.changeLanguage('de')
    H.api.projectGitRepos.mockResolvedValue({
      repos: [{ path: OTHER, source: 'agent' }],
      omitted: 0,
      limit: 24,
      incomplete,
    })
    mount()
    await screen.findByTestId('git-repo-section')
    if (incomplete) {
      const notice = screen.getByTestId('git-repos-incomplete')
      expect(notice).toHaveTextContent(i18next.t('components.gitPanel.repos_incomplete'))
      expect(notice).not.toHaveTextContent('Some repositories may be missing')
      expect(notice).toHaveAttribute('role', 'status')
      expect(notice).toHaveClass('text-muted')
      expect(screen.getAllByTestId('git-repos-incomplete')).toHaveLength(1)
      expect(within(notice).queryByRole('button')).toBeNull()
      expect(screen.queryByTestId('git-repos-error')).toBeNull()
    } else {
      expect(screen.queryByTestId('git-repos-incomplete')).toBeNull()
    }
  })

  it('shows incomplete tracking even when no repository could be listed', async () => {
    H.api.projectGitRepos.mockResolvedValue({ repos: [], omitted: 0, limit: 24, incomplete: true })
    mount()
    const notice = await screen.findByTestId('git-repos-incomplete')
    // The new key names the outcome in the reader's words and how a dropped
    // repository returns, not the mechanism that dropped it.
    expect(notice).toHaveTextContent(
      'Some repositories the agent worked in may not be listed. Each one appears here once the agent works in it again.',
    )
    expect(notice).toHaveTextContent(i18next.t('components.gitPanel.repos_incomplete'))
    // The old mechanism wording is gone.
    expect(notice).not.toHaveTextContent('tool call')
    expect(notice).not.toHaveTextContent('Some repositories may be missing')
    expect(screen.queryByTestId('git-repo-section')).toBeNull()
    expect(screen.queryByTestId('git-repos-omitted')).toBeNull()
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

  it('keeps the list and every section mounted when a background refetch fails', async () => {
    // React Query retains the last good listing when a refetch rejects and sets
    // `error` beside it. The panel used to render only the notice then, which
    // unmounted every GitPanel: open state lost, every status refetched, and a
    // gateway restart mid-session wiped the tab the user was looking at.
    H.api.projectGitRepos.mockResolvedValue({
      repos: [
        { path: PROJECT, source: 'project' },
        { path: OTHER, source: 'agent' },
      ],
      omitted: 0,
      limit: 24,
    })
    const { queryClient } = mount()
    const toggles = await screen.findAllByTestId('git-repo-toggle')
    await waitFor(() => expect(toggles[0]).toHaveAttribute('aria-expanded', 'true'), REPO_STATUS_CHAIN)
    // The user opened the second section: state the error must not reset.
    await userEvent.click(toggles[1])
    expect(toggles[1]).toHaveAttribute('aria-expanded', 'true')
    const statusCalls = H.api.projectGitStatus.mock.calls.length

    H.api.projectGitRepos.mockRejectedValue(new Error('gateway restarting'))
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: ['git-repos', 'chat-1'] })
    })

    const notice = await screen.findByTestId('git-repos-error')
    expect(within(notice).getByTestId('git-repos-retry')).toBeInTheDocument()
    // The list is still shown below, so the notice names the REFRESH and warns
    // the shown list may be stale -- it must not say "couldn't list" while the
    // repositories are visible right below it.
    expect(notice).toHaveTextContent(
      "Couldn't refresh this chat's repositories. The list below may be out of date.",
    )
    expect(notice).not.toHaveTextContent("Couldn't list this chat's repositories.")
    // Same DOM nodes, same open state: nothing was unmounted.
    expect(toggles[0]).toBeInTheDocument()
    expect(toggles[1]).toBeInTheDocument()
    expect(toggles[1]).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getAllByTestId('git-repo-section').map(s => s.getAttribute('data-path'))).toEqual([PROJECT, OTHER])
    expect(screen.getByTestId('git-repos-caption')).toBeInTheDocument()
    // No section was remounted, so no status was re-asked for.
    expect(H.api.projectGitStatus.mock.calls.length).toBe(statusCalls)
    // The notice sits above the list, not in place of it.
    expect(screen.getByTestId('git-repos-panel').firstElementChild).toContainElement(notice)

    // A recovered refetch clears the notice and leaves the list alone.
    H.api.projectGitRepos.mockResolvedValue({
      repos: [
        { path: PROJECT, source: 'project' },
        { path: OTHER, source: 'agent' },
      ],
      omitted: 0,
      limit: 24,
    })
    await userEvent.click(within(notice).getByTestId('git-repos-retry'))
    await waitFor(() => expect(screen.queryByTestId('git-repos-error')).toBeNull())
    expect(toggles[1]).toBeInTheDocument()
    expect(toggles[1]).toHaveAttribute('aria-expanded', 'true')
  })

  it('names a background-refresh failure as a refresh, not a failed listing, while the list stays shown', async () => {
    H.api.projectGitRepos.mockResolvedValue({
      repos: [
        { path: PROJECT, source: 'project' },
        { path: OTHER, source: 'agent' },
      ],
      omitted: 0,
      limit: 24,
    })
    const { queryClient } = mount()
    await screen.findAllByTestId('git-repo-section')

    H.api.projectGitRepos.mockRejectedValue(new Error('gateway restarting'))
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: ['git-repos', 'chat-1'] })
    })

    const notice = await screen.findByTestId('git-repos-error')
    // Refresh copy, not the first-load "couldn't list" sentence: the repos are
    // still listed right below the notice.
    expect(notice).toHaveTextContent(
      "Couldn't refresh this chat's repositories. The list below may be out of date.",
    )
    expect(notice).not.toHaveTextContent("Couldn't list this chat's repositories.")
    // The list is still there.
    expect(screen.getAllByTestId('git-repo-section').map(s => s.getAttribute('data-path'))).toEqual([PROJECT, OTHER])
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

describe('GitReposPanel status polling cap', () => {
  /** Two status poll intervals plus slack, so a timer that fires would have fired. */
  const POLL_WINDOW_MS = 5000 * 2 + 500
  const dirtyPaths = Array.from({ length: MAX_POLLING_SECTIONS + 2 }, (_, i) => `/home/me/src/dirty-${i}`)
  const statusCalls = (path: string) =>
    H.api.projectGitStatus.mock.calls.filter(call => call[0] === path).length
  const callsByPath = () => Object.fromEntries(dirtyPaths.map(path => [path, statusCalls(path)]))

  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    H.api.projectGitRepos.mockResolvedValue({
      repos: dirtyPaths.map(path => ({ path, source: 'agent' })),
    })
    // Every repository has changes, so every section opens by itself.
    H.api.projectGitStatus.mockImplementation((path: string) =>
      Promise.resolve({
        repo: true,
        repoRoot: path,
        branch: 'main',
        files: [{ path: 'a.txt', status: 'M', staged: false }],
      }),
    )
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('polls only the most recently opened sections, and the one the user opens joins them', async () => {
    const { unmount } = mount()
    const toggles = await screen.findAllByTestId('git-repo-toggle')
    // Repository list -> each section's status query -> it opens itself.
    for (const toggle of toggles) {
      await waitFor(() => expect(toggle).toHaveAttribute('aria-expanded', 'true'), REPO_STATUS_CHAIN)
    }
    const before = callsByPath()
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_WINDOW_MS) })
    const after = callsByPath()
    const polled = dirtyPaths.filter(path => after[path] > before[path])
    // Every dirty section is open, but only the cap's worth keep polling.
    expect(polled).toHaveLength(MAX_POLLING_SECTIONS)

    // Reopening a section that was not polling makes it the newest open one.
    const quiet = dirtyPaths.find(path => !polled.includes(path))!
    const quietToggle = toggles[dirtyPaths.indexOf(quiet)]
    fireEvent.click(quietToggle)
    expect(quietToggle).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(quietToggle)
    expect(quietToggle).toHaveAttribute('aria-expanded', 'true')
    const reopened = callsByPath()
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_WINDOW_MS) })
    const later = callsByPath()
    expect(later[quiet]).toBeGreaterThan(reopened[quiet])
    expect(dirtyPaths.filter(path => later[path] > reopened[path])).toHaveLength(MAX_POLLING_SECTIONS)
    unmount()
  })
})
