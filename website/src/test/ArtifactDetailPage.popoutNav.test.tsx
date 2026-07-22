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

  it('main dashboard: the "Iterate" affordance is hidden pending redesign', async () => {
    // The header "Iterate" button is gated behind SHOW_ARTIFACT_ITERATE (false),
    // so it does not render even in the main dashboard (its `!popout` branch).
    // When the redesign re-enables the flag, restore this to click the button
    // and assert the local-nav prefill / sendNav path (fork analogue of
    // upstream's "Ask agent to address").
    vi.mocked(api).createChatSlot = vi.fn().mockResolvedValue({ key: 'new-slot-1' })
    renderPage(false)
    await waitFor(() => expect(screen.getByText(/Artifact: cr-queue/i)).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /iterate/i })).toBeNull()
    expect(vi.mocked(api).createChatSlot).not.toHaveBeenCalled()
  })
})
