import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { i18next, initI18n } from '../i18n/all'

const H = vi.hoisted(() => ({ api: { projectGitBranchDiff: vi.fn() } }))

vi.mock('../api/client', () => ({ api: H.api }))
// The patch renderer is the chat's own; this spec pins what the section hands it.
vi.mock('../components/DiffBlock', () => ({
  default: ({ code }: { code: string }) => <pre data-testid="branch-diff">{code}</pre>,
}))

import GitBranchChanges from '../components/GitBranchChanges'

const PROJECT = '/workspace/project'
const PATCH = 'diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1,2 @@\n one\n+two'

function mount(head = 'aaa1111') {
  // `retryDelay: 0`: the section sets `retry: 1`, so a rejected read is asked twice.
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  const ui = (h: string) => (
    <QueryClientProvider client={queryClient}>
      <GitBranchChanges projectDir={PROJECT} head={h} />
    </QueryClientProvider>
  )
  const view = render(ui(head))
  return { ...view, setHead: (h: string) => view.rerender(ui(h)) }
}

const open = () => userEvent.click(screen.getByTestId('git-branch-changes-toggle'))

beforeEach(async () => {
  await initI18n()
  await i18next.changeLanguage('en')
  H.api.projectGitBranchDiff.mockReset().mockResolvedValue({ repo: true, base: 'main', diff: PATCH, omitted: 0 })
})

describe('GitBranchChanges', () => {
  it('reads nothing until opened, then hands the branch patch to the diff renderer', async () => {
    mount()
    expect(screen.getByText('Branch changes')).toBeInTheDocument()
    expect(H.api.projectGitBranchDiff).not.toHaveBeenCalled()

    await open()

    expect((await screen.findByTestId('branch-diff')).textContent).toBe(PATCH)
    expect(H.api.projectGitBranchDiff).toHaveBeenCalledWith(PROJECT)
    expect(screen.getByText('since main')).toBeInTheDocument()
    expect(screen.queryByTestId('git-branch-changes-partial')).toBeNull()
  })

  it('reads the branch again once the log shows a new HEAD commit', async () => {
    const view = mount('aaa1111')
    await open()
    await screen.findByTestId('branch-diff')
    expect(H.api.projectGitBranchDiff).toHaveBeenCalledTimes(1)

    view.setHead('bbb2222')

    await waitFor(() => expect(H.api.projectGitBranchDiff).toHaveBeenCalledTimes(2))
  })

  it('says some files are not shown when the server left any out', async () => {
    H.api.projectGitBranchDiff.mockResolvedValue({ repo: true, base: 'origin/main', diff: PATCH, omitted: 3 })
    mount()
    await open()

    expect(await screen.findByTestId('git-branch-changes-partial')).toHaveTextContent('Some changed files are not shown.')
  })

  it('does not claim no changes when every changed file was left out', async () => {
    H.api.projectGitBranchDiff.mockResolvedValue({ repo: true, base: 'main', diff: '', omitted: 1 })
    mount()
    await open()

    expect(await screen.findByTestId('git-branch-changes-partial')).toBeInTheDocument()
    expect(screen.queryByText('No changes since main.')).toBeNull()
  })

  it('tells a repository with no base apart from a branch with no changes', async () => {
    H.api.projectGitBranchDiff.mockResolvedValue({ repo: true, base: null, diff: '', omitted: 0 })
    const first = mount()
    await open()
    expect(await screen.findByText('There is no base branch to compare with.')).toBeInTheDocument()
    first.unmount()

    H.api.projectGitBranchDiff.mockResolvedValue({ repo: true, base: 'main', diff: '', omitted: 0 })
    mount()
    await open()
    expect(await screen.findByText('No changes since main.')).toBeInTheDocument()
    expect(screen.queryByTestId('branch-diff')).toBeNull()
  })

  it('renders a refused repository with the panel refusal copy, not an outage', async () => {
    const message = 'Branch changes are off for this repository.'
    H.api.projectGitBranchDiff.mockRejectedValue(Object.assign(new Error(message), {
      body: JSON.stringify({ error: message, code: 'git_branch_diff_filter_refused', cause: 'declared' }),
    }))
    mount()
    await open()

    const notice = await screen.findByTestId('git-branch-changes-error')
    expect(notice).toHaveTextContent(i18next.t('components.gitPanel.filter_refused'))
    expect(notice).not.toHaveTextContent("Couldn't read this branch's changes.")
  })
})
