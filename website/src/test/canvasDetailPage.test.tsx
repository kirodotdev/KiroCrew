import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react'
import { Route, Routes } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { NavigationLeaveGuardProvider } from '../components/NavigationLeaveGuard'
import { emptyCanvas, serializeCanvas } from '../components/canvas/canvasModel'
import { hasFullHeightPage } from '../lib/artifactKinds'
import { api } from '../api/client'
import type { Artifact } from '../types'
import { renderWithProviders } from './helpers'

vi.mock('../api/client')
vi.mock('../pages/ChatPage', () => ({ default: () => <div />, PREFILL_STORAGE_KEY: 'kirocrew_prefill' }))

const canvas: Artifact = {
  slug: 'board', name: 'Board', kind: 'canvas', source: 'manual', description: 'Sprint plan', tags: [],
  version: 1, content_token: 't1', content: serializeCanvas(emptyCanvas()),
  created_at: '2026-11-13T12:00:00Z', updated_at: '2026-11-13T12:00:00Z',
}
const notes: Artifact = { ...canvas, slug: 'notes', name: 'Notes', kind: 'markdown', content: '# Notes', content_token: undefined }

async function renderPage(artifact: Artifact) {
  vi.mocked(api).artifact = vi.fn().mockResolvedValue(artifact)
  vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: artifact.slug, versions: [1] })
  vi.mocked(api).artifactVersion = vi.fn().mockResolvedValue(artifact)
  vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: artifact.slug, events: [] })
  vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
  vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
  renderWithProviders(
    <NavigationLeaveGuardProvider>
      <Routes>
        <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
        <Route path="/artifacts" element={<div data-testid="library" />} />
      </Routes>
    </NavigationLeaveGuardProvider>,
    { route: `/artifacts/${artifact.slug}` },
  )
  await screen.findByRole('button', { name: 'More actions' })
  await waitFor(() => expect(api.artifactEvents).toHaveBeenCalled())
}

/** The page body: the nearest ancestor of the artifact body that is a flex item of the page. */
const pageBody = (inner: HTMLElement) => inner.closest('div.flex-1.min-h-0') as HTMLElement

beforeEach(() => { vi.mocked(api).artifactFolders = vi.fn().mockResolvedValue({ folders: [] }) })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('hasFullHeightPage', () => {
  it('is true for a canvas and no other kind', () => {
    expect(hasFullHeightPage('canvas')).toBe(true)
    for (const kind of ['markdown', 'text', 'json', 'svg', 'html', 'widget', 'dashboard', 'image', 'webapp'] as const) {
      expect(hasFullHeightPage(kind)).toBe(false)
    }
  })
})

describe('canvas detail page', () => {
  it('lets the canvas take the height the layout leaves, and never scrolls the page', async () => {
    await renderPage(canvas)
    const box = await screen.findByTestId('canvas-artifact')
    // No guessed offset from the page chrome; the box is a flex item that fills what is left.
    expect(box.style.height).toBe('')
    expect(box.className).toContain('flex-1')
    const body = pageBody(box)
    expect(body.className).not.toContain('overflow-y-auto')
    expect(body.className).toContain('min-h-0')
  })

  it('keeps one slim row and holds the rest in a Details bar until it is opened', async () => {
    await renderPage(canvas)
    await screen.findByTestId('canvas-artifact')
    // The row: Back, title, Version and More stay; the slug line, folder, star, tags and the footer wait.
    expect(screen.getByRole('button', { name: /Back/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Board' })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Version' })).toBeInTheDocument()
    for (const hidden of ['Star artifact', 'Add a tag']) expect(screen.queryByRole('button', { name: hidden })).toBeNull()
    expect(screen.queryByRole('combobox', { name: 'Compare with' })).toBeNull()
    expect(screen.queryByText(/^Created /)).toBeNull()
    expect(screen.queryByText('Activity')).toBeNull()

    const details = screen.getByRole('button', { name: 'Details' })
    expect(details).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(details)
    expect(details).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('button', { name: 'Star artifact' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add a tag' })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Compare with' })).toBeInTheDocument()
    expect(screen.getByText('Sprint plan')).toBeInTheDocument()
    expect(screen.getByText(/Created /)).toBeInTheDocument()
    expect(screen.getByText('Activity')).toBeInTheDocument()
    // The canvas is still the page's one scroller: the bar scrolls inside itself.
    expect(screen.getByTestId('canvas-artifact')).toBeInTheDocument()

    fireEvent.click(details)
    expect(screen.queryByRole('button', { name: 'Add a tag' })).toBeNull()
  })

  it('leaves through Back', async () => {
    await renderPage(canvas)
    await screen.findByTestId('canvas-artifact')
    fireEvent.click(screen.getByRole('button', { name: /Back/ }))
    expect(await screen.findByTestId('library')).toBeInTheDocument()
  })

  it('opens an empty canvas on the same page', async () => {
    await renderPage(canvas)
    expect(await screen.findByTestId('canvas-editor')).toBeInTheDocument()
    expect(screen.queryAllByTestId('canvas-shape')).toHaveLength(0)
  })
})

describe('a page of any other kind', () => {
  it('keeps the scrolling layout, its header chips and its footer', async () => {
    await renderPage(notes)
    expect(screen.queryByRole('button', { name: 'Details' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Star artifact' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add a tag' })).toBeInTheDocument()
    expect(screen.getByText(/Created /)).toBeInTheDocument()
    expect(screen.getByText('Activity')).toBeInTheDocument()
    expect(pageBody(screen.getByText('Activity')).className).toContain('overflow-y-auto')
  })
})
