// The Browse pane opens inside the caller's current selection (#17129): the chat's project,
// the folder form's directory, the configured workspace, the scaffolder's root. With no
// selection it opens at $HOME as before; a selection that no longer lists falls back to $HOME
// rather than leaving a notice about a path the user never asked to see.
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import ProjectPicker from '../components/ProjectPicker'
import { ApiError } from '../api/apiError'

const browseDirs = vi.fn()
const recentProjects = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    browseDirs: (path?: string) => browseDirs(path),
    recentProjects: () => recentProjects(),
    // Mocked even where no test reads it: the picker reads favourites on every open.
    favoriteProjects: async () => ({ dirs: [] }),
  },
}))

const anchorRect = {
  top: 100, bottom: 130, left: 100, right: 200, width: 100, height: 30, x: 100, y: 100,
  toJSON() {},
} as DOMRect

const picker = (open: boolean, startPath?: string) => (
  <ProjectPicker open={open} onOpenChange={() => {}} anchorRect={anchorRect} onSelect={() => {}} startPath={startPath} />
)

const HOME = { path: '/home/user', parent: '/home', dirs: [{ name: 'other', path: '/home/user/other' }] }
const PROJECT = { path: '/home/user/proj', parent: '/home/user', dirs: [{ name: 'src', path: '/home/user/proj/src' }] }

const listing = (path?: string) => (path === '/home/user/proj' ? PROJECT : HOME)
const comboValue = async () => (await screen.findByRole('combobox') as HTMLInputElement).value

describe('ProjectPicker Browse start path (#17129)', () => {
  beforeEach(() => {
    browseDirs.mockReset()
    recentProjects.mockReset()
    // No recent projects -> the picker opens straight on the Browse tab.
    recentProjects.mockResolvedValue({ dirs: [] })
  })

  it('opens at $HOME when the caller has no selection', async () => {
    browseDirs.mockImplementation(async (p?: string) => listing(p))
    renderWithProviders(picker(true))
    await waitFor(async () => expect(await comboValue()).toBe('/home/user/'))
    expect(browseDirs).toHaveBeenCalledTimes(1)
    expect(browseDirs).toHaveBeenCalledWith(undefined)
  })

  it('treats a blank selection as no selection', async () => {
    browseDirs.mockImplementation(async (p?: string) => listing(p))
    renderWithProviders(picker(true, '   '))
    await waitFor(async () => expect(await comboValue()).toBe('/home/user/'))
    expect(browseDirs).toHaveBeenCalledWith(undefined)
  })

  it('opens inside the selected project', async () => {
    browseDirs.mockImplementation(async (p?: string) => listing(p))
    renderWithProviders(picker(true, '/home/user/proj'))
    await waitFor(async () => expect(await comboValue()).toBe('/home/user/proj/'))
    expect(await screen.findByText('src')).toBeInTheDocument()
    expect(browseDirs).toHaveBeenCalledTimes(1)
    expect(browseDirs).toHaveBeenCalledWith('/home/user/proj')
  })

  it.each([
    ['no longer a directory', new ApiError(400, 'Not a directory', JSON.stringify({ error: 'Not a directory', code: 'not_a_directory' }))],
    ['refused as sensitive', new ApiError(403, 'Access denied', JSON.stringify({ error: 'Access denied', code: 'access_denied' }))],
  ])('falls back to $HOME, naming the selection, when it is %s', async (_label, err) => {
    browseDirs.mockImplementation(async (p?: string) => {
      if (p === '/gone') throw err
      return listing(p)
    })
    renderWithProviders(picker(true, '/gone'))
    await waitFor(async () => expect(await comboValue()).toBe('/home/user/'))
    expect(browseDirs.mock.calls).toEqual([['/gone'], [undefined]])
    // The switch from the selection to `~` is explained, not silent: the notice names the
    // refused path and the home listing still on screen.
    const notice = await screen.findByTestId('pp-listing-error')
    expect(notice.textContent).toContain('/gone')
    expect(notice.textContent).toContain('/home/user/')
  })

  it('clears the carried notice on the next successful listing', async () => {
    browseDirs.mockImplementation(async (p?: string) => {
      if (p === '/gone') throw new ApiError(400, 'Not a directory', JSON.stringify({ error: 'Not a directory', code: 'not_a_directory' }))
      return listing(p)
    })
    renderWithProviders(picker(true, '/gone'))
    await screen.findByTestId('pp-listing-error')
    fireEvent.click(await screen.findByText('other'))
    await waitFor(() => expect(screen.queryByTestId('pp-listing-error')).toBeNull())
  })

  it('keeps the notice, naming the path, when the selection fails recoverably', async () => {
    browseDirs.mockRejectedValue(new ApiError(500, 'boom'))
    renderWithProviders(picker(true, '/home/user/proj'))
    const notice = await screen.findByTestId('pp-listing-error')
    expect(notice.textContent).toContain('/home/user/proj')
    expect(browseDirs).toHaveBeenCalledTimes(1)
  })

  it('reads the selection once per open', async () => {
    browseDirs.mockImplementation(async (p?: string) => listing(p))
    const view = renderWithProviders(picker(true, '/home/user/proj'))
    await waitFor(async () => expect(await comboValue()).toBe('/home/user/proj/'))
    // The caller's field changes while the picker is up: the listing stays put.
    view.rerender(picker(true, '/home/user'))
    expect(browseDirs).toHaveBeenCalledTimes(1)
    // The next open reads the new selection.
    view.rerender(picker(false, '/home/user'))
    view.rerender(picker(true, '/home/user'))
    await waitFor(() => expect(browseDirs).toHaveBeenLastCalledWith('/home/user'))
    expect(browseDirs).toHaveBeenCalledTimes(2)
  })
})
