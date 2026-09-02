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

/**
 * Dispatch a key from whatever actually has focus, the way a browser does.
 *
 * Firing directly on the <nav> is what let a real focus bug hide: if opening the
 * menu leaves focus on <body>, a browser's keydown never reaches the nav handler
 * at all, but a test that targets the nav still passes. Going through
 * document.activeElement makes these assertions fail in exactly that case.
 */
function pressKey(key: string) {
  const active = document.activeElement
  expect(active).not.toBe(document.body)
  fireEvent.keyDown(active as Element, { key })
}

describe('WindowsTitlebarMenu', () => {
  afterEach(() => {
    delete (window as Window & { electronAPI?: unknown }).electronAPI
    delete document.documentElement.dataset.mode
  })

  it('rests as a hamburger and expands into the application menu labels', async () => {
    const api = installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)

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
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })

    expect(screen.queryByText('File')).toBeNull()
    expect(screen.getByRole('button', { name: 'Open menu' })).toBeTruthy()
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

  it('collapses when the already-open label is clicked again', async () => {
    installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    // File opens with the hamburger, so its label is the active one.
    expect(await screen.findByRole('menuitem', { name: /Settings/ })).toBeTruthy()

    fireEvent.click(screen.getByText('File'))

    expect(screen.queryByRole('menu')).toBeNull()
    expect(screen.getByRole('button', { name: 'Open menu' })).toBeTruthy()
  })

  it('collapses rather than stranding an empty popup when the IPC read fails', async () => {
    const api = installMenuAPI()
    api.getAppMenuItems.mockRejectedValueOnce(new Error('ipc gone'))
    render(<header><WindowsTitlebarMenu /></header>)

    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    // A failed read must not leave the labels expanded over an empty popup —
    // the menu session ends and the hamburger comes back.
    await waitFor(() => expect(screen.queryByText('File')).toBeNull())
    expect(screen.getByRole('button', { name: 'Open menu' })).toBeTruthy()
  })

  it('walks top-level menus with ArrowRight / ArrowLeft, wrapping at both ends', async () => {
    const api = installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    // Opening moves focus into the row; wait for it, because pressing a key
    // before focus lands is not the state a real keyboard user is ever in.
    await waitFor(() => expect(screen.getByText('File')).toHaveFocus())

    // File (index 0) is active -> ArrowRight lands on Edit and opens it.
    pressKey('ArrowRight')
    await waitFor(() => expect(api.getAppMenuItems).toHaveBeenLastCalledWith('edit-menu'))
    expect(screen.getByText('Edit')).toHaveFocus()

    // ArrowLeft twice from Edit wraps past File round to Help.
    pressKey('ArrowLeft')
    await waitFor(() => expect(api.getAppMenuItems).toHaveBeenLastCalledWith('file-menu'))
    pressKey('ArrowLeft')
    await waitFor(() => expect(api.getAppMenuItems).toHaveBeenLastCalledWith('help-menu'))
    expect(screen.getByText('Help')).toHaveFocus()
  })

  it('ignores keys that are not menu navigation', async () => {
    const api = installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    await screen.findByRole('menuitem', { name: /Settings/ })
    const callsBefore = api.getAppMenuItems.mock.calls.length

    pressKey('a')

    expect(api.getAppMenuItems.mock.calls).toHaveLength(callsBefore)
    expect(screen.getByRole('menu')).toBeTruthy()
  })

  it('moves focus from the labels into the popup on ArrowDown', async () => {
    installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    const settings = await screen.findByRole('menuitem', { name: /Settings/ })

    pressKey('ArrowDown')

    // First enabled item, skipping the separator.
    expect(settings).toHaveFocus()
  })

  it('cycles popup items with ArrowDown / ArrowUp / Home / End', async () => {
    installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    const settings = await screen.findByRole('menuitem', { name: /Settings/ })
    const exit = screen.getByRole('menuitem', { name: /Exit/ })
    const popup = screen.getByRole('menu')

    // Enter the popup the way a user does — ArrowDown from the labels — so the
    // cycling below starts from a real focused item rather than from the
    // container itself.
    pressKey('ArrowDown')
    expect(settings).toHaveFocus()

    // The separator is not focusable, so Settings and Exit are the only stops.
    fireEvent.keyDown(popup, { key: 'ArrowDown' })
    expect(exit).toHaveFocus()

    // Wraps forward off the end, and back off the front.
    fireEvent.keyDown(popup, { key: 'ArrowDown' })
    expect(settings).toHaveFocus()
    fireEvent.keyDown(popup, { key: 'ArrowUp' })
    expect(exit).toHaveFocus()

    fireEvent.keyDown(popup, { key: 'Home' })
    expect(settings).toHaveFocus()
    fireEvent.keyDown(popup, { key: 'End' })
    expect(exit).toHaveFocus()
  })

  it('renders a checkbox item with its check state and a rewritten accelerator', async () => {
    const api = installMenuAPI()
    api.getAppMenuItems.mockResolvedValueOnce([
      { type: 'checkbox', index: 0, label: 'Keep on Top', accelerator: '', enabled: true, checked: true },
      { type: 'normal', index: 1, label: 'Zoom In', accelerator: 'CommandOrControl+Plus', enabled: true, checked: false },
      { type: 'normal', index: 2, label: 'Unavailable', accelerator: '', enabled: false, checked: false },
    ])
    render(<header><WindowsTitlebarMenu /></header>)

    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    const toggle = await screen.findByRole('menuitemcheckbox', { name: /Keep on Top/ })
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    // The Electron accelerator token is rewritten to the cap the user reads.
    expect(screen.getByRole('menuitem', { name: /Zoom In/ })).toHaveTextContent('Ctrl+Plus')
    expect(screen.getByRole('menuitem', { name: /Unavailable/ })).toBeDisabled()
  })

  it('opens a different menu when its label is clicked without a hover first', async () => {
    const api = installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    await screen.findByRole('menuitem', { name: /Settings/ })

    // Touch and some AT drivers deliver a click with no preceding mouseEnter,
    // so the click handler has to open a non-active menu on its own.
    fireEvent.click(screen.getByText('View'))

    await waitFor(() => expect(api.getAppMenuItems).toHaveBeenLastCalledWith('view-menu'))
    expect(await screen.findByRole('menuitem', { name: /Reload/ })).toBeTruthy()
  })

  describe('focus is handed back to the hamburger on close', () => {
    // Collapsing unmounts the labels AND the portal popup, so without an
    // explicit restore the focused element is destroyed and a keyboard user
    // lands on <body> — they have to Tab in from the top of the header again.
    // The restore is deferred to a rAF because the hamburger does not exist
    // until the render that drops `expanded`, so these await it rather than
    // stubbing rAF synchronously (which would fire before that render and
    // defeat the very deferral under test).
    const hamburger = () => screen.getByRole('button', { name: 'Open menu' })
    const expectFocusRestored = () => waitFor(() => expect(hamburger()).toHaveFocus())

    it('restores focus on Escape from the label row', async () => {
      installMenuAPI()
      render(<header><WindowsTitlebarMenu /></header>)
      fireEvent.click(hamburger())
      await screen.findByRole('menuitem', { name: /Settings/ })

      pressKey('Escape')

      await expectFocusRestored()
    })

    it('restores focus on Escape from inside the popup', async () => {
      installMenuAPI()
      render(<header><WindowsTitlebarMenu /></header>)
      fireEvent.click(hamburger())
      await waitFor(() => expect(screen.getByText('File')).toHaveFocus())

      // Descend into the popup first, so Escape is pressed from where a real
      // user would press it — with focus on an item, not on the container.
      pressKey('ArrowDown')
      expect(await screen.findByRole('menuitem', { name: /Settings/ })).toHaveFocus()

      pressKey('Escape')

      await expectFocusRestored()
    })

    it('restores focus after a command is picked', async () => {
      installMenuAPI()
      render(<header><WindowsTitlebarMenu /></header>)
      fireEvent.click(hamburger())

      fireEvent.click(await screen.findByRole('menuitem', { name: /Settings/ }))

      await expectFocusRestored()
    })

    it('restores focus when the open label is clicked shut', async () => {
      installMenuAPI()
      render(<header><WindowsTitlebarMenu /></header>)
      fireEvent.click(hamburger())
      await screen.findByRole('menuitem', { name: /Settings/ })

      fireEvent.click(screen.getByText('File'))

      await expectFocusRestored()
    })

    it('restores focus when an IPC failure collapses a keyboard-opened menu', async () => {
      const api = installMenuAPI()
      render(<header><WindowsTitlebarMenu /></header>)
      fireEvent.click(hamburger())
      await waitFor(() => expect(screen.getByText('File')).toHaveFocus())

      // Focus is inside the menu, so a failure that tears it down must hand
      // focus back rather than dropping the user on <body>.
      api.getAppMenuItems.mockRejectedValueOnce(new Error('ipc gone'))
      pressKey('ArrowRight')

      await expectFocusRestored()
    })

    it('does NOT steal focus when the failing open was hover-initiated', async () => {
      // The containment guard: on hover the user's focus is still wherever they
      // were typing, so an IPC failure must not yank it into the titlebar.
      const api = installMenuAPI()
      render(
        <div>
          <header><WindowsTitlebarMenu /></header>
          <input aria-label="Message" />
        </div>,
      )
      fireEvent.click(hamburger())
      await waitFor(() => expect(screen.getByText('File')).toHaveFocus())

      const input = screen.getByLabelText('Message')
      input.focus()
      expect(input).toHaveFocus()

      api.getAppMenuItems.mockRejectedValueOnce(new Error('ipc gone'))
      fireEvent.mouseEnter(screen.getByText('View'))

      await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
      expect(input).toHaveFocus()
      expect(hamburger()).not.toHaveFocus()
    })

    it('does NOT pull focus back on an outside click or a window blur', async () => {
      installMenuAPI()
      render(
        <div>
          <header><WindowsTitlebarMenu /></header>
          <button type="button">Outside</button>
        </div>,
      )

      // Outside click: the user is deliberately elsewhere, so yanking focus
      // into the titlebar would fight them.
      fireEvent.click(hamburger())
      fireEvent.pointerDown(screen.getByRole('button', { name: 'Outside' }))
      await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
      expect(hamburger()).not.toHaveFocus()

      // Window blur: same, and the listener must not leak its Event object in
      // as a truthy restore flag.
      fireEvent.click(hamburger())
      fireEvent(window, new Event('blur'))
      await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
      expect(hamburger()).not.toHaveFocus()
    })
  })

  it('moves focus into the label row when the menu is opened', async () => {
    // Expanding replaces the hamburger with the label row, unmounting the
    // element that was just activated. If focus is not moved in, it lands on
    // <body>, the nav's onKeyDown stops receiving anything, and a keyboard user
    // cannot drive the menu they just opened.
    installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)

    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    await waitFor(() => expect(screen.getByText('File')).toHaveFocus())
  })

  it('is fully operable from the keyboard end to end', async () => {
    // The whole chord, each key dispatched from whatever actually has focus:
    // open -> traverse -> descend into the popup -> Escape back out.
    const api = installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)

    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    await waitFor(() => expect(screen.getByText('File')).toHaveFocus())

    pressKey('ArrowRight')
    await waitFor(() => expect(api.getAppMenuItems).toHaveBeenLastCalledWith('edit-menu'))
    expect(screen.getByText('Edit')).toHaveFocus()

    pressKey('ArrowDown')
    expect(await screen.findByRole('menuitem', { name: /Reload/ })).toHaveFocus()

    pressKey('Escape')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Open menu' })).toHaveFocus())
  })

  it('does not dispatch a disabled item', async () => {
    const api = installMenuAPI()
    api.getAppMenuItems.mockResolvedValueOnce([
      { type: 'normal', index: 0, label: 'Unavailable', accelerator: '', enabled: false, checked: false },
    ])
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    const item = await screen.findByRole('menuitem', { name: /Unavailable/ })

    fireEvent.click(item)

    expect(api.executeAppMenuItem).not.toHaveBeenCalled()
  })
})
