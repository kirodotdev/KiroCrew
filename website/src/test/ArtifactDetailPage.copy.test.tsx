import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, waitFor, fireEvent } from '@testing-library/react'
import { Routes, Route, useNavigate } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { copyToClipboard } from '../utils/clipboard'
import type { Artifact } from '../types'
import { moreButton, openMore } from './artifactMoreMenu'

// The sandboxed frame mints its document URL through the api client. The
// automock resolves every method to `undefined`, which the component cannot
// await — without this stub the frame throws instead of rendering.
beforeEach(() => {
  vi.mocked(api.sandboxDocUrl).mockResolvedValue({ url: '/sandbox-doc/test/tok' })
})


vi.mock('../api/client')
vi.mock('../utils/clipboard', () => ({
  // The contract: `copyToClipboard` resolves a boolean and never rejects.
  // `true` = the text actually reached the clipboard. The default mock resolves
  // `true` so the happy path exercises the boolean-gated confirmation.
  copyToClipboard: vi.fn().mockResolvedValue(true),
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

// Copy lives in the toolbar's "More" menu, which stays open after a copy so the
// item's Copied / Copy failed label is the confirmation.
const copyItem = (name: string = 'Copy content') => {
  if (!screen.queryByRole('menu')) openMore()
  return screen.getByRole('menuitem', { name })
}
const copyBtn = () => copyItem()

describe('ArtifactDetailPage copy content', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.removeItem('mc-reading-width')
    vi.mocked(copyToClipboard).mockResolvedValue(true)
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
    expect(await screen.findByRole('menuitem', { name: 'Copied' })).toBeInTheDocument()
  })

  it('is offered from the toolbar\'s More menu rather than a row of its own above the body', async () => {
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(moreButton().closest('[data-hover-tip]')?.parentElement).toHaveClass('mc-art-toolbar')
    expect(copyBtn().closest('[role="menu"]')).not.toBeNull()
  })

  it('keeps iframe artifacts full width', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'html', content: '<main>Full-width report</main>' }),
    )
    const { container } = renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())

    expect(copyBtn()).toBeInTheDocument()
    // No reading-width setting for an iframe: it always spans the pane.
    expect(screen.queryByRole('menuitemcheckbox', { name: 'Full width' })).toBeNull()

    const iframe = await waitFor(() => {
      const node = container.querySelector('iframe')
      expect(node).not.toBeNull()
      return node as HTMLIFrameElement
    })
    expect((iframe.parentElement as HTMLElement).style.maxWidth).toBe('')
  })

  it('reports a failed copy through the page notice and closes the menu so it is seen', async () => {
    // Under the boolean contract `copyToClipboard` never rejects; a failed copy
    // resolves `false`. The failure UI must key off that, not off a rejection.
    vi.mocked(copyToClipboard).mockResolvedValueOnce(false)
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())

    fireEvent.click(copyBtn())
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    expect(screen.getByRole('alert')).toHaveTextContent('Copy failed')
    // The item itself never turns into an error surface: it still reads Copy content.
    expect(copyItem('Copy content')).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Copy failed' })).toBeNull()
  })

  it('lets a retry after a failure own the status and timeout', async () => {
    vi.mocked(copyToClipboard)
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true)
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())

    fireEvent.click(copyBtn())
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    vi.useFakeTimers()
    fireEvent.click(copyBtn())
    await act(async () => { await Promise.resolve() })
    expect(screen.getByRole('menuitem', { name: 'Copied' })).toBeInTheDocument()

    act(() => vi.advanceTimersByTime(1499))
    expect(screen.getByRole('menuitem', { name: 'Copied' })).toBeInTheDocument()
    act(() => vi.advanceTimersByTime(1))
    expect(copyBtn()).toBeInTheDocument()
  })

  it('ignores an older copy attempt that settles after the latest attempt', async () => {
    let resolveFirst: ((v: boolean) => void) | undefined
    let resolveSecond: ((v: boolean) => void) | undefined
    vi.mocked(copyToClipboard)
      .mockImplementationOnce(() => new Promise<boolean>((resolve) => { resolveFirst = resolve }))
      .mockImplementationOnce(() => new Promise<boolean>((resolve) => { resolveSecond = resolve }))
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())

    fireEvent.click(copyBtn())
    fireEvent.click(copyBtn())
    await act(async () => { resolveSecond?.(true) })
    expect(screen.getByRole('menuitem', { name: 'Copied' })).toBeInTheDocument()

    // The stale first attempt settles late as a failure; the attempt guard must
    // keep it from clobbering the current 'Copied' state.
    await act(async () => { resolveFirst?.(false) })
    expect(screen.getByRole('menuitem', { name: 'Copied' })).toBeInTheDocument()
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

  it('drops a copy failure notice when the route moves to another artifact', async () => {
    // The route element is reused across artifacts, so A's failure banner
    // must not stay up over B.
    function SwitchArtifact() {
      const navigate = useNavigate()
      return <button type="button" onClick={() => navigate('/artifacts/other')}>go to other</button>
    }
    vi.mocked(api).artifact = vi.fn((slug: string) => Promise.resolve(mkArtifact({ slug, name: slug })))
    vi.mocked(copyToClipboard).mockResolvedValueOnce(false)
    renderWithProviders(
      <>
        <SwitchArtifact />
        <Routes>
          <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
        </Routes>
      </>,
      { route: '/artifacts/cr-queue' },
    )
    await screen.findByText('cr-queue', { selector: 'button' })
    fireEvent.click(copyBtn())
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Copy failed'))
    fireEvent.click(screen.getByRole('button', { name: 'go to other' }))
    await screen.findByText('other', { selector: 'button' })
    expect(screen.queryByRole('alert')).toBeNull()
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
    openMore()
    await screen.findByRole('menu')
    expect(screen.queryByRole('menuitem', { name: 'Copy content' })).toBeNull()
  })

  it('hides the copy button while editing', async () => {
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Edit content' }))
    await waitFor(() => expect(screen.getByRole('button', { name: /Cancel/ })).toBeInTheDocument())
    openMore()
    await screen.findByRole('menu')
    expect(screen.queryByRole('menuitem', { name: 'Copy content' })).toBeNull()
  })
})
