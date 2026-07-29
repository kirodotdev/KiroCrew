import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import WindowsTitlebarMenu from '../components/WindowsTitlebarMenu'

describe('WindowsTitlebarMenu', () => {
  afterEach(() => {
    delete (window as Window & { electronAPI?: unknown }).electronAPI
    delete document.documentElement.dataset.mode
  })

  it('renders the native application menu anchors in titlebar order', () => {
    render(<WindowsTitlebarMenu />)
    expect(screen.getAllByRole('button').map(item => item.textContent)).toEqual([
      'File',
      'Edit',
      'View',
      'Connection',
      'Window',
      'Help',
    ])
  })

  it('opens the matching native submenu below its trigger', () => {
    document.documentElement.dataset.mode = 'dark'
    const showAppMenu = vi.fn()
    ;(window as Window & { electronAPI?: { showAppMenu: typeof showAppMenu } }).electronAPI = { showAppMenu }
    const { getByText } = render(<header><WindowsTitlebarMenu /></header>)
    const view = getByText('View')
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

    fireEvent.click(view)

    expect(showAppMenu).toHaveBeenCalledWith('view-menu', { x: 91, y: 42 }, 'dark')
  })
})
