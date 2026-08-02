import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import WindowsTitlebarMenu from '../components/WindowsTitlebarMenu'

type MenuAPI = {
  getAppMenuItems: ReturnType<typeof vi.fn>
  executeAppMenuItem: ReturnType<typeof vi.fn>
}

function installMenuAPI() {
  const api: MenuAPI = {
    getAppMenuItems: vi.fn(async (id: string) => id === 'file-menu'
      ? [
          { type: 'normal', index: 0, label: 'Settings…', accelerator: 'CmdOrCtrl+,', enabled: true, checked: false },
          { type: 'separator', index: 1 },
          { type: 'normal', index: 2, label: 'Exit', accelerator: '', enabled: true, checked: false },
        ]
      : [{ type: 'normal', index: 0, label: 'Reload', accelerator: 'CmdOrCtrl+R', enabled: true, checked: false }]),
    executeAppMenuItem: vi.fn(),
  }
  ;(window as Window & { electronAPI?: MenuAPI }).electronAPI = api
  return api
}

describe('WindowsTitlebarMenu', () => {
  afterEach(() => {
    delete (window as Window & { electronAPI?: unknown }).electronAPI
    delete document.documentElement.dataset.mode
  })

  it('rests as a hamburger and expands into the application menu labels', async () => {
    const api = installMenuAPI()
    const onExpandedChange = vi.fn()
    render(<header><WindowsTitlebarMenu onExpandedChange={onExpandedChange} /></header>)

    const hamburger = screen.getByRole('button', { name: 'Open menu' })
    expect(screen.queryByText('File')).toBeNull()
    fireEvent.click(hamburger)

    expect(screen.getAllByRole('button').map(item => item.textContent)).toEqual([
      'File',
      'Edit',
      'View',
      'Connection',
      'Window',
      'Help',
    ])
    expect(api.getAppMenuItems).toHaveBeenCalledWith('file-menu')
    expect(await screen.findByRole('menuitem', { name: /Settings/ })).toBeTruthy()
    expect(onExpandedChange).toHaveBeenCalledWith(true)
  })

  it('switches the active submenu when another label is hovered', async () => {
    const api = installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    const view = screen.getByText('View')
    vi.spyOn(view, 'getBoundingClientRect').mockReturnValue({
      x: 91,
      y: 4,
      left: 91,
      top: 4,
      right: 137,
      bottom: 32,
      width: 46,
      height: 28,
      toJSON: () => ({}),
    })
    vi.spyOn(view.closest('header') as HTMLElement, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 800,
      bottom: 42,
      width: 800,
      height: 42,
      toJSON: () => ({}),
    })

    fireEvent.mouseEnter(view)

    await waitFor(() => expect(api.getAppMenuItems).toHaveBeenLastCalledWith('view-menu'))
    expect(await screen.findByRole('menuitem', { name: /Reload/ })).toBeTruthy()
    expect(screen.getByText('File')).toBeTruthy()
    expect(view.getAttribute('aria-expanded')).toBe('true')
  })

  it('collapses to the hamburger on Escape', () => {
    installMenuAPI()
    const onExpandedChange = vi.fn()
    render(<header><WindowsTitlebarMenu onExpandedChange={onExpandedChange} /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })

    expect(screen.queryByText('File')).toBeNull()
    expect(screen.getByRole('button', { name: 'Open menu' })).toBeTruthy()
    expect(onExpandedChange).toHaveBeenLastCalledWith(false)
  })

  it('collapses when the user clicks outside the menu session', () => {
    installMenuAPI()
    render(<div><header><WindowsTitlebarMenu /></header><button type="button">Outside</button></div>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    fireEvent.pointerDown(screen.getByRole('button', { name: 'Outside' }))

    expect(screen.queryByText('File')).toBeNull()
    expect(screen.getByRole('button', { name: 'Open menu' })).toBeTruthy()
  })

  it('executes a selected command in Electron and collapses', async () => {
    const api = installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    fireEvent.click(await screen.findByRole('menuitem', { name: /Settings/ }))

    expect(api.executeAppMenuItem).toHaveBeenCalledWith('file-menu', 0)
    expect(screen.queryByText('File')).toBeNull()
  })
})
