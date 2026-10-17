import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { PublishHub, deploySiteIdFor, findLiveSite } from '../components/PublishHub'
import { PREVIEW_ARTIFACT_DEPLOY } from '../utils/previewFlags'
import type { Artifact } from '../types'

/**
 * An artifact already on the public web keeps its link in the AWS account, not on
 * the artifact. The panel reads the deploy's site list when it opens and, for a
 * site under this artifact's id, shows the link and turns the public-web row into
 * an update of that same site. Anything short of a match leaves the panel as it
 * is with no lookup at all.
 */

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  )
}

const artifact: Artifact = {
  slug: 'launch-notes',
  name: 'Launch Notes',
  kind: 'widget',
  description: '',
  content: '',
  version: 1,
  created_at: '',
  updated_at: '',
  tags: [],
}

const publicWebRow = (over: Record<string, unknown> = {}) => ({
  id: 'deploy-web',
  label: 'Public Web',
  icon: 'Globe',
  kinds: [],
  configured: true,
  setupRoute: '/deploy',
  endpoint: '/api/deploy/deploy',
  ...over,
})

const LIVE_URL = 'https://d111111abcdef8.cloudfront.net/'

interface Route {
  appProviders?: Record<string, unknown>[]
  /** The body `/api/deploy/list` answers with, or an HTTP status to fail with. */
  sites?: Record<string, unknown> | number
  /** Holds the `/api/deploy/list` answer until it settles. */
  hold?: Promise<void>
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

/** Answers every request by URL, and records what was asked for. */
function routeFetch(spy: ReturnType<typeof vi.spyOn>, route: Route) {
  const calls: { url: string; body?: Record<string, unknown> }[] = []
  spy.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined
    calls.push({ url, body })
    if (url.includes('/api/artifacts/publish-providers')) return json({ providers: [], kind: artifact.kind })
    if (url.includes('/api/publish-providers')) return json({ providers: route.appProviders ?? [publicWebRow()] })
    if (url.includes('/api/deploy/list')) {
      if (route.hold) await route.hold
      return typeof route.sites === 'number'
        ? json({ error: 'list failed' }, route.sites)
        : json(route.sites ?? { sites: [], configured: true })
    }
    if (url.includes('/api/deploy/deploy')) {
      return body?.confirm
        ? json({ url: LIVE_URL, site_id: artifact.slug })
        : json({ requires_confirm: true, message: 'Ready to publish', content_digest: 'd1', profile: body?.profile ?? 'default', region: 'us-west-2' })
    }
    return json({})
  })
  return calls
}

const listCalls = (calls: { url: string }[]) => calls.filter(c => c.url.includes('/api/deploy/list')).length

/** Bound for every wait on what the site list renders. That list is a chained
 *  query: `/api/deploy/list` is enabled only once the providers query
 *  (`/api/publish-providers`) has answered with a configured public-web row,
 *  so its result lands two fetches after render. */
const SITE_LIST_WAIT = { timeout: 5000 }

beforeEach(() => { localStorage.setItem(PREVIEW_ARTIFACT_DEPLOY, '1') })
afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('deploySiteIdFor', () => {
  it('keeps an ordinary slug as it is', () => {
    expect(deploySiteIdFor('launch-notes')).toBe('launch-notes')
  })

  it('cuts a long slug to the 64 characters the deploy stores', () => {
    const slug = 'a'.repeat(70)
    expect(deploySiteIdFor(slug)).toBe('a'.repeat(64))
  })

  it('normalizes case and characters the deploy does not keep', () => {
    expect(deploySiteIdFor(' My Notes.v2_ ')).toBe('my-notes-v2')
  })
})

describe('findLiveSite', () => {
  const site = { site_id: 'launch-notes', url: LIVE_URL, status: 'Deployed', profile: 'default' }

  it('finds the site listed under the artifact id', () => {
    expect(findLiveSite({ sites: [{ site_id: 'other' }, site], configured: true }, 'launch-notes')).toEqual(site)
  })

  it('answers null for no listing, an unconfigured deploy, or no matching site', () => {
    expect(findLiveSite(undefined, 'launch-notes')).toBeNull()
    expect(findLiveSite({ sites: [site], configured: false }, 'launch-notes')).toBeNull()
    expect(findLiveSite({ sites: [{ site_id: 'launch-notes-2' }], configured: true }, 'launch-notes')).toBeNull()
  })
})

describe('PublishHub — an existing public link', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>
  beforeEach(() => { fetchSpy = vi.spyOn(globalThis, 'fetch') })

  it('shows the live link and an update in place of the public-web row', async () => {
    routeFetch(fetchSpy, {
      sites: { sites: [{ site_id: 'launch-notes', url: LIVE_URL, status: 'Deployed', profile: 'team' }], configured: true },
    })
    render(<PublishHub artifact={artifact} />, { wrapper })

    expect(await screen.findByText('Already published to the public web', {}, SITE_LIST_WAIT)).toBeDefined()
    expect(screen.getByText(LIVE_URL)).toBeDefined()
    const open = screen.getByRole('link', { name: /Open published page/ })
    expect(open.getAttribute('href')).toBe(LIVE_URL)
    expect(open.getAttribute('target')).toBe('_blank')
    expect(screen.getByRole('button', { name: /Update \(same URL\)/ })).toBeDefined()
    // The update stands in for the row, so the destination is not offered twice.
    expect(screen.queryByText('Public Web')).toBeNull()
  })

  it('updates the live site through the account that holds it', async () => {
    const calls = routeFetch(fetchSpy, {
      sites: { sites: [{ site_id: 'launch-notes', url: LIVE_URL, status: 'Deployed', profile: 'team' }], configured: true },
    })
    render(<PublishHub artifact={artifact} />, { wrapper })

    // The update button says a review comes before anything changes.
    expect(await screen.findByText('You review and confirm before the published page changes.', {}, SITE_LIST_WAIT)).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: /Update \(same URL\)/ }))
    expect(screen.getByText('This replaces the published page. Its URL stays the same.')).toBeDefined()
    // Every step of the flow names it an update, not a first publish.
    expect(screen.getByText((_, el) => el?.tagName === 'DIV'
      && el.textContent === 'Update launch-notes via Public Web?')).toBeDefined()
    expect(screen.queryByRole('button', { name: /^Publish$/ })).toBeNull()
    // The time limit is stated against the page that is already public.
    expect(screen.getByText(/The time limit applies to the published page from now on/)).toBeDefined()

    // The middle step only opens the review, and says so; the page changes at
    // the final confirm.
    fireEvent.click(screen.getByRole('button', { name: 'Review update' }))
    await screen.findByText((_, el) => el?.tagName === 'DIV'
      && el.textContent === 'Ready to update launch-notes via Public Web')
    // The deploy's own first-publish line gives way to the update's.
    expect(screen.queryByText(/Ready to publish/)).toBeNull()
    expect(screen.getByText('This replaces the published page. Its URL stays the same.')).toBeDefined()

    expect(screen.queryByText('Confirm & Publish')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm & Update' }))
    fireEvent.click(await screen.findByRole('button', { name: 'I understand, publish publicly' }))
    await screen.findByText('Published!')

    const deploys = calls.filter(c => c.url.includes('/api/deploy/deploy'))
    expect(deploys).toHaveLength(2)
    for (const d of deploys) {
      expect(d.body?.site_id).toBe('launch-notes')
      expect(d.body?.profile).toBe('team')
    }
    expect(deploys[1].body?.expected_profile).toBe('team')
  })

  it('shows the link a publish just made on the next open, before the listing can see it', async () => {
    const calls = routeFetch(fetchSpy, {})
    // One client across both opens, as in the app, so the second open sees the cache.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const shared = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>
        <MemoryRouter>{children}</MemoryRouter>
      </QueryClientProvider>
    )
    const first = render(<PublishHub artifact={artifact} />, { wrapper: shared })

    fireEvent.click(await screen.findByText('Public Web'))
    await waitFor(() => expect(listCalls(calls)).toBe(1), SITE_LIST_WAIT)
    const publish = screen.getAllByRole('button')
      .find(b => b.textContent?.includes('Publish') && !b.textContent?.includes('Close'))
    fireEvent.click(publish!)
    await screen.findByText(/Ready to publish/)
    fireEvent.click(screen.getByText('Confirm & Publish'))
    fireEvent.click(await screen.findByRole('button', { name: 'I understand, publish publicly' }))
    await screen.findByText('Published!')
    // A first publish goes out with no profile: the backend picks its default.
    const deploys = calls.filter(c => c.url.includes('/api/deploy/deploy'))
    expect(deploys.every(d => d.body?.profile === undefined)).toBe(true)

    // The listing still answers with no site (its tags lag the deploy), yet the
    // reopened panel shows the new link and offers the update.
    first.unmount()
    render(<PublishHub artifact={artifact} />, { wrapper: shared })
    expect(await screen.findByText('Already published to the public web', {}, SITE_LIST_WAIT)).toBeDefined()
    expect(screen.getByRole('link', { name: /Open published page/ }).getAttribute('href')).toBe(LIVE_URL)
    expect(screen.getByRole('button', { name: /Update \(same URL\)/ })).toBeDefined()
    expect(listCalls(calls)).toBe(1)
  })

  it.each([
    ['a failed listing', 500, /list failed/],
    ['an account it could not read', { sites: [], configured: true, profile_errors: ['team: access denied'] }, /team: access denied/],
    ['a listing that cannot run on this host', { sites: [], configured: false, error: 'deploy features are not supported on Windows' }, /not supported on Windows/],
  ] as const)('says the check failed for %s, with the destinations still usable', async (_label, sites, detail) => {
    routeFetch(fetchSpy, { sites: sites as Route['sites'] })
    render(<PublishHub artifact={artifact} />, { wrapper })

    expect(await screen.findByText('Could not check for an existing public link', {}, SITE_LIST_WAIT)).toBeDefined()
    expect(screen.getByText(detail)).toBeDefined()
    expect(screen.getByText(/publishing here may not update that page/)).toBeDefined()
    expect(screen.getByText('Public Web')).toBeDefined()
  })

  it('checks again in place after a failed listing, and shows the link it then finds', async () => {
    const route: Route = { sites: 500 }
    const calls = routeFetch(fetchSpy, route)
    render(<PublishHub artifact={artifact} />, { wrapper })

    expect(await screen.findByText('Could not check for an existing public link', {}, SITE_LIST_WAIT)).toBeDefined()
    route.sites = { sites: [{ site_id: 'launch-notes', url: LIVE_URL, profile: 'team' }], configured: true }
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))

    expect(await screen.findByText('Already published to the public web', {}, SITE_LIST_WAIT)).toBeDefined()
    expect(screen.queryByText('Could not check for an existing public link')).toBeNull()
    expect(listCalls(calls)).toBe(2)
  })

  it('keeps the failure visible beside a link shown from an earlier read', async () => {
    const route: Route = { sites: { sites: [{ site_id: 'launch-notes', url: LIVE_URL, profile: 'team' }], configured: true } }
    routeFetch(fetchSpy, route)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter><PublishHub artifact={artifact} /></MemoryRouter>
      </QueryClientProvider>,
    )
    expect(await screen.findByText('Already published to the public web', {}, SITE_LIST_WAIT)).toBeDefined()

    // The next read of the listing fails; the earlier answer stays cached.
    route.sites = 500
    await act(() => qc.invalidateQueries({ queryKey: ['deploy-web', 'sites'] }))

    expect(await screen.findByText('Could not check for an existing public link', {}, SITE_LIST_WAIT)).toBeDefined()
    expect(screen.getByText('Already published to the public web')).toBeDefined()
    expect(screen.getByRole('button', { name: 'Check again' })).toBeDefined()
    // The link on screen is the page an update targets, so the may-not-update
    // warning is not shown.
    expect(screen.queryByText(/publishing here may not update that page/)).toBeNull()
  })

  it('says nothing failed when the listing answers cleanly', async () => {
    const calls = routeFetch(fetchSpy, { sites: { sites: [], configured: true } })
    render(<PublishHub artifact={artifact} />, { wrapper })

    await screen.findByText('Public Web')
    await waitFor(() => expect(listCalls(calls)).toBe(1), SITE_LIST_WAIT)
    await waitFor(() => expect(screen.queryByText(/Checking for an existing public link/)).toBeNull(), SITE_LIST_WAIT)
    expect(screen.queryByText('Could not check for an existing public link')).toBeNull()
  })

  it('says it is checking while the site list loads, with the destinations still usable', async () => {
    let release!: () => void
    const hold = new Promise<void>(r => { release = r })
    routeFetch(fetchSpy, {
      sites: { sites: [{ site_id: 'launch-notes', url: LIVE_URL }], configured: true },
      hold,
    })
    render(<PublishHub artifact={artifact} />, { wrapper })

    expect((await screen.findByRole('status', {}, SITE_LIST_WAIT)).textContent).toBe('Checking for an existing public link…')
    expect(screen.getByText('Public Web')).toBeDefined()

    release()
    await screen.findByText('Already published to the public web', {}, SITE_LIST_WAIT)
    expect(screen.queryByText('Checking for an existing public link…')).toBeNull()
  })

  it('keeps the other destinations listed beside the live link', async () => {
    routeFetch(fetchSpy, {
      appProviders: [
        publicWebRow(),
        { id: 'drive', label: 'Team Drive', icon: 'Upload', kinds: [], configured: true, setupRoute: '', endpoint: '/api/apps/drive/publish' },
      ],
      sites: { sites: [{ site_id: 'launch-notes', url: LIVE_URL }], configured: true },
    })
    render(<PublishHub artifact={artifact} />, { wrapper })

    await screen.findByText('Already published to the public web', {}, SITE_LIST_WAIT)
    expect(screen.getByText('Team Drive')).toBeDefined()
  })

  it('shows the site but no link when its URL is not http(s)', async () => {
    routeFetch(fetchSpy, {
      sites: { sites: [{ site_id: 'launch-notes', url: 'javascript:alert(1)' }], configured: true },
    })
    render(<PublishHub artifact={artifact} />, { wrapper })

    await screen.findByText('Already published to the public web', {}, SITE_LIST_WAIT)
    expect(screen.queryByRole('link', { name: /Open published page/ })).toBeNull()
    expect(screen.queryByText('javascript:alert(1)')).toBeNull()
    expect(screen.getByRole('button', { name: /Update \(same URL\)/ })).toBeDefined()
  })

  it.each([
    ['no site under this artifact', { sites: [{ site_id: 'someone-else', url: LIVE_URL }], configured: true }],
    ['an unconfigured deploy', { sites: [], configured: false }],
    ['a failed listing', 500],
  ] as const)('offers the destination list as it is for %s', async (_label, sites) => {
    const calls = routeFetch(fetchSpy, { sites: sites as Route['sites'] })
    render(<PublishHub artifact={artifact} />, { wrapper })

    await screen.findByText('Public Web')
    await waitFor(() => expect(listCalls(calls)).toBe(1), SITE_LIST_WAIT)
    await waitFor(() => expect(screen.queryByText(/Checking for an existing public link/)).toBeNull(), SITE_LIST_WAIT)
    expect(screen.queryByText('Already published to the public web')).toBeNull()
    expect(screen.queryByRole('button', { name: /Update \(same URL\)/ })).toBeNull()
  })

  it('does not look anything up while the public-web destination is not offered', async () => {
    localStorage.removeItem(PREVIEW_ARTIFACT_DEPLOY)
    const calls = routeFetch(fetchSpy, {
      appProviders: [
        publicWebRow(),
        { id: 'drive', label: 'Team Drive', icon: 'Upload', kinds: [], configured: true, setupRoute: '', endpoint: '/api/apps/drive/publish' },
      ],
    })
    render(<PublishHub artifact={artifact} />, { wrapper })

    await screen.findByText('Team Drive')
    expect(screen.queryByText('Public Web')).toBeNull()
    expect(listCalls(calls)).toBe(0)
  })

  it('does not look anything up while the public-web destination needs setup', async () => {
    const calls = routeFetch(fetchSpy, { appProviders: [publicWebRow({ configured: false })] })
    render(<PublishHub artifact={artifact} />, { wrapper })

    await screen.findByText('Public Web')
    expect(screen.getByText('Setup required')).toBeDefined()
    expect(listCalls(calls)).toBe(0)
  })
})
