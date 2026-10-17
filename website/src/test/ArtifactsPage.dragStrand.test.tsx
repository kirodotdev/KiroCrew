/**
 * The library's drag mirror must not outlive dnd-kit's own drag store.
 *
 * `ArtifactsPage` sets `activeDrag` / `overFolderId` from `onDragStart` /
 * `onDragOver` and clears them from `onDragEnd` / `onDragCancel`. dnd-kit fires
 * the end callbacks only once a layout effect has populated
 * `sensorContext.active`, on the commit after the start, so a
 * press-move-release finishing before that commit leaves dnd-kit idle with no
 * callback: the folder tree keeps its drag-mode unfile lane and a folder stays
 * lit as a drop target with nothing being dragged. `DndActiveProbe` plus the
 * page's reconciler clear the mirror on the next commit instead.
 *
 * The dnd-kit pointer lifecycle cannot be simulated in jsdom, so this stubs the
 * DndContext, captures the page's real handlers, and drives `useDndContext`'s
 * `active` as dnd-kit's store — the pattern from
 * ChatSidebar.dragFreezeOrder.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import ArtifactsPage from '../pages/ArtifactsPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import type { Artifact, ArtifactFolder } from '../types'

vi.mock('../api/client')

// Captured lifecycle props from the page's DndContext (children pass through,
// so the real handlers run), plus `active` standing in for dnd-kit's store.
const dnd = vi.hoisted(() => ({
  handlers: {} as Record<string, ((e: unknown) => void) | undefined>,
  active: null as { id: string } | null,
}))

vi.mock('@dnd-kit/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@dnd-kit/core')>()
  return {
    ...actual,
    DndContext: (props: {
      children?: unknown
      onDragStart?: (e: unknown) => void
      onDragOver?: (e: unknown) => void
      onDragEnd?: (e: unknown) => void
      onDragCancel?: (e: unknown) => void
    }) => {
      dnd.handlers.onDragStart = props.onDragStart
      dnd.handlers.onDragOver = props.onDragOver
      dnd.handlers.onDragEnd = props.onDragEnd
      dnd.handlers.onDragCancel = props.onDragCancel
      return props.children as never
    },
    useDndContext: () => ({ ...actual.useDndContext(), active: dnd.active }),
  }
})

vi.mock('@virtuoso.dev/masonry', () => ({ VirtuosoMasonry: () => null }))

const FOLDER = 'folder-archive'
const SLUG = 'quarterly-report'

const artifact = {
  slug: SLUG, name: 'Quarterly report', kind: 'markdown', source: 'chat',
  pinned: false, description: '', tags: [], version: 1,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:00:00.000000+00:00',
  folder_id: '',
} as Artifact

const folders = [
  { id: FOLDER, name: 'Archive', parent_id: '', order: 0, item_count: 0 } as ArtifactFolder,
]

function renderPage() {
  const m = vi.mocked(api)
  m.artifacts = vi.fn().mockResolvedValue({ artifacts: [artifact] })
  m.artifactFolders = vi.fn().mockResolvedValue({ folders })
  m.artifactSessionDocs = vi.fn().mockResolvedValue({ docs: [] })
  m.getArtifactPublishProviders = vi.fn().mockResolvedValue({ providers: [], kind: 'widget' })
  m.publishProviders = vi.fn().mockResolvedValue({ providers: [] })
  m.themeBoot = vi.fn().mockResolvedValue({})
  return renderWithProviders(<ArtifactsPage />)
}

/** The folder tree's own drag-mode affordance: the unfile lane only offers a
 *  drop hint while `dragActive` is true. */
const unfileHint = () => screen.queryByText(/drop here to unfile/)
/** The folder row lights up as a drop target from `overFolderId`. */
const folderLit = () => !!screen.getByText('Archive').closest('tr')?.className.includes('bg-accent/15')

const startDragOverFolder = () => {
  act(() => {
    dnd.handlers.onDragStart?.({
      active: { id: `artifact:${SLUG}`, data: { current: { type: 'artifact', slug: SLUG, name: artifact.name, folderId: '' } } },
    })
    dnd.handlers.onDragOver?.({
      over: { id: `folder-row-drop:${FOLDER}`, data: { current: { type: 'folder-drop', folderId: FOLDER } } },
    })
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  localStorage.setItem('mc-artifacts-view', 'table')
  dnd.handlers = {}
  dnd.active = null
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false }))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ArtifactsPage — library drag mirror is reconciled against dnd-kit', () => {
  it('clears drag mode and the folder highlight when dnd-kit goes idle with no end callback', async () => {
    const { rerender } = renderPage()
    await waitFor(() => expect(screen.getByText('Archive')).toBeTruthy())

    dnd.active = { id: `artifact:${SLUG}` }
    startDragOverFolder()
    expect(unfileHint()).toBeTruthy()
    expect(folderLit()).toBe(true)

    // The store goes idle and nothing calls onDragEnd / onDragCancel. The next
    // commit must clear both halves of the mirror.
    dnd.active = null
    rerender(<ArtifactsPage />)

    expect(unfileHint()).toBeNull()
    expect(folderLit()).toBe(false)
  })

  it('holds drag mode and the highlight while dnd-kit still reports the drag', async () => {
    // Guards the case above against passing vacuously: a commit alone must not
    // clear the mirror, only a commit with an idle store.
    const { rerender } = renderPage()
    await waitFor(() => expect(screen.getByText('Archive')).toBeTruthy())

    dnd.active = { id: `artifact:${SLUG}` }
    startDragOverFolder()
    rerender(<ArtifactsPage />)

    expect(unfileHint()).toBeTruthy()
    expect(folderLit()).toBe(true)
  })

  it('tears the mirror down on a reported end, with the store already idle', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('Archive')).toBeTruthy())

    dnd.active = { id: `artifact:${SLUG}` }
    startDragOverFolder()
    dnd.active = null
    act(() => {
      dnd.handlers.onDragEnd?.({
        active: { id: `artifact:${SLUG}`, data: { current: { type: 'artifact', slug: SLUG, name: artifact.name, folderId: '' } } },
        over: null,
      })
    })

    expect(unfileHint()).toBeNull()
    expect(folderLit()).toBe(false)
  })
})
