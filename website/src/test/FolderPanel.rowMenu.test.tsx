/**
 * FolderPanel — the listing row menu (#43258).
 *
 * A folder tab opened on a directory that is not the chat's project root (a
 * `/tmp/...` path from a chat chip) renders the one-level listing, whose rows
 * used to offer only a click. They now open the SAME `FileRowMenu` the Files
 * tab's tree opens on its rows: Add to chat and Download, plus the app rows
 * for the `folder-row` surface. These tests pin the three ways a row opens it,
 * what the menu does, and how the panel dismisses it -- the parts Pierre owns
 * for the tree and this panel owns for the listing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'

import FolderPanel from '../pages/chat/FolderPanel'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: false, desktopLauncher: false, remoteEditor: '', remoteEditorHost: '' }),
}))

const CWD = '/tmp/pr42850-evidence'
const LISTING = {
  path: CWD,
  parent: '/tmp',
  dirs: [{ name: 'frames', path: `${CWD}/frames`, mtime: 1 }],
  files: [
    { name: '01-before-dark.png', path: `${CWD}/01-before-dark.png`, mtime: 2 },
    { name: 'notes.md', path: `${CWD}/notes.md`, mtime: 3 },
  ],
}

const row = (name: string) => screen.getByText(name).closest('[role="button"]') as HTMLElement
const menuButtonOf = (name: string) => row(name).querySelector('[data-testid="folder-row-menu"]') as HTMLElement

async function renderListing(props: Partial<React.ComponentProps<typeof FolderPanel>> = {}) {
  vi.spyOn(api, 'browseFiles').mockResolvedValue(LISTING)
  const onFileOpen = vi.fn()
  const onAddToContext = vi.fn()
  const utils = renderWithProviders(
    <FolderPanel path={CWD} projectDir="/home/dev/project" onClose={vi.fn()} onFileOpen={onFileOpen} onAddToContext={onAddToContext} {...props} />,
  )
  await waitFor(() => expect(screen.getByText('notes.md')).toBeTruthy())
  return { ...utils, onFileOpen, onAddToContext }
}

describe('FolderPanel — listing row menu', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('opens the shared menu on right-click, with Add to chat and Download for a file', async () => {
    const { onFileOpen, onAddToContext } = await renderListing()
    fireEvent.contextMenu(row('notes.md'), { clientX: 40, clientY: 60 })
    const menu = await screen.findByRole('menu')
    expect(menu).toHaveAttribute('data-file-tree-context-menu-root', 'true')
    expect(screen.getAllByRole('menuitem').map(el => el.textContent)).toEqual(['Add to chat', 'Download'])
    // Opening the menu is not opening the file.
    expect(onFileOpen).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('menuitem', { name: 'Add to chat' }))
    // The ABSOLUTE path, as the tree reports it: the composer host decides
    // whether it can be relativized.
    expect(onAddToContext).toHaveBeenCalledWith(`${CWD}/notes.md`, 'file')
    // Selecting closes the menu and hands focus back to the row.
    expect(screen.queryByRole('menu')).toBeNull()
    expect(row('notes.md')).toHaveFocus()
  })

  it('opens the menu from the hover button without activating the row', async () => {
    const { onFileOpen } = await renderListing()
    fireEvent.click(menuButtonOf('01-before-dark.png'))
    expect(await screen.findByRole('menu')).toBeInTheDocument()
    expect(onFileOpen).not.toHaveBeenCalled()
    // The first item takes focus, so a keyboard user can act at once.
    expect(screen.getByRole('menuitem', { name: 'Add to chat' })).toHaveFocus()
  })

  it('opens the menu from the keyboard: Shift+F10 and the ContextMenu key', async () => {
    const { onFileOpen } = await renderListing()
    fireEvent.keyDown(row('notes.md'), { key: 'F10', shiftKey: true })
    expect(await screen.findByRole('menu')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('menu')).toBeNull()

    fireEvent.keyDown(row('notes.md'), { key: 'ContextMenu' })
    expect(await screen.findByRole('menu')).toBeInTheDocument()
    expect(onFileOpen).not.toHaveBeenCalled()
  })

  it('offers Add to chat as a folder on a directory row, and no Download', async () => {
    const { onAddToContext } = await renderListing()
    fireEvent.contextMenu(row('frames'))
    await screen.findByRole('menu')
    expect(screen.queryByRole('menuitem', { name: 'Download' })).toBeNull()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Add to chat' }))
    expect(onAddToContext).toHaveBeenCalledWith(`${CWD}/frames`, 'dir')
  })

  it('offers Download alone when the host wires no Add to chat', async () => {
    await renderListing({ onAddToContext: undefined })
    fireEvent.contextMenu(row('notes.md'))
    await screen.findByRole('menu')
    expect(screen.getAllByRole('menuitem').map(el => el.textContent)).toEqual(['Download'])
  })

  it('streams a Download through /api/file-download and reports a failure in the panel', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 400, statusText: 'Bad Request', headers: new Headers() })
    const origFetch = global.fetch
    global.fetch = fetchMock as unknown as typeof fetch
    try {
      await renderListing()
      fireEvent.contextMenu(row('notes.md'))
      await screen.findByRole('menu')
      fireEvent.click(screen.getByRole('menuitem', { name: 'Download' }))
      await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
        `/api/file-download?path=${encodeURIComponent(`${CWD}/notes.md`)}`,
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ))
      // The menu is gone by then, so the panel's own notice carries the failure.
      const notice = await screen.findByTestId('folder-panel-contrib-error')
      expect(notice).toHaveTextContent('Download failed')
    } finally {
      global.fetch = origFetch
    }
  })

  it('toggles the menu closed on a second press of the open row\'s button', async () => {
    await renderListing()
    const btn = menuButtonOf('notes.md')
    fireEvent.click(btn)
    await screen.findByRole('menu')
    // The pointer down on the button is not an "outside" press (it would let the
    // click reopen the menu at once); the click itself closes it.
    fireEvent.pointerDown(btn)
    expect(screen.getByRole('menu')).toBeInTheDocument()
    fireEvent.click(btn)
    expect(screen.queryByRole('menu')).toBeNull()
    expect(row('notes.md')).toHaveFocus()
    // Another row's button moves the menu rather than closing it.
    fireEvent.click(btn)
    await screen.findByRole('menu')
    fireEvent.click(menuButtonOf('01-before-dark.png'))
    expect(screen.getByRole('menu')).toBeInTheDocument()
  })

  it('keeps the menu through a scroll of a container the row is not in (a streaming transcript)', async () => {
    // The listing row is light DOM, so the menu's dismiss listener sits on the
    // document and sees every scroll on the page; only a scroller that holds the
    // row may close it.
    await renderListing()
    const transcript = document.createElement('div')
    document.body.appendChild(transcript)
    try {
      fireEvent.contextMenu(row('notes.md'))
      await screen.findByRole('menu')
      fireEvent.scroll(transcript)
      expect(screen.getByRole('menu')).toBeInTheDocument()
      // The listing's own scroller holds the row: the rows moved, dismiss.
      fireEvent.scroll(row('notes.md').parentElement!)
      expect(screen.queryByRole('menu')).toBeNull()
    } finally {
      transcript.remove()
    }
  })

  it('answers Escape through the shared Escape stack, not a listener of its own', async () => {
    // `useEscapeLayer` installs ONE capture listener on the document for every
    // layer and stops propagation on a press it takes, so the probe sits in the
    // capture phase too (a later listener on the same node still runs) and reads
    // the press already defaultPrevented: the stack took it, not a raw listener.
    await renderListing()
    fireEvent.contextMenu(row('notes.md'))
    await screen.findByRole('menu')
    const seen: boolean[] = []
    const probe = (e: KeyboardEvent) => { seen.push(e.defaultPrevented) }
    document.addEventListener('keydown', probe, true)
    try {
      fireEvent.keyDown(document.body, { key: 'Escape' })
    } finally {
      document.removeEventListener('keydown', probe, true)
    }
    expect(screen.queryByRole('menu')).toBeNull()
    expect(seen).toEqual([true])
    // With no menu open the layer is inactive and Escape passes through untouched.
    const seenIdle: boolean[] = []
    const probeIdle = (e: KeyboardEvent) => { seenIdle.push(e.defaultPrevented) }
    document.addEventListener('keydown', probeIdle, true)
    try {
      fireEvent.keyDown(document.body, { key: 'Escape' })
    } finally {
      document.removeEventListener('keydown', probeIdle, true)
    }
    expect(seenIdle).toEqual([false])
  })

  it('dismisses on a pointer down outside the menu, and keeps it on one inside', async () => {
    await renderListing()
    fireEvent.contextMenu(row('notes.md'))
    const menu = await screen.findByRole('menu')
    fireEvent.pointerDown(screen.getByRole('menuitem', { name: 'Download' }))
    expect(screen.getByRole('menu')).toBe(menu)
    fireEvent.pointerDown(document.body)
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('leaves the menu behind when the tab navigates to another directory', async () => {
    await renderListing()
    fireEvent.contextMenu(row('notes.md'))
    await screen.findByRole('menu')
    vi.spyOn(api, 'browseFiles').mockResolvedValue({ path: `${CWD}/frames`, parent: CWD, dirs: [], files: [] })
    fireEvent.pointerDown(row('frames'))
    fireEvent.click(row('frames'))
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
  })

  it('gives the parent-folder row no menu: it is navigation, not an entry', async () => {
    await renderListing()
    const parent = row('Parent folder')
    expect(parent.querySelector('[data-testid="folder-row-menu"]')).toBeNull()
    fireEvent.contextMenu(parent)
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('opens the menu on a search match too', async () => {
    vi.spyOn(api, 'fileSearch').mockResolvedValue({ root: CWD, results: [{ name: 'deep.png', path: `${CWD}/frames/deep.png`, kind: 'file', sub: 'frames' }] } as never)
    const { onAddToContext } = await renderListing()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'deep' } })
    await waitFor(() => expect(screen.getByText('deep.png')).toBeTruthy())
    fireEvent.contextMenu(row('deep.png'))
    await screen.findByRole('menu')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Add to chat' }))
    expect(onAddToContext).toHaveBeenCalledWith(`${CWD}/frames/deep.png`, 'file')
  })
})
