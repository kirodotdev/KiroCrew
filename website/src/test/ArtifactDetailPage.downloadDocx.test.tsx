import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { downloadBlob, downloadUrl, isAndroidWebView } from '../utils/download'
import type { Artifact } from '../types'
import { findMoreItem, openMore } from './artifactMoreMenu'

vi.mock('../api/client')
vi.mock('../utils/download', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/download')>()),
  downloadBlob: vi.fn(),
  downloadUrl: vi.fn(),
  isAndroidWebView: vi.fn(() => false),
}))
vi.mock('../pages/ChatPage', () => ({ default: () => <div />, PREFILL_STORAGE_KEY: 'kirocrew_prefill' }))

const mkArtifact = (overrides: Partial<Artifact> = {}): Artifact => ({
  slug: 'cr-queue', name: 'CR Queue: "plan"', kind: 'markdown', source: 'chat', description: '', tags: [],
  version: 2, created_at: '2026-05-21T22:00:00.000000+00:00', updated_at: '2026-05-21T22:30:00.000000+00:00',
  content: '# Plan', ...overrides,
})

const item = 'Download as .docx'
const opened = async (artifact: Artifact) => {
  vi.mocked(api).artifact = vi.fn().mockResolvedValue(artifact)
  renderWithProviders(
    <Routes><Route path="/artifacts/:slug" element={<ArtifactDetailPage />} /></Routes>,
    { route: '/artifacts/cr-queue' },
  )
  await waitFor(() => expect(screen.getByText(/CR Queue/)).toBeInTheDocument())
}

describe('ArtifactDetailPage download as .docx', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(isAndroidWebView).mockReturnValue(false)
    vi.mocked(api.sandboxDocUrl).mockResolvedValue({ url: '/sandbox-doc/test/tok' })
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    vi.mocked(api).artifactVersion = vi.fn().mockResolvedValue(mkArtifact({ version: 1 }))
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: 'cr-queue', events: [] })
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
    vi.mocked(api).artifactDocx = vi.fn().mockResolvedValue(new Blob(['docx']))
  })

  it.each(['markdown', 'text'] as const)('saves a %s artifact from the gateway, named from its slug and version', async (kind) => {
    const split = Array.from('ghp_' + 'a'.repeat(36)).join('\0')
    await opened(mkArtifact({ kind, name: `CR Queue ${split}` }))
    fireEvent.click(await findMoreItem(item))

    await waitFor(() => expect(downloadBlob).toHaveBeenCalledTimes(1))
    expect(api.artifactDocx).toHaveBeenCalledWith('cr-queue', null)
    expect(vi.mocked(downloadBlob).mock.calls[0][1]).toBe('cr-queue-v2.docx')
  })

  it.each(['image', 'canvas', 'webapp', 'json', 'svg', 'html'] as const)('does not offer it for a %s artifact', async (kind) => {
    await opened(mkArtifact({ kind }))
    openMore()
    await screen.findByRole('menu')
    expect(screen.getByRole('menuitem', { name: 'Download' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: item })).toBeNull()
  })

  it('asks for the snapshot on screen, not the live text', async () => {
    await opened(mkArtifact())
    fireEvent.click(screen.getByRole('combobox', { name: /^Version$/i }))
    fireEvent.click(await screen.findByRole('option', { name: 'v1' }))
    await waitFor(() => expect(screen.getByText(/Showing v1/)).toBeInTheDocument())
    fireEvent.click(await findMoreItem(item))

    await waitFor(() => expect(api.artifactDocx).toHaveBeenCalledWith('cr-queue', 1))
  })

  it.each([false, true])('reports a refused export on the page notice and saves nothing (in-app browser: %s)', async (inApp) => {
    vi.mocked(isAndroidWebView).mockReturnValue(inApp)
    vi.mocked(api).artifactDocx = vi.fn().mockRejectedValue(Object.assign(new Error('too large'), { status: 413 }))
    await opened(mkArtifact())
    fireEvent.click(await findMoreItem(item))

    expect(await screen.findByText('Download failed')).toBeInTheDocument()
    expect(downloadBlob).not.toHaveBeenCalled()
    expect(downloadUrl).not.toHaveBeenCalled()
  })

  it('checks the export, then hands an in-app browser the route itself, since it drops a blob download', async () => {
    vi.mocked(isAndroidWebView).mockReturnValue(true)
    await opened(mkArtifact())
    fireEvent.click(await findMoreItem(item))

    await waitFor(() => expect(downloadUrl).toHaveBeenCalledWith('/api/artifacts/cr-queue/docx', 'cr-queue-v2.docx'))
    expect(api.artifactDocx).toHaveBeenCalledWith('cr-queue', null)
    expect(downloadBlob).not.toHaveBeenCalled()
  })
})
