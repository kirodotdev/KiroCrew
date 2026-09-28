/**
 * MemoryGraphTab must not be pulled into the eager graph by DeveloperPage.
 *
 * It is the only eager owner of the sigma/graphology stack, which the build
 * isolates as `vendor-graph` (~180 KB gzip). A static import from DeveloperPage
 * put that chunk in the entry modulepreload set for every page load, for one of
 * eight tabs on an internals-only route.
 *
 * As in the mermaid test, the mock factory counts LOADS: vitest runs it the
 * first time the module is requested, so a static import registers a load simply
 * by importing DeveloperPage.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const probe = vi.hoisted(() => ({ loads: 0 }))

vi.mock('../pages/overview/MemoryGraphTab', () => {
  probe.loads++
  return { default: () => <div data-testid="memory-graph" /> }
})

// The sibling tabs are heavy and irrelevant here — only the memory tab's
// loading boundary is under test.
vi.mock('../pages/LogsPage', () => ({ LogViewer: () => <div /> }))
vi.mock('../pages/SystemPage', () => ({ default: () => <div /> }))
vi.mock('../pages/TelemetryPanel', () => ({ default: () => <div /> }))
vi.mock('../pages/SessionArchive', () => ({ default: () => <div /> }))
vi.mock('../pages/LocalStorageDebug', () => ({ default: () => <div /> }))
vi.mock('../pages/settings/McpManagement', () => ({ McpManagement: () => <div /> }))
vi.mock('../pages/overview', () => ({
  KiroCrewCfgTab: () => <div />,
  AgentCfgTab: () => <div />,
}))

import DeveloperPage from '../pages/DeveloperPage'

describe('DeveloperPage defers the memory graph', () => {
  it('does not load MemoryGraphTab when the page module is imported', () => {
    expect(probe.loads).toBe(0)
  })

  it('does not load it while a different tab is shown', async () => {
    render(<MemoryRouter initialEntries={['/developer?tab=logs']}><DeveloperPage /></MemoryRouter>)
    await Promise.resolve()
    expect(probe.loads).toBe(0)
  })

  it('loads it when the memory tab is selected', async () => {
    render(<MemoryRouter initialEntries={['/developer?tab=memory']}><DeveloperPage /></MemoryRouter>)
    expect(await screen.findByTestId('memory-graph')).toBeInTheDocument()
    expect(probe.loads).toBe(1)
  })
})
