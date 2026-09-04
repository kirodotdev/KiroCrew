// PublishHub — the outcome of a publish, across the two response shapes a
// provider endpoint returns.
//
// Two defects are pinned here, both of which presented as a SUCCESSFUL publish
// looking broken, or a FAILED one looking successful:
//
//  1. `POST /api/artifacts/{slug}/publish` answers with the serialized artifact
//     (a `publication` block), not `{url}`. The panel recognized neither, fell
//     through to `{url: ''}` and rendered its error branch with an UNDEFINED
//     message — a bare red icon, no text, on a publish that had succeeded.
//  2. The mirror-image lie: `publish_sync.publish()` treats the version push as
//     best-effort on a RE-publish, persisting the failure into
//     `publication.last_error` and returning normally, so the route answers 200
//     with stale remote content. That must not read as "Published!".
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { PublishHub, readPublishOutcome } from '../components/PublishHub'
import type { Artifact } from '../types'

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  )
}

const fakeArtifact: Artifact = {
  slug: 'test-app',
  name: 'Test App',
  kind: 'widget',
  description: '',
  content: '',
  version: 1,
  created_at: '',
  updated_at: '',
  tags: [],
}

const PROVIDER = {
  id: 'internal-registry',
  label: 'Internal registry',
  icon: 'Upload',
  kinds: [],
  configured: true,
  setupRoute: '/publisher',
  endpoint: '/api/apps/publisher/publish',
}

function providersResponse() {
  return new Response(JSON.stringify({ providers: [PROVIDER] }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function previewResponse() {
  return new Response(
    JSON.stringify({
      requires_confirm: true,
      message: 'Publishes this artifact as PRIVATE.',
      content_digest: 'abc123',
    }),
    { status: 200, headers: { 'Content-Type': 'application/json' } },
  )
}

/** The serialized artifact the core publish route returns on success. */
function artifactPublishResponse(viewUrl: string | null = 'https://registry.internal.example/view/1') {
  return new Response(
    JSON.stringify({
      slug: 'test-app',
      name: 'Test App',
      kind: 'widget',
      version: 1,
      publication: {
        provider: 'internal-registry',
        visibility: 'PRIVATE',
        ...(viewUrl === null ? {} : { view_url: viewUrl }),
      },
    }),
    { status: 200, headers: { 'Content-Type': 'application/json' } },
  )
}

/** Drive the panel to the confirm step, then through the acknowledgment. */
async function reachConfirmStep(fetchSpy: ReturnType<typeof vi.spyOn>) {
  fetchSpy.mockImplementationOnce(async () => providersResponse())
  render(<PublishHub artifact={fakeArtifact} />, { wrapper })
  await waitFor(() => expect(screen.getByText('Internal registry')).toBeDefined())
  fireEvent.click(screen.getByText('Internal registry'))

  fetchSpy.mockImplementationOnce(async () => previewResponse())
  const publishBtn = screen
    .getAllByRole('button')
    .find(b => b.textContent?.includes('Publish') && !b.textContent?.includes('Close'))
  fireEvent.click(publishBtn!)
  await waitFor(() => expect(screen.getByText(/Confirm & Publish/)).toBeDefined())
  return screen.getByText(/Confirm & Publish/).closest('button')!
}

/** Click Confirm, then acknowledge the blocking public-exposure modal. */
async function commitThroughAck(confirmBtn: HTMLElement) {
  fireEvent.click(confirmBtn)
  await waitFor(() => expect(screen.getByText(/I understand, publish publicly/)).toBeDefined())
  fireEvent.click(screen.getByText(/I understand, publish publicly/))
}

describe('readPublishOutcome', () => {
  it('accepts both the deploy shape and the artifact shape', () => {
    expect(readPublishOutcome({ url: 'https://a/b' })).toEqual({ url: 'https://a/b' })
    expect(readPublishOutcome({ public_url: 'https://a/c' })).toEqual({ url: 'https://a/c' })
    expect(readPublishOutcome({ publication: { view_url: 'https://a/d' } })).toEqual({
      url: 'https://a/d',
    })
    // Published, but the destination exposes no browsable URL: success WITHOUT a
    // link. Callers must not infer success from a non-empty url.
    expect(readPublishOutcome({ publication: { provider: 'x' } })).toEqual({ url: '' })
  })

  it('reports a persisted push failure as an error, not a 200 success', () => {
    expect(
      readPublishOutcome({
        publication: { view_url: 'https://a/d', last_error: 'sync failed: 403 from provider' },
      }),
    ).toEqual({ error: 'sync failed: 403 from provider' })
    // Whitespace-only is not a failure — the core writes "" to clear it.
    expect(
      readPublishOutcome({ publication: { view_url: 'https://a/d', last_error: '  ' } }),
    ).toEqual({ url: 'https://a/d' })
  })

  it('carries a notice ALONGSIDE success, never folded into an error', () => {
    // notice = published, link not usable yet (CloudFront rolling out). Success.
    expect(
      readPublishOutcome({
        publication: { view_url: 'https://a/d', notice: 'still rolling out' },
      }),
    ).toEqual({ url: 'https://a/d', notice: 'still rolling out' })
    // A real error still wins over a notice — last_error is checked first.
    expect(
      readPublishOutcome({
        publication: { view_url: 'https://a/d', notice: 'still rolling out', last_error: 'push failed' },
      }),
    ).toEqual({ error: 'push failed' })
    // Whitespace-only notice is dropped, leaving a plain success.
    expect(
      readPublishOutcome({ publication: { view_url: 'https://a/d', notice: '  ' } }),
    ).toEqual({ url: 'https://a/d' })
  })

  it('rejects everything that is not an outcome', () => {
    expect(readPublishOutcome(null)).toBeNull()
    expect(readPublishOutcome(undefined)).toBeNull()
    expect(readPublishOutcome({})).toBeNull()
    expect(readPublishOutcome({ error: 'nope' })).toBeNull()
    expect(readPublishOutcome({ url: '' })).toBeNull()
    // An UNpublished artifact carries publication: null — not a publish success.
    expect(readPublishOutcome({ publication: null })).toBeNull()
  })
})

describe('PublishHub — publish outcome rendering', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    // `vi.spyOn` on an already-spied `fetch` hands back the SAME spy, so without
    // a restore the recorded calls accumulate across tests.
    vi.restoreAllMocks()
    fetchSpy = vi.spyOn(globalThis, 'fetch')
  })

  it('renders the artifact-shaped success as Published, with the view link', async () => {
    const confirmBtn = await reachConfirmStep(fetchSpy)
    fetchSpy.mockImplementationOnce(async () => artifactPublishResponse())
    await commitThroughAck(confirmBtn)

    await waitFor(() => expect(screen.getByText(/Published!/)).toBeDefined())
    expect(screen.getByText('https://registry.internal.example/view/1')).toBeDefined()
    // The regression: this shape used to render the error branch with no message.
    expect(screen.queryByText(/Unexpected response/i)).toBeNull()
  })

  it('reports success even when the destination exposes no view URL', async () => {
    const confirmBtn = await reachConfirmStep(fetchSpy)
    fetchSpy.mockImplementationOnce(async () => artifactPublishResponse(null))
    await commitThroughAck(confirmBtn)

    await waitFor(() => expect(screen.getByText(/Published!/)).toBeDefined())
  })

  it('surfaces a 200 whose publication carries last_error as an error', async () => {
    const confirmBtn = await reachConfirmStep(fetchSpy)
    fetchSpy.mockImplementationOnce(async () =>
      new Response(
        JSON.stringify({
          slug: 'test-app',
          publication: {
            provider: 'internal-registry',
            view_url: 'https://registry.internal.example/view/1',
            last_error: 'sync failed: destination rejected the push',
          },
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    await commitThroughAck(confirmBtn)

    await waitFor(() =>
      expect(screen.getByText(/sync failed: destination rejected the push/)).toBeDefined(),
    )
    expect(screen.queryByText(/Published!/)).toBeNull()
  })

  it('reports an UNRECOGNIZED response as a named error, never a blank one', async () => {
    const confirmBtn = await reachConfirmStep(fetchSpy)
    fetchSpy.mockImplementationOnce(async () =>
      new Response(JSON.stringify({ something: 'else' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    await commitThroughAck(confirmBtn)

    // The failure being pinned is an EMPTY error line, so assert on text.
    await waitFor(() => expect(screen.getByText(/Unexpected response/i)).toBeDefined())
  })

  it('treats an error response as authoritative even beside a publication block', async () => {
    const confirmBtn = await reachConfirmStep(fetchSpy)
    fetchSpy.mockImplementationOnce(async () =>
      new Response(
        JSON.stringify({ error: 'publishing to this destination is not permitted', publication: {} }),
        { status: 403, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    await commitThroughAck(confirmBtn)

    await waitFor(() =>
      expect(screen.getByText(/publishing to this destination is not permitted/)).toBeDefined(),
    )
    expect(screen.queryByText(/Published!/)).toBeNull()
  })

  it('still shows the public-exposure warning and the blocking acknowledgment', async () => {
    // Unchanged by this PR, and asserted so a later change cannot drop the
    // safeguard while only the outcome reader is under test.
    const confirmBtn = await reachConfirmStep(fetchSpy)
    expect(screen.getByText(/Anyone with the published link can view this content/)).toBeDefined()

    fireEvent.click(confirmBtn)
    await waitFor(() => expect(screen.getByText(/I understand, publish publicly/)).toBeDefined())
    const confirmed = fetchSpy.mock.calls.filter(c => {
      const init = c[1] as RequestInit | undefined
      if (!init?.body) return false
      return JSON.parse(String(init.body)).confirm === true
    })
    expect(confirmed).toHaveLength(0)
  })
})
