/**
 * Library drag-to-folder arms an undo offer — for artifacts AND folder nests.
 *
 * Both moves used to complete silently: a mis-aimed drop filed the artifact
 * (or re-parented the folder) somewhere the user never chose, with nothing on
 * screen saying where it went. The library now mirrors ChatSidebar's
 * session-drag wiring: a DRAG-initiated move performs the move and parks its
 * inverse in useMoveUndo, and MoveUndoBar offers it back for 8 seconds.
 *
 * The dnd-kit pointer-drag lifecycle can't be simulated in jsdom (it needs
 * real PointerEvents plus layout measurement), so this stubs the DndContext,
 * captures the page's real `onDragEnd`, and invokes it with the same payloads
 * the draggable cards declare (`satisfies LibraryDrag`) — the established
 * pattern from ChatSidebar.dragFreezeOrder.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import type { Artifact, ArtifactFolder } from '../types'

vi.mock('../api/client')

// Captured lifecycle props from the page's DndContext. Stubbing the context
// (children pass through) is what lets the real handlers run without a gesture.
const dnd = vi.hoisted(() => ({ handlers: {} as Record<string, ((e: unknown) => void) | undefined> }))

vi.mock('@dnd-kit/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@dnd-kit/core')>()
  return {
    ...actual,
    DndContext: (props: { children?: unknown; onDragEnd?: (e: unknown) => void }) => {
      dnd.handlers.onDragEnd = props.onDragEnd
      return props.children as never
    },
  }
})

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'initial', 'animate', 'exit', 'transition', 'variants',
    'whileHover', 'whileTap', 'whileInView', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef<HTMLElement, Record<string, unknown> & { children?: React.ReactNode }>((props, ref) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children' || FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    useReducedMotion: () => true,
  }
})

// VirtuosoMasonry virtualizes against real layout, which jsdom lacks.
vi.mock('@virtuoso.dev/masonry', () => ({
  VirtuosoMasonry: () => null,
}))

import ArtifactsPage from '../pages/ArtifactsPage'
import { MOVE_UNDO_MS } from '../components/MoveUndoBar'

const ARCHIVE = 'folder-archive'
const LATER = 'folder-later'
const SLUG = 'quarterly-report'

const artifact: Artifact = {
  slug: SLUG, name: 'Quarterly report', kind: 'markdown', source: 'chat',
  pinned: false, description: '', tags: [], version: 1,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:00:00.000000+00:00',
  folder_id: '',
} as Artifact

const folders: ArtifactFolder[] = [
  { id: ARCHIVE, name: 'Archive', parent_id: '', order: 0 } as ArtifactFolder,
  { id: LATER, name: 'Later', parent_id: '', order: 1 } as ArtifactFolder,
]

function renderPage(over: { artifact?: Partial<Artifact>; folders?: ArtifactFolder[] } = {}) {
  vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [{ ...artifact, ...over.artifact }] })
  // STATEFUL folder mock: `updateArtifactFolder` persists the move, so the
  // post-settle refetch returns the moved state. A mock frozen on the pre-move
  // list would retire a live offer (live state stops matching = offer dropped)
  // and hide exactly the lifecycle these tests exist to pin.
  const serverFolders = (over.folders ?? folders).map(f => ({ ...f }))
  vi.mocked(api).artifactFolders = vi.fn().mockImplementation(async () => ({ folders: serverFolders.map(f => ({ ...f })) }))
  vi.mocked(api).updateArtifactFolder = vi.fn().mockImplementation(async (id: string, body: { parent_id?: string }) => {
    const f = serverFolders.find(x => x.id === id)
    if (f && body.parent_id !== undefined) f.parent_id = body.parent_id
    return {}
  })
  return renderWithProviders(<ArtifactsPage />)
}

/** The drop handler exists once the DndContext mounts, but the FOLDER list is
 *  what the arm site reads the dragged folder's current parent from — wait for
 *  a folder card to render so the drop is judged against loaded data. */
async function ready() {
  await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
  await waitFor(() => expect(screen.getByText('Archive')).toBeTruthy())
}

const barIn = (c: HTMLElement) => c.querySelector('[data-testid="session-move-undo"]') as HTMLElement | null
const undoButtonIn = (c: HTMLElement) => c.querySelector('[data-testid="session-move-undo-button"]') as HTMLElement

/** Invoke the page's real onDragEnd with the payload a card drop produces. */
function dropArtifact(toFolderId: string | '', fromFolderId = '') {
  act(() => {
    dnd.handlers.onDragEnd?.({
      active: { id: `artifact:${SLUG}`, data: { current: { type: 'artifact', slug: SLUG, name: artifact.name, folderId: fromFolderId } } },
      over: { id: `folder-drop:${toFolderId}`, data: { current: { type: 'folder-drop', folderId: toFolderId } } },
    })
  })
}

function dropFolder(id: string, toFolderId: string | '') {
  act(() => {
    dnd.handlers.onDragEnd?.({
      active: { id: `folder:${id}`, data: { current: { type: 'folder', id, name: folders.find(f => f.id === id)?.name ?? id } } },
      over: { id: `folder-drop:${toFolderId}`, data: { current: { type: 'folder-drop', folderId: toFolderId } } },
    })
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  dnd.handlers = {}
  vi.mocked(api).setArtifactFolder = vi.fn().mockResolvedValue({})
  vi.mocked(api).sandboxDocUrl = vi.fn().mockResolvedValue({ url: '/sandbox-doc/test/tok' })
})
afterEach(() => { vi.useRealTimers() })

describe('artifact drag-to-folder undo', () => {
  it('performs the move and offers it back, naming the destination', async () => {
    const { container } = renderPage()
    await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
    expect(barIn(container)).toBeNull()
    dropArtifact(ARCHIVE)
    await waitFor(() => expect(api.setArtifactFolder).toHaveBeenCalledWith(SLUG, ARCHIVE))
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    expect(barIn(container)!.textContent).toContain('Archive')
    // The card leaves the current view on drop, so the VISIBLE row (not just
    // the tooltip) must say which card went.
    expect(barIn(container)!.textContent).toContain('Quarterly report')
  })

  it('undo posts the ORIGINAL folder back, then retires the offer', async () => {
    const { container } = renderPage()
    await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
    dropArtifact(ARCHIVE)
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    vi.mocked(api.setArtifactFolder).mockClear()
    fireEvent.click(undoButtonIn(container))
    // '' — the artifact move hook's contract for "unfiled root".
    await waitFor(() => expect(api.setArtifactFolder).toHaveBeenCalledWith(SLUG, ''))
    expect(barIn(container)).toBeNull()
  })

  it('arms nothing when the artifact is dropped on the folder it already lives in', async () => {
    const { container } = renderPage({ artifact: { folder_id: ARCHIVE } })
    await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
    dropArtifact(ARCHIVE, ARCHIVE)
    await Promise.resolve()
    expect(api.setArtifactFolder).not.toHaveBeenCalled()
    expect(barIn(container)).toBeNull()
  })

  it('expires the offer on its own clock', async () => {
    const { container } = renderPage()
    await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
    // Fake timers only from here: the page's initial queries need real timers
    // to resolve, but the deadline scheduled at arm time must be reachable.
    vi.useFakeTimers()
    dropArtifact(ARCHIVE)
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(barIn(container)).toBeTruthy()
    act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS + 50) })
    expect(barIn(container)).toBeNull()
  })

  it('a second move supersedes the first — the bar offers only the newest inverse', async () => {
    const { container } = renderPage()
    await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
    dropArtifact(ARCHIVE)
    await waitFor(() => expect(barIn(container)?.textContent).toContain('Archive'))
    // The drag payload carries the folder the card was rendered in — after the
    // first (optimistic) move that is Archive.
    dropArtifact(LATER, ARCHIVE)
    await waitFor(() => expect(barIn(container)?.textContent).toContain('Later'))
    vi.mocked(api.setArtifactFolder).mockClear()
    fireEvent.click(undoButtonIn(container))
    // Undo replays the SECOND move's inverse (back to Archive), not the first's.
    await waitFor(() => expect(api.setArtifactFolder).toHaveBeenCalledWith(SLUG, ARCHIVE))
  })
})

describe('folder nest undo', () => {
  it('performs the nest and offers it back, naming the destination', async () => {
    const { container } = renderPage()
    await ready()
    dropFolder(LATER, ARCHIVE)
    await waitFor(() => expect(api.updateArtifactFolder).toHaveBeenCalledWith(LATER, { parent_id: ARCHIVE }))
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    expect(barIn(container)!.textContent).toContain('Archive')
  })

  it('undo restores the previous parent', async () => {
    // Later starts nested under Archive and is dragged to the root drop zone.
    const nested: ArtifactFolder[] = [
      folders[0],
      { ...folders[1], parent_id: ARCHIVE } as ArtifactFolder,
    ]
    const { container } = renderPage({ folders: nested })
    await ready()
    dropFolder(LATER, '')
    await waitFor(() => expect(api.updateArtifactFolder).toHaveBeenCalledWith(LATER, { parent_id: '' }))
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    vi.mocked(api.updateArtifactFolder).mockClear()
    fireEvent.click(undoButtonIn(container))
    await waitFor(() => expect(api.updateArtifactFolder).toHaveBeenCalledWith(LATER, { parent_id: ARCHIVE }))
    expect(barIn(container)).toBeNull()
  })

  it('expires the offer on its own clock', async () => {
    const { container } = renderPage()
    await ready()
    vi.useFakeTimers()
    dropFolder(LATER, ARCHIVE)
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(barIn(container)).toBeTruthy()
    act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS + 50) })
    expect(barIn(container)).toBeNull()
  })

  it('arming a folder nest DISMISSES a live artifact offer — one bar, and no resurrection', async () => {
    const { container } = renderPage()
    await ready()
    dropArtifact(ARCHIVE)
    await waitFor(() => expect(barIn(container)?.textContent).toContain('Archive'))
    dropFolder(LATER, ARCHIVE)
    await waitFor(() => expect(api.updateArtifactFolder).toHaveBeenCalled())
    // One bar (one ⌘Z listener), and it is the folder nest's: both offers name
    // Archive as the destination, so the discriminator is the moved item's
    // tooltip ("… — Later" vs "… — Quarterly report").
    await waitFor(() => {
      const bars = container.querySelectorAll('[data-testid="session-move-undo"]')
      expect(bars.length).toBe(1)
      expect(bars[0].querySelector('[title*="Later"]')).toBeTruthy()
      expect(bars[0].querySelector('[title*="Quarterly report"]')).toBeNull()
    })
    // The displaced artifact offer was RETIRED, not hidden: undoing the winner
    // must not let the older bar re-mount under the cursor, where a second
    // click on Undo would reverse two unrelated moves.
    fireEvent.click(undoButtonIn(container))
    await waitFor(() => expect(barIn(container)).toBeNull())
    await act(async () => { await Promise.resolve() })
    expect(barIn(container)).toBeNull()
  })

  it('rolls back only this mutation\u2019s fields when the folder update fails', async () => {
    // The rollback is field-scoped compare-and-set, not a whole-list snapshot
    // restore: a snapshot would clobber unrelated concurrent optimistic
    // changes. Reject the PATCH and assert the optimistic parent_id snaps back.
    const { container, queryClient } = renderPage()
    await ready()
    vi.mocked(api.updateArtifactFolder).mockRejectedValueOnce(new Error('boom'))
    dropFolder(LATER, ARCHIVE)
    await waitFor(() => expect(api.updateArtifactFolder).toHaveBeenCalled())
    // Failure path: the offer never goes live, so no bar…
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(barIn(container)).toBeNull()
    // …and the cache's parent_id is restored to its pre-move value.
    await waitFor(() => {
      const cached = queryClient.getQueryData<{ folders: ArtifactFolder[] }>(['artifact-folders'])
      expect(cached?.folders.find(f => f.id === LATER)?.parent_id ?? '').toBe('')
    })
  })
})
