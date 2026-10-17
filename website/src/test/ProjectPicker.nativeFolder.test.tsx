// The desktop app's OS-native folder chooser in the project picker. The button
// exists only where a pick can work -- the desktop shell's bridge is present AND
// its main process confirms this window's gateway runs on this machine -- and
// everything else about the picker is identical with or without it. A chosen
// folder is checked through the same gateway listing a typed path takes before
// it is committed; a cancel leaves the picker open as it was, and a refused
// folder or a dialog that could not open is told beside the button.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ProjectPicker from '../components/ProjectPicker'
import { api, ApiError } from '../api/client'

const rect = (top: number, left: number, width = 80, height = 24): DOMRect => ({
  top, left, width, height, bottom: top + height, right: left + width, x: left, y: top, toJSON: () => ({}),
} as DOMRect)

const BUTTON_NAME = /open system folder picker/i

type Bridge = { available: ReturnType<typeof vi.fn>; pick: ReturnType<typeof vi.fn> }

function installBridge({ available = true, picked = null as string | null } = {}): Bridge {
  const bridge = {
    available: vi.fn().mockResolvedValue(available),
    pick: vi.fn().mockResolvedValue(picked),
  }
  ;(window as { folderPickAPI?: unknown }).folderPickAPI = bridge
  return bridge
}

function mount() {
  const onSelect = vi.fn()
  const onOpenChange = vi.fn()
  renderWithProviders(
    <ProjectPicker open={true} onOpenChange={onOpenChange} anchorRect={rect(100, 50)} onSelect={onSelect} />,
  )
  return { onSelect, onOpenChange }
}

beforeEach(() => {
  Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
  vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: ['/home/u/projA'] })
  vi.spyOn(api, 'favoriteProjects').mockResolvedValue({ dirs: [] })
  vi.spyOn(api, 'browseDirs').mockResolvedValue({ path: '/home/u', parent: '/home', dirs: [] })
})

afterEach(() => {
  delete (window as { folderPickAPI?: unknown }).folderPickAPI
  vi.restoreAllMocks()
})

/** The picker's own controls, which must be the same with or without the native chooser. */
async function expectInAppPickerIntact() {
  expect(await screen.findByText('/home/u/projA')).toBeInTheDocument()
  expect(screen.getByText('Favorites')).toBeInTheDocument()
  expect(screen.getByText('Recent')).toBeInTheDocument()
  fireEvent.mouseDown(screen.getByText('Browse'))
  expect(await screen.findByRole('combobox', { name: 'Project directory path' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Select' })).toBeInTheDocument()
}

describe('ProjectPicker: OS-native folder chooser', () => {
  it('shows no system-dialog button without the desktop bridge, and the in-app picker is unchanged', async () => {
    mount()
    await expectInAppPickerIntact()
    expect(screen.queryByRole('button', { name: BUTTON_NAME })).not.toBeInTheDocument()
  })

  it('shows no system-dialog button when the shell says the gateway is remote', async () => {
    const bridge = installBridge({ available: false })
    mount()
    await waitFor(() => expect(bridge.available).toHaveBeenCalled())
    await expectInAppPickerIntact()
    expect(screen.queryByRole('button', { name: BUTTON_NAME })).not.toBeInTheDocument()
    expect(bridge.pick).not.toHaveBeenCalled()
  })

  it('shows no system-dialog button when the availability check fails', async () => {
    const bridge = installBridge()
    bridge.available.mockRejectedValue(new Error('restricted to the local dashboard'))
    mount()
    await waitFor(() => expect(bridge.available).toHaveBeenCalled())
    await expectInAppPickerIntact()
    expect(screen.queryByRole('button', { name: BUTTON_NAME })).not.toBeInTheDocument()
  })

  it('commits the chosen folder after the gateway lists it', async () => {
    const bridge = installBridge({ picked: '/home/u/chosen' })
    const { onSelect, onOpenChange } = mount()
    fireEvent.click(await screen.findByRole('button', { name: BUTTON_NAME }))

    await waitFor(() => expect(onSelect).toHaveBeenCalledWith('/home/u/chosen'))
    // The bridge is asked with no arguments: the renderer chooses no dialog options.
    expect(bridge.pick).toHaveBeenCalledTimes(1)
    expect(bridge.pick).toHaveBeenCalledWith()
    // The same listing a typed path takes vouches for the folder before it is used.
    expect(api.browseDirs).toHaveBeenCalledWith('/home/u/chosen')
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('keeps the picker open and unchanged when the dialog is cancelled', async () => {
    const bridge = installBridge({ picked: null })
    const { onSelect, onOpenChange } = mount()
    const button = await screen.findByRole('button', { name: BUTTON_NAME })
    const listingsBefore = vi.mocked(api.browseDirs).mock.calls.length
    fireEvent.click(button)

    await waitFor(() => expect(bridge.pick).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(button).not.toHaveAttribute('aria-disabled'))
    expect(onSelect).not.toHaveBeenCalled()
    expect(onOpenChange).not.toHaveBeenCalledWith(false)
    expect(vi.mocked(api.browseDirs).mock.calls.length).toBe(listingsBefore)
    // Still on the tab it was on, with its rows.
    expect(screen.getByText('/home/u/projA')).toBeInTheDocument()
  })

  it('does not commit a chosen folder the gateway refuses to list, and says so beside the button', async () => {
    installBridge({ picked: '/home/u/.ssh' })
    vi.mocked(api.browseDirs).mockImplementation(async (path?: string) => {
      if (path === '/home/u/.ssh') {
        throw new ApiError(403, 'Access denied', JSON.stringify({ error: 'Access denied', code: 'access_denied' }))
      }
      return { path: '/home/u', parent: '/home', dirs: [] }
    })
    const { onSelect, onOpenChange } = mount()
    fireEvent.click(await screen.findByRole('button', { name: BUTTON_NAME }))

    const notice = await screen.findByTestId('pp-native-pick-error')
    expect(notice).toHaveTextContent("/home/u/.ssh can't be used as the project folder")
    expect(onSelect).not.toHaveBeenCalled()
    expect(onOpenChange).not.toHaveBeenCalledWith(false)
    // The pane the user was on is left as it was: same tab, same rows, no listing notice.
    expect(screen.getByText('/home/u/projA')).toBeInTheDocument()
    expect(screen.queryByTestId('pp-listing-error')).not.toBeInTheDocument()
  })

  it('names a timed-out check of the chosen folder as a timeout', async () => {
    installBridge({ picked: '/home/u/slow' })
    vi.mocked(api.browseDirs).mockImplementation(async (path?: string) => {
      if (path === '/home/u/slow') throw Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })
      return { path: '/home/u', parent: '/home', dirs: [] }
    })
    const { onSelect } = mount()
    fireEvent.click(await screen.findByRole('button', { name: BUTTON_NAME }))
    expect(await screen.findByTestId('pp-native-pick-error')).toHaveTextContent('Checking /home/u/slow timed out')
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('tells a pick the shell refused or could not show, rather than reading it as a cancel', async () => {
    const bridge = installBridge()
    bridge.pick.mockRejectedValue(new Error('dashboard:pick-folder is restricted to the local dashboard'))
    const { onSelect, onOpenChange } = mount()
    const button = await screen.findByRole('button', { name: BUTTON_NAME })
    fireEvent.click(button)

    expect(await screen.findByTestId('pp-native-pick-error')).toHaveTextContent("The system folder picker couldn't open")
    expect(onSelect).not.toHaveBeenCalled()
    expect(onOpenChange).not.toHaveBeenCalledWith(false)
    expect(api.browseDirs).not.toHaveBeenCalledWith(expect.stringContaining('restricted'))

    // The next attempt starts clean: a cancel clears the notice.
    bridge.pick.mockResolvedValue(null)
    await waitFor(() => expect(button).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(button)
    await waitFor(() => expect(screen.queryByTestId('pp-native-pick-error')).not.toBeInTheDocument())
  })

  it('opens one dialog at a time', async () => {
    const bridge = installBridge()
    let release: (value: string | null) => void = () => {}
    bridge.pick.mockImplementation(() => new Promise<string | null>(resolve => { release = resolve }))
    mount()
    const button = await screen.findByRole('button', { name: BUTTON_NAME })
    fireEvent.click(button)
    await waitFor(() => expect(button).toHaveAttribute('aria-disabled', 'true'))
    fireEvent.click(button)
    expect(bridge.pick).toHaveBeenCalledTimes(1)
    release(null)
    await waitFor(() => expect(button).not.toHaveAttribute('aria-disabled'))
  })

  it('drops a folder chosen after the picker closed', async () => {
    const bridge = installBridge()
    let release: (value: string | null) => void = () => {}
    bridge.pick.mockImplementation(() => new Promise<string | null>(resolve => { release = resolve }))
    const onSelect = vi.fn()
    const view = renderWithProviders(
      <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={onSelect} />,
    )
    fireEvent.click(await screen.findByRole('button', { name: BUTTON_NAME }))
    await waitFor(() => expect(bridge.pick).toHaveBeenCalledTimes(1))

    view.rerender(<ProjectPicker open={false} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={onSelect} />)
    release('/home/u/late')
    // Reopening does not adopt the earlier open's answer either.
    view.rerender(<ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={onSelect} />)
    expect(await screen.findByText('/home/u/projA')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: BUTTON_NAME })).not.toHaveAttribute('aria-disabled'))
    expect(onSelect).not.toHaveBeenCalled()
    expect(vi.mocked(api.browseDirs).mock.calls.some(([p]) => p === '/home/u/late')).toBe(false)
  })

  it('drops a pick whose picker unmounted while the folder was being checked', async () => {
    installBridge({ picked: '/home/u/first' })
    let releaseCheck: () => void = () => {}
    vi.mocked(api.browseDirs).mockImplementation((path?: string) => {
      if (path === '/home/u/first') {
        return new Promise(resolve => { releaseCheck = () => resolve({ path: '/home/u/first', parent: '/home/u', dirs: [] }) })
      }
      return Promise.resolve({ path: '/home/u', parent: '/home', dirs: [] })
    })
    const onSelect = vi.fn()
    const view = renderWithProviders(
      <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={onSelect} />,
    )
    fireEvent.click(await screen.findByRole('button', { name: BUTTON_NAME }))
    await waitFor(() => expect(api.browseDirs).toHaveBeenCalledWith('/home/u/first'))

    view.unmount()
    releaseCheck()
    // Let the check's settlement run; an unmounted picker must not commit it.
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(onSelect).not.toHaveBeenCalled()
  })
})
