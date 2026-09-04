/**
 * The Capabilities rail is grouped (Agent / Knowledge & assets / Automation)
 * and hosts Knowledge as a tab — the surface's only home now that it has no
 * main-rail item. Pins three things a refactor could silently drop:
 *  1. the three group headers render (SidePanelLayout keys them on the
 *     `group` string of each tab),
 *  2. a `?tab=knowledge` deep link — what the /knowledge redirect emits —
 *     selects the Knowledge tab,
 *  3. the pane mounts KnowledgePage with `embedded`, so it does not render a
 *     second page title under the pane header.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import React from 'react'

// Tab bodies are irrelevant; the rail + pane wiring is what is under test.
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => <div /> }))
vi.mock('../pages/AgentsPage', () => ({ default: () => <div /> }))
vi.mock('../pages/HooksPage', () => ({ default: () => <div /> }))
vi.mock('../pages/connections/ConnectionsPage', () => ({ default: () => <div /> }))
vi.mock('../pages/overview', () => ({
  SkillsTab: () => <div />,
  PromptsTab: () => <div />,
  SteeringTab: () => <div />,
}))
vi.mock('../pages/KnowledgePage', () => ({
  default: ({ embedded }: { embedded?: boolean }) => (
    <div data-testid="knowledge-pane">{embedded ? 'embedded' : 'standalone'}</div>
  ),
}))
vi.mock('../components/RestartButton', () => ({ default: () => <div /> }))

import CapabilitiesPage from '../pages/CapabilitiesPage'

function wrap(initialEntry: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <QueryClientProvider client={qc}>
        <CapabilitiesPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('CapabilitiesPage — grouped rail with the Knowledge tab', () => {
  it('renders the three group headers', async () => {
    wrap('/capabilities')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Knowledge' })).toBeTruthy())
    // Group headers are plain text nodes above the first tab of each group.
    expect(screen.getByText('Agent')).toBeTruthy()
    expect(screen.getByText('Knowledge & instructions')).toBeTruthy()
    expect(screen.getByText('Automation')).toBeTruthy()
  })

  it('?tab=knowledge (the /knowledge redirect target) mounts KnowledgePage embedded', async () => {
    wrap('/capabilities?tab=knowledge')
    await waitFor(() => expect(screen.getByTestId('knowledge-pane')).toBeTruthy())
    expect(screen.getByTestId('knowledge-pane').textContent).toBe('embedded')
  })
})
