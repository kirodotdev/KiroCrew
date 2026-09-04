import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { IssueSource } from '../types'
import type { PullRequestLink } from '../utils/pullRequestLinks'

const mockApi = vi.hoisted(() => ({ fetchIssueSource: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div>{content}</div>,
}))

import IssuePanel, { issueHandoff, issueStateLabel, labelChipStyle } from '../components/IssuePanel'

const openIssue: IssueSource = {
  provider: 'github',
  url: 'https://github.com/acme/widgets/issues/9',
  number: 9,
  title: 'Crash on empty label list',
  description: '## Steps\nOpen the panel with no labels.',
  state: 'open',
  stateReason: '',
  author: 'octocat',
  createdAt: '2026-07-20T09:00:00Z',
  updatedAt: '2026-07-28T09:00:00Z',
  closedAt: '',
  closedBy: '',
  labels: [
    { name: 'bug', color: 'd73a4a', description: 'Something is broken' },
    { name: 'good first issue', color: 'ffffff', description: '' },
  ],
  assignees: ['hubot', 'monalisa'],
  milestone: { title: 'v2.1', state: 'open', dueOn: '2026-08-01' },
  commentCount: 2,
  locked: false,
  reactions: { total: 3, plus1: 2, minus1: 0, laugh: 0, hooray: 1, confused: 0, heart: 0, rocket: 0, eyes: 0 },
  comments: [
    { id: 'c1', author: 'reporter', body: 'Still happening on main.', createdAt: '2026-07-21T09:00:00Z', url: 'https://github.com/acme/widgets/issues/9#issuecomment-1' },
    { id: 'c2', author: 'maintainer', body: 'Reproduced.', createdAt: '2026-07-22T09:00:00Z', url: '' },
  ],
  linkedChanges: [
    { provider: 'github', url: 'https://github.com/acme/widgets/pull/12', number: 12, title: 'Guard the empty label list', state: 'OPEN' },
  ],
  partialSections: [],
}

const closedIssue: IssueSource = {
  ...openIssue,
  url: 'https://gitlab.com/acme/service/-/issues/4',
  provider: 'gitlab',
  number: 4,
  title: 'Old request',
  state: 'closed',
  stateReason: 'not_planned',
  closedAt: '2026-07-25T09:00:00Z',
  closedBy: 'maintainer',
  labels: [],
  assignees: [],
  milestone: null,
  reactions: null,
  comments: [],
  linkedChanges: [],
}

const links: PullRequestLink[] = [
  { url: openIssue.url, provider: 'github', number: 9, repo: 'widgets', kind: 'issue' },
  { url: closedIssue.url, provider: 'gitlab', number: 4, repo: 'service', kind: 'issue' },
]

function renderPanel(overrides?: { issues?: PullRequestLink[]; onAddToChat?: (t: string) => void }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onAddToChat = overrides?.onAddToChat ?? vi.fn()
  return {
    onAddToChat,
    ...render(
      <QueryClientProvider client={client}>
        <IssuePanel
          issues={overrides?.issues ?? links}
          selectedUrl={(overrides?.issues ?? links)[0].url}
          onSelect={vi.fn()}
          onAddToChat={onAddToChat}
        />
      </QueryClientProvider>,
    ),
  }
}

describe('IssuePanel', () => {
  beforeEach(() => {
    mockApi.fetchIssueSource.mockReset()
    mockApi.fetchIssueSource.mockImplementation((url: string) =>
      Promise.resolve(url === closedIssue.url ? closedIssue : openIssue))
  })

  it('renders the header facts from the contract payload', async () => {
    renderPanel()
    expect(await screen.findByText('Crash on empty label list')).toBeInTheDocument()
    // Both the source strip chip and the header carry the number.
    expect(screen.getAllByText('#9').length).toBeGreaterThan(0)
    expect(screen.getByText('Open')).toBeInTheDocument()
    expect(screen.getByText('octocat')).toBeInTheDocument()
    // Labels, assignees, milestone, reaction tallies.
    expect(screen.getByText('bug')).toBeInTheDocument()
    expect(screen.getByText('good first issue')).toBeInTheDocument()
    expect(screen.getByText('hubot, monalisa')).toBeInTheDocument()
    expect(screen.getByText('v2.1')).toBeInTheDocument()
    expect(screen.getByText('Thumbs up 2')).toBeInTheDocument()
    expect(screen.getByText('Hooray 1')).toBeInTheDocument()
    // Zero-count reactions are dropped rather than rendered as "0".
    expect(screen.queryByText(/^Heart /)).toBeNull()
  })

  it('prefixes # onto the bare label colour and picks a readable foreground', async () => {
    renderPanel()
    const dark = await screen.findByText('bug')
    expect(dark).toHaveStyle({ background: '#d73a4a' })
    const light = screen.getByText('good first issue')
    expect(light).toHaveStyle({ background: '#ffffff' })
    // Auto-contrast: white background must not get white text.
    expect(labelChipStyle('ffffff').color).toBe('#1c1c1c')
    expect(labelChipStyle('d73a4a').color).toBe('#ffffff')
    // A malformed colour degrades to theme tokens instead of an invisible chip.
    expect(labelChipStyle('nothex').background).toBe('var(--bg-hover)')
    expect(labelChipStyle('').background).toBe('var(--bg-hover)')
  })

  it('shows description, comments, and linked changes across its tabs', async () => {
    renderPanel()
    // Description is the default tab.
    expect(await screen.findByText(/Open the panel with no labels/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /Comments/ }))
    expect(screen.getByText('Still happening on main.')).toBeInTheDocument()
    expect(screen.getByText('Reproduced.')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /Linked/ }))
    expect(screen.getByText('Guard the empty label list')).toBeInTheDocument()
    expect(screen.getByText('#12')).toBeInTheDocument()
  })

  it('hides the Linked tab when the provider reported no linked changes', async () => {
    renderPanel({ issues: [links[1]] })
    expect(await screen.findByText('Old request')).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: /Linked/ })).toBeNull()
  })

  it('names the close reason for a closed issue', async () => {
    renderPanel({ issues: [links[1]] })
    expect(await screen.findByText('Closed as not planned')).toBeInTheDocument()
    expect(issueStateLabel({ ...closedIssue, stateReason: 'completed' })).toBe('Closed as completed')
    // GitLab reports no reason, so the badge falls back to the plain state.
    expect(issueStateLabel({ ...closedIssue, stateReason: '' })).toBe('Closed')
    expect(issueStateLabel(openIssue)).toBe('Open')
  })

  it('renders empty states for a payload with no description or comments', async () => {
    mockApi.fetchIssueSource.mockResolvedValue({ ...closedIssue, description: '' })
    renderPanel({ issues: [links[1]] })
    expect(await screen.findByText('No description was provided.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: /Comments/ }))
    expect(screen.getByText('No comments were returned.')).toBeInTheDocument()
  })

  it('surfaces a provider failure with a retry, not a blank panel', async () => {
    mockApi.fetchIssueSource.mockRejectedValue(new Error('glab: 404 project not found'))
    renderPanel({ issues: [links[0]] })
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.getByText('Could not load this issue')).toBeInTheDocument()
    expect(screen.getByText(/404 project not found/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('turns an authentication failure into the CLI login instruction', async () => {
    mockApi.fetchIssueSource.mockRejectedValue(new Error('not logged in; run `gh auth login`'))
    renderPanel({ issues: [links[0]] })
    expect(await screen.findByText('GitHub CLI login required')).toBeInTheDocument()
    expect(screen.getByText('gh auth login')).toBeInTheDocument()
  })

  it('refreshes only on demand, passing the force flag', async () => {
    renderPanel({ issues: [links[0]] })
    await screen.findByText('Crash on empty label list')
    expect(mockApi.fetchIssueSource).toHaveBeenCalledTimes(1)
    expect(mockApi.fetchIssueSource).toHaveBeenLastCalledWith(openIssue.url, false)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh issue' }))
    await waitFor(() => expect(mockApi.fetchIssueSource).toHaveBeenCalledTimes(2))
    expect(mockApi.fetchIssueSource).toHaveBeenLastCalledWith(openIssue.url, true)
  })

  it('renders the source strip only when the session has more than one issue', async () => {
    const single = renderPanel({ issues: [links[0]] })
    await screen.findByText('Crash on empty label list')
    expect(screen.queryByRole('tablist', { name: 'Issues' })).toBeNull()
    single.unmount()

    renderPanel()
    await screen.findByText('Crash on empty label list')
    const strip = screen.getByRole('tablist', { name: 'Issues' })
    expect(strip).toBeInTheDocument()
    expect(strip.querySelectorAll('[role="tab"]')).toHaveLength(2)
  })

  it('hands the issue off to the chat composer', async () => {
    const onAddToChat = vi.fn()
    renderPanel({ issues: [links[0]], onAddToChat })
    await screen.findByText('Crash on empty label list')
    fireEvent.click(screen.getAllByRole('button', { name: 'Add to chat' })[0])
    expect(onAddToChat).toHaveBeenCalledTimes(1)
    const text = onAddToChat.mock.calls[0][0] as string
    expect(text).toContain('#9')
    expect(text).toContain('Crash on empty label list')
    expect(text).toContain(openIssue.url)
  })

  it('builds a handoff that names the state, labels, and url', () => {
    const text = issueHandoff(openIssue)
    expect(text).toContain('- State: Open')
    expect(text).toContain('- Labels: bug, good first issue')
    expect(text).toContain('- Reported by: octocat')
    expect(text).toContain(`- Issue: ${openIssue.url}`)
  })
})
