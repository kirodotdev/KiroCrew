/**
 * Optimistic-concurrency save path (#7751): the detail page's Save carries the
 * `expected_token` token from its last fetch, and a 409 renders as a
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

const TOKEN_V1 = 'a'.repeat(64)
const TOKEN_LIVE = 'b'.repeat(64)

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
  content_token: TOKEN_V1,
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
  it('Save sends the expected_token token from the last fetch', async () => {
    const updateSpy = vi.fn().mockResolvedValue(mkArtifact({ content: '# v1 edited' }))
    vi.mocked(api).updateArtifact = updateSpy
    await editAndDirty()
    fireEvent.keyDown(document, { key: 's', metaKey: true })
    await waitFor(() =>
      expect(updateSpy).toHaveBeenCalledWith('cr-queue', {
        content: '# v1 edited',
        snapshot: false,
        expected_token: TOKEN_V1,
      }),
    )
  })

  it('409 shows the changed-on-disk notice and keeps the buffer', async () => {
    vi.mocked(api).updateArtifact = vi.fn().mockRejectedValue(
      new ApiError(
        409,
        'artifact content changed since it was read',
        JSON.stringify({ error: 'conflict', current_token: TOKEN_LIVE, version: 1 }),
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
    // The Save button now performs an informed overwrite (the token was
    // rebased onto the newer content), so its label must say so while the
    // banner is up — a reflex second Cmd+S reads as the overwrite it is.
    expect(
      screen.getByRole('button', { name: /Save — overwrite newer content/ }),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Save$/ })).not.toBeInTheDocument()
    // The buffer survives: still editing, user text intact.
    expect(screen.getByTestId('editor-stub')).toHaveValue('# v1 edited')
    // The page re-based: the artifact was refetched for the viewer.
    expect(vi.mocked(api.artifact).mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('a metadata edit after the 409 keeps the warning and the overwrite label', async () => {
    // The token was rebased when the 409 arrived, so Save overwrites until
    // the edit ends. A tag edit in between must not clear the warning: the
    // conflict state is separate from the generic save error a metadata
    // action resets for its own message.
    const updateSpy = vi
      .fn()
      .mockRejectedValueOnce(
        new ApiError(
          409,
          'conflict',
          JSON.stringify({ error: 'conflict', current_token: TOKEN_LIVE, version: 1 }),
        ),
      )
      .mockResolvedValue(mkArtifact({ tags: ['later'] }))
    vi.mocked(api).updateArtifact = updateSpy
    await editAndDirty()
    fireEvent.keyDown(document, { key: 's', metaKey: true })
    await waitFor(() =>
      expect(screen.getByText(/Content changed since you loaded it/)).toBeInTheDocument(),
    )
    // Add a tag (a metadata-only update) while the conflict is showing.
    fireEvent.click(screen.getByRole('button', { name: /Add a tag/ }))
    const input = screen.getByLabelText(/Add a tag/)
    fireEvent.change(input, { target: { value: 'later' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() =>
      expect(updateSpy).toHaveBeenLastCalledWith('cr-queue', expect.objectContaining({ tags: ['later'] })),
    )
    // Warning, action and label all survive the metadata edit.
    expect(screen.getByText(/Content changed since you loaded it/)).toBeInTheDocument()
    expect(screen.getByText(/View the newer content/)).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /Save — overwrite newer content/ }),
    ).toBeInTheDocument()
    expect(screen.getByTestId('editor-stub')).toHaveValue('# v1 edited')
  })

  it('the token is held from edit start and rebased only from the 409 body', async () => {
    // The decisive distinction: the artifact QUERY keeps returning the stale
    // TOKEN_V1 throughout (as any background refetch would mid-edit), so if the
    // save read its token from the query it would send TOKEN_V1 twice. The
    // held-token model instead sends TOKEN_V1 first, then — after a 409 whose
    // body names TOKEN_LIVE — sends TOKEN_LIVE, the one token refresh the user
    // has been shown a banner for.
    const updateSpy = vi
      .fn()
      .mockRejectedValueOnce(
        new ApiError(
          409,
          'conflict',
          JSON.stringify({ error: 'conflict', current_token: TOKEN_LIVE, version: 1 }),
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
      expected_token: TOKEN_V1,
    }))
    fireEvent.keyDown(document, { key: 's', metaKey: true })
    await waitFor(() => expect(updateSpy).toHaveBeenCalledTimes(2))
    expect(updateSpy).toHaveBeenNthCalledWith(2, 'cr-queue', expect.objectContaining({
      expected_token: TOKEN_LIVE,
    }))
  })

  it('a held Cmd+S never saves through the 409-rebased token on its key-repeat', async () => {
    // The OS auto-repeats keydown while the key is held. The first keydown's
    // save comes back 409 and rebases the held token to TOKEN_LIVE so that the
    // NEXT save is an informed overwrite -- and the repeat keydown, still part
    // of the same hold, would be that next save: an overwrite of content the
    // user has not seen. Repeats must not save at all; only one PATCH is made,
    // and the banner is what the user sees.
    const updateSpy = vi
      .fn()
      .mockRejectedValueOnce(
        new ApiError(
          409,
          'conflict',
          JSON.stringify({ error: 'conflict', current_token: TOKEN_LIVE, version: 1 }),
        ),
      )
      .mockResolvedValue(mkArtifact({ content: '# v1 edited' }))
    vi.mocked(api).updateArtifact = updateSpy
    await editAndDirty()
    fireEvent.keyDown(document, { key: 's', metaKey: true })
    await waitFor(() =>
      expect(screen.getByText(/Content changed since you loaded it/)).toBeInTheDocument(),
    )
    fireEvent.keyDown(document, { key: 's', metaKey: true, repeat: true })
    fireEvent.keyDown(document, { key: 's', metaKey: true, repeat: true })
    // Give any wrongly-issued save a chance to land before asserting.
    await new Promise(r => setTimeout(r, 50))
    expect(updateSpy).toHaveBeenCalledTimes(1)
    expect(screen.getByText(/Content changed since you loaded it/)).toBeInTheDocument()
  })

  it('a second save while one is in flight is dropped, not queued behind a rebased token', async () => {
    // A click or a distinct keystroke landing while the first PATCH is still
    // pending. If it were allowed to run, it would read whatever token the
    // first save leaves behind; after a 409 that is the live token and the
    // second save silently overwrites. The in-flight guard drops it instead.
    let settle!: (v: unknown) => void
    const pending = new Promise(r => { settle = r })
    const updateSpy = vi
      .fn()
      .mockImplementationOnce(() => pending.then(() => {
        throw new ApiError(
          409,
          'conflict',
          JSON.stringify({ error: 'conflict', current_token: TOKEN_LIVE, version: 1 }),
        )
      }))
      .mockResolvedValue(mkArtifact({ content: '# v1 edited' }))
    vi.mocked(api).updateArtifact = updateSpy
    await editAndDirty()
    fireEvent.keyDown(document, { key: 's', metaKey: true })
    await waitFor(() => expect(updateSpy).toHaveBeenCalledTimes(1))
    // A second, distinct (non-repeat) Cmd+S while the first is still pending.
    fireEvent.keyDown(document, { key: 's', metaKey: true })
    settle(undefined)
    await waitFor(() =>
      expect(screen.getByText(/Content changed since you loaded it/)).toBeInTheDocument(),
    )
    await new Promise(r => setTimeout(r, 50))
    expect(updateSpy).toHaveBeenCalledTimes(1)
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

  it('a sync action flushes the buffer under the same guard and aborts on 409', async () => {
    // The Snapshot-to-publish action first flushes the dirty buffer. That
    // flush is a content write from the edit session, so it carries the held
    // token; a 409 keeps the buffer, shows the conflict notice, and the
    // snapshot itself never runs (it would have versioned the other writer's
    // content under the user's name).
    const published = mkArtifact({
      live_dirty: true,
      publication: {
        artifact_id: 'pub-1',
        view_url: 'https://example.test/artifact/pub-1',
        provider: 'stub',
        visibility: 'PRIVATE',
        shared_with: [],
        auto_sync: false,
        last_synced_kirocrew_version: 1,
        version_map: { '1': 1 },
        published_at: '2026-05-21T22:00:00.000000+00:00',
        published_by: 'me',
        last_error: '',
        notice: '',
      },
    })
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(published)
    vi.mocked(api).getArtifactPublishProviders = vi.fn().mockResolvedValue({
      kind: 'markdown',
      providers: [
        {
          name: 'stub',
          display_name: 'Stub',
          capabilities: [],
          kind_support: 'native',
          capable: true,
        },
      ],
    })
    vi.mocked(api).upstreamStatus = vi.fn().mockResolvedValue({ upstream_ahead: false })
    const updateSpy = vi.fn().mockRejectedValue(
      new ApiError(
        409,
        'artifact content changed since it was read',
        JSON.stringify({ error: 'conflict', current_token: TOKEN_LIVE, version: 1 }),
      ),
    )
    vi.mocked(api).updateArtifact = updateSpy
    await editAndDirty()
    fireEvent.click(await screen.findByRole('button', { name: /Snapshot to publish/ }))
    await waitFor(() =>
      expect(screen.getByText(/Content changed since you loaded it/)).toBeInTheDocument(),
    )
    // The sync banner says why its action did not run (short form; the edit
    // bar's notice carries the explanation).
    expect(screen.getAllByText(/Save refused — content changed/).length).toBeGreaterThanOrEqual(2)
    // Exactly one call: the guarded flush. The snapshot=true write never ran.
    expect(updateSpy).toHaveBeenCalledTimes(1)
    expect(updateSpy).toHaveBeenCalledWith('cr-queue', {
      content: '# v1 edited',
      snapshot: false,
      expected_token: TOKEN_V1,
    })
    // Buffer intact, still editing.
    expect(screen.getByTestId('editor-stub')).toHaveValue('# v1 edited')
  })
})
