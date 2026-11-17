import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
// `/all` for every catalog: `../i18n` registers English only.
import '../i18n/all'
import type { PullRequestSource } from '../types'

const mockApi = vi.hoisted(() => ({
  pullRequestChecks: vi.fn(),
  pullRequestSource: vi.fn(),
  pullRequestStatuses: vi.fn(),
  resolvePullRequestThread: vi.fn(),
  enablePullRequestAutoMerge: vi.fn(),
  markPullRequestReady: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div>{content}</div>,
}))

import PullRequestPanel from '../components/PullRequestPanel'

/** The Changes tab's file rows: what each `files[]` entry renders as. */

const source: PullRequestSource = {
  provider: 'gerrit',
  url: 'https://gerrit.example/c/platform/hal/+/478681',
  number: 478681,
  title: 'push_packet hal_structs [New Feature]: add the FLEX addr-mode command struct',
  description: '',
  state: 'OPEN',
  draft: false,
  mergedAt: '',
  updatedAt: '2026-11-17T12:00:00Z',
  headBranch: 'refs/changes/81/478681/2',
  baseBranch: 'dev',
  headSha: 'abcdef123456',
  author: 'Jonny Graham',
  additions: 41,
  deletions: 0,
  changedFiles: 2,
  files: [
    { path: 'al13ml/pupa/cmd.yaml', status: 'added', additions: 1, deletions: 0, patch: '@@ -0,0 +1 @@\n+../../al12/pupa/cmd.yaml', symlink: true },
    { path: 'al12/pupa/cmd.yaml', status: 'added', additions: 40, deletions: 0, patch: '@@ -0,0 +1 @@\n+fields: []' },
  ],
  commits: [],
  checks: [],
  comments: [],
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <PullRequestPanel
        sources={[{ url: source.url, provider: 'gerrit', number: source.number, repo: 'platform/hal' }]}
        selectedUrl={source.url}
        onSelect={() => {}}
        onAddToChat={vi.fn()}
      />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  mockApi.pullRequestChecks.mockReset()
  mockApi.pullRequestChecks.mockResolvedValue({ checks: [] })
  mockApi.pullRequestSource.mockReset()
  mockApi.pullRequestSource.mockResolvedValue(source)
  mockApi.pullRequestStatuses.mockReset()
  mockApi.pullRequestStatuses.mockResolvedValue({ statuses: {} })
})

describe('PullRequestPanel file rows', () => {
  it('marks a symbolic link on its row and leaves regular files unmarked', async () => {
    // A link's patch is its target, which reads like a one-line file; the badge
    // is what tells the reader it is a link, as Gerrit's own UI does.
    renderPanel()
    expect(await screen.findByText('al13ml/pupa/cmd.yaml')).toBeInTheDocument()
    const badges = screen.getAllByTestId('pr-file-symlink')
    expect(badges).toHaveLength(1)
    expect(badges[0]).toHaveTextContent('Symlink')
    expect(screen.getByRole('button', { name: /al13ml\/pupa\/cmd\.yaml/i })).toContainElement(badges[0])
    expect(screen.getByRole('button', { name: /al12\/pupa\/cmd\.yaml/i })).not.toContainElement(badges[0])
  })
})
