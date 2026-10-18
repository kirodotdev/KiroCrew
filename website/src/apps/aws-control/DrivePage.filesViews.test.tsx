/**
 * The Files pane's three views, driven by props.
 *
 * `DriveFilesSearchResults`, `DriveFilesGrid` and `DriveFilesTable` read
 * everything from the pane's state object and own no state of their own, so a
 * plain object of spies stands in for `useDrivePageFiles` here. These cases
 * pin the controls the page-level specs reach only through a full listing: the
 * keyboard paths of the in-place editors, the tile's menu items, and the
 * Cancel of each confirm strip. Each asserts the pane callback the control
 * hands its target to, which is the whole of what a view does.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { DriveFilesGrid, DriveFilesSearchResults, DriveFilesTable } from './DrivePageFilesViews'
import type { DriveFilesPane } from './useDrivePageFiles'

/** How long a Radix menu may take to render its items after the trigger's key. */
const MENU_OPEN_TIMEOUT_MS = 2000

const FILE = { key: 'docs/a.txt', size: 12, modified: '2026-01-02T03:04:05Z' }
const HIT = { key: 'docs/b.md', size: 7, modified: '2026-01-02T03:04:05Z' }

/** A pane with one folder and one file, every callback a spy. */
function makePane(over: Record<string, unknown> = {}): DriveFilesPane {
  const mutation = () => ({ isPending: false, isError: false, error: null, variables: undefined, mutate: vi.fn(), reset: vi.fn() })
  const pane = {
    folders: ['docs/sub'],
    files: [FILE],
    setPath: vi.fn(),
    dropProps: () => ({}),
    dropTarget: null,
    confirmFolder: null,
    setConfirmFolder: vi.fn(),
    folderDeleteMut: mutation(),
    handOff: true,
    dragProps: () => ({ draggable: true }),
    highlightProps: () => ({}),
    highlighted: () => false,
    movingKey: null,
    rememberMenuOpener: vi.fn(),
    skipRestoreIfDialog: vi.fn(),
    download: vi.fn(),
    openRename: vi.fn(),
    openShare: vi.fn(),
    openMove: vi.fn(),
    moveBusy: false,
    openConfirmDelete: vi.fn(),
    renaming: null,
    renameValue: '',
    setRenameValue: vi.fn(),
    renameError: '',
    renameMut: mutation(),
    commitRename: vi.fn(),
    closeRename: vi.fn(),
    setPreview: vi.fn(),
    confirmDelete: null,
    setConfirmDelete: vi.fn(),
    deleteFailedFor: () => false,
    deleteMut: mutation(),
    attachScroller: vi.fn(),
    edges: { left: false, right: false },
    debouncedQuery: 'b',
    setQuery: vi.fn(),
    setHighlightKey: vi.fn(),
    searchQ: {
      isLoading: false, isFetching: false, isError: false, isSuccess: true, error: null,
      data: { results: [HIT], capped: false, limit: 100 }, refetch: vi.fn(),
    },
    ...over,
  }
  return pane as unknown as DriveFilesPane
}

/** Opens a row or tile menu from the keyboard and selects one item. */
async function choose(trigger: HTMLElement, itemTestId: string) {
  fireEvent.keyDown(trigger, { key: 'Enter' })
  fireEvent.click(await screen.findByTestId(itemTestId, {}, { timeout: MENU_OPEN_TIMEOUT_MS }))
}

describe('DriveFilesGrid', () => {
  it('opens a folder tile from the keyboard, and ignores other keys', () => {
    const pane = makePane()
    render(<DriveFilesGrid pane={pane} />)
    const tile = screen.getByTestId('drive-grid-folder')
    fireEvent.keyDown(tile, { key: 'a' })
    expect(pane.setPath).not.toHaveBeenCalled()
    fireEvent.keyDown(tile, { key: 'Enter' })
    expect(pane.setPath).toHaveBeenCalledWith('docs/sub')
  })

  it('cancels or confirms a folder delete on its tile', () => {
    const pane = makePane({ confirmFolder: 'docs/sub' })
    render(<DriveFilesGrid pane={pane} />)
    fireEvent.click(screen.getByTestId('drive-grid-confirm-cancel'))
    expect(pane.setConfirmFolder).toHaveBeenCalledWith(null)
    fireEvent.click(screen.getByTestId('drive-grid-confirm-action'))
    expect(pane.folderDeleteMut.mutate).toHaveBeenCalledWith('docs/sub', expect.objectContaining({ onSuccess: expect.any(Function) }))
  })

  it('hands each file menu item its file', async () => {
    const pane = makePane()
    render(<DriveFilesGrid pane={pane} />)
    await choose(screen.getByTestId('drive-grid-more'), 'drive-grid-download')
    expect(pane.download).toHaveBeenCalledWith(FILE.key)
    await choose(screen.getByTestId('drive-grid-more'), 'drive-grid-share')
    expect(pane.openShare).toHaveBeenCalledWith(FILE.key)
    await choose(screen.getByTestId('drive-grid-more'), 'drive-grid-move')
    expect(pane.openMove).toHaveBeenCalledWith(FILE.key)
  })

  it('opens the preview from the file name', () => {
    const pane = makePane()
    render(<DriveFilesGrid pane={pane} />)
    fireEvent.click(screen.getByTestId('drive-grid-preview-open'))
    expect(pane.setPreview).toHaveBeenCalledWith({ key: FILE.key, size: FILE.size })
  })

  it('cancels a file delete on its tile', () => {
    const pane = makePane({ confirmDelete: FILE.key })
    render(<DriveFilesGrid pane={pane} />)
    fireEvent.click(screen.getByTestId('drive-grid-confirm-cancel'))
    expect(pane.setConfirmDelete).toHaveBeenCalledWith(null)
  })

  it('edits, commits and closes the tile rename editor', () => {
    const pane = makePane({ renaming: FILE.key, renameValue: 'a.txt' })
    render(<DriveFilesGrid pane={pane} />)
    const input = screen.getByTestId('drive-grid-rename-input')
    fireEvent.change(input, { target: { value: 'c.txt' } })
    expect(pane.setRenameValue).toHaveBeenCalledWith('c.txt')
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(pane.commitRename).toHaveBeenCalledWith(FILE.key)
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(pane.closeRename).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByTestId('drive-grid-rename-save'))
    expect(pane.commitRename).toHaveBeenCalledTimes(2)
  })
})

describe('DriveFilesTable', () => {
  it('keeps a click on the folder menu trigger from opening the folder', () => {
    const pane = makePane()
    render(<DriveFilesTable pane={pane} />)
    fireEvent.click(screen.getByTestId('drive-folder-more'))
    expect(pane.setPath).not.toHaveBeenCalled()
  })

  it('cancels a folder delete from its strip', () => {
    const pane = makePane({ confirmFolder: 'docs/sub' })
    render(<DriveFilesTable pane={pane} />)
    fireEvent.click(screen.getByTestId('drive-folder-delete-cancel'))
    expect(pane.setConfirmFolder).toHaveBeenCalledWith(null)
  })

  it('commits or closes the row rename editor from the keyboard', () => {
    const pane = makePane({ renaming: FILE.key, renameValue: 'a.txt' })
    render(<DriveFilesTable pane={pane} />)
    const input = screen.getByTestId('drive-rename-input')
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(pane.commitRename).toHaveBeenCalledWith(FILE.key)
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(pane.closeRename).toHaveBeenCalledTimes(1)
  })
})

describe('DriveFilesSearchResults', () => {
  it('re-issues a failed search from its notice', () => {
    const refetch = vi.fn()
    const pane = makePane({
      searchQ: { isLoading: false, isFetching: false, isError: true, isSuccess: false, error: new Error('boom'), data: undefined, refetch },
    })
    render(<DriveFilesSearchResults pane={pane} />)
    fireEvent.click(screen.getByTestId('drive-search-error-retry'))
    expect(refetch).toHaveBeenCalledTimes(1)
  })

  it('opens the preview from a hit and downloads from its menu', async () => {
    const pane = makePane()
    render(<DriveFilesSearchResults pane={pane} />)
    fireEvent.click(screen.getByTestId('drive-search-open'))
    expect(pane.setPreview).toHaveBeenCalledWith({ key: HIT.key, size: HIT.size })
    await choose(screen.getByTestId('drive-search-more'), 'drive-search-download')
    expect(pane.download).toHaveBeenCalledWith(HIT.key)
  })

  it('commits or closes a hit rename editor from the keyboard', () => {
    const pane = makePane({ renaming: HIT.key, renameValue: 'b.md' })
    render(<DriveFilesSearchResults pane={pane} />)
    const input = screen.getByTestId('drive-rename-input')
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(pane.commitRename).toHaveBeenCalledWith(HIT.key)
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(pane.closeRename).toHaveBeenCalledTimes(1)
  })

  it('cancels a hit delete from its strip', () => {
    const pane = makePane({ confirmDelete: HIT.key })
    render(<DriveFilesSearchResults pane={pane} />)
    fireEvent.click(screen.getByTestId('drive-delete-cancel'))
    expect(pane.setConfirmDelete).toHaveBeenCalledWith(null)
  })
})
