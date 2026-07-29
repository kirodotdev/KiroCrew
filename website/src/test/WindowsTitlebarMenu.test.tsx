import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import WindowsTitlebarMenu from '../components/WindowsTitlebarMenu'

describe('WindowsTitlebarMenu', () => {
  afterEach(() => {
    delete (window as Window & { electronAPI?: unknown }).electronAPI
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
    const showAppMenu = vi.fn()
    ;(window as Window & { electronAPI?: { showAppMenu: typeof showAppMenu } }).electronAPI = { showAppMenu }
    const { getByText } = render(<WindowsTitlebarMenu />)
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

    fireEvent.click(view)

    expect(showAppMenu).toHaveBeenCalledWith('view-menu', { x: 91, y: 32 })
  })
})
