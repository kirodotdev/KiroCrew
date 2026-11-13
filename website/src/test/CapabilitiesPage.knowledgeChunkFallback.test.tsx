/**
 * The Knowledge tab is a lazy chunk (CapabilitiesPage.tsx), so the pane has a
 * Suspense boundary of its own. Pins that its fallback is the ContentSkeleton
 * the Templates tab shows while ITS chunk loads: with `fallback={null}` the
 * first visit after a deploy showed an empty pane until the chunk landed
 * (review finding on PR #8985).
 *
 * Both chunks are mocked with an import that never settles, so the fallback
 * is the steady state the assertions read, not a frame a resolved chunk
 * could have replaced by the time they run.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import React from 'react'

// Tab bodies are irrelevant; only the two Suspense fallbacks are under test.
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => <div /> }))
vi.mock('../pages/HooksPage', () => ({ default: () => <div /> }))
vi.mock('../pages/connections/ConnectionsPage', () => ({ default: () => <div /> }))
vi.mock('../pages/overview', () => ({
  SkillsTab: () => <div />,
  PromptsTab: () => <div />,
  SteeringTab: () => <div />,
}))
vi.mock('../components/RestartButton', () => ({ default: () => <div /> }))
// The two lazy chunks, held pending for the whole file.
vi.mock('../pages/KnowledgePage', () => new Promise<never>(() => {}))
vi.mock('../pages/overview/AgentTemplatesTab', () => new Promise<never>(() => {}))

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

/** Skeleton boxes on screen (`Skeleton` renders `data-slot="skeleton"`). */
async function skeletonCount(initialEntry: string): Promise<number> {
  const { container, unmount } = wrap(initialEntry)
  // The tab resolves from the query string, so the pane is not necessarily in
  // the first frame.
  await waitFor(() => expect(container.querySelector('[data-slot="skeleton"]')).toBeTruthy())
  const count = container.querySelectorAll('[data-slot="skeleton"]').length
  unmount()
  return count
}

describe('CapabilitiesPage — the Knowledge chunk loads behind a skeleton', () => {
  it('shows the same ContentSkeleton the Templates tab shows while its chunk loads', async () => {
    const templates = await skeletonCount('/capabilities?tab=templates')
    const knowledge = await skeletonCount('/capabilities?tab=knowledge')
    expect(knowledge).toBeGreaterThan(0)
    expect(knowledge).toBe(templates)
  })
})
