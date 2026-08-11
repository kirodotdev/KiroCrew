import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { copyToClipboard } from '../utils/clipboard'
import type { Artifact } from '../types'

vi.mock('../api/client')
vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(undefined),
}))
// Stub the embedded chat page — covered by its own suites.
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))

const RAW = '# Release notes\n\n- raw **markdown** source'

const mkArtifact = (overrides: Partial<Artifact> = {}): Artifact => ({
  slug: 'cr-queue',
  name: 'CR Queue',
  kind: 'markdown',
  source: 'chat',
  description: '',
  tags: [],
  version: 2,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:30:00.000000+00:00',
  content: RAW,
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

const copyBtn = () => screen.getByRole('button', { name: 'Copy content' })

describe('ArtifactDetailPage copy content', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(copyToClipboard).mockResolvedValue(undefined)
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    vi.mocked(api).artifactEvents = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', events: [] })
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('copies the raw stored source and confirms with a check state', async () => {
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    fireEvent.click(copyBtn())
    // Raw source as stored — not the rendered markdown.
    expect(copyToClipboard).toHaveBeenCalledWith(RAW)
    // Brief confirmation: the control flips to its "Copied" state.
    expect(await screen.findByRole('button', { name: 'Copied' })).toBeInTheDocument()
  })

  it('copies the selected historical version, not live', async () => {
    vi.mocked(api).artifactVersion = vi
      .fn()
      .mockResolvedValue(mkArtifact({ version: 1, content: 'old v1 body' }))
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    // Pick v1 in the version dropdown (Radix: open, then click the row).
    fireEvent.click(screen.getByRole('combobox', { name: /Version/i }))
    fireEvent.click(await screen.findByRole('option', { name: 'v1' }))
    await waitFor(() => expect(api.artifactVersion).toHaveBeenCalledWith('cr-queue', 1))
    // The page swaps to a loading state while the snapshot fetch resolves —
    // wait for the historical body before copying.
    await screen.findByText(/old v1 body/)
    fireEvent.click(copyBtn())
    expect(copyToClipboard).toHaveBeenCalledWith('old v1 body')
  })

  it('offers no copy button for image artifacts (bytes, not text)', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({
        kind: 'image',
        content: undefined,
        image: { mime: 'image/png', ext: 'png', alt: 'A chart' },
      }),
    )
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: 'Copy content' })).toBeNull()
  })

  it('hides the copy button while editing', async () => {
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Edit content' }))
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Copy content' })).toBeNull(),
    )
  })
})
