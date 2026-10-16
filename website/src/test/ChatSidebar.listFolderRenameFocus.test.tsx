/**
 * #13598 — ChatSidebar list-view folder rename: Enter and Escape restore focus
 * to the folder header row; a pointer blur moves nothing.
 *
 * Renders the folder row/block DIRECTLY from folderBlocks.tsx via
 * `createFolderBlocks` + `renderFolderBlock`, bypassing the full ChatSidebar.
 * Only the dnd-kit hooks that would otherwise require a live DnD context are
 * mocked; the component tree and the focus-restoration logic run unmodified.
 *
 * Regression contract: removing or breaking the
 *   `requestAnimationFrame(focusListFolderRow)`
 * path inside folderBlocks.tsx's `renderFolderHeader` will make the Enter and
 * Escape assertions fail here, because `[data-folder-row]` will not become
 * document.activeElement.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent, act } from '@testing-library/react'
import { useState, useRef, useCallback } from 'react'

// ── dnd-kit stubs ────────────────────────────────────────────────────────────
// `DndDroppable` (used inside renderFolderBlock's DndDroppable wrapper) calls
// useDroppable, which requires a live DndContext.  Mock only those hooks so
// the component tree renders in happy-dom without a DnD provider.
vi.mock('@dnd-kit/core', async (orig) => {
  const real = await orig<typeof import('@dnd-kit/core')>()
  return {
    ...real,
    useDroppable: () => ({ setNodeRef: () => {}, isOver: false }),
    useDndContext: () => ({ active: null }),
    useDraggable: () => ({ setNodeRef: () => {}, listeners: {}, attributes: {}, isDragging: false }),
    // DndContext and DragOverlay are only needed by listLanes; not used here.
    DndContext: ({ children }: { children: React.ReactNode }) => children,
    DragOverlay: () => null,
  }
})

// useSortable is used by SortableSubfolderBlock (child-folder path); with no
// child folders in this test it is never called, but the import itself must
// not throw when there is no SortableContext in scope.
vi.mock('@dnd-kit/sortable', async (orig) => {
  const real = await orig<typeof import('@dnd-kit/sortable')>()
  return {
    ...real,
    useSortable: () => ({
      listeners: {},
      attributes: {},
      setNodeRef: () => {},
      transform: null,
      transition: undefined,
      isDragging: false,
    }),
    SortableContext: ({ children }: { children: React.ReactNode }) => children,
  }
})

// ── late imports (after vi.mock hoisting) ────────────────────────────────────
import { createFolderBlocks } from '../pages/chat-sidebar/folderBlocks'
import { useImeGuard } from '../hooks/useImeGuard'
import type { SidebarScope } from '../pages/chat-sidebar/scope'
import type { LaneRows } from '../pages/chat-sidebar/laneRows'
import type { ChatFolder } from '../types'

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

const FOLDER_ID = 'folder-focus-13598'

const testFolder: ChatFolder = {
  id: FOLDER_ID,
  name: 'Focus Test Folder',
  collapsed: false,
  parent_id: '',
  color: '',
  icon: '',
}

/**
 * Minimal test rig: renders the REAL renderFolderBlock output from
 * folderBlocks.tsx with the rename Input already open (editingId === folder.id).
 * Provides only the shell fields that renderFolderBlock/renderFolderHeader
 * actually dereference for this narrow path.
 */
function FolderBlockRig({
  folder,
  onCommit,
}: {
  folder: ChatFolder
  onCommit?: (id: string, name: string) => void
}) {
  const [editingId, setEditingId] = useState<string | null>(folder.id)
  const [editName, setEditName] = useState(folder.name)
  const folderEditInputRef = useRef<HTMLInputElement | null>(null)
  const ime = useImeGuard()

  const renameCommit = useCallback(
    (id: string, name: string): boolean => {
      onCommit?.(id, name.trim())
      setEditingId(null)
      return true
    },
    [onCommit],
  )

  // Minimal shell — only the fields renderFolderBlock/renderFolderHeader read
  // on the rename path (editingId === folder.id, editScope === 'list', no
  // child folders or sessions).  TypeScript-cast to bypass the full interface.
  const shell = {
    // Rename state
    editingId,
    editScope: 'list',
    editName,
    setEditName,
    folderEditInputRef,
    ime,
    renameCommit,
    setEditingId,
    setEditScope: () => {},
    suppressMenuRestoreRef: { current: false },
    onMenuCloseAutoFocus: () => {},

    // Folder list (no children)
    folders: [folder],
    folderSubtrees: new Map<string, Set<string>>(),
    isFolderHidden: () => false,
    isFolderFilteredOut: () => false,
    folderCompare: () => 0,
    filterHiddenFolders: new Set<string>(),
    folderNameMatchIds: null,
    foldersWithActiveSubtree: new Set<string>(),

    // Lane state
    listNarrowed: false,
    searchRanked: false,
    hideEmptyFolderBody: false,
    folderConductorPin: false,
    folderDragWithheld: true,
    folderReorderable: false,
    activeDrag: null,
    boardLaneActive: false,
    connected: true,
    instantCloseId: null,
    folderSortMode: 'name',
    slotFilter: '',
    slotFolders: {} as Record<string, string>,
    unreadSet: new Set<string>(),
    pinned: { has: () => false } as unknown as ReturnType<typeof import('../pages/chat-sidebar/sidebarRows').useSidebarRows>['pinned'],
    staleExpanded: new Set<string>(),
    hiddenByContainer: new Map<string, ChatFolder[]>(),
    revealFlash: null,
    revealedContainers: new Set<string>(),

    // Stubs for list-rendering helpers
    sidebarRows: {
      folderTree: {
        rowsIn: () => [],
        allRowsIn: () => [],
      },
      isPinned: () => false,
    } as unknown as SidebarScope['sidebarRows'],
    splitStale: (rows: unknown[]) => ({ fresh: rows, stale: [] }),
    heldLane: (rows: unknown[], navScope: unknown, container: unknown) =>
      ({ rows, navScope, container }),
    startsAutomaticSection: () => false,
    isActiveRow: () => false,
    renderSessionRow: () => null,
    renderStaleSection: () => null,

    // No-op callbacks
    setFolderModal: () => {},
    setFolderCreateError: () => {},
    folderCreateError: null,
    createChatInFolder: () => {},
    onSelectSlot: undefined,
    onOpenSlotInNewTab: undefined,
    armHold: () => {},
    disarmHold: () => {},
    toggleCollapse: () => {},
    moveFolderTo: () => {},
    dispatch: () => {},
    deleteFolderWithConfirm: () => {},
    setFolderActionError: () => {},
    toggleFolderFilter: () => {},
    toggleReveal: () => {},
    updateFolderMutation: { mutate: () => {} },
    setDerivedHintCol: () => {},
    derivedHintCol: null,
  } as unknown as SidebarScope & LaneRows

  // Call the REAL createFolderBlocks with the minimal shell.
  // renderFolderBlock returns an array of ReactNode (ContextMenu → DndDroppable
  // → folder-header div); render it inside a plain wrapper.
  const { renderFolderBlock } = createFolderBlocks(shell)
  const nodes = renderFolderBlock(folder, /* depth */ 0)

  return (
    <>
      <div data-testid="folder-rig">{nodes}</div>
      <button type="button" data-testid="elsewhere">Elsewhere</button>
    </>
  )
}

const nextFrame = () => act(async () => { await new Promise(requestAnimationFrame) })

describe('folderBlocks.tsx renderFolderHeader — focus contract (#13598)', () => {
  it('[data-folder-row] renders and the rename Input is mounted from the start', () => {
    render(<FolderBlockRig folder={testFolder} />)
    // The real [data-folder-row] div (tabIndex=-1) must be in the DOM
    const row = document.querySelector(`[data-folder-row="${FOLDER_ID}"]`)
    expect(row).toBeTruthy()
    // The rename Input is already mounted (editingId === folder.id from the start)
    const input = (row as HTMLElement).querySelector('input')
    expect(input).toBeTruthy()
  })

  it('Enter commits rename and calls requestAnimationFrame(focusListFolderRow)', async () => {
    const onCommit = vi.fn()
    render(<FolderBlockRig folder={testFolder} onCommit={onCommit} />)

    const row = document.querySelector(`[data-folder-row="${FOLDER_ID}"]`) as HTMLElement
    const input = row.querySelector('input') as HTMLInputElement

    act(() => { input.focus() })
    act(() => { fireEvent.change(input, { target: { value: 'Renamed' } }) })
    // Trigger the real onEnter handler from folderBlocks.tsx's bindEnter
    act(() => { fireEvent.keyDown(input, { key: 'Enter' }) })
    // The rAF in folderBlocks.tsx fires here
    await nextFrame()

    expect(document.activeElement).toBe(row)
    expect(document.activeElement).not.toBe(document.body)
    expect(onCommit).toHaveBeenCalledWith(FOLDER_ID, 'Renamed')
  })

  it('Escape cancels rename and calls requestAnimationFrame(focusListFolderRow)', async () => {
    render(<FolderBlockRig folder={testFolder} />)

    const row = document.querySelector(`[data-folder-row="${FOLDER_ID}"]`) as HTMLElement
    const input = row.querySelector('input') as HTMLInputElement

    act(() => { input.focus() })
    // Trigger the real onEscape handler from folderBlocks.tsx's bindEnter
    act(() => { fireEvent.keyDown(input, { key: 'Escape' }) })
    await nextFrame()

    expect(document.activeElement).toBe(row)
    expect(document.activeElement).not.toBe(document.body)
  })

  it('pointer blur (onBlur) commits but does NOT fire requestAnimationFrame(focusListFolderRow)', async () => {
    render(<FolderBlockRig folder={testFolder} />)

    const row = document.querySelector(`[data-folder-row="${FOLDER_ID}"]`) as HTMLElement
    const input = row.querySelector('input') as HTMLInputElement
    const elsewhere = document.querySelector('[data-testid="elsewhere"]') as HTMLElement

    act(() => { input.focus() })
    // Moving focus away triggers onBlur → renameCommit (no rAF, no redirect)
    act(() => { elsewhere.focus() })
    await nextFrame()

    expect(document.activeElement).toBe(elsewhere)
    expect(document.activeElement).not.toBe(row)
    expect(document.activeElement).not.toBe(document.body)
  })

  it('Enter: activeElement is the real [data-folder-row] after the rAF fires', async () => {
    render(<FolderBlockRig folder={testFolder} />)

    const row = document.querySelector(`[data-folder-row="${FOLDER_ID}"]`) as HTMLElement
    const input = row.querySelector('input') as HTMLInputElement

    act(() => { input.focus() })
    expect(document.activeElement).toBe(input)

    act(() => { fireEvent.keyDown(input, { key: 'Enter' }) })
    await nextFrame()

    expect(document.activeElement).toBe(row)
  })

  it('Escape: activeElement is the real [data-folder-row] after the rAF fires', async () => {
    render(<FolderBlockRig folder={testFolder} />)

    const row = document.querySelector(`[data-folder-row="${FOLDER_ID}"]`) as HTMLElement
    const input = row.querySelector('input') as HTMLInputElement

    act(() => { input.focus() })
    expect(document.activeElement).toBe(input)

    act(() => { fireEvent.keyDown(input, { key: 'Escape' }) })
    await nextFrame()

    expect(document.activeElement).toBe(row)
  })
})
