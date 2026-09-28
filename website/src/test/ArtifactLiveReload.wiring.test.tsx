/**
 * Wiring coverage for file-backed artifact live-reload.
 *
 * The backend re-reads a file-backed artifact's `source_path` on every GET, but
 * both artifact surfaces fetched once per mount and the shipped QueryClient
 * never expires a query on its own — so an agent rewriting the backing file left
 * the open surface rendering pre-edit content until it was closed and reopened.
 * `useArtifactLiveReload` must be mounted on BOTH surfaces (the detail page,
 * which also backs /popout/artifact/:slug, and the side-panel Artifacts tab) and
 * must actively refetch, not merely mark the query stale.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import ArtifactPanel from '../components/ArtifactPanel'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import type { Artifact } from '../types'

vi.mock('../api/client')
// Stub the embedded chat page — covered by its own suites.
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))
// The real bodies lazily load Monaco / navigate a blob iframe; neither belongs
// in a test of which content the surface chose to render. The module's pure
// helpers (isEditableKind, artifactAssetUrl, …) are kept — both surfaces call
// them during render.
vi.mock('../components/ArtifactBody', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../components/ArtifactBody')>()),
  ArtifactBodyNative: ({ content }: { content: string }) => (
    <div data-testid="body-native">{content}</div>
  ),
  ArtifactBodyIframe: () => <div data-testid="body-iframe" />,
  ArtifactBodyImage: () => <div data-testid="body-image" />,
}))

const SLUG = 'flow-report'
const SOURCE_PATH = '/repo/out/flow.html'
const BEFORE = '# before the rebake'
const AFTER = '# after the rebake'
/** Comfortably past the hook's 400ms debounce, for the post-signal assertions. */
const WAIT_MS = 3_000

// Minimal controllable EventSource, same seam as useFileWatch.test.ts.
class MockEventSource {
  static instances: MockEventSource[] = []
  url: string
  closed = false
  onopen: (() => void) | null = null
  onmessage: ((ev: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  constructor(url: string) {
    this.url = url
    MockEventSource.instances.push(this)
  }
  close() {
    this.closed = true
  }
}

const watchStreams = () =>
  MockEventSource.instances.filter((es) => es.url.startsWith('/api/file-watch?'))

/**
 * Fire one watch frame. Real timers on purpose: the surfaces under test mount a
 * query graph whose settling RTL drives through `waitFor`, and swapping in fake
 * timers there fights that. The hook's debounce is short enough to simply wait
 * out — the `waitFor` deadline below covers it.
 */
async function signalFileChange(): Promise<void> {
  const stream = watchStreams()[0]
  await act(async () => {
    stream.onmessage?.({ data: JSON.stringify({ content: AFTER, mtime: 2 }) })
  })
}

const mkArtifact = (overrides: Partial<Artifact> = {}): Artifact => ({
  slug: SLUG,
  name: 'Flow Report',
  kind: 'markdown',
  source: 'chat',
  description: '',
  tags: [],
  version: 1,
  created_at: '2026-06-01T00:00:00Z',
  updated_at: '2026-06-01T01:00:00Z',
  content: BEFORE,
  source_path: SOURCE_PATH,
  ...overrides,
})

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('EventSource', MockEventSource as unknown as typeof EventSource)
  vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
  vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: SLUG, versions: [1] })
  vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: SLUG, events: [] })
  vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
  vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
  vi.mocked(api.sandboxDocUrl).mockResolvedValue({ url: '/sandbox-doc/test/tok' })
  // Boot queries that ride along on the provider tree / page chrome; resolving
  // them keeps React Query's "data cannot be undefined" out of stderr.
  vi.mocked(api.themeBoot).mockResolvedValue({} as Awaited<ReturnType<typeof api.themeBoot>>)
  vi.mocked(api).artifactFolders = vi.fn().mockResolvedValue({ folders: [] })
})

afterEach(() => {
  MockEventSource.instances = []
  vi.unstubAllGlobals()
})

describe('ArtifactDetailPage live-reload', () => {
  function renderPage() {
    return renderWithProviders(
      <Routes>
        <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
      </Routes>,
      { route: `/artifacts/${SLUG}` },
    )
  }

  it('refetches and repaints when the backing file changes on disk', async () => {
    renderPage()
    expect(await screen.findByTestId('body-native')).toHaveTextContent(BEFORE)
    // The live pointer — not the artifact slug — is what gets watched.
    await waitFor(() => expect(watchStreams()).toHaveLength(1))
    expect(watchStreams()[0].url).toBe(
      '/api/file-watch?path=' + encodeURIComponent(SOURCE_PATH),
    )
    expect(api.artifact).toHaveBeenCalledTimes(1)

    vi.mocked(api.artifact).mockResolvedValue(mkArtifact({ content: AFTER }))
    await signalFileChange()

    // Actively refetched (not just marked stale — the shipped client's
    // staleTime: Infinity would repaint nothing) and the new content is on screen.
    await waitFor(() => expect(api.artifact).toHaveBeenCalledTimes(2), { timeout: WAIT_MS })
    await waitFor(
      () => expect(screen.getByTestId('body-native')).toHaveTextContent(AFTER),
      { timeout: WAIT_MS },
    )
  })

  it('opens no watch stream for an artifact that is not file-backed', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ source_path: undefined }))
    renderPage()
    expect(await screen.findByTestId('body-native')).toHaveTextContent(BEFORE)
    expect(watchStreams()).toHaveLength(0)
  })
})

describe('ArtifactPanel live-reload', () => {
  it('refetches and repaints when the backing file changes on disk', async () => {
    renderWithProviders(
      <Routes>
        <Route
          path="/"
          element={<ArtifactPanel slug={SLUG} kind="markdown" content={BEFORE} onClose={vi.fn()} />}
        />
      </Routes>,
    )
    expect(await screen.findByTestId('body-native')).toHaveTextContent(BEFORE)
    await waitFor(() => expect(watchStreams()).toHaveLength(1))
    expect(watchStreams()[0].url).toBe(
      '/api/file-watch?path=' + encodeURIComponent(SOURCE_PATH),
    )

    const callsBefore = vi.mocked(api.artifact).mock.calls.length
    vi.mocked(api.artifact).mockResolvedValue(mkArtifact({ content: AFTER }))
    await signalFileChange()

    await waitFor(
      () => expect(vi.mocked(api.artifact).mock.calls.length).toBeGreaterThan(callsBefore),
      { timeout: WAIT_MS },
    )
    await waitFor(
      () => expect(screen.getByTestId('body-native')).toHaveTextContent(AFTER),
      { timeout: WAIT_MS },
    )
  })
})
