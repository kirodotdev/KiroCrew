/**
 * Popout navigation containment tests for ArtifactDetailPage.
 *
 * Inside a popout window (`popout` prop), every affordance that leaves the
 * artifact view must FORWARD its navigation intent to a main dashboard window
 * instead of calling the router — an in-window navigate() would flip App.tsx
 * out of the popout branch and mount the entire dashboard inside the popout.
 * In the main dashboard the same affordances navigate locally, unchanged.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { forwardToMain } from '../utils/artifactPopout'
import type { Artifact } from '../types'

vi.mock('../api/client')
// Stub the embedded chat page — the companion toggle opens the embedded panel,
// which would otherwise mount the full ChatPage and its whole hook graph.
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))
// Replace only the popout→main forwarding entry point; the rest of the module
// (registerPopout, the coordination map, …) stays real.
vi.mock('../utils/artifactPopout', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../utils/artifactPopout')>()
  return { ...mod, forwardToMain: vi.fn() }
})

const mkArtifact = (overrides: Partial<Artifact> = {}): Artifact => ({
  slug: 'cr-queue',
  name: 'CR Queue',
  kind: 'markdown',
  source: 'chat',
  description: 'Hourly CR snapshot',
  tags: [],
  version: 2,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:30:00.000000+00:00',
  content: '# CR Queue',
  ...overrides,
})

const sessionEvents = {
  slug: 'cr-queue',
  events: [
    { ts: '2026-05-25T22:00:00.000Z', type: 'iterated', by: 'agent', session_id: 'chat-42', version: 2 },
  ],
}

function renderPage(popout: boolean) {
  return renderWithProviders(
    <Routes>
      <Route path="/artifacts/:slug" element={<ArtifactDetailPage popout={popout} />} />
      <Route path="/artifacts" element={<div>library page target</div>} />
      <Route path="/chat" element={<div>chat page target</div>} />
    </Routes>,
    { route: '/artifacts/cr-queue' },
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
  vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
  vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue(sessionEvents)
  // The companion toggle creates a bound session, which dispatches fetchSlots()
  // in the background. Without this mock the automock resolves undefined and the
  // fetchSlots.fulfilled reducer throws on `payload.map` AFTER the test ends — an
  // unhandled rejection that fails the run (`Errors: N errors`) while every test
  // still reports as passing.
  vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
})

describe('ArtifactDetailPage popout navigation containment', () => {
  it('popout: an activity-timeline session link forwards to the main window instead of navigating', async () => {
    renderPage(true)
    await waitFor(() => expect(screen.getByText(/from session chat-42/i)).toBeInTheDocument())
    fireEvent.click(screen.getByText(/from session chat-42/i))
    expect(vi.mocked(forwardToMain)).toHaveBeenCalledWith({ path: '/chat', slotKey: 'chat-42' })
    // The popout window itself must stay pinned to the artifact.
    expect(screen.queryByText('chat page target')).toBeNull()
    expect(screen.getByText(/Artifact: cr-queue/i)).toBeInTheDocument()
  })

  it('main dashboard: the same session link still navigates locally (control case)', async () => {
    renderPage(false)
    await waitFor(() => expect(screen.getByText(/from session chat-42/i)).toBeInTheDocument())
    fireEvent.click(screen.getByText(/from session chat-42/i))
    expect(vi.mocked(forwardToMain)).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.getByText('chat page target')).toBeInTheDocument())
  })

  it('popout: the load-error back-link forwards to the library instead of navigating', async () => {
    vi.mocked(api).artifact = vi.fn().mockRejectedValue(new Error('boom'))
    renderPage(true)
    await waitFor(() => expect(screen.getByText(/failed to load artifact/i)).toBeInTheDocument())
    fireEvent.click(screen.getByText(/back to library/i))
    expect(vi.mocked(forwardToMain)).toHaveBeenCalledWith({ path: '/artifacts' })
    expect(screen.queryByText('library page target')).toBeNull()
  })

  it('main dashboard: the load-error back-link navigates locally (control case)', async () => {
    vi.mocked(api).artifact = vi.fn().mockRejectedValue(new Error('boom'))
    renderPage(false)
    await waitFor(() => expect(screen.getByText(/failed to load artifact/i)).toBeInTheDocument())
    fireEvent.click(screen.getByText(/back to library/i))
    expect(vi.mocked(forwardToMain)).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.getByText('library page target')).toBeInTheDocument())
  })

  it('popout: the companion chat toggle renders and stays in-window', async () => {
    // The sparkle is a panel toggle, and a popout has its own store + WS, so it
    // works in place — and must NOT forward a nav intent to a main window.
    vi.mocked(api).createChatSlot = vi.fn().mockResolvedValue({ key: 'new-slot-1' })
    vi.mocked(api).chatSlotContext = vi.fn().mockResolvedValue({ ok: true })
    renderPage(true)
    await waitFor(() => expect(screen.getByText(/Artifact: cr-queue/i)).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1))
    expect(vi.mocked(forwardToMain)).not.toHaveBeenCalled()
  })

  it('main dashboard: the companion chat toggle creates a bound session', async () => {
    vi.mocked(api).createChatSlot = vi.fn().mockResolvedValue({ key: 'new-slot-1' })
    vi.mocked(api).chatSlotContext = vi.fn().mockResolvedValue({ ok: true })
    renderPage(false)
    await waitFor(() => expect(screen.getByText(/Artifact: cr-queue/i)).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api).createChatSlot.mock.calls[0][7]).toBe('cr-queue')
  })
})
