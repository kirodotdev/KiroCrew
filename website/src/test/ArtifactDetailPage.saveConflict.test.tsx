/**
 * Optimistic-concurrency save path (#7751): the detail page's Save carries the
 * `expected_sha256` token from its last fetch, and a 409 renders as a
 * changed-on-disk notice that PRESERVES the user's buffer instead of silently
 * clobbering the other writer.
 *
 * Pierre is stubbed with a typable textarea (the MarkdownPanelCoverage
 * pattern) because the real editor cannot be driven under jsdom — these tests
 * need a genuinely dirty buffer, which the main suite deliberately avoids.
 */
import { forwardRef, useImperativeHandle } from 'react'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import type { PierreEditorHandle } from '../pierre'
import type { Artifact } from '../types'

vi.mock('../api/client')
// The embedded companion chat is covered by its own suites.
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))
// Typable editor stub: emits the CodeEditor onChange the way Pierre would.
vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreEditor: forwardRef<
    PierreEditorHandle,
    { file: { contents: string }; onChange?: (v: string) => void }
  >(function PierreEditorStub({ file, onChange }, ref) {
    useImperativeHandle(ref, () => ({ jumpToLine: () => {}, focus: () => {} }) as unknown as PierreEditorHandle, [])
    // Controlled, not defaultValue: the "buffer survives" assertions must
    // FAIL if a future change re-seeds editedContent during the 409 path —
    // an uncontrolled textarea would keep showing the user's keystrokes
    // regardless and mask exactly the regression this suite exists to catch.
    return (
      <textarea
        data-testid="editor-stub"
        aria-label="editor stub"
        value={file.contents}
        onChange={e => onChange?.(e.target.value)}
      />
    )
  }),
}))

const SHA_V1 = 'a'.repeat(64)
const SHA_LIVE = 'b'.repeat(64)

const mkArtifact = (overrides: Partial<Artifact> = {}): Artifact => ({
  slug: 'cr-queue',
  name: 'CR Queue',
  kind: 'markdown',
  source: 'chat',
  description: '',
  tags: [],
  version: 1,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:30:00.000000+00:00',
  content: '# v1',
  content_sha256: SHA_V1,
  ...overrides,
})

function renderRoute() {
  return renderWithProviders(
    <Routes>
      <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
    </Routes>,
    { route: '/artifacts/cr-queue' },
  )
}

async function editAndDirty() {
  renderRoute()
  await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
  fireEvent.click(screen.getByTitle('Edit content'))
  const editor = await screen.findByTestId('editor-stub')
  fireEvent.change(editor, { target: { value: '# v1 edited' } })
}

beforeEach(() => {
  vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
  vi.mocked(api).artifactVersions = vi
    .fn()
    .mockResolvedValue({ slug: 'cr-queue', versions: [1] })
  vi.mocked(api.sandboxDocUrl).mockResolvedValue({ url: '/sandbox-doc/test/tok' })
})

describe('save conflict token', () => {
  it('Save sends the expected_sha256 token from the last fetch', async () => {
    const updateSpy = vi.fn().mockResolvedValue(mkArtifact({ content: '# v1 edited' }))
    vi.mocked(api).updateArtifact = updateSpy
    await editAndDirty()
    fireEvent.keyDown(document, { key: 's', metaKey: true })
    await waitFor(() =>
      expect(updateSpy).toHaveBeenCalledWith('cr-queue', {
        content: '# v1 edited',
        snapshot: false,
        expected_sha256: SHA_V1,
      }),
    )
  })

  it('409 shows the changed-on-disk notice and keeps the buffer', async () => {
    vi.mocked(api).updateArtifact = vi.fn().mockRejectedValue(
      new ApiError(
        409,
        'artifact content changed since it was read',
        JSON.stringify({ error: 'conflict', current_sha256: SHA_LIVE, version: 1 }),
      ),
    )
    await editAndDirty()
    fireEvent.keyDown(document, { key: 's', metaKey: true })
    // The specific conflict notice, not the generic error passthrough.
    await waitFor(() =>
      expect(
        screen.getByText(/Content changed since you loaded it/),
      ).toBeInTheDocument(),
    )
    // The 409 gets its own title (protection, not a fault) and an affordance
    // to inspect the newer content without leaving the edit buffer.
    expect(screen.getByText(/Save refused — content changed/)).toBeInTheDocument()
    expect(screen.getByText(/View the newer content/)).toBeInTheDocument()
    // The buffer survives: still editing, user text intact.
    expect(screen.getByTestId('editor-stub')).toHaveValue('# v1 edited')
    // The page re-based: the artifact was refetched for the viewer.
    expect(vi.mocked(api.artifact).mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('the token is held from edit start and rebased only from the 409 body', async () => {
    // The decisive distinction: the artifact QUERY keeps returning the stale
    // SHA_V1 throughout (as any background refetch would mid-edit), so if the
    // save read its token from the query it would send SHA_V1 twice. The
    // held-token model instead sends SHA_V1 first, then — after a 409 whose
    // body names SHA_LIVE — sends SHA_LIVE, the one token refresh the user
    // has been shown a banner for.
    const updateSpy = vi
      .fn()
      .mockRejectedValueOnce(
        new ApiError(
          409,
          'conflict',
          JSON.stringify({ error: 'conflict', current_sha256: SHA_LIVE, version: 1 }),
        ),
      )
      .mockResolvedValue(mkArtifact({ content: '# v1 edited' }))
    vi.mocked(api).updateArtifact = updateSpy
    await editAndDirty()
    fireEvent.keyDown(document, { key: 's', metaKey: true })
    await waitFor(() =>
      expect(screen.getByText(/Content changed since you loaded it/)).toBeInTheDocument(),
    )
    expect(updateSpy).toHaveBeenNthCalledWith(1, 'cr-queue', expect.objectContaining({
      expected_sha256: SHA_V1,
    }))
    fireEvent.keyDown(document, { key: 's', metaKey: true })
    await waitFor(() => expect(updateSpy).toHaveBeenCalledTimes(2))
    expect(updateSpy).toHaveBeenNthCalledWith(2, 'cr-queue', expect.objectContaining({
      expected_sha256: SHA_LIVE,
    }))
  })

  it('non-409 errors keep the generic error message path', async () => {
    vi.mocked(api).updateArtifact = vi
      .fn()
      .mockRejectedValue(new ApiError(500, 'disk full'))
    await editAndDirty()
    fireEvent.keyDown(document, { key: 's', metaKey: true })
    await waitFor(() => expect(screen.getByText(/disk full/)).toBeInTheDocument())
    expect(screen.queryByText(/Content changed since you loaded it/)).toBeNull()
  })
})
