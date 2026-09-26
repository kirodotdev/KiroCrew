/**
 * Stale-write guard on the detail page (#7751): a content save carries the
 * `content_token` the edit started from, and a 409 keeps the draft, re-bases on
 * the live token and turns the next Save into a deliberate overwrite.
 *
 * Pierre is stubbed with a controlled textarea because the real editor cannot be
 * driven under jsdom; controlled so a regression that re-seeds the buffer fails.
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
    JSON.stringify({ error: 'conflict', current_token: TOKEN_LIVE, version: 1 }),
  )

async function editAndDirty() {
  renderWithProviders(
    <Routes>
      <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
    </Routes>,
    { route: '/artifacts/cr-queue' },
  )
  await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
  fireEvent.click(screen.getByTitle('Edit content'))
  const editor = await screen.findByTestId('editor-stub')
  fireEvent.change(editor, { target: { value: '# v1 edited' } })
}

const saveCalls = () => vi.mocked(api.updateArtifact).mock.calls.map(([, body]) => body)

beforeEach(() => {
  vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
  vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'cr-queue', versions: [1] })
  vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: 'cr-queue', events: [] })
  vi.mocked(api.sandboxDocUrl).mockResolvedValue({ url: '/sandbox-doc/test/tok' })
})

describe('stale-write guard', () => {
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
    expect(screen.getByTestId('editor-stub')).toHaveValue('# v1 edited')
    const overwrite = await screen.findByRole('button', { name: 'Save — overwrite newer content' })

    fireEvent.click(overwrite)
    await waitFor(() => expect(saveCalls()).toHaveLength(2))
    expect(saveCalls()[1]).toEqual({ content: '# v1 edited', snapshot: false, expected_token: TOKEN_LIVE })
    await waitFor(() => expect(screen.queryByText(/Content changed since you loaded it/)).not.toBeInTheDocument())
    expect(await screen.findByRole('button', { name: 'Save' })).toBeInTheDocument()
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
