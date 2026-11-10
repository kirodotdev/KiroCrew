/**
 * The App SDK's issue list (`useIssues`, `IssueList`, `IssueCard`).
 *
 * Three rules hold it: it reaches `/api/source/issues` only when the hosting app
 * declares it (the single-issue read does not count, and no identity fails
 * closed); provider text is text and only http(s) urls are opened or fetched;
 * both views draw every provider from the one `IssueSummary` shape.
 *
 * The views sit behind the SDK's lazy boundary, so the first query for anything
 * they draw waits on that chunk (LAZY).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import type { IssueSearchRequest, IssueSearchResponse, IssueSummary } from '../types'

const mockApi = vi.hoisted(() => ({ fetchIssues: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))

import { AppApiProvider, IssueCard, IssueList, useIssues } from '../app-sdk/index'
import { AppIdentityProvider } from '../app-sdk/identity'
import { AppApiPermissionError } from '../app-sdk/apiError'
import { ISSUES_PATH, scopedIssuesTransport } from '../app-sdk/issues/useIssues'
import type { AppApi } from '../app-sdk/index'

const LAZY = { timeout: 5000 }
/** An error read retries once (~1s) under the app's own query client. */
const RETRIED = { timeout: 5000 }

function issue(over: Partial<IssueSummary> = {}): IssueSummary {
  return {
    provider: 'github',
    url: 'https://github.com/acme/widgets/issues/4821',
    key: '#4821',
    number: 4821,
    title: 'Crash when the list is empty',
    state: 'open',
    statusLabel: 'Open',
    labels: [{ name: 'bug', color: 'd73a4a', description: 'Something is broken' }],
    assignees: [{ login: 'octocat', name: 'Mona', avatarUrl: 'https://avatars.example/u/1' }],
    author: { login: 'hubot', name: '', avatarUrl: '' },
    priority: 'High',
    commentCount: 3,
    createdAt: '2026-11-01T10:00:00Z',
    updatedAt: '2026-11-09T10:00:00Z',
    excerpt: 'Steps to reproduce: open the list.',
    project: 'acme/widgets',
    linkedChanges: [],
    ...over,
  }
}

const PROVIDERS: IssueSummary[] = [
  issue(),
  issue({ provider: 'gitlab', url: 'https://gitlab.com/acme/w/-/issues/7', key: '#7', number: 7, project: 'acme/w', statusLabel: 'opened' }),
  issue({ provider: 'jira', url: 'https://acme.atlassian.net/browse/CREW-142', key: 'CREW-142', number: null, project: 'CREW', statusLabel: 'In Progress' }),
  issue({ provider: 'linear', url: 'https://linear.app/acme/issue/ENG-77', key: 'ENG-77', number: null, project: 'ENG', statusLabel: 'Todo', state: 'open' }),
]

const QUERY: IssueSearchRequest = { provider: 'github', scope: 'acme/widgets' }

let fetchMock: ReturnType<typeof vi.fn>
let pages: Record<string, IssueSearchResponse>
let appSeq = 0

function page(issues: IssueSummary[], nextCursor: string | null): IssueSearchResponse {
  return { provider: 'github', scope: 'acme/widgets', issues, nextCursor, total: null }
}

function bodies(): IssueSearchRequest[] {
  return fetchMock.mock.calls
    .filter(c => new URL(String(c[0]), 'http://localhost').pathname === ISSUES_PATH)
    .map(c => JSON.parse(String((c[1] as RequestInit).body)) as IssueSearchRequest)
}

beforeEach(() => {
  mockApi.fetchIssues.mockReset()
  pages = { '': page([issue()], null) }
  fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? '{}')) as IssueSearchRequest
    const answer = pages[body.cursor ?? '']
    if (!answer) return new Response(JSON.stringify({ error: 'no such page' }), { status: 400 })
    return new Response(JSON.stringify(answer), { status: 200, headers: { 'Content-Type': 'application/json' } })
  })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function externalApp(allowedApiPaths: string[], children: ReactNode) {
  // One app id per mount: an external app's query client is held per id.
  appSeq += 1
  return (
    <AppApiProvider appName={`issues-app-${appSeq}`} allowedApiPaths={allowedApiPaths} navigateFn={() => {}}>
      {children}
    </AppApiProvider>
  )
}

describe('scopedIssuesTransport', () => {
  const fakeApi = () => ({ post: vi.fn(async () => page([], null)) }) as unknown as AppApi & { post: ReturnType<typeof vi.fn> }

  it('is null unless the list itself is declared', () => {
    const api = fakeApi()
    expect(scopedIssuesTransport(api, [])).toBeNull()
    expect(scopedIssuesTransport(api, ['/api/source/issue'])).toBeNull()
    expect(scopedIssuesTransport(api, ['/api/source/pull-request'])).toBeNull()
    for (const declared of [[ISSUES_PATH], ['/api/source/issues/*'], ['/api/source/*']]) {
      expect(scopedIssuesTransport(api, declared), declared.join()).not.toBeNull()
    }
    expect(api.post).not.toHaveBeenCalled()
  })
})

describe('fails closed', () => {
  it('refuses an app that declares only the single-issue read, and sends nothing', async () => {
    render(externalApp(['/api/source/issue', '/api/source/pull-request'], <IssueList query={QUERY} />))
    expect(await screen.findByTestId('app-issue-list-not-declared', {}, LAZY)).toHaveTextContent(ISSUES_PATH)
    expect(fetchMock).not.toHaveBeenCalled()
    expect(mockApi.fetchIssues).not.toHaveBeenCalled()
  })

  it('refuses with no app identity and no SDK provider', async () => {
    const client = new QueryClient()
    render(<QueryClientProvider client={client}><IssueList query={QUERY} /></QueryClientProvider>)
    expect(await screen.findByTestId('app-issue-list-not-declared', {}, LAZY)).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
    expect(mockApi.fetchIssues).not.toHaveBeenCalled()
  })

  it('reports the refusal from the hook as an AppApiPermissionError', () => {
    const client = new QueryClient()
    const { result } = renderHook(() => useIssues(QUERY), {
      wrapper: ({ children }) => externalApp([], <QueryClientProvider client={client}>{children}</QueryClientProvider>),
    })
    expect(result.current.error).toBeInstanceOf(AppApiPermissionError)
    expect(result.current.isLoading).toBe(false)
    expect(result.current.issues).toEqual([])
    act(() => { result.current.fetchNextPage(); result.current.refetch() })
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('keeps the dashboard client for a builtin page', async () => {
    mockApi.fetchIssues.mockResolvedValue(page([issue()], null))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <AppIdentityProvider appId="issue-radar" origin="builtin"><IssueList query={QUERY} /></AppIdentityProvider>
      </QueryClientProvider>,
    )
    expect(await screen.findByText('Crash when the list is empty', {}, LAZY)).toBeInTheDocument()
    expect(mockApi.fetchIssues).toHaveBeenCalledWith({ ...QUERY, cursor: null })
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('IssueList', () => {
  it('pages through nextCursor and hides Load more on the last page', async () => {
    pages = {
      '': page([issue()], 'c2'),
      c2: page([issue({ key: '#4822', url: 'https://github.com/acme/widgets/issues/4822', title: 'Second page' })], null),
    }
    render(externalApp([ISSUES_PATH], <IssueList query={QUERY} />))
    fireEvent.click(await screen.findByRole('button', { name: 'Load more' }, LAZY))
    expect(await screen.findByText('Second page')).toBeInTheDocument()
    expect(screen.getByText('Crash when the list is empty')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
    expect(bodies()).toEqual([{ ...QUERY, cursor: null }, { ...QUERY, cursor: 'c2' }])
  })

  it('starts again from the first page when a filter changes', async () => {
    pages = {
      '': page([issue()], 'c2'),
      c2: page([issue({ key: '#4822', url: 'https://github.com/acme/widgets/issues/4822', title: 'Second page' })], null),
    }
    render(externalApp([ISSUES_PATH], <IssueList query={QUERY} showFilters />))
    fireEvent.click(await screen.findByRole('button', { name: 'Load more' }, LAZY))
    await screen.findByText('Second page')
    pages = { '': page([issue({ key: '#9', url: 'https://github.com/acme/widgets/issues/9', title: 'Closed one', state: 'closed', statusLabel: 'Closed' })], null) }
    fireEvent.click(screen.getByRole('combobox', { name: 'State' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Closed' }))
    expect(await screen.findByText('Closed one')).toBeInTheDocument()
    expect(screen.queryByText('Second page')).toBeNull()
    expect(bodies().at(-1)).toEqual({ ...QUERY, state: 'closed', cursor: null })
  })

  it('sends typed filters once typing pauses, and "Assigned to me" as assignee me', async () => {
    render(externalApp([ISSUES_PATH], <IssueList query={QUERY} showFilters />))
    await screen.findByText('Crash when the list is empty', {}, LAZY)
    fireEvent.change(screen.getByLabelText('Search issues'), { target: { value: 'crash ' } })
    fireEvent.change(screen.getByLabelText('Labels'), { target: { value: 'bug, ui,' } })
    await waitFor(() => expect(bodies().at(-1)).toEqual({ ...QUERY, text: 'crash', labels: ['bug', 'ui'], cursor: null }))
    // The box keeps what was typed; committing the trimmed value does not rewrite it.
    expect(screen.getByLabelText('Labels')).toHaveValue('bug, ui,')
    fireEvent.click(screen.getByRole('button', { name: 'Assigned to me' }))
    await waitFor(() => expect(bodies().at(-1)).toMatchObject({ assignee: 'me', text: 'crash' }))
    expect(screen.getByLabelText('Assignee')).toHaveValue('me')
    expect(screen.getByRole('button', { name: 'Assigned to me' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('shows the empty state, or the app\'s own text', async () => {
    pages = { '': page([], null) }
    const { unmount } = render(externalApp([ISSUES_PATH], <IssueList query={QUERY} />))
    expect(await screen.findByTestId('app-issue-list-empty', {}, LAZY)).toHaveTextContent('No issues match.')
    unmount()
    render(externalApp([ISSUES_PATH], <IssueList query={QUERY} emptyText="Nothing to triage" />))
    expect(await screen.findByTestId('app-issue-list-empty', {}, LAZY)).toHaveTextContent('Nothing to triage')
  })

  it('shows the provider error with a retry', async () => {
    pages = {}
    render(externalApp([ISSUES_PATH], <IssueList query={QUERY} />))
    expect(await screen.findByTestId('app-issue-list-error', {}, RETRIED)).toHaveTextContent('no such page')
    pages = { '': page([issue()], null) }
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Crash when the list is empty')).toBeInTheDocument()
  })

  it('keeps the loaded issues when Load more fails, and offers the agent hand-off', async () => {
    pages = { '': page([issue()], 'c2') }
    render(externalApp([ISSUES_PATH], <IssueList query={QUERY} />))
    fireEvent.click(await screen.findByRole('button', { name: 'Load more' }, LAZY))
    expect(await screen.findByTestId('app-issue-list-error', {}, RETRIED)).toHaveTextContent('no such page')
    expect(screen.getByRole('button', { name: /Ask the agent/ })).toBeInTheDocument()
    expect(screen.getByText('Crash when the list is empty')).toBeInTheDocument()
  })

  it('shows a skeleton while the first page loads', async () => {
    let release: (r: Response) => void = () => {}
    fetchMock.mockImplementationOnce(() => new Promise<Response>(r => { release = r }))
    render(externalApp([ISSUES_PATH], <IssueList query={QUERY} view="expanded" />))
    expect(await screen.findByTestId('issue-list-loading', {}, LAZY)).toBeInTheDocument()
    release(new Response(JSON.stringify(page([issue()], null)), { status: 200 }))
    expect(await screen.findByText('Crash when the list is empty')).toBeInTheDocument()
  })
})

describe('IssueCard', () => {
  it.each(['condensed', 'expanded'] as const)('draws all four providers in the %s view', async view => {
    render(<div>{PROVIDERS.map(i => <IssueCard key={i.key} issue={i} view={view} />)}</div>)
    for (const i of PROVIDERS) {
      expect(await screen.findByText(i.key, {}, LAZY)).toBeInTheDocument()
      expect(screen.getAllByText(i.statusLabel).length).toBeGreaterThan(0)
    }
    expect(document.querySelector('[data-provider-mark="github"]')).not.toBeNull()
    expect(document.querySelector('[data-provider-mark="gitlab"]')).not.toBeNull()
    expect(screen.getByTestId('jira-provider-mark')).toBeInTheDocument()
    expect(screen.getByTestId('linear-provider-mark')).toBeInTheDocument()
    if (view === 'expanded') {
      for (const name of ['GitHub', 'GitLab', 'Jira', 'Linear']) {
        const link = screen.getByRole('link', { name: `Open in ${name}` })
        expect(link).toHaveAttribute('target', '_blank')
        expect(link).toHaveAttribute('rel', 'noopener noreferrer')
      }
    }
  })

  it.each(['condensed', 'expanded'] as const)('draws the sparse shape in the %s view', async view => {
    const long = 'A very long title '.repeat(20)
    const sparse = issue({ priority: null, author: null, labels: [], assignees: [], excerpt: '', title: long, commentCount: 0 })
    const rtl = issue({ key: '#5', url: 'https://github.com/acme/widgets/issues/5', title: 'خطأ عند فتح القائمة', excerpt: 'نص عربي', priority: null })
    render(<div><IssueCard issue={sparse} view={view} /><IssueCard issue={rtl} view={view} /></div>)
    expect(await screen.findByText(long.trim(), { normalizer: s => s.trim() }, LAZY)).toBeInTheDocument()
    const rtlTitle = screen.getByText('خطأ عند فتح القائمة')
    expect(rtlTitle).toHaveAttribute('dir', 'auto')
    expect(screen.queryByText(/Priority:/)).toBeNull()
    if (view === 'expanded') {
      expect(screen.getByText('Unassigned')).toBeInTheDocument()
      expect(screen.queryByText(/Opened by/)).not.toBeNull() // the RTL card still has its author
    }
  })

  it('renders provider text as text and opens only http(s) urls', async () => {
    const hostile = issue({
      url: 'javascript:alert(1)',
      title: '<img src=x onerror=alert(1)>',
      excerpt: '<b>bold</b>',
      labels: [{ name: '<script>x</script>', color: 'zzzzzz', description: '' }],
      assignees: [{ login: 'a', name: '<i>name</i>', avatarUrl: 'javascript:alert(1)' }],
      author: { login: 'b', name: 'b', avatarUrl: 'data:image/png;base64,AAAA' },
    })
    const { container } = render(<div><IssueCard issue={hostile} view="expanded" /><IssueCard issue={hostile} /></div>)
    expect((await screen.findAllByText('<img src=x onerror=alert(1)>', {}, LAZY)).length).toBe(2)
    expect(screen.getByText('<b>bold</b>')).toBeInTheDocument()
    expect(screen.getAllByText('<script>x</script>').length).toBeGreaterThan(0)
    expect(screen.getByText('<i>name</i>')).toBeInTheDocument()
    expect(container.querySelector('script, b, i, img')).toBeNull()
    expect(container.querySelector('a')).toBeNull()
  })

  it('fetches an avatar only from an http(s) url', async () => {
    const { container } = render(<IssueCard issue={issue()} view="expanded" />)
    await screen.findByText('#4821', {}, LAZY)
    const img = container.querySelector('img')
    expect(img).toHaveAttribute('src', 'https://avatars.example/u/1')
    expect(img).toHaveAttribute('referrerpolicy', 'no-referrer')
  })

  it('is a keyboard-reachable button with an accessible name when onSelect is given', async () => {
    const onSelect = vi.fn()
    render(<IssueCard issue={PROVIDERS[2]} onSelect={onSelect} />)
    const row = await screen.findByRole('button', { name: 'Jira CREW-142: Crash when the list is empty (In Progress)' }, LAZY)
    row.focus()
    expect(row).toHaveFocus()
    fireEvent.click(row)
    expect(onSelect).toHaveBeenCalledWith(PROVIDERS[2])
  })

  it('links the condensed row out to the issue without onSelect', async () => {
    render(<IssueCard issue={PROVIDERS[3]} />)
    const row = await screen.findByRole('link', { name: /Linear ENG-77/ }, LAZY)
    expect(row).toHaveAttribute('href', 'https://linear.app/acme/issue/ENG-77')
    expect(row).toHaveAttribute('rel', 'noopener noreferrer')
    expect(within(row).getByTestId('issue-status')).toHaveTextContent('Todo')
  })

  it('caps the condensed row at two label chips', async () => {
    const many = issue({ labels: ['a', 'b', 'c'].map(name => ({ name, color: '00ff00', description: '' })) })
    render(<IssueCard issue={many} />)
    const chips = await screen.findByTestId('issue-row-labels', {}, LAZY)
    expect(chips.children).toHaveLength(2)
  })
})
