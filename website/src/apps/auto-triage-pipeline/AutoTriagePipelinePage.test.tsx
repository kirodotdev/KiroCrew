/**
 * AutoTriagePipelinePage — the page-entry the builtin registry lazy-loads for the
 * `/auto-triage-pipeline` route. It is deliberately thin: a full-height surface
 * wrapping `PipelineView`. The only behaviour worth pinning here is that it DOES
 * mount the view (rather than a placeholder) and gives it the full-page chrome —
 * so a regression that swapped the view out, or dropped the surface classes the
 * wave-2 drawing needs, is caught. The HTTP seam is mocked so nothing dials; the
 * view's own contract is covered in `views/PipelineView.render.test.tsx`.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('./api', () => ({
  autoTriagePipelineApi: {
    // No repo connected → the genuine empty state, so the page mounts the view
    // without needing a fabric fetch.
    listConnectedRepos: vi.fn(async () => []),
    crewFabric: vi.fn(async () => ({ items: [] })),
  },
  loadStoredPreference: vi.fn(() => null),
  saveRepoPreference: vi.fn(),
}))

import AutoTriagePipelinePage from './AutoTriagePipelinePage'

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return render(
    <QueryClientProvider client={qc}>
      <AutoTriagePipelinePage />
    </QueryClientProvider>,
  )
}

describe('AutoTriagePipelinePage', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('mounts the pipeline view inside a full-height page surface', async () => {
    const { container } = renderPage()

    // The outer surface is the full-page chrome the drawing needs — a single
    // full-height container, not a narrow panel.
    const root = container.firstElementChild as HTMLElement
    expect(root.className).toContain('h-full')
    expect(root.className).toContain('overflow-hidden')

    // And it really rendered PipelineView: with no repo connected the view
    // resolves to its "No repository connected" state (testId atp-no-repo),
    // which only the view can produce.
    await waitFor(() => expect(screen.getByTestId('atp-no-repo')).toBeTruthy())
  })
})
