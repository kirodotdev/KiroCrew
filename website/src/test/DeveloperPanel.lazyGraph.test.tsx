/**
 * MemoryGraphTab must not be pulled into the eager graph by DeveloperPanel.
 *
 * It is the only eager owner of the sigma/graphology stack, which the build
 * isolates as `vendor-graph` (~180 KB gzip). A static import from the panel
 * would put that chunk in the entry modulepreload set for every page load.
 *
 * As in the mermaid test, the mock factory counts LOADS: vitest runs it the
 * first time the module is requested, so a static import registers a load simply
 * by importing DeveloperPanel.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const probe = vi.hoisted(() => ({ loads: 0 }))

vi.mock('../pages/overview/MemoryGraphTab', () => {
  probe.loads++
  return { default: () => <div data-testid="memory-graph" /> }
})

// The sibling pages are heavy and irrelevant here — only the memory page's
// loading boundary is under test.
vi.mock('../pages/LogsPage', () => ({ LogViewer: () => <div data-testid="log-viewer" /> }))
vi.mock('../pages/SystemPage', () => ({ default: () => <div /> }))
vi.mock('../pages/TelemetryPanel', () => ({ default: () => <div /> }))
vi.mock('../pages/SessionArchive', () => ({ default: () => <div /> }))
vi.mock('../pages/LocalStorageDebug', () => ({ default: () => <div /> }))
vi.mock('../pages/settings/McpManagement', () => ({ McpManagement: () => <div /> }))
vi.mock('../pages/overview', () => ({
  KiroCrewCfgTab: () => <div />,
  AgentCfgTab: () => <div />,
}))

import { DeveloperPanel } from '../pages/settings/DeveloperPanel'

// Read once, right after the static import and before any test renders, so
// the import check does not depend on which test runs first.
const loadsAtImport = probe.loads

// Await the LogViewer and MemoryGraphTab lazy imports in DeveloperPanel.tsx.
const LAZY_PAGE_TIMEOUT_MS = 5000

const at = (path: string) => (
  <MemoryRouter initialEntries={[path]}><DeveloperPanel basePath="/settings" /></MemoryRouter>
)

describe('DeveloperPanel defers the memory graph', () => {
  it('does not load MemoryGraphTab when the panel module is imported', () => {
    expect(loadsAtImport).toBe(0)
  })

  it('does not load it while a different page is shown', async () => {
    const before = probe.loads
    render(at('/settings/developer/logs'))
    expect(await screen.findByTestId('log-viewer', {}, { timeout: LAZY_PAGE_TIMEOUT_MS })).toBeInTheDocument()
    expect(probe.loads).toBe(before)
    expect(screen.queryByTestId('memory-graph')).toBeNull()
  })

  it('loads it when the memory page is selected', async () => {
    render(at('/settings/developer/memory'))
    expect(await screen.findByTestId('memory-graph', {}, { timeout: LAZY_PAGE_TIMEOUT_MS })).toBeInTheDocument()
    expect(probe.loads).toBe(1)
  })
})
