/**
 * Stale-write guard on the detail page (#7751): a content save carries the
 * `content_token` the edit started from, and a 409 keeps the draft and turns
 * only the explicit Save into a deliberate overwrite on the live token.
 *
 * Pierre is stubbed with a controlled textarea because the real editor cannot be
 * driven under jsdom; controlled so a regression that re-seeds the buffer fails.
 */
import { forwardRef, useImperativeHandle } from 'react'
import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import { Routes, Route, useNavigate } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import type { PierreEditorHandle } from '../pierre'
import type { Artifact } from '../types'

vi.mock('../api/client')
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))
vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreEditor: forwardRef<
    PierreEditorHandle,
    { file: { contents: string }; onChange?: (v: string) => void }
  >(function PierreEditorStub({ file, onChange }, ref) {
    useImperativeHandle(ref, () => ({ jumpToLine: () => {}, focus: () => {} }) as unknown as PierreEditorHandle, [])
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
const TOKEN_REFETCHED = 'b'.repeat(64)
const TOKEN_LIVE = 'c'.repeat(64)
const TOKEN_SAVED = 'd'.repeat(64)
const TOKEN_B = 'e'.repeat(64)

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

const conflict = () =>
  new ApiError(
    409,
    'artifact changed since it was read',
    JSON.stringify({ error: 'conflict', code: 'artifact_conflict', current_token: TOKEN_LIVE }),
  )

async function editAndDirty() {
  const view = renderWithProviders(
    <Routes>
      <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
    </Routes>,
    { route: '/artifacts/cr-queue' },
  )
  await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
  fireEvent.click(screen.getByTitle('Edit content'))
  const editor = await screen.findByTestId('editor-stub')
  fireEvent.change(editor, { target: { value: '# v1 edited' } })
  return view
}

/** Pick `label` in the Version selector, confirming the discard prompt when asked. */
async function pickVersion(label: string, { discard = false } = {}) {
  fireEvent.click(await screen.findByRole('combobox', { name: /Version/i }))
  fireEvent.click(await screen.findByRole('option', { name: label }))
  if (discard) {
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Discard changes' }))
  }
}

const saveCalls = () => vi.mocked(api.updateArtifact).mock.calls.map(([, body]) => body)

const FORK_METADATA = {
  upstream_artifact_id: 'up-1',
  upstream_url: 'https://remote.example.com/a/up-1',
  upstream_owner: 'alice',
  upstream_version: 3,
  forked_at: '2026-06-01T00:00:00Z',
}

/** Render the fork banner: a forked artifact plus one registered publish provider. */
function withForkBanner() {
  vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ fork_metadata: FORK_METADATA }))
  vi.mocked(api).getArtifactPublishProviders = vi.fn().mockResolvedValue({
    providers: [{
      name: 'companion', display_name: 'Companion', capabilities: ['content_versions'],
      kind_support: 'native', capable: true,
      sharing_model: {
        supports_private: true, supports_shared: true, supports_public: true,
        principal_kind: 'user', supports_roles: false, supports_expiration: false,
        programmable: true, out_of_band_url: '',
      },
      sync_model: { authority: 'mirror', concurrency: 'token', collab_mode: 'mirror' },
      discovery_model: {
        list_mine: true, list_shared_with_me: true, list_public: true,
        full_text_search: false, pull_by_id: true,
      },
    }],
    kind: 'markdown',
  })
  vi.mocked(api).upstreamStatus = vi.fn().mockResolvedValue({ upstream_ahead: false })
  vi.mocked(api).pullLatest = vi.fn().mockResolvedValue({ pull_result: { pulled: true } })
}

/** A write that stays on the wire until `settle` or `refuse` is called. */
function pendingWrite() {
  let settle!: (a: Artifact) => void
  let refuse!: (err: unknown) => void
  const write = new Promise<Artifact>((resolve, reject) => {
    settle = resolve
    refuse = reject
  })
  return { write, settle: (a: Artifact) => settle(a), refuse: (err: unknown) => refuse(err) }
}

function SwitchArtifact({ to }: { to: string }) {
  const navigate = useNavigate()
  return <button type="button" onClick={() => navigate(`/artifacts/${to}`)}>go to {to}</button>
}

function renderCrossArtifactSave(write: Promise<Artifact>) {
  vi.mocked(api.artifact).mockImplementation(async artifactSlug => (
    artifactSlug === 'file-a'
      ? mkArtifact({
          slug: 'file-a',
          name: 'File A',
          content: '# file a',
          content_token: undefined,
        })
      : mkArtifact({
          slug: 'store-b',
          name: 'Store B',
          content: '# store b',
          content_token: TOKEN_B,
        })
  ))
  vi.mocked(api.artifactVersions).mockImplementation(async artifactSlug => ({
    slug: artifactSlug,
    versions: [1],
  }))
  vi.mocked(api.artifactEvents).mockImplementation(async artifactSlug => ({
    slug: artifactSlug,
    events: [],
  }))
  vi.mocked(api).updateArtifact = vi
    .fn()
    .mockReturnValueOnce(write)
    .mockResolvedValue(mkArtifact({
      slug: 'store-b',
      name: 'Store B',
      content: '# store b edited',
      content_token: TOKEN_SAVED,
    }))
  renderWithProviders(
    <>
      <SwitchArtifact to="store-b" />
      <Routes>
        <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
      </Routes>
    </>,
    { route: '/artifacts/file-a' },
  )
}

async function beginCrossArtifactSave() {
  await screen.findByText('File A')
  fireEvent.click(screen.getByTitle('Edit content'))
  fireEvent.change(await screen.findByTestId('editor-stub'), { target: { value: '# file a edited' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(saveCalls()).toHaveLength(1))

  fireEvent.click(screen.getByRole('button', { name: 'go to store-b' }))
  await screen.findByText('Store B')
  fireEvent.click(screen.getByTitle('Edit content'))
  fireEvent.change(await screen.findByTestId('editor-stub'), { target: { value: '# store b edited' } })
}

beforeEach(() => {
  vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
  vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'cr-queue', versions: [1] })
  vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: 'cr-queue', events: [] })
  vi.mocked(api.sandboxDocUrl).mockResolvedValue({ url: '/sandbox-doc/test/tok' })
})

describe('stale-write guard', () => {
  it('a late tokenless save from another artifact does not clear the current edit token', async () => {
    const first = pendingWrite()
    renderCrossArtifactSave(first.write)
    await beginCrossArtifactSave()

    first.settle(mkArtifact({
      slug: 'file-a',
      name: 'File A',
      content: '# file a edited',
      content_token: undefined,
    }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(saveCalls()).toHaveLength(2))
    expect(saveCalls()[1]).toEqual({
      content: '# store b edited',
      snapshot: false,
      expected_token: TOKEN_B,
    })
  })

  it('a late 409 from another artifact does not arm a conflict in the current editor', async () => {
    const first = pendingWrite()
    renderCrossArtifactSave(first.write)
    await beginCrossArtifactSave()

    first.refuse(conflict())
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled())
    expect(screen.queryByText(/Content changed since you loaded it/)).not.toBeInTheDocument()
    expect(screen.queryByText('Not saved.')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save — overwrite newer content' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(saveCalls()).toHaveLength(2))
    expect(saveCalls()[1]).toEqual({
      content: '# store b edited',
      snapshot: false,
      expected_token: TOKEN_B,
    })
  })

  it('sends the token the edit started from, then the token its own save returned', async () => {
    vi.mocked(api).updateArtifact = vi
      .fn()
      .mockResolvedValue(mkArtifact({ content: '# v1 edited', content_token: TOKEN_SAVED }))
    await editAndDirty()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(saveCalls()).toHaveLength(1))
    expect(saveCalls()[0]).toEqual({ content: '# v1 edited', snapshot: false, expected_token: TOKEN_V1 })

    fireEvent.change(screen.getByTestId('editor-stub'), { target: { value: '# v1 edited twice' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Save' }))
    await waitFor(() => expect(saveCalls()).toHaveLength(2))
    expect(saveCalls()[1].expected_token).toBe(TOKEN_SAVED)
  })

  it('a 409 keeps the draft, shows the conflict and makes the next Save an overwrite on the live token', async () => {
    vi.mocked(api).updateArtifact = vi
      .fn()
      .mockRejectedValueOnce(conflict())
      .mockResolvedValue(mkArtifact({ content: '# v1 edited', content_token: TOKEN_SAVED }))
    await editAndDirty()
    // The refetch after the 409 carries a different token; the next save must
    // still send the one the 409 named, not whatever the refetch returned.
    vi.mocked(api).artifact = vi
      .fn()
      .mockResolvedValue(mkArtifact({ content: '# newer', content_token: TOKEN_REFETCHED }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await screen.findByText(/Content changed since you loaded it/)
    expect(screen.getByText('Not saved.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open the newer version in a new tab' })).toHaveAttribute(
      'href',
      '/artifacts/cr-queue',
    )
    expect(screen.getByRole('link', { name: 'Open the newer version in a new tab' })).toHaveAttribute(
      'target',
      '_blank',
    )
    expect(screen.getByTestId('editor-stub')).toHaveValue('# v1 edited')
    const overwrite = await screen.findByRole('button', { name: 'Save — overwrite newer content' })

    fireEvent.click(overwrite)
    await waitFor(() => expect(saveCalls()).toHaveLength(2))
    expect(saveCalls()[1]).toEqual({ content: '# v1 edited', snapshot: false, expected_token: TOKEN_LIVE })
    await waitFor(() => expect(screen.queryByText(/Content changed since you loaded it/)).not.toBeInTheDocument())
    expect(await screen.findByRole('button', { name: 'Save' })).toBeInTheDocument()
  })

  it('switching version after a 409 clears the conflict notice', async () => {
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    vi.mocked(api).artifactVersion = vi.fn().mockResolvedValue(mkArtifact({ content: '# v1 historical' }))
    vi.mocked(api).updateArtifact = vi.fn().mockRejectedValue(conflict())
    await editAndDirty()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await screen.findByText(/Content changed since you loaded it/)

    await pickVersion('v1', { discard: true })

    await waitFor(() => expect(screen.queryByTestId('editor-stub')).not.toBeInTheDocument())
    await waitFor(() => expect(api.artifactVersion).toHaveBeenCalledWith('cr-queue', 1))
    expect(await screen.findByText('CR Queue')).toBeInTheDocument()
    expect(screen.queryByText(/Content changed since you loaded it/)).not.toBeInTheDocument()
    expect(screen.queryByText('Not saved.')).not.toBeInTheDocument()
  })

  it('a refused pull flush that returns after a version switch shows no notice', async () => {
    withForkBanner()
    const v1 = mkArtifact({ content: '# v1 historical', fork_metadata: FORK_METADATA })
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    vi.mocked(api).artifactVersion = vi.fn().mockResolvedValue(v1)
    const flush = pendingWrite()
    vi.mocked(api).updateArtifact = vi.fn().mockReturnValueOnce(flush.write)
    const { queryClient } = await editAndDirty()
    // Cached, so v1 renders at once and the banner, whose pull is still waiting on
    // the flush, stays mounted through the switch.
    queryClient.setQueryData(['artifact', 'cr-queue', 'version', 1], v1)
    fireEvent.click(await screen.findByRole('button', { name: 'Pull latest' }))
    await waitFor(() => expect(saveCalls()).toHaveLength(1))

    await pickVersion('v1', { discard: true })
    await waitFor(() => expect(screen.queryByTestId('editor-stub')).not.toBeInTheDocument())
    flush.refuse(conflict())

    // The pull re-enables only once the refusal has been handled.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Pull latest' })).toBeEnabled())
    expect(screen.queryByText('Not saved.')).not.toBeInTheDocument()
    expect(api.pullLatest).not.toHaveBeenCalled()
  })

  it('a refused save that returns after Escape and discard shows no notice', async () => {
    const save = pendingWrite()
    vi.mocked(api).updateArtifact = vi.fn().mockReturnValueOnce(save.write)
    await editAndDirty()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(saveCalls()).toHaveLength(1))
    fireEvent.keyDown(document, { key: 'Escape' })
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(screen.queryByTestId('editor-stub')).not.toBeInTheDocument())
    save.refuse(conflict())

    // The selector re-enables only once the refusal has been handled.
    await waitFor(() => expect(screen.getByRole('combobox', { name: /Version/i })).toBeEnabled())
    expect(screen.queryByText('Not saved.')).not.toBeInTheDocument()
  })

  it('a version switch leaves preview mode, so the next edit opens the editor', async () => {
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    vi.mocked(api).artifactVersion = vi.fn().mockResolvedValue(mkArtifact({ content: '# v1 historical' }))
    await editAndDirty()
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    await waitFor(() => expect(screen.queryByTestId('editor-stub')).not.toBeInTheDocument())

    await pickVersion('v1', { discard: true })
    await waitFor(() => expect(api.artifactVersion).toHaveBeenCalledWith('cr-queue', 1))
    await pickVersion('Live')
    fireEvent.click(await screen.findByTitle('Edit content'))
    expect(await screen.findByTestId('editor-stub')).toBeInTheDocument()
  })

  // Only the relabelled Save may send the overwrite token; every other save
  // path keeps the base token and is refused again.
  it.each([
    ['the Snapshot button', () => fireEvent.click(screen.getByRole('button', { name: /^Snapshot/ })), true],
    ['Cmd+S', () => fireEvent.keyDown(document, { key: 's', metaKey: true }), false],
    ['Cmd+Shift+S', () => fireEvent.keyDown(document, { key: 's', metaKey: true, shiftKey: true }), true],
  ])('after a 409, %s keeps the base token', async (_label, act, snapshot) => {
    vi.mocked(api).updateArtifact = vi.fn().mockRejectedValue(conflict())
    await editAndDirty()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await screen.findByRole('button', { name: 'Save — overwrite newer content' })

    act()
    await waitFor(() => expect(saveCalls()).toHaveLength(2))
    expect(saveCalls()[1]).toEqual({ content: '# v1 edited', snapshot, expected_token: TOKEN_V1 })
    expect(screen.getByTestId('editor-stub')).toHaveValue('# v1 edited')
  })

  it('after a 409, a pull still flushes on the base token and is refused, not an overwrite', async () => {
    withForkBanner()
    vi.mocked(api).updateArtifact = vi.fn().mockRejectedValue(conflict())
    await editAndDirty()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await screen.findByRole('button', { name: 'Save — overwrite newer content' })

    fireEvent.click(await screen.findByRole('button', { name: 'Pull latest' }))
    await waitFor(() => expect(saveCalls()).toHaveLength(2))
    expect(saveCalls()[1].expected_token).toBe(TOKEN_V1)
    expect(api.pullLatest).not.toHaveBeenCalled()
    // The refusal shows once, as the localized notice, not the raw server message.
    expect(screen.getByText('Not saved.')).toBeInTheDocument()
    expect(screen.queryByText(/artifact changed since it was read/)).not.toBeInTheDocument()
    expect(screen.getByTestId('editor-stub')).toHaveValue('# v1 edited')
  })

  // Every write carries the base token and only its own response advances it,
  // so a second write sent before the first settles would be a false 409.
  it('a save repeated while the first is on the wire sends no second write', async () => {
    const first = pendingWrite()
    vi.mocked(api).updateArtifact = vi
      .fn()
      .mockReturnValueOnce(first.write)
      .mockResolvedValue(mkArtifact({ content: '# v1 edited', content_token: TOKEN_SAVED }))
    await editAndDirty()
    fireEvent.keyDown(document, { key: 's', metaKey: true })
    fireEvent.keyDown(document, { key: 's', metaKey: true })
    fireEvent.keyDown(document, { key: 's', metaKey: true, shiftKey: true })
    expect(saveCalls()).toHaveLength(1)

    first.settle(mkArtifact({ content: '# v1 edited', content_token: TOKEN_SAVED }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled())
    expect(screen.queryByText('Not saved.')).not.toBeInTheDocument()
    fireEvent.keyDown(document, { key: 's', metaKey: true })
    await waitFor(() => expect(saveCalls()).toHaveLength(2))
    expect(saveCalls()[1].expected_token).toBe(TOKEN_SAVED)
  })

  it('a save while a pull is flushing the draft sends no second write', async () => {
    withForkBanner()
    const flush = pendingWrite()
    vi.mocked(api).updateArtifact = vi.fn().mockReturnValueOnce(flush.write)
    await editAndDirty()
    fireEvent.click(await screen.findByRole('button', { name: 'Pull latest' }))
    await waitFor(() => expect(saveCalls()).toHaveLength(1))
    fireEvent.keyDown(document, { key: 's', metaKey: true })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(saveCalls()).toHaveLength(1)

    flush.settle(mkArtifact({ content: '# v1 edited', content_token: TOKEN_SAVED }))
    await waitFor(() => expect(api.pullLatest).toHaveBeenCalledTimes(1))
    expect(saveCalls()).toHaveLength(1)
  })

  it('the fork banner is disabled while a save is on the wire', async () => {
    withForkBanner()
    const first = pendingWrite()
    vi.mocked(api).updateArtifact = vi.fn().mockReturnValueOnce(first.write)
    await editAndDirty()
    const pull = await screen.findByRole('button', { name: 'Pull latest' })
    fireEvent.keyDown(document, { key: 's', metaKey: true })
    await waitFor(() => expect(pull).toBeDisabled())
    fireEvent.click(pull)
    expect(saveCalls()).toHaveLength(1)
    expect(api.pullLatest).not.toHaveBeenCalled()

    first.settle(mkArtifact({ content: '# v1 edited', content_token: TOKEN_SAVED }))
    await waitFor(() => expect(pull).toBeEnabled())
  })

  it('omits the token for an artifact that has none', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ content_token: undefined }))
    vi.mocked(api).updateArtifact = vi.fn().mockResolvedValue(mkArtifact({ content: '# v1 edited' }))
    await editAndDirty()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(saveCalls()).toHaveLength(1))
    expect(saveCalls()[0].expected_token).toBeUndefined()
  })
})
