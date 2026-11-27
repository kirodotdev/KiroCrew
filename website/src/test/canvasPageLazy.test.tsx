/**
 * The canvas page's controls live outside its lazily loaded board, so they stay while the
 * board's chunk loads or fails to load: Back, the version and the menu are never replaced by
 * a spinner or the failure notice.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, screen, within } from '@testing-library/react'
import { emptyCanvas, serializeCanvas } from '../components/canvas/canvasModel'
import type { Artifact } from '../types'
import { namedCeiling } from './namedCeiling'

const chunk = vi.hoisted(() => ({ mode: 'pending' as 'pending' | 'fail' }))

vi.mock('../api/client')
vi.mock('../pages/ChatPage', () => ({ default: () => <div />, PREFILL_STORAGE_KEY: 'kirocrew_prefill' }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
// The board's chunk: never arrives, or fails to load.
vi.mock('../components/canvas/CanvasArtifactBody', () => {
  if (chunk.mode === 'fail') throw new Error('chunk failed to load')
  return new Promise(() => {})
})

const canvas: Artifact = {
  slug: 'board', name: 'Board', kind: 'canvas', source: 'manual', description: '', tags: [],
  version: 1, content_token: 't1', content: serializeCanvas(emptyCanvas()),
  created_at: '2026-11-13T12:00:00Z', updated_at: '2026-11-13T12:00:00Z',
}

// Each test re-imports the page's module graph, then waits on the artifact query and the board's
// lazy chunk settling (lazyCanvas → lazyRetry → CanvasArtifactBody), several seconds under a
// loaded runner: every wait on that chain uses this.
const PAGE_READY = namedCeiling('PAGE_READY', 5000)

async function renderPage() {
  // A fresh module graph per test: `lazy()` caches the first load's outcome for the module's life.
  vi.resetModules()
  const { api } = await import('../api/client')
  const { default: ArtifactDetailPage } = await import('../pages/ArtifactDetailPage')
  const { NavigationLeaveGuardProvider } = await import('../components/NavigationLeaveGuard')
  const { renderWithProviders } = await import('./helpers')
  const { Route, Routes } = await import('react-router-dom')
  vi.mocked(api).artifactFolders = vi.fn().mockResolvedValue({ folders: [] })
  vi.mocked(api).artifact = vi.fn().mockResolvedValue(canvas)
  vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: canvas.slug, versions: [1] })
  vi.mocked(api).artifactVersion = vi.fn().mockResolvedValue(canvas)
  vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: canvas.slug, events: [] })
  vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
  vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
  renderWithProviders(
    <NavigationLeaveGuardProvider>
      <Routes>
        <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
        <Route path="/artifacts" element={<div data-testid="library" />} />
      </Routes>
    </NavigationLeaveGuardProvider>,
    { route: `/artifacts/${canvas.slug}` },
  )
  return await screen.findByTestId('canvas-page-pill', undefined, PAGE_READY)
}

function expectControls(pillEl: HTMLElement) {
  const pill = within(pillEl)
  expect(pill.getByRole('button', { name: 'Back' })).toBeInTheDocument()
  expect(pill.getByRole('button', { name: 'Version' })).toBeInTheDocument()
  expect(pill.getByRole('button', { name: 'More actions' })).toBeInTheDocument()
  expect(pill.getByRole('button', { name: 'Toggle agent chat' })).toBeInTheDocument()
}

beforeEach(() => { chunk.mode = 'pending' })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('canvas page while its board is not loaded', () => {
  it('keeps Back, Version, chat and More while the chunk is still loading', async () => {
    const pill = await renderPage()
    expectControls(pill)
    expect(screen.queryByTestId('canvas-artifact-page')).toBeNull()
    openMore()
    expect(await screen.findByRole('menuitem', { name: 'Activity' }, PAGE_READY)).toBeInTheDocument()
  })

  it('keeps the controls above the failure notice when the chunk fails, and Back still leaves', async () => {
    chunk.mode = 'fail'
    const pill = await renderPage()
    const tryAgain = await screen.findByRole('button', { name: 'Try Again' }, PAGE_READY)
    expectControls(pill)
    expect(pill).not.toContainElement(tryAgain)
    // The controls leave the board's corner for the flow, so they sit above the notice, not on it.
    // No editor renders on a failed board, so the controls also drop the zoom corner's reserve.
    const controls = screen.getByTestId('canvas-page-controls')
    expect(controls).toContainElement(pill)
    expect(controls).not.toHaveClass('absolute')
    expect(controls).not.toHaveClass('right-[5.5rem]')
    expect(controls.compareDocumentPosition(tryAgain) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    fireEvent.click(within(pill).getByRole('button', { name: 'Back' }))
    expect(await screen.findByTestId('library', undefined, PAGE_READY)).toBeInTheDocument()
  })
})

function openMore() {
  fireEvent.keyDown(screen.getByRole('button', { name: 'More actions' }), { key: 'Enter' })
}
