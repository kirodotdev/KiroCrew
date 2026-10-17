// GitLab groups in Issue Radar, tested at the FETCH boundary so the requests
// each surface makes are the thing under test:
//   • Settings follows and unfollows a group, and says what to do when it can't;
//   • the "Review requests" dashboard lists each group's merge requests, its
//     count (with "+" when capped), and a per-group error;
//   • the rail offers that dashboard only once a group is followed, so with none
//     followed the rail is the four dashboards it always was and no
//     review-request call is made.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

import { REFRESH_DEFAULTS } from '../apps/issue-radar/lib/format'
import { installSoftNavigate, __resetNavSeamForTests } from '../utils/errorReport'

const ctx = {
  value: {} as Record<string, unknown>,
}
vi.mock('../apps/issue-radar/context', () => ({ useIssueRadar: () => ctx.value }))

const GitLabGroupsSection = (await import('../apps/issue-radar/views/settings/GitLabGroupsSection')).default
const GroupReviewsView = (await import('../apps/issue-radar/views/GroupReviewsView')).default
const DashboardsSection = (await import('../apps/issue-radar/components/DashboardsSection')).default
const SettingsSection = (await import('../apps/issue-radar/components/SettingsSection')).default

const API = '/api/apps/issue-radar'

/** Every surface here settles through a chain: the group-list query resolves,
 * then each followed group mounts and runs its own review-request query (or a
 * follow/unfollow round-trip updates the shared list). This bounds each wait on
 * that chain explicitly rather than relying on the default findBy deadline. */
const CHAIN_WAIT = { timeout: 5_000 }

const ROW = {
  number: 12,
  title: 'Add widget',
  url: 'https://gitlab.com/acme/web/api/-/merge_requests/12',
  project: 'acme/web/api',
  author: 'alice',
  updated_at: '2026-07-02T00:00:00Z',
  truncated_fields: [] as string[],
}

function jsonResponse(status: number, body: unknown): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as unknown as Response
}

type Handler = (url: string, init: RequestInit) => Response

let fetchMock: ReturnType<typeof vi.fn>

/** Route each request by method + path; anything unrouted fails the test loudly. */
function serve(routes: Record<string, Handler>) {
  fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input)
    const key = `${init.method ?? 'GET'} ${url.split('?')[0]}`
    const handler = routes[key]
    if (!handler) throw new Error(`unexpected request: ${key}`)
    return handler(url, init)
  })
  vi.stubGlobal('fetch', fetchMock)
}

const calls = () => fetchMock.mock.calls.map(([u, init]) => `${(init as RequestInit | undefined)?.method ?? 'GET'} ${String(u)}`)
const reviewCalls = () => calls().filter((c) => c.includes('/gitlab-groups/review-requests'))

const groupsRoute = (groups: Array<{ host: string; path: string }>) => ({
  [`GET ${API}/gitlab-groups`]: () => jsonResponse(200, { groups }),
})

/** The followed-groups read failing on the server. */
const groupsReadFails = () => ({
  [`GET ${API}/gitlab-groups`]: () => jsonResponse(500, { error: 'config store unreadable' }),
})

function renderWithClient(node: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } })
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>)
}

beforeEach(() => {
  fetchMock = vi.fn()
  sessionStorage.clear()
  ctx.value = {
    mainView: 'dashboard',
    dashboardTab: 'overview',
    openDashboard: vi.fn(),
    openSettings: vi.fn(),
    refreshPrefs: REFRESH_DEFAULTS,
  }
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  __resetNavSeamForTests()
})

describe('GitLab groups in Settings', () => {
  it('makes no review-request call when no group is followed', async () => {
    serve(groupsRoute([]))
    renderWithClient(<GitLabGroupsSection />)

    expect(await screen.findByText("You aren't following any GitLab groups yet.", undefined, CHAIN_WAIT)).toBeTruthy()
    expect(calls()).toEqual([`GET ${API}/gitlab-groups`])
    expect(reviewCalls()).toEqual([])
  })

  it('follows a group and lists it with a labelled Unfollow button', async () => {
    serve({
      ...groupsRoute([]),
      [`POST ${API}/gitlab-groups`]: () => jsonResponse(200, { host: 'gitlab.com', path: 'acme/web' }),
    })
    renderWithClient(<GitLabGroupsSection />)
    await screen.findByText("You aren't following any GitLab groups yet.", undefined, CHAIN_WAIT)

    fireEvent.change(screen.getByLabelText('Group path or link'), { target: { value: '  acme/web ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Follow group' }))

    expect(await screen.findByRole('button', { name: 'Unfollow acme/web' }, CHAIN_WAIT)).toBeTruthy()
    const post = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === 'POST')
    expect(JSON.parse((post?.[1] as RequestInit).body as string)).toEqual({ group: 'acme/web' })
    expect((screen.getByLabelText('Group path or link') as HTMLInputElement).value).toBe('')
    expect(reviewCalls()).toEqual([])
  })

  it('keeps a new draft typed while a follow request is in flight', async () => {
    let release: (r: Response) => void = () => {}
    serve({
      ...groupsRoute([]),
      [`POST ${API}/gitlab-groups`]: () => jsonResponse(200, { host: 'gitlab.com', path: 'acme/web' }),
    })
    const routed = fetchMock.getMockImplementation()!
    fetchMock.mockImplementation((input: RequestInfo | URL, init: RequestInit = {}) => (
      init.method === 'POST'
        ? new Promise<Response>((resolve) => { release = resolve })
        : routed(input, init)
    ))
    renderWithClient(<GitLabGroupsSection />)
    await screen.findByText("You aren't following any GitLab groups yet.", undefined, CHAIN_WAIT)

    const field = screen.getByLabelText('Group path or link') as HTMLInputElement
    fireEvent.change(field, { target: { value: 'acme/web' } })
    fireEvent.click(screen.getByRole('button', { name: 'Follow group' }))
    fireEvent.change(field, { target: { value: 'acme/mobile' } })
    release(jsonResponse(200, { host: 'gitlab.com', path: 'acme/web' }))

    expect(await screen.findByRole('button', { name: 'Unfollow acme/web' }, CHAIN_WAIT)).toBeTruthy()
    expect(field.value).toBe('acme/mobile')
  })

  it.each([
    ['invalid_group', 400, "That isn't a GitLab group path."],
    ['group_not_found', 404, 'GitLab has no group at that path'],
    ['setup_required', 502, 'Run glab auth login'],
  ])('says what to do when following fails with %s', async (code, status, sentence) => {
    serve({
      ...groupsRoute([]),
      [`POST ${API}/gitlab-groups`]: () => jsonResponse(status, { error: 'raw provider text', code }),
    })
    renderWithClient(<GitLabGroupsSection />)
    await screen.findByText("You aren't following any GitLab groups yet.", undefined, CHAIN_WAIT)

    fireEvent.change(screen.getByLabelText('Group path or link'), { target: { value: 'acme/x' } })
    fireEvent.click(screen.getByRole('button', { name: 'Follow group' }))

    const alert = await screen.findByRole('alert', undefined, CHAIN_WAIT)
    expect(alert.textContent).toContain(sentence)
    expect(alert.textContent).not.toContain('raw provider text')
    expect(screen.queryAllByTestId('gitlab-group-row')).toHaveLength(0)
  })

  it('unfollows a group', async () => {
    serve({
      ...groupsRoute([{ host: 'gitlab.acme.example', path: 'acme' }]),
      [`DELETE ${API}/gitlab-groups`]: () => jsonResponse(200, { host: 'gitlab.acme.example', path: 'acme' }),
    })
    renderWithClient(<GitLabGroupsSection />)
    fireEvent.click(await screen.findByRole('button', { name: 'Unfollow acme' }, CHAIN_WAIT))

    await waitFor(() => expect(screen.queryAllByTestId('gitlab-group-row')).toHaveLength(0), CHAIN_WAIT)
    expect(calls()).toContain(`DELETE ${API}/gitlab-groups?host=gitlab.acme.example&path=acme`)
  })

  it('keeps a typed group path across the Ask the agent round trip', async () => {
    serve(groupsReadFails())
    const navigated: string[] = []
    installSoftNavigate((to) => { navigated.push(to) })
    const first = renderWithClient(<GitLabGroupsSection />)
    const notice = await screen.findByRole('alert', undefined, CHAIN_WAIT)
    expect(notice.textContent).toContain("Couldn't load your GitLab groups")

    fireEvent.change(screen.getByLabelText('Group path or link'), { target: { value: 'acme/draft' } })
    fireEvent.click(within(notice).getByRole('button', { name: 'Ask the agent' }))
    expect(navigated).toEqual(['/chat'])
    // The hand-off's navigation unmounts Settings; coming back mounts it fresh.
    first.unmount()

    renderWithClient(<GitLabGroupsSection />)
    expect((screen.getByLabelText('Group path or link') as HTMLInputElement).value).toBe('acme/draft')
  })

  it('keeps a typed group path across the hand-off on an unfollow error', async () => {
    serve({
      ...groupsRoute([{ host: 'gitlab.com', path: 'acme' }]),
      [`DELETE ${API}/gitlab-groups`]: () => jsonResponse(502, { error: 'raw', code: 'setup_required' }),
    })
    const navigated: string[] = []
    installSoftNavigate((to) => { navigated.push(to) })
    const first = renderWithClient(<GitLabGroupsSection />)
    fireEvent.click(await screen.findByRole('button', { name: 'Unfollow acme' }, CHAIN_WAIT))
    const notice = await screen.findByRole('alert', undefined, CHAIN_WAIT)
    expect(notice.textContent).toContain("Couldn't unfollow this group")

    fireEvent.change(screen.getByLabelText('Group path or link'), { target: { value: 'acme/next' } })
    fireEvent.click(within(notice).getByRole('button', { name: 'Ask the agent' }))
    expect(navigated).toEqual(['/chat'])
    first.unmount()

    renderWithClient(<GitLabGroupsSection />)
    expect((screen.getByLabelText('Group path or link') as HTMLInputElement).value).toBe('acme/next')
  })

  it('starts empty after a followed group, so a remount does not bring the old path back', async () => {
    serve({
      ...groupsRoute([]),
      [`POST ${API}/gitlab-groups`]: () => jsonResponse(200, { host: 'gitlab.com', path: 'acme/web' }),
    })
    const first = renderWithClient(<GitLabGroupsSection />)
    await screen.findByText("You aren't following any GitLab groups yet.", undefined, CHAIN_WAIT)
    fireEvent.change(screen.getByLabelText('Group path or link'), { target: { value: 'acme/web' } })
    fireEvent.click(screen.getByRole('button', { name: 'Follow group' }))
    await screen.findByRole('button', { name: 'Unfollow acme/web' }, CHAIN_WAIT)
    first.unmount()

    renderWithClient(<GitLabGroupsSection />)
    expect((screen.getByLabelText('Group path or link') as HTMLInputElement).value).toBe('')
  })
})

describe('Review requests dashboard', () => {
  it('lists each followed group with its count and merge requests', async () => {
    serve({
      ...groupsRoute([{ host: 'gitlab.com', path: 'acme/web' }]),
      [`GET ${API}/gitlab-groups/review-requests`]: () => jsonResponse(200, {
        host: 'gitlab.com', path: 'acme/web', login: 'me',
        pulls: [ROW, { ...ROW, number: 13, title: 'Fix login', project: 'acme/web/ui/app' }],
        truncated: false, limit: 300,
      }),
    })
    renderWithClient(<GroupReviewsView />)

    expect(await screen.findByText('Waiting for you: 2', undefined, CHAIN_WAIT)).toBeTruthy()
    const link = screen.getByText('Add widget').closest('a')
    expect(link?.getAttribute('href')).toBe(ROW.url)
    expect(link?.getAttribute('target')).toBe('_blank')
    expect(screen.getByText('acme/web/api!12')).toBeTruthy()
    expect(screen.getByText('acme/web/ui/app!13')).toBeTruthy()
    expect(reviewCalls()).toEqual([`GET ${API}/gitlab-groups/review-requests?host=gitlab.com&path=acme%2Fweb`])
    expect(screen.queryByText(/Showing the newest/)).toBeNull()
  })

  it('lets a row\'s metadata wrap at narrow widths, with a long author truncated', async () => {
    const author = 'project_12345678_bot_0123456789abcdef'
    serve({
      ...groupsRoute([{ host: 'gitlab.com', path: 'acme' }]),
      [`GET ${API}/gitlab-groups/review-requests`]: () => jsonResponse(200, {
        host: 'gitlab.com', path: 'acme', login: 'me', pulls: [{ ...ROW, author }], truncated: false, limit: 300,
      }),
    })
    renderWithClient(<GroupReviewsView />)

    const project = await screen.findByText('acme/web/api!12', undefined, CHAIN_WAIT)
    const row = project.parentElement!
    expect(row.className).toContain('flex-wrap')
    const byline = within(row).getByText(`· ${author}`)
    expect(byline.getAttribute('title')).toBe(author)
    expect(byline.className).toContain('truncate')
    expect(byline.className).toContain('min-w-0')
    expect(byline.className).not.toContain('shrink-0')
    for (const child of Array.from(row.children)) expect(child.className).not.toContain('shrink-0')
  })

  it('says when a capped count has more behind it and that the list is the newest N', async () => {
    serve({
      ...groupsRoute([{ host: 'gitlab.com', path: 'acme' }]),
      [`GET ${API}/gitlab-groups/review-requests`]: () => jsonResponse(200, {
        host: 'gitlab.com', path: 'acme', login: 'me', pulls: [ROW], truncated: true, limit: 1,
      }),
    })
    renderWithClient(<GroupReviewsView />)
    expect(await screen.findByText('Waiting for you: more than 1', undefined, CHAIN_WAIT)).toBeTruthy()
    expect(screen.getByText('Showing the newest 1.')).toBeTruthy()
    const seeAll = screen.getByRole('link', { name: 'See all on GitLab' })
    expect(seeAll.getAttribute('href')).toBe(
      'https://gitlab.com/groups/acme/-/merge_requests?state=opened&reviewer_username=me',
    )
  })

  it('shows a shortened title as shortened and a dropped link as plain text', async () => {
    serve({
      ...groupsRoute([{ host: 'gitlab.com', path: 'acme' }]),
      [`GET ${API}/gitlab-groups/review-requests`]: () => jsonResponse(200, {
        host: 'gitlab.com', path: 'acme', login: 'me',
        pulls: [{ ...ROW, title: 'Long title', url: '', truncated_fields: ['title', 'url'] }],
        truncated: false, limit: 300,
      }),
    })
    renderWithClient(<GroupReviewsView />)
    const title = await screen.findByText('Long title…', undefined, CHAIN_WAIT)
    expect(title.closest('a')).toBeNull()
  })

  it('gives each group its own error, in words that say what to do', async () => {
    serve({
      ...groupsRoute([{ host: 'gitlab.com', path: 'gone' }, { host: 'gitlab.com', path: 'acme' }]),
      [`GET ${API}/gitlab-groups/review-requests`]: (url) => (url.includes('path=gone')
        ? jsonResponse(404, { error: 'raw', code: 'group_not_found' })
        : jsonResponse(200, { host: 'gitlab.com', path: 'acme', login: 'me', pulls: [], truncated: false, limit: 300 })),
    })
    renderWithClient(<GroupReviewsView />)

    const alert = await screen.findByRole('alert', undefined, CHAIN_WAIT)
    expect(alert.textContent).toContain('GitLab has no group at that path')
    expect(await screen.findByText('Nothing in this group is waiting for your review.', undefined, CHAIN_WAIT)).toBeTruthy()
  })

  it('points to Settings when no group is followed, without a review-request call', async () => {
    serve(groupsRoute([]))
    renderWithClient(<GroupReviewsView />)
    fireEvent.click(await screen.findByRole('button', { name: 'Follow a GitLab group' }, CHAIN_WAIT))
    expect(ctx.value.openSettings).toHaveBeenCalledWith({ kind: 'general', anchor: 'gitlabGroups' })
    expect(reviewCalls()).toEqual([])
  })
})

describe('Dashboards rail entry', () => {
  it('keeps the rail unchanged while no group is followed', async () => {
    serve(groupsRoute([]))
    renderWithClient(<DashboardsSection />)
    await waitFor(() => expect(calls()).toEqual([`GET ${API}/gitlab-groups`]), CHAIN_WAIT)
    expect(screen.getAllByRole('button').map((b) => b.textContent)).toEqual(['Overview', 'Graph', 'Tagging', 'Pipeline', 'CI runs'])
  })

  it('offers Review requests once a group is followed', async () => {
    serve(groupsRoute([{ host: 'gitlab.com', path: 'acme' }]))
    renderWithClient(<DashboardsSection />)
    fireEvent.click(await screen.findByRole('button', { name: 'Review requests' }, CHAIN_WAIT))
    expect(ctx.value.openDashboard).toHaveBeenCalledWith('reviews')
    expect(reviewCalls()).toEqual([])
  })

  it('keeps Review requests listed while it is the page on screen', async () => {
    serve(groupsRoute([]))
    ctx.value = { ...ctx.value, dashboardTab: 'reviews' }
    renderWithClient(<DashboardsSection />)
    await waitFor(() => expect(calls()).toEqual([`GET ${API}/gitlab-groups`]), CHAIN_WAIT)
    expect(screen.getByRole('button', { name: 'Review requests' })).toBeTruthy()
  })

  it('shows a failed group read as an error, not as no groups', async () => {
    serve(groupsReadFails())
    renderWithClient(<DashboardsSection />)

    const notice = await screen.findByTestId('dashboards-gitlab-groups-error', undefined, CHAIN_WAIT)
    expect(notice.getAttribute('role')).toBe('alert')
    expect(notice.textContent).toContain("Couldn't load your GitLab groups")
    expect(within(notice).getByRole('button', { name: 'Ask the agent' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Review requests' })).toBeTruthy()
    expect(reviewCalls()).toEqual([])
  })
})

describe('Settings rail entry', () => {
  it('opens General settings at the GitLab groups section', () => {
    ctx.value = { ...ctx.value, mainView: 'settings', repos: [], settingsTarget: { kind: 'general' }, onAddRepo: vi.fn() }
    render(<SettingsSection />)
    fireEvent.click(screen.getByRole('button', { name: 'GitLab groups' }))
    expect(ctx.value.openSettings).toHaveBeenCalledWith({ kind: 'general', anchor: 'gitlabGroups' })
  })
})
