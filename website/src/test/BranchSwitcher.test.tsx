import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { i18next, initI18n } from '../i18n/all'
import type { GitBranchList, GitBranchRow } from '../api/client/files'

const H = vi.hoisted(() => ({
  api: {
    projectGitBranches: vi.fn(),
    projectGitSwitch: vi.fn(),
  },
}))

vi.mock('../api/client', () => ({ api: H.api }))

import BranchSwitcher, { isValidNewBranchName } from '../components/BranchSwitcher'

const PROJECT = '/workspace/project'

function row(name: string, extra: Partial<GitBranchRow> = {}): GitBranchRow {
  return {
    name,
    sha: 'abc1234',
    date: '2026-10-01T00:00:00Z',
    author: 'Ada',
    subject: `work on ${name}`,
    switchable: true,
    ...extra,
  }
}

const LIST: GitBranchList = {
  repo: true,
  current: 'main',
  local: [row('main', { current: true }), row('feature/login', { ahead: 2 }), row('secret', { switchable: false })],
  remote: [row('origin/review-fix')],
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  const invalidate = vi.spyOn(qc, 'invalidateQueries')
  render(
    <QueryClientProvider client={qc}>
      <BranchSwitcher projectDir={PROJECT} branch="main" />
    </QueryClientProvider>,
  )
  return { invalidate }
}

async function openPicker() {
  fireEvent.click(screen.getByTestId('branch-switcher-trigger'))
  await screen.findByText('feature/login')
}

function apiError(code: string, extra: Record<string, unknown> = {}) {
  return Object.assign(new Error(code), { body: JSON.stringify({ error: 'x', code, ...extra }) })
}

beforeEach(async () => {
  await initI18n()
  await i18next.changeLanguage('en')
  H.api.projectGitBranches.mockReset().mockResolvedValue(LIST)
  H.api.projectGitSwitch.mockReset().mockResolvedValue({ ok: true, branch: 'feature/login', previous: 'main' })
})

describe('BranchSwitcher', () => {
  it('narrows to the viewport when 360px does not fit beside the gutters', async () => {
    const original = window.innerWidth
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 320 })
    try {
      mount()
      await openPicker()
      const pop = screen.getByTestId('branch-switcher')
      expect(pop.style.width).toBe('304px')
      expect(Number.parseFloat(pop.style.left) + 304).toBeLessThanOrEqual(320 - 8)
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: original })
    }
  })

  it('reads the branch list only once opened', async () => {
    mount()
    expect(screen.getByTestId('branch-switcher-trigger')).toHaveTextContent('main')
    expect(H.api.projectGitBranches).not.toHaveBeenCalled()
    await openPicker()
    expect(H.api.projectGitBranches).toHaveBeenCalledWith(PROJECT)
    expect(screen.getByText('Local')).toBeInTheDocument()
    expect(screen.getByText('Remote')).toBeInTheDocument()
  })

  it('switches to a local branch and refreshes every working-tree view', async () => {
    const { invalidate } = mount()
    await openPicker()
    fireEvent.mouseDown(screen.getByText('feature/login'))
    await waitFor(() => expect(screen.queryByTestId('branch-switcher')).toBeNull())
    expect(H.api.projectGitSwitch).toHaveBeenCalledWith({ path: PROJECT, branch: 'feature/login' })
    const keys = invalidate.mock.calls.map(c => JSON.stringify((c[0] as { queryKey: unknown }).queryKey))
    for (const k of [['git-status', PROJECT], ['git-log', PROJECT], ['git-branches', PROJECT], ['project-tree', PROJECT], ['project-git']]) {
      expect(keys).toContain(JSON.stringify(k))
    }
  })

  it('checks out a remote branch as a tracking branch with its short name', async () => {
    mount()
    await openPicker()
    fireEvent.mouseDown(screen.getByText('origin/review-fix'))
    await waitFor(() => expect(H.api.projectGitSwitch).toHaveBeenCalled())
    expect(H.api.projectGitSwitch).toHaveBeenCalledWith({ path: PROJECT, branch: 'review-fix', track: 'origin/review-fix' })
  })

  it('offers to create a typed name that does not exist, and Enter picks the only match', async () => {
    mount()
    await openPicker()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'feat/new-thing' } })
    expect(screen.getByTestId('branch-create')).toHaveTextContent('Create branch “feat/new-thing”')
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    await waitFor(() => expect(H.api.projectGitSwitch).toHaveBeenCalledWith({ path: PROJECT, branch: 'feat/new-thing', create: true }))
  })

  it('does not offer creation for an invalid name', async () => {
    mount()
    await openPicker()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'bad..name' } })
    expect(screen.queryByTestId('branch-create')).toBeNull()
    expect(screen.getByText('Not a valid branch name')).toBeInTheDocument()
  })

  it('never switches to the current branch or a redacted one', async () => {
    mount()
    await openPicker()
    fireEvent.mouseDown(screen.getByText('secret'))
    fireEvent.mouseDown(screen.getAllByText('main').find(el => el.closest('[role="option"]'))!)
    expect(H.api.projectGitSwitch).not.toHaveBeenCalled()
  })

  it('shows the localized reason when uncommitted changes block the switch and stays open', async () => {
    H.api.projectGitSwitch.mockRejectedValue(apiError('git_switch_dirty'))
    mount()
    await openPicker()
    fireEvent.mouseDown(screen.getByText('feature/login'))
    expect(await screen.findByTestId('branch-switcher-error')).toHaveTextContent(
      'Your uncommitted changes would be overwritten. Commit or stash them, then switch.',
    )
    expect(screen.getByTestId('branch-switcher')).toBeInTheDocument()
  })

  it('falls back to git’s own line for an uncoded failure', async () => {
    H.api.projectGitSwitch.mockRejectedValue(apiError('git_switch_failed', { detail: 'fatal: something odd' }))
    mount()
    await openPicker()
    fireEvent.mouseDown(screen.getByText('feature/login'))
    const notice = await screen.findByTestId('branch-switcher-error')
    expect(notice).toHaveTextContent("Couldn't switch branches")
    expect(notice).toHaveTextContent('fatal: something odd')
  })

  it('explains a filter-driver block and disables every row', async () => {
    H.api.projectGitBranches.mockResolvedValue({ ...LIST, switchBlocked: 'filter' })
    mount()
    await openPicker()
    expect(screen.getByTestId('branch-switcher-blocked')).toHaveTextContent(/declares a content filter/)
    fireEvent.mouseDown(screen.getByText('feature/login'))
    expect(H.api.projectGitSwitch).not.toHaveBeenCalled()
  })

  it('closes on Escape', async () => {
    mount()
    await openPicker()
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('branch-switcher')).toBeNull())
  })
})

describe('isValidNewBranchName', () => {
  it.each(['main', 'feat/x', 'release-1.2', 'a_b'])('accepts %s', name => {
    expect(isValidNewBranchName(name)).toBe(true)
  })
  it.each(['', '-x', 'a..b', 'x.lock', 'HEAD', 'feat/', 'a b', 'feat/CON', 'a~1'])('rejects %s', name => {
    expect(isValidNewBranchName(name)).toBe(false)
  })
})

describe('BranchSwitcher keyboard start', () => {
  it('highlights the first switchable row, not the checked-out branch', async () => {
    mount()
    await openPicker()
    const selected = screen.getAllByRole('option').find(el => el.getAttribute('aria-selected') === 'true')
    expect(selected).toHaveTextContent('feature/login')
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    await waitFor(() => expect(H.api.projectGitSwitch).toHaveBeenCalledWith({ path: PROJECT, branch: 'feature/login' }))
  })
})
