import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, waitFor, fireEvent } from '@testing-library/react'
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
    localStorage.removeItem('mc-reading-width')
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
    vi.useRealTimers()
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

  it('aligns the copy control with the reading-width card and follows full width', async () => {
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    const toolbar = copyBtn().parentElement as HTMLElement

    expect(toolbar.style.maxWidth).toBe('var(--mc-content-width, 900px)')
    expect(toolbar.style.margin).toBe('0px auto')

    fireEvent.click(screen.getByRole('button', { name: 'Medium width' }))
    expect(toolbar.style.maxWidth).toBe('')
    expect(toolbar.style.margin).toBe('')
  })

  it('keeps iframe artifacts and their copy control full width', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'html', content: '<main>Full-width report</main>' }),
    )
    const { container } = renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())

    const toolbar = copyBtn().parentElement as HTMLElement
    expect(toolbar.style.maxWidth).toBe('')
    expect(toolbar.style.margin).toBe('')
    expect(screen.queryByRole('button', { name: 'Medium width' })).toBeNull()

    const iframe = await waitFor(() => {
      const node = container.querySelector('iframe')
      expect(node).not.toBeNull()
      return node as HTMLIFrameElement
    })
    expect((iframe.parentElement as HTMLElement).style.maxWidth).toBe('')
  })

  it('shows a brief accessible failure state when clipboard copying rejects', async () => {
    vi.mocked(copyToClipboard).mockRejectedValueOnce(new Error('clipboard unavailable'))
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    vi.useFakeTimers()

    fireEvent.click(copyBtn())
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })

    const failed = screen.getByRole('button', { name: 'Copy failed' })
    expect(failed).toHaveAttribute('aria-live', 'polite')
    expect(failed).toHaveClass('text-danger')

    act(() => vi.advanceTimersByTime(1500))
    expect(copyBtn()).toBeInTheDocument()
  })

  it('lets a retry own the status and timeout after a copy failure', async () => {
    vi.mocked(copyToClipboard)
      .mockRejectedValueOnce(new Error('clipboard unavailable'))
      .mockResolvedValueOnce(undefined)
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    vi.useFakeTimers()

    fireEvent.click(copyBtn())
    await act(async () => { await Promise.resolve() })
    const failed = screen.getByRole('button', { name: 'Copy failed' })

    fireEvent.click(failed)
    await act(async () => { await Promise.resolve() })
    expect(screen.getByRole('button', { name: 'Copied' })).toBeInTheDocument()

    act(() => vi.advanceTimersByTime(1499))
    expect(screen.getByRole('button', { name: 'Copied' })).toBeInTheDocument()
    act(() => vi.advanceTimersByTime(1))
    expect(copyBtn()).toBeInTheDocument()
  })

  it('ignores an older copy attempt that settles after the latest attempt', async () => {
    let rejectFirst: ((reason?: unknown) => void) | undefined
    let resolveSecond: (() => void) | undefined
    vi.mocked(copyToClipboard)
      .mockImplementationOnce(() => new Promise<void>((_resolve, reject) => { rejectFirst = reject }))
      .mockImplementationOnce(() => new Promise<void>((resolve) => { resolveSecond = resolve }))
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())

    fireEvent.click(copyBtn())
    fireEvent.click(copyBtn())
    await act(async () => { resolveSecond?.() })
    expect(screen.getByRole('button', { name: 'Copied' })).toBeInTheDocument()

    await act(async () => { rejectFirst?.(new Error('late failure')) })
    expect(screen.getByRole('button', { name: 'Copied' })).toBeInTheDocument()
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
