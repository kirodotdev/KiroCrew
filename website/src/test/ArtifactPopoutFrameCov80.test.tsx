// ArtifactPopoutFrame is the window shell for a popped-out artifact: it mounts
// the full detail page, registers as a live popout for the slug, mirrors the
// artifact name into the OS window title, and offers a "return" affordance.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const artifact = vi.fn()
vi.mock('../api/client', () => ({ api: { artifact: (...a: unknown[]) => artifact(...a) } }))

const { registerPopout, unregister, returnSelfToMain } = vi.hoisted(() => {
  const unregister = vi.fn()
  return { registerPopout: vi.fn(() => unregister), unregister, returnSelfToMain: vi.fn() }
})
vi.mock('../utils/artifactPopout', () => ({ registerPopout, returnSelfToMain }))
vi.mock('../pages/ArtifactDetailPage', () => ({
  default: (p: Record<string, unknown>) => <div data-testid="artifact-detail" data-popout={String(p.popout)} />,
}))

import ArtifactPopoutFrame from '../pages/ArtifactPopoutFrame'

function renderFrame(route = '/popout/artifact/zz-slug') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path="/popout/artifact/:slug" element={<ArtifactPopoutFrame />} />
          <Route path="/popout/artifact" element={<ArtifactPopoutFrame />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  artifact.mockReset()
  artifact.mockResolvedValue({ slug: 'zz-slug', name: 'zz-artifact-name' })
  registerPopout.mockClear()
  unregister.mockClear()
  returnSelfToMain.mockClear()
  document.title = ''
})

describe('ArtifactPopoutFrame', () => {
  it('mounts the artifact detail page in popout mode', () => {
    renderFrame()
    expect(screen.getByTestId('artifact-detail')).toHaveAttribute('data-popout', 'true')
  })

  it('registers as a live popout for the slug and unregisters on unmount', () => {
    const { unmount } = renderFrame()
    expect(registerPopout).toHaveBeenCalledWith('zz-slug')
    unmount()
    expect(unregister).toHaveBeenCalled()
  })

  it('mirrors the fetched artifact name into the window title', async () => {
    renderFrame()
    await waitFor(() => expect(document.title).toBe('zz-artifact-name — Kiro Crew'))
    expect(artifact).toHaveBeenCalledWith('zz-slug')
  })

  it('titles from the slug until the name lands', () => {
    artifact.mockReturnValue(new Promise(() => {}))
    renderFrame()
    expect(document.title).toBe('zz-slug — Kiro Crew')
  })

  it('skips the fetch and registration without a slug', () => {
    renderFrame('/popout/artifact')
    expect(artifact).not.toHaveBeenCalled()
    expect(registerPopout).not.toHaveBeenCalled()
    expect(document.title).toBe('Artifact — Kiro Crew')
  })

  it('returns to the main window from the labelled affordance', () => {
    renderFrame()
    fireEvent.click(screen.getByLabelText('Return to main window and close this popout'))
    expect(returnSelfToMain).toHaveBeenCalled()
  })
})
