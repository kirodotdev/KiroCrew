/**
 * Home's table rows file into a folder by drag, as its gallery cards do.
 *
 * dnd-kit's pointer lifecycle cannot run in jsdom, so this records which
 * elements register as draggables and with what move data; the page's own
 * `handleDragEnd` reads exactly that data to move the artifact.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type * as React from 'react'
import { screen, waitFor, within, cleanup } from '@testing-library/react'
import ArtifactsPage from '../pages/ArtifactsPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import type { Artifact } from '../types'

vi.mock('../api/client')
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
vi.mock('@virtuoso.dev/masonry', () => ({ VirtuosoMasonry: () => null }))

const drags = vi.hoisted(() => new Map<string, Record<string, unknown>>())

vi.mock('../components/dnd', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../components/dnd')>()
  return {
    ...actual,
    DndDraggable: ({ id, data, children }: {
      id: string
      data: Record<string, unknown>
      children: (p: { setNodeRef: (el: HTMLElement | null) => void; listeners: Record<string, unknown>; attributes: Record<string, unknown>; isDragging: boolean }) => React.ReactNode
    }) => {
      drags.set(id, data)
      return <>{children({
        setNodeRef: (el) => { el?.setAttribute('data-drag-id', id) },
        listeners: {},
        attributes: {},
        isDragging: false,
      })}</>
    },
  }
})

const mk = (slug: string, overrides: Partial<Artifact> = {}): Artifact => ({
  slug, name: slug.replace(/-/g, ' '), kind: 'markdown', source: 'chat', pinned: false,
  description: '', tags: [], version: 1,
  created_at: '2026-11-01T10:00:00+00:00', updated_at: '2026-11-01T10:00:00+00:00',
  ...overrides,
})

beforeEach(() => {
  drags.clear()
  localStorage.clear()
  localStorage.setItem('mc-artifacts-view', 'table')
  const m = vi.mocked(api)
  m.artifacts = vi.fn().mockResolvedValue({
    artifacts: [mk('kept-doc', { folder_id: 'ops' }), mk('chat-widget', { sweepable: true })],
    output_cap: 200,
  })
  m.artifactFolders = vi.fn().mockResolvedValue({ folders: [{ id: 'ops', name: 'Ops', order: 0, parent_id: '', item_count: 1 }] })
  m.artifactSessionDocs = vi.fn().mockResolvedValue({ docs: [] })
  m.getArtifactPublishProviders = vi.fn().mockResolvedValue({ providers: [], kind: 'widget' })
  m.publishProviders = vi.fn().mockResolvedValue({ providers: [] })
  m.themeBoot = vi.fn().mockResolvedValue({})
  m.artifact = vi.fn().mockImplementation((slug: string) => Promise.resolve(mk(slug)))
})
afterEach(() => cleanup())

describe('Home table view', () => {
  it('registers every row in both groups as a draggable carrying its move data', async () => {
    renderWithProviders(<ArtifactsPage />)
    const home = await waitFor(() => screen.getByTestId('artifacts-home'))
    const kept = await waitFor(() => within(home).getByText('kept doc').closest('li'))
    const output = within(home).getByText('chat widget').closest('li')
    expect(kept?.getAttribute('data-drag-id')).toBe('row-artifact:kept-doc')
    expect(output?.getAttribute('data-drag-id')).toBe('row-artifact:chat-widget')
    expect(drags.get('row-artifact:kept-doc')).toEqual({ type: 'artifact', slug: 'kept-doc', name: 'kept doc', folderId: 'ops' })
    expect(drags.get('row-artifact:chat-widget')).toEqual({ type: 'artifact', slug: 'chat-widget', name: 'chat widget', folderId: '' })
  })
})
