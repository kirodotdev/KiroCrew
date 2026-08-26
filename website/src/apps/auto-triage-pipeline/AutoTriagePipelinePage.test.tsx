/**
 * AutoTriagePipelinePage — the page-entry the builtin registry lazy-loads for the
 * `/auto-triage-pipeline` route.
 *
 * The page carries TWO views over two different data sources, so what is worth
 * pinning here is the routing between them, not their internals: that it defaults
 * to the pipeline view, that the lanes tab really mounts the crew-fabric view, and
 * that the page still gives whichever view is showing the full-height chrome the
 * drawing needs. Each view's own contract is covered in its own render test.
 *
 * Both HTTP seams are mocked so nothing dials.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import userEvent from '@testing-library/user-event'

vi.mock('./api', () => ({
  // The crew-fabric seam the LANES tab reads. No repo connected -> that view's
  // genuine empty state, which is the only thing it can produce without a fetch.
  autoTriagePipelineApi: {
    listConnectedRepos: vi.fn(async () => []),
    crewFabric: vi.fn(async () => ({ items: [] })),
  },
  // The fold seam the PIPELINE tab reads. An empty overview is a real answer: a
  // machine whose pipeline has never run has no steps to draw.
  autoTriagePipelineFoldApi: {
    overview: vi.fn(async () => ({
      steps: [],
      totalEvents: 0,
      unparseable: 0,
      unmappedEvents: [],
      firstEventAt: null,
      lastEventAt: null,
      recentHours: 24,
    })),
    step: vi.fn(async () => ({ step: '', count: 0, items: [] })),
    itemSessions: vi.fn(async () => ({
      number: 0,
      count: 0,
      sessions: [],
      populatedColumns: [],
    })),
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

  it('gives the active view a full-height page surface', () => {
    const { container } = renderPage()
    const root = container.firstElementChild as HTMLElement
    expect(root.className).toContain('h-full')
    expect(root.className).toContain('overflow-hidden')
  })

  it('defaults to the pipeline view, not the lane board', async () => {
    renderPage()
    // Addressed by ROLE and accessible name, the way assistive tech and a keyboard
    // user reach them -- the shared tabs component is what implements the keyboard
    // contract, so the test should not depend on a test id this page happens to add.
    expect(screen.getByRole('tab', { selected: true }).textContent).toMatch(/pipeline/i)
    // With no steps folded, the pipeline view resolves to its own empty state --
    // proving the fold view mounted rather than the lane board.
    await waitFor(() => expect(screen.getByTestId('atp-no-pipeline')).toBeTruthy())
  })

  it('mounts the lane board when its tab is chosen', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(screen.getByRole('tab', { name: /lanes/i }))
    expect(screen.getByRole('tab', { selected: true }).textContent).toMatch(/lanes/i)
    // `atp-no-repo` is a state only the lane board can produce, so seeing it is
    // evidence the other view is really mounted -- not merely that a tab styled
    // itself as selected.
    await waitFor(() => expect(screen.getByTestId('atp-no-repo')).toBeTruthy())
  })

  it('gives its tabs the keyboard contract the roles announce', async () => {
    // The earlier hand-rolled row put role=tab on plain buttons with no roving
    // tabindex and no arrow handling, so AT announced "tab 1 of 2" while the arrow
    // keys did nothing. Exactly one tab may be in the tab order.
    renderPage()
    const tabs = screen.getAllByRole('tab')
    expect(tabs.length).toBe(2)
    const focusable = tabs.filter((t) => t.getAttribute('tabindex') !== '-1')
    expect(focusable.length).toBe(1)
  })
})
