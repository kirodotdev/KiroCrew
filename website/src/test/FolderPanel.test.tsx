/**
 * FolderPanel — the side-panel body for a `folder` tab.
 *
 * Exists because a markdown path chip pointing at a directory used to open the
 * file viewer and claim "file not found". These tests pin the replacement
 * behaviour: list the directory, navigate into it, and hand files off to the
 * normal file-tab path.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'

import FolderPanel from '../pages/chat/FolderPanel'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

const LISTING = {
  path: '/Users/me/ws',
  parent: '/Users/me',
  dirs: [{ name: 'src', path: '/Users/me/ws/src', mtime: 1 }],
  files: [{ name: 'README.md', path: '/Users/me/ws/README.md', mtime: 2 }],
}

describe('FolderPanel', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('lists the directory it was opened on', async () => {
    vi.spyOn(api, 'browseFiles').mockResolvedValue(LISTING)
    renderWithProviders(<FolderPanel path="/Users/me/ws" onClose={vi.fn()} />)
    await waitFor(() => expect(screen.getByText('src')).toBeTruthy())
    expect(screen.getByText('README.md')).toBeTruthy()
  })

  it('hands a file off to onFileOpen rather than handling it itself', async () => {
    vi.spyOn(api, 'browseFiles').mockResolvedValue(LISTING)
    const onFileOpen = vi.fn()
    renderWithProviders(
      <FolderPanel path="/Users/me/ws" onClose={vi.fn()} onFileOpen={onFileOpen} />,
    )
    fireEvent.click(await waitFor(() => screen.getByText('README.md')))
    expect(onFileOpen).toHaveBeenCalledWith('/Users/me/ws/README.md')
  })

  it('navigates into a subdirectory in place and reports the new path', async () => {
    const browse = vi.spyOn(api, 'browseFiles').mockResolvedValue(LISTING)
    const onPathChange = vi.fn()
    renderWithProviders(
      <FolderPanel path="/Users/me/ws" onClose={vi.fn()} onPathChange={onPathChange} />,
    )
    fireEvent.click(await waitFor(() => screen.getByText('src')))
    // Navigation is internal to the tab (no tab-per-directory explosion), but
    // the new cwd is lifted so the tab strip label can follow.
    expect(onPathChange).toHaveBeenCalledWith('/Users/me/ws/src')
    await waitFor(() => expect(browse).toHaveBeenCalledWith('/Users/me/ws/src'))
  })

  it('offers a parent row, and suppresses it at the filesystem root', async () => {
    vi.spyOn(api, 'browseFiles').mockResolvedValue(LISTING)
    const onPathChange = vi.fn()
    const { unmount } = renderWithProviders(
      <FolderPanel path="/Users/me/ws" onClose={vi.fn()} onPathChange={onPathChange} />,
    )
    fireEvent.click(await waitFor(() => screen.getByText('Parent folder')))
    expect(onPathChange).toHaveBeenCalledWith('/Users/me')
    unmount()

    // At the root the backend reports parent === path; an up-row there would go
    // nowhere, so it must not render.
    vi.spyOn(api, 'browseFiles').mockResolvedValue({ path: '/', parent: '/', dirs: [], files: [] })
    renderWithProviders(<FolderPanel path="/" onClose={vi.fn()} />)
    await waitFor(() => expect(screen.getByText('Empty folder')).toBeTruthy())
    expect(screen.queryByText('Parent folder')).toBeNull()
  })

  it('surfaces a listing failure instead of rendering a silently empty folder', async () => {
    vi.spyOn(api, 'browseFiles').mockRejectedValue(new Error('Access denied'))
    renderWithProviders(<FolderPanel path="/Users/me/ws" onClose={vi.fn()} />)
    await waitFor(() => expect(screen.getByText('Access denied')).toBeTruthy())
    expect(screen.queryByText('Empty folder')).toBeNull()
  })

  it('reveals the directory in the OS file manager', async () => {
    vi.spyOn(api, 'browseFiles').mockResolvedValue(LISTING)
    const reveal = vi.spyOn(api, 'revealPath').mockResolvedValue(undefined as never)
    renderWithProviders(<FolderPanel path="/Users/me/ws" onClose={vi.fn()} />)
    fireEvent.click(await waitFor(() => screen.getByLabelText('Reveal in Finder')))
    expect(reveal).toHaveBeenCalledWith('/Users/me/ws')
  })

  it('activates rows by keyboard', async () => {
    vi.spyOn(api, 'browseFiles').mockResolvedValue(LISTING)
    const onFileOpen = vi.fn()
    renderWithProviders(
      <FolderPanel path="/Users/me/ws" onClose={vi.fn()} onFileOpen={onFileOpen} />,
    )
    const row = await waitFor(() => screen.getByText('README.md').closest('[role="button"]')!)
    fireEvent.keyDown(row, { key: 'Enter' })
    expect(onFileOpen).toHaveBeenCalledWith('/Users/me/ws/README.md')
  })
})
