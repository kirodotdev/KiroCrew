/**
 * FolderPanel — the side-panel body for a `folder` tab.
 *
 * Exists because a markdown path chip pointing at a directory used to open the
 * file viewer and claim "file not found". These tests pin the replacement
 * behaviour: list the directory, navigate into it, and hand files off to the
 * normal file-tab path.
 */
import { useState } from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'

import FolderPanel from '../pages/chat/FolderPanel'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

// The header reveal button is gated on the shared reveal gate (directLocal AND a
// launcher on the gateway host: a remote or headless session cannot drive
// Finder there). Most tests exercise the local desktop case, so pin both true;
// dedicated tests cover the hidden cases.
// Reveal and "Open in <editor>" live in the header's one overflow menu; Radix
// opens a dropdown on a primary-button pointerdown.
const openMoreMenu = async () => {
  const trigger = await screen.findByTestId('folder-panel-more')
  act(() => { fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false, pointerType: 'mouse' }) })
  return screen.findByRole('menu')
}

const brandingEnv = vi.hoisted(() => ({ directLocal: true, desktopLauncher: true, remoteEditor: '', remoteEditorHost: '' }))
vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({
    botName: 'Test', avatar: '',
    directLocal: brandingEnv.directLocal,
    desktopLauncher: brandingEnv.desktopLauncher,
    remoteEditor: brandingEnv.remoteEditor,
    remoteEditorHost: brandingEnv.remoteEditorHost,
  }),
}))

const LISTING = {
  path: '/Users/me/ws',
  parent: '/Users/me',
  dirs: [{ name: 'src', path: '/Users/me/ws/src', mtime: 1 }],
  files: [{ name: 'README.md', path: '/Users/me/ws/README.md', mtime: 2 }],
}

describe('FolderPanel', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    brandingEnv.directLocal = true
    brandingEnv.desktopLauncher = true
    brandingEnv.remoteEditor = ''
    brandingEnv.remoteEditorHost = ''
  })

  it('lists the directory it was opened on', async () => {
    vi.spyOn(api, 'browseFiles').mockResolvedValue(LISTING)
    renderWithProviders(<FolderPanel path="/Users/me/ws" onClose={vi.fn()} />)
    await waitFor(() => expect(screen.getByText('src')).toBeTruthy())
    expect(screen.getByText('README.md')).toBeTruthy()
  })

  it('shows the full folder path in the bubble on keyboard focus, never only in a native title', async () => {
    vi.spyOn(api, 'browseFiles').mockResolvedValue(LISTING)
    renderWithProviders(<FolderPanel path="/Users/me/ws" onClose={vi.fn()} />)
    await waitFor(() => expect(screen.getByText('src')).toBeTruthy())
    // The header names the folder and the body line shows its whole path; both
    // are cut off with an ellipsis in a narrow panel.
    // `ws` is described by its path through a hidden copy, so pick the shown line.
    const pathLine = screen.getAllByText('/Users/me/ws').find(el => !el.hidden)!
    for (const line of [screen.getByText('ws'), pathLine]) {
      expect(line).toHaveAttribute('title', '')
      expect(line).toHaveAttribute('tabindex', '0')
      fireEvent.focus(line)
      expect(screen.getByRole('tooltip')).toHaveTextContent('/Users/me/ws')
      fireEvent.blur(line)
    }
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
    // The signal is react-query's, so a superseded listing aborts; this asserts the
    // path it navigated to, which is the subject, rather than the argument count.
    await waitFor(() => expect(browse).toHaveBeenCalledWith('/Users/me/ws/src', expect.any(AbortSignal)))
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

  it('steps Back and Forward through the folders the tab has shown, dropping Forward on a new folder change', async () => {
    const listings: Record<string, typeof LISTING> = {
      '/Users/me/ws': LISTING,
      '/Users/me': { path: '/Users/me', parent: '/Users', dirs: [{ name: 'ws', path: '/Users/me/ws', mtime: 1 }], files: [] },
      '/Users/me/ws/src': { path: '/Users/me/ws/src', parent: '/Users/me/ws', dirs: [], files: [] },
    }
    vi.spyOn(api, 'browseFiles').mockImplementation(async (p: string) => listings[p])
    // The production round trip: the tab record takes the reported path and hands it back.
    function Tab() {
      const [path, setPath] = useState('/Users/me/ws')
      return <FolderPanel path={path} onClose={vi.fn()} onPathChange={setPath} />
    }
    renderWithProviders(<Tab />)
    const back = screen.getByRole('button', { name: 'Back' })
    const forward = screen.getByRole('button', { name: 'Forward' })
    const header = within(back.closest('div')!)
    expect(header.getByText('ws')).toBeTruthy()
    expect(back).toHaveAttribute('aria-disabled', 'true')
    expect(forward).toHaveAttribute('aria-disabled', 'true')

    fireEvent.click(await screen.findByText('Parent folder'))
    expect(header.getByText('me')).toBeTruthy()
    expect(back).toHaveAttribute('aria-disabled', 'false')

    fireEvent.click(back)
    expect(header.getByText('ws')).toBeTruthy()
    expect(back).toHaveAttribute('aria-disabled', 'true')
    expect(forward).toHaveAttribute('aria-disabled', 'false')

    fireEvent.click(forward)
    expect(header.getByText('me')).toBeTruthy()
    expect(forward).toHaveAttribute('aria-disabled', 'true')

    fireEvent.click(back)
    fireEvent.click(await screen.findByText('src'))
    expect(header.getByText('src')).toBeTruthy()
    expect(forward).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(back)
    expect(header.getByText('ws')).toBeTruthy()
  })

  it('surfaces a listing failure instead of rendering a silently empty folder', async () => {
    vi.spyOn(api, 'browseFiles').mockRejectedValue(new Error('Access denied'))
    renderWithProviders(<FolderPanel path="/Users/me/ws" onClose={vi.fn()} />)
    // Catalog copy, not the server's own string: rendering that raw is what the
    // i18n invariant forbids. The point still stands — a failure is not an empty folder.
    await waitFor(() => expect(screen.getByText(/^Unable to list folder/)).toBeTruthy())
    expect(screen.queryByText('Empty folder')).toBeNull()
  })

  it('reveals the directory in the OS file manager', async () => {
    vi.spyOn(api, 'browseFiles').mockResolvedValue(LISTING)
    const reveal = vi.spyOn(api, 'revealPath').mockResolvedValue(undefined as never)
    renderWithProviders(<FolderPanel path="/Users/me/ws" onClose={vi.fn()} />)
    const menu = await openMoreMenu()
    const item = within(menu).getByRole('menuitem', { name: 'Show in file manager' })
    await act(async () => { fireEvent.click(item) })
    // Routes through the shared revealOrOpen helper, which passes the explicit
    // 'reveal' action so a failed reveal surfaces the shared message.
    expect(reveal).toHaveBeenCalledWith('/Users/me/ws', 'reveal')
  })

  it('hides the reveal button for a remote (non-local) session', async () => {
    brandingEnv.directLocal = false
    vi.spyOn(api, 'browseFiles').mockResolvedValue(LISTING)
    renderWithProviders(<FolderPanel path="/Users/me/ws" onClose={vi.fn()} />)
    // The listing still renders; only the header reveal button is gone, because
    // /api/reveal would drive Finder on a host the user is not looking at. With no
    // editor set either, the overflow menu has no item, so its trigger is gone too.
    await waitFor(() => expect(screen.getByText('src')).toBeTruthy())
    expect(screen.queryByTestId('folder-panel-more')).toBeNull()
    expect(screen.queryByText('Show in file manager')).not.toBeInTheDocument()
  })

  it('hides the reveal button when the gateway host has no desktop launcher', async () => {
    // Direct-local (an `ssh -L` browser reads as one) but headless: the reveal
    // could only be answered with a copy of the folder path.
    brandingEnv.desktopLauncher = false
    vi.spyOn(api, 'browseFiles').mockResolvedValue(LISTING)
    renderWithProviders(<FolderPanel path="/Users/me/ws" onClose={vi.fn()} />)
    await waitFor(() => expect(screen.getByText('src')).toBeTruthy())
    expect(screen.queryByTestId('folder-panel-more')).toBeNull()
    expect(screen.queryByText('Show in file manager')).not.toBeInTheDocument()
  })

  /**
   * The control names the GATEWAY's file manager, not the browser's: `/api/reveal`
   * shells out on the gateway, so a dashboard opened from a Mac against a Linux
   * gateway must not promise Finder. The wording also has to hold for a DIRECTORY,
   * which is what this button reveals.
   */
  it.each([
    ['darwin', 'Open in Finder'],
    ['win32', 'Open in File Explorer'],
    // The sentinel a non-owner dashboard user (and a probe that could not run)
    // gets. It must never be read as a platform we can name.
    ['gateway', 'Show in file manager'],
    ['linux', 'Show in file manager'],
  ])('names the reveal control for a %s gateway host', async (platform, label) => {
    vi.spyOn(api, 'browseFiles').mockResolvedValue(LISTING)
    const { queryClient } = renderWithProviders(
      <FolderPanel path="/Users/me/ws" onClose={vi.fn()} />,
    )
    act(() => { queryClient.setQueryData(['kiro-prerequisite'], { platform }) })
    const menu = await openMoreMenu()
    // A menu row names itself with visible text, so the label reaches pointer and
    // screen-reader users alike.
    await waitFor(() => expect(within(menu).getByRole('menuitem', { name: label })).toBeTruthy())
  })

  it('opens the folder in the chosen remote editor, as a folder link with no line suffix', async () => {
    vi.spyOn(api, 'browseFiles').mockResolvedValue(LISTING)
    brandingEnv.remoteEditor = 'kiro'
    brandingEnv.remoteEditorHost = 'dev-box'
    renderWithProviders(<FolderPanel path="/Users/me/ws" onClose={vi.fn()} />)
    const menu = await openMoreMenu()
    const link = within(menu).getByRole('menuitem', { name: 'Open in Kiro' })
    expect(link.tagName).toBe('A')
    expect(link.getAttribute('href')).toBe('kiro://vscode-remote/ssh-remote+dev-box/Users/me/ws')
  })

  it('keeps the header to Refresh plus one overflow trigger holding Reveal and the editor link', async () => {
    vi.spyOn(api, 'browseFiles').mockResolvedValue(LISTING)
    brandingEnv.remoteEditor = 'kiro'
    brandingEnv.remoteEditorHost = 'dev-box'
    renderWithProviders(<FolderPanel path="/Users/me/ws" onClose={vi.fn()} />)
    const trigger = await screen.findByTestId('folder-panel-more')
    const row = trigger.parentElement as HTMLElement
    // The direct children of the header row that are actions; the Back/Forward pair
    // sits in its own nested group.
    const actions = Array.from(row.children).filter(el => el.matches('button, a'))
    expect(actions.map(el => el.getAttribute('aria-label'))).toEqual(['Refresh', 'More actions'])
    expect(within(row).queryByRole('link')).toBeNull()
    const menu = await openMoreMenu()
    expect(within(menu).getAllByRole('menuitem').map(el => el.textContent)).toEqual(['Show in file manager', 'Open in Kiro'])
  })

  it('offers no remote editor link while no editor is picked', async () => {
    vi.spyOn(api, 'browseFiles').mockResolvedValue(LISTING)
    brandingEnv.remoteEditorHost = 'dev-box'
    renderWithProviders(<FolderPanel path="/Users/me/ws" onClose={vi.fn()} />)
    const menu = await openMoreMenu()
    expect(within(menu).getAllByRole('menuitem').map(el => el.textContent)).toEqual(['Show in file manager'])
    expect(screen.queryByTestId('folder-panel-remote-editor')).toBeNull()
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
