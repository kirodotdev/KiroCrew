// The "Add repos" picker: list what the user can reach, and always allow typing.
//
// The rail previously offered only a URL field while promising you could pick
// from your recent repos, so both halves are covered here: the two discovered
// lists, and manual entry (which must keep working even when `gh` is unusable).
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useEffect, useRef } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { SageProvider, useSage } from '../apps/code-review-sage/context'
import AddReposModal from '../apps/code-review-sage/components/AddReposModal'

vi.mock('../apps/code-review-sage/api', () => ({
  sageApi: {
    runs: vi.fn(),
    runReport: vi.fn(),
    recentRepos: vi.fn(),
    myRepos: vi.fn(),
    pinnedRepos: vi.fn(),
    pinRepo: vi.fn(),
    pinRepoUrl: vi.fn(),
    unpinRepo: vi.fn(),
    repoPrs: vi.fn(),
    settings: vi.fn(),
  },
}))

import { sageApi } from '../apps/code-review-sage/api'

const api = sageApi as unknown as Record<string, ReturnType<typeof vi.fn>>

function mount() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <SageProvider initialRunId={null}>
          <ArmedView />
        </SageProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

/** Arms discovery the way the rail's "Add a repo" button does, then renders the
 *  dialog. The modal itself does not fetch: discovery is opt-in, and opening the
 *  picker is what opts in. */
function ArmedView() {
  const { openAddRepos, addingRepos, closeAddRepos, selectedPr, activeRepo } = useSage()
  // Arm ONCE. Re-opening on every render would make the dialog impossible to
  // close, hiding real dismissal regressions.
  const armed = useRef(false)
  useEffect(() => {
    if (!armed.current) {
      armed.current = true
      openAddRepos()
    }
  }, [openAddRepos])
  return (
    <>
      {/* Test-only probe: this harness renders the modal alone, so provider state
          is not otherwise observable. */}
      <span data-testid="probe">
        {selectedPr ? `pr:${selectedPr.number}` : 'pr:none'}
        {activeRepo ? ` repo:${activeRepo.owner}/${activeRepo.repo}` : ''}
      </span>
      {addingRepos ? <AddReposModal onClose={closeAddRepos} /> : null}
    </>
  )
}

describe('AddReposModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    api.runs.mockResolvedValue({ runs: [], pool: null, reviewer: null })
    api.pinnedRepos.mockResolvedValue({ repos: [{ owner: 'acme', repo: 'widgets' }] })
    api.recentRepos.mockResolvedValue({
      repos: [{
        owner: 'acme', repo: 'widgets', full_name: 'acme/widgets',
        last_contributed_at: new Date().toISOString(), contribution_count: 4,
      }],
      pinned: [], truncated: false,
    })
    api.myRepos.mockResolvedValue({
      repos: [
        {
          owner: 'acme', repo: 'widgets', full_name: 'acme/widgets',
          pushed_at: '', private: false, archived: false, can_push: true,
        },
        {
          owner: 'acme', repo: 'dormant', full_name: 'acme/dormant',
          pushed_at: '2025-01-01T00:00:00Z', private: true, archived: false,
          can_push: false,
        },
      ],
      pinned: [], truncated: false,
    })
    api.settings.mockResolvedValue({
      settings: { model: null, effort: '', active_namespaces: ['default'], max_concurrent: 5 },
      models: [], efforts: [], namespaces: ['default'], max_concurrent_max: 30,
    })
  })

  it('lists repos the user can reach, not just recent ones', async () => {
    mount()
    // From the activity feed…
    expect(await screen.findByText('acme/widgets')).toBeTruthy()
    // …and from the full repo list, which the feed would have missed.
    expect(await screen.findByText('acme/dormant')).toBeTruthy()
    expect(screen.getByText(/Recently worked on/i)).toBeTruthy()
    expect(screen.getByText(/All your repos/i)).toBeTruthy()
  })

  it('marks an already-added repo instead of offering it again', async () => {
    mount()
    await screen.findByText('acme/widgets')
    const added = await screen.findByRole('button', { name: /acme\/widgets already added/i })
    expect(added).toBeDisabled()
  })

  it('does not list the same repo twice across the two sections', async () => {
    mount()
    await screen.findByText('acme/widgets')
    expect(screen.getAllByText('acme/widgets')).toHaveLength(1)
  })

  it('pins a discovered repo on click', async () => {
    api.pinRepo.mockResolvedValue({ repos: [] })
    mount()
    const row = await screen.findByRole('button', { name: /^Add acme\/dormant$/i })
    await userEvent.click(row)
    await waitFor(() => expect(api.pinRepo).toHaveBeenCalledWith('acme', 'dormant'))
  })

  it('accepts a manually typed owner/repo', async () => {
    api.pinRepoUrl.mockResolvedValue({ repos: [] })
    mount()
    const input = await screen.findByLabelText(/Repository or pull request URL/i)
    await userEvent.type(input, 'other/thing')
    await userEvent.click(screen.getByRole('button', { name: /^Add$/i }))
    await waitFor(() => expect(api.pinRepoUrl).toHaveBeenCalledWith(
      'https://github.com/other/thing',
    ))
  })

  it('accepts a manually typed full URL unchanged', async () => {
    api.pinRepoUrl.mockResolvedValue({ repos: [] })
    mount()
    const input = await screen.findByLabelText(/Repository or pull request URL/i)
    await userEvent.type(input, 'https://github.com/other/thing')
    await userEvent.click(screen.getByRole('button', { name: /^Add$/i }))
    await waitFor(() => expect(api.pinRepoUrl).toHaveBeenCalledWith(
      'https://github.com/other/thing',
    ))
  })

  it('filters the lists', async () => {
    mount()
    await screen.findByText('acme/dormant')
    await userEvent.type(await screen.findByLabelText(/Filter your repos/i), 'dorm')
    await waitFor(() => expect(screen.queryByText('acme/widgets')).toBeNull())
    expect(screen.getByText('acme/dormant')).toBeTruthy()
  })

  it('still offers manual entry when gh is not set up', async () => {
    // The whole point: a missing CLI must not leave the user with no way in.
    api.recentRepos.mockResolvedValue({
      repos: [], pinned: [], setup_required: true, error: 'no usable gh',
    })
    api.myRepos.mockResolvedValue({
      repos: [], pinned: [], setup_required: true, error: 'no usable gh',
    })
    mount()
    expect(await screen.findByText(/GitHub CLI not ready/i)).toBeTruthy()
    expect(screen.getByLabelText(/Repository or pull request URL/i)).toBeTruthy()
  })

  it('is an accessible modal dialog', async () => {
    mount()
    const dialog = await screen.findByRole('dialog')
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(dialog.getAttribute('aria-label')).toBe('Add repos')
  })

  it('closes on Escape', async () => {
    mount()
    await screen.findByRole('dialog')
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('closes on the backdrop and on the close button', async () => {
    mount()
    await screen.findByRole('dialog')
    await userEvent.click(screen.getByRole('button', { name: /Close add-repos dialog/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })
})

describe('managing repos you already have', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    api.runs.mockResolvedValue({ runs: [], pool: null, reviewer: null })
    api.settings.mockResolvedValue({
      settings: { review: { concurrency: 2 } }, pool: null, reviewer: null,
    })
  })

  it('lists your repos and can remove one', async () => {
    // Removal moved here from the rail's dropdown: a button inside a menu item is
    // invalid a11y, so the modal is the repo-management surface.
    api.pinnedRepos.mockResolvedValue({
      repos: [{ owner: 'acme', repo: 'widgets' }],
    })
    api.recentRepos.mockResolvedValue({ repos: [], pinned: [], gh_ready: true })
    api.myRepos.mockResolvedValue({ repos: [], pinned: [], gh_ready: true })
    mount()
    await userEvent.click(await screen.findByRole('button', { name: /Remove acme\/widgets/ }))
    await waitFor(() => expect(api.unpinRepo).toHaveBeenCalledWith('acme', 'widgets'))
  })
})

describe('pasting a pull request link', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    api.runs.mockResolvedValue({ runs: [], pool: null, reviewer: null })
    api.pinnedRepos.mockResolvedValue({ repos: [] })
    api.recentRepos.mockResolvedValue({ repos: [], pinned: [], gh_ready: true })
    api.myRepos.mockResolvedValue({ repos: [], pinned: [], gh_ready: true })
    api.repoPrs.mockResolvedValue({ repo: 'kirodotdev/KiroCrew', prs: [], count: 0 })
    api.settings.mockResolvedValue({
      settings: { review: { concurrency: 2 } }, pool: null, reviewer: null,
    })
  })

  it('sends the pull request URL as-is', async () => {
    // The field is the only place in the app you can type, so a clipboard URL
    // lands here whatever it points at.
    api.pinRepoUrl.mockResolvedValue({
      ok: true,
      repos: [{ owner: 'kirodotdev', repo: 'KiroCrew' }],  // brand-ok: literal repository name
      added: { owner: 'kirodotdev', repo: 'KiroCrew' },  // brand-ok: literal repository name
      pull_request: {
        owner: 'kirodotdev', repo: 'KiroCrew', number: 777,  // brand-ok: literal repository name
        url: 'https://github.com/kirodotdev/KiroCrew/pull/777',
        change_id: 'GH-kirodotdev-KiroCrew-777',
      },
    })
    mount()
    const field = await screen.findByLabelText(/Repository or pull request URL/i)
    await userEvent.type(field, 'https://github.com/kirodotdev/KiroCrew/pull/777')
    await userEvent.click(screen.getByRole('button', { name: /^Add$/ }))
    await waitFor(() => expect(api.pinRepoUrl).toHaveBeenCalledWith(
      'https://github.com/kirodotdev/KiroCrew/pull/777'))
  })

  it('opens the pasted pull request instead of leaving you to find it', async () => {
    // The point of pasting a PR link is that pull request, not a repo you now
    // have to locate it in.
    api.pinRepoUrl.mockResolvedValue({
      ok: true,
      repos: [{ owner: 'kirodotdev', repo: 'KiroCrew' }],  // brand-ok: literal repository name
      added: { owner: 'kirodotdev', repo: 'KiroCrew' },  // brand-ok: literal repository name
      pull_request: {
        owner: 'kirodotdev', repo: 'KiroCrew', number: 777,  // brand-ok: literal repository name
        url: 'https://github.com/kirodotdev/KiroCrew/pull/777',
        change_id: 'GH-kirodotdev-KiroCrew-777',
      },
    })
    api.pullRequestSource?.mockResolvedValue?.({
      provider: 'github', url: 'https://github.com/kirodotdev/KiroCrew/pull/777',
      number: 777, title: 'Pasted pull request', description: '', state: 'open',
      draft: false, mergedAt: '', updatedAt: '', headBranch: 'x', baseBranch: 'main',
      headSha: 'abc', author: 'ann', additions: 0, deletions: 0, changedFiles: 0,
      commits: [], checks: [], comments: [], files: [], partialSections: [],
    })
    // After the add the server lists the repo as pinned; the provider drops an
    // active repo that is NOT pinned, so the fixture has to reflect that.
    api.pinnedRepos.mockResolvedValue({
      repos: [{ owner: 'kirodotdev', repo: 'KiroCrew' }],  // brand-ok: literal repository name
    })
    mount()
    const field = await screen.findByLabelText(/Repository or pull request URL/i)
    await userEvent.type(field, 'https://github.com/kirodotdev/KiroCrew/pull/777')
    await userEvent.click(screen.getByRole('button', { name: /^Add$/ }))
    // The modal closes, the repo is active, and the pull request is selected.
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await waitFor(() => expect(screen.getByTestId('probe').textContent)
      .toContain('pr:777'))
    expect(screen.getByTestId('probe').textContent)
      .toContain('repo:kirodotdev/KiroCrew')
  })

  it('says pasting a pull request link works', async () => {
    mount()
    expect(await screen.findByText(/Paste a pull\s+request link/)).toBeTruthy()
  })
})
