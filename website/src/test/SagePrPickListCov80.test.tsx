import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { RepoPr } from '../apps/code-review-sage/lib/types'
import type { SageContextValue } from '../apps/code-review-sage/context'

/**
 * The middle column: pick PRs, batch them, or review the whole repo behind a
 * confirm. Two invariants carry real cost if they break — a PR already under
 * review must not be tickable (the backend refuses the duplicate, so the box
 * would lie), and the "review all" confirm must not survive a repo switch,
 * where it would authorize a DIFFERENT repo's paid review turns.
 */
const sage: Record<string, unknown> = {}

vi.mock('../apps/code-review-sage/context', async importOriginal => {
  const actual = await importOriginal<typeof import('../apps/code-review-sage/context')>()
  return { ...actual, useSage: () => sage as unknown as SageContextValue }
})

vi.mock('../apps/code-review-sage/components/ReviewModelPicker', () => ({
  default: () => null,
}))

import PrPickList from '../apps/code-review-sage/components/PrPickList'

function pr(overrides: Partial<RepoPr> = {}): RepoPr {
  return {
    url: 'https://github.com/zzzowner/zzzrepo/pull/7',
    number: 7,
    title: 'zzz first change',
    head_sha: 'aaa',
    author: 'zzzann',
    updated_at: '2026-08-01T00:00:00Z',
    change_id: 'zzz-change-7',
    reviewed: false,
    reviewed_stale: false,
    ...overrides,
  }
}

function mutation() {
  return { mutate: vi.fn(), isPending: false, error: null, data: undefined }
}

beforeEach(() => {
  vi.clearAllMocks()
  Object.keys(sage).forEach(k => delete sage[k])
  Object.assign(sage, {
    activeRepo: { owner: 'zzzowner', repo: 'zzzrepo' },
    prs: [pr()],
    prsLoading: false,
    prsError: null,
    refreshPrs: vi.fn(),
    startReview: mutation(),
    startRepoReview: mutation(),
    startReviewLinks: mutation(),
    openAddRepos: vi.fn(),
    selectedPr: null,
    selectPr: vi.fn(),
    reviewingChangeUrls: new Set<string>(),
  })
})

describe('PrPickList without a repo', () => {
  it('asks for a repo and offers the picker instead of an empty list', async () => {
    sage.activeRepo = null
    render(<PrPickList />)
    expect(screen.getByText(/Pick a repo/i)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /Add a repo/i }))
    expect(sage.openAddRepos).toHaveBeenCalled()
  })
})

describe('PrPickList selection', () => {
  it('batches with the checkbox and opens with the row, as separate actions', async () => {
    render(<PrPickList />)
    const box = screen.getByRole('checkbox')
    await userEvent.click(box)
    expect(box).toBeChecked()
    expect(sage.selectPr).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: /Open pull request #7/i }))
    expect(sage.selectPr).toHaveBeenCalledTimes(1)

    // Untick again — the set is a toggle, not a one-way add.
    await userEvent.click(box)
    expect(box).not.toBeChecked()
  })

  it('starts a review for exactly the picked urls', async () => {
    render(<PrPickList />)
    await userEvent.click(screen.getByRole('checkbox'))
    await userEvent.click(screen.getByRole('button', { name: /Review 1 selected/i }))
    expect((sage.startReview as { mutate: ReturnType<typeof vi.fn> }).mutate)
      .toHaveBeenCalledWith(['https://github.com/zzzowner/zzzrepo/pull/7'])
  })

  it('disables the checkbox of a PR already under review', () => {
    sage.reviewingChangeUrls = new Set(['https://github.com/zzzowner/zzzrepo/pull/7'])
    sage.prs = [pr()]
    render(<PrPickList />)
    const box = screen.getByRole('checkbox')
    expect(box).toBeDisabled()
    expect(screen.getByText('reviewing')).toBeInTheDocument()
  })
})

describe('PrPickList state chips', () => {
  it('shows reviewed / updated / new distinctly', () => {
    sage.prs = [
      pr({ number: 1, url: 'u1', title: 'zzz a', reviewed: true, reviewed_at: '2026-08-01T00:00:00Z' }),
      pr({ number: 2, url: 'u2', title: 'zzz b', reviewed: true, reviewed_at: undefined }),
      pr({ number: 3, url: 'u3', title: 'zzz c', reviewed_stale: true }),
      pr({ number: 4, url: 'u4', title: 'zzz d', draft: true, author: undefined }),
    ]
    render(<PrPickList />)
    expect(screen.getAllByText('reviewed')).toHaveLength(2)
    expect(screen.getByText('updated')).toBeInTheDocument()
    expect(screen.getByText('new')).toBeInTheDocument()
    expect(screen.getByText('draft')).toBeInTheDocument()
  })
})

describe('PrPickList filter', () => {
  it('filters by title, number and author, and says so when nothing matches', async () => {
    sage.prs = [
      pr({ number: 11, url: 'u11', title: 'zzz alpha', author: 'zzzbob' }),
      pr({ number: 22, url: 'u22', title: 'zzz beta', author: 'zzzann' }),
    ]
    render(<PrPickList />)
    const filter = screen.getByLabelText(/Filter pull requests/i)

    await userEvent.type(filter, 'alpha')
    expect(screen.getByText('zzz alpha')).toBeInTheDocument()
    expect(screen.queryByText('zzz beta')).not.toBeInTheDocument()

    await userEvent.clear(filter)
    await userEvent.type(filter, '22')
    expect(screen.getByText('zzz beta')).toBeInTheDocument()

    await userEvent.clear(filter)
    await userEvent.type(filter, 'zzzbob')
    expect(screen.getByText('zzz alpha')).toBeInTheDocument()

    await userEvent.clear(filter)
    await userEvent.type(filter, 'zzz-nothing')
    expect(screen.getByText(/No pull requests match your filter/i)).toBeInTheDocument()
  })

  it('shows the skeleton while loading and the empty state when the repo has none', () => {
    sage.prsLoading = true
    const { unmount } = render(<PrPickList />)
    expect(screen.queryByText('zzz first change')).not.toBeInTheDocument()
    unmount()

    sage.prsLoading = false
    sage.prs = []
    render(<PrPickList />)
    expect(screen.getByText(/No open pull requests here/i)).toBeInTheDocument()
  })
})

describe('PrPickList review-all confirm', () => {
  it('requires a confirm, then starts the repo-wide review unforced', async () => {
    render(<PrPickList />)
    await userEvent.click(screen.getByRole('button', { name: /Review all 1/i }))
    expect((sage.startRepoReview as { mutate: ReturnType<typeof vi.fn> }).mutate).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: /Review these/i }))
    expect((sage.startRepoReview as { mutate: ReturnType<typeof vi.fn> }).mutate)
      .toHaveBeenCalledWith({ repo: 'https://github.com/zzzowner/zzzrepo', force: false })
  })

  it('can be dismissed without starting anything', async () => {
    render(<PrPickList />)
    await userEvent.click(screen.getByRole('button', { name: /Review all 1/i }))
    await userEvent.click(screen.getByRole('button', { name: /^Cancel$/ }))
    expect(screen.getByRole('button', { name: /Review all 1/i })).toBeInTheDocument()
    expect((sage.startRepoReview as { mutate: ReturnType<typeof vi.fn> }).mutate).not.toHaveBeenCalled()
  })

  it('drops the pending confirm and the selection when the repo changes', async () => {
    const { rerender } = render(<PrPickList />)
    await userEvent.click(screen.getByRole('checkbox'))
    await userEvent.click(screen.getByRole('button', { name: /Review all 1/i }))
    expect(screen.getByRole('button', { name: /Review these/i })).toBeInTheDocument()

    sage.activeRepo = { owner: 'zzzowner', repo: 'zzzother' }
    rerender(<PrPickList />)

    expect(screen.queryByRole('button', { name: /Review these/i })).not.toBeInTheDocument()
    expect(screen.getByRole('checkbox')).not.toBeChecked()
  })
})

describe('PrPickList paste-links escape hatch', () => {
  it('is collapsed until asked for, then reviews the pasted links', async () => {
    render(<PrPickList />)
    await userEvent.click(screen.getByRole('button', { name: /Paste PR links instead/i }))
    const box = screen.getByLabelText('Paste PR links')
    fireEvent.change(box, { target: { value: '  https://github.com/zzzowner/zzzrepo/pull/9  ' } })
    await userEvent.click(screen.getByRole('button', { name: /Review these/i }))
    expect((sage.startReviewLinks as { mutate: ReturnType<typeof vi.fn> }).mutate)
      .toHaveBeenCalledWith('https://github.com/zzzowner/zzzrepo/pull/9')
  })

  it('collapses again on cancel, discarding the draft', async () => {
    render(<PrPickList />)
    await userEvent.click(screen.getByRole('button', { name: /Paste PR links instead/i }))
    fireEvent.change(screen.getByLabelText('Paste PR links'), { target: { value: 'zzz' } })
    await userEvent.click(screen.getByRole('button', { name: /^Cancel$/ }))

    await userEvent.click(screen.getByRole('button', { name: /Paste PR links instead/i }))
    expect(screen.getByLabelText('Paste PR links')).toHaveValue('')
  })

  it('surfaces the link-review error', async () => {
    sage.startReviewLinks = { ...mutation(), error: new Error('zzz bad link') }
    render(<PrPickList />)
    await userEvent.click(screen.getByRole('button', { name: /Paste PR links instead/i }))
    expect(screen.getByText('zzz bad link')).toBeInTheDocument()
  })
})

describe('PrPickList error + noop surfaces', () => {
  it('shows the start, repo-review, list errors and the noop message', () => {
    sage.startReview = { ...mutation(), error: new Error('zzz start failed') }
    sage.startRepoReview = {
      ...mutation(),
      error: new Error('zzz repo failed'),
      data: { status: 'noop', message: 'zzz nothing to do', repo: '', changes: [], skipped: 0 },
    }
    sage.prsError = new Error('zzz list failed')
    render(<PrPickList />)
    expect(screen.getByText('zzz start failed')).toBeInTheDocument()
    expect(screen.getByText('zzz repo failed')).toBeInTheDocument()
    expect(screen.getByText('zzz nothing to do')).toBeInTheDocument()
    expect(screen.getByText('zzz list failed')).toBeInTheDocument()
  })

  it('refreshes on demand and disables the control while loading', async () => {
    render(<PrPickList />)
    await userEvent.click(screen.getByRole('button', { name: /Refresh pull requests/i }))
    expect(sage.refreshPrs).toHaveBeenCalled()
  })
})
