// Windows drive navigation in the Browse tab.
//
// On Windows the parent of a drive root (`C:\`) is not a directory: the
// backend answers `parent: ""` there, and offers the mounted drives behind
// `api.browseDrives()` as the virtual level above. Before this, the Back
// control disappeared at the drive root (parent === path) and the only route to
// `D:\` was pasting a full path -- and a typed `D:\` did not even browse,
// because auto-drill keyed on `/` alone.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act, cleanup } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ProjectPicker from '../components/ProjectPicker'
import { api } from '../api/client'

type BrowseDirsResult = Awaited<ReturnType<typeof api.browseDirs>>

const listing = (path: string, parent: string, dirs: { name: string; path: string }[] = []): BrowseDirsResult =>
  ({ path, parent, dirs })

const DRIVES = { path: '', parent: '', dirs: [{ name: 'C:\\', path: 'C:\\' }, { name: 'D:\\', path: 'D:\\' }] }

const rect = (top: number, left: number, width = 80, height = 24): DOMRect => ({
  top, left, width, height, bottom: top + height, right: left + width, x: left, y: top, toJSON: () => ({}),
} as DOMRect)

function mount() {
  const onSelect = vi.fn()
  renderWithProviders(
    <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={onSelect} />,
  )
  return { onSelect }
}

const drain = () => act(async () => { await vi.advanceTimersByTimeAsync(0) })

beforeEach(() => {
  vi.useFakeTimers()
  Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
  // No recent projects -> the picker opens straight on the Browse tab.
  vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: [] })
  vi.spyOn(api, 'browseDrives').mockResolvedValue(DRIVES)
})

afterEach(() => {
  vi.runOnlyPendingTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('ProjectPicker: Windows drive roots', () => {
  it('still hides Back at the POSIX root, whose parent is itself', async () => {
    vi.spyOn(api, 'browseDirs').mockResolvedValue(listing('/', '/', [{ name: 'home', path: '/home' }]))
    mount()
    await drain()
    expect(screen.queryByRole('button', { name: 'Back' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'All drives' })).toBeNull()
  })

  it('shows Back at a drive root and lists the drives when it is pressed', async () => {
    const browseSpy = vi.spyOn(api, 'browseDirs').mockResolvedValue(listing('C:\\', '', [{ name: 'Users', path: 'C:\\Users' }]))
    mount()
    await drain()
    const input = screen.getByRole('combobox') as HTMLInputElement
    expect(input.value).toBe('C:\\')

    // At a drive root the control names its destination, not "Back" — as
    // visible text, not only as the tooltip.
    expect(screen.queryByRole('button', { name: 'Back' })).toBeNull()
    expect(screen.getByRole('button', { name: 'All drives' }).textContent).toContain('All drives')
    fireEvent.click(screen.getByRole('button', { name: 'All drives' }))
    await drain()

    expect(api.browseDrives).toHaveBeenCalledTimes(1)
    // The drive list is not a directory: nothing is browsed and the input is
    // cleared so the drive rows are not filtered by the old path.
    expect(browseSpy).toHaveBeenCalledTimes(1)
    expect(input.value).toBe('')
    // The hint is drive-shaped here, not the POSIX `/path/to/project`.
    expect(input.placeholder).toBe('e.g. D:\\path\\to\\project')
    // The top has no Back of its own.
    expect(screen.queryByRole('button', { name: 'All drives' })).toBeNull()
    expect(screen.getByRole('option', { name: /C:\\/ })).toBeTruthy()
    expect(screen.getByRole('option', { name: /D:\\/ })).toBeTruthy()
  })

  it('drills into a drive chosen from the drive list', async () => {
    const browseSpy = vi.spyOn(api, 'browseDirs').mockResolvedValue(listing('C:\\', ''))
    mount()
    await drain()
    fireEvent.click(screen.getByRole('button', { name: 'All drives' }))
    await drain()

    browseSpy.mockClear()
    browseSpy.mockResolvedValue(listing('D:\\', '', [{ name: 'work', path: 'D:\\work' }]))
    fireEvent.click(screen.getByRole('option', { name: /D:\\/ }))
    await drain()

    expect(browseSpy).toHaveBeenCalledWith('D:\\')
    expect((screen.getByRole('combobox') as HTMLInputElement).value).toBe('D:\\')
    expect((screen.getByRole('combobox') as HTMLInputElement).placeholder).toBe('/path/to/project')
    expect(screen.getByRole('option', { name: /work/ })).toBeTruthy()
  })

  it('the back control keeps its shape one level down: chevron plus a visible "Back"', async () => {
    vi.spyOn(api, 'browseDirs').mockResolvedValue(listing('D:\\work', 'D:\\', [{ name: 'notes', path: 'D:\\work\\notes' }]))
    mount()
    await drain()
    const back = screen.getByRole('button', { name: 'Back' })
    expect(back.textContent).toContain('Back')
    // The tooltip names the destination, so the two states read as one control.
    expect(back.getAttribute('title')).toBe('Back to D:\\')
    expect(screen.queryByRole('button', { name: 'All drives' })).toBeNull()
  })

  it('the failure notice names a drive other than the one on screen', async () => {
    vi.spyOn(api, 'browseDirs').mockResolvedValue(listing('D:\\', '', [{ name: 'work', path: 'D:\\work' }]))
    vi.mocked(api.browseDrives).mockRejectedValue(new Error('503'))
    mount()
    await drain()
    fireEvent.click(screen.getByRole('button', { name: 'All drives' }))
    await drain()
    const text = screen.getByRole('alert').textContent ?? ''
    expect(text).toContain('still shows D:\\')
    expect(text).toContain('E:\\')
  })

  it('a failed directory listing is said out loud too, and typing clears it', async () => {
    const browseSpy = vi.spyOn(api, 'browseDirs').mockResolvedValue(listing('C:\\', '', [{ name: 'Users', path: 'C:\\Users' }]))
    mount()
    await drain()
    browseSpy.mockRejectedValue(new Error('403'))
    fireEvent.click(screen.getByRole('option', { name: /Users/ }))
    await drain()
    const alert = screen.getByRole('alert')
    expect(screen.getByTestId('pp-listing-error')).toBeTruthy()
    // Names what failed, and the surviving listing the way the field shows it.
    expect(alert.textContent).toContain('Could not open C:\\Users')
    expect(alert.textContent).toContain('still shows C:\\.')
    // The rows the user was on stay put and usable.
    expect(screen.getByRole('option', { name: /Users/ })).toBeTruthy()
    // A failed drill by click leaves the field naming the listing still on
    // screen (C:\), so that intact directory stays committable.
    expect((screen.getByRole('combobox') as HTMLInputElement).value).toBe('C:\\')
    expect((screen.getByRole('button', { name: 'Select' }) as HTMLButtonElement).disabled).toBe(false)
    // The first keystroke retires the notice.
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'C:\\U' } })
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('offers the agent hand-off only where the mount opts in', async () => {
    vi.spyOn(api, 'browseDirs').mockResolvedValue(listing('C:\\', ''))
    vi.mocked(api.browseDrives).mockRejectedValue(new Error('503'))
    // Default (FolderConfigModal and friends float over unsaved drafts): no button.
    mount()
    await drain()
    fireEvent.click(screen.getByRole('button', { name: 'All drives' }))
    await drain()
    expect(screen.getByRole('alert')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Ask the agent/i })).toBeNull()
    cleanup()
    // ChatPage opts in: the notice carries the hand-off.
    renderWithProviders(
      <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} errorHandoff />,
    )
    await drain()
    fireEvent.click(screen.getByRole('button', { name: 'All drives' }))
    await drain()
    expect(screen.getByRole('button', { name: /Ask the agent/i })).toBeTruthy()
  })

  it('a typed path that failed cannot be committed by button or Ctrl+Enter', async () => {
    vi.spyOn(api, 'browseDirs').mockResolvedValue(listing('C:\\', '', [{ name: 'Users', path: 'C:\\Users' }]))
    const { onSelect } = mount()
    await drain()
    vi.mocked(api.browseDirs).mockRejectedValue(new Error('404'))
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'Z:\\' } })
    await act(async () => { await vi.advanceTimersByTimeAsync(250) })
    expect(screen.getByTestId('pp-listing-error')).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toContain('Could not open Z:\\')
    // The field names Z:\, not the C:\ rows below — committing would plant it.
    expect((screen.getByRole('button', { name: 'Select' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter', ctrlKey: true })
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('ArrowLeft at the caret start on a drive root also opens the drive list', async () => {
    vi.spyOn(api, 'browseDirs').mockResolvedValue(listing('C:\\', ''))
    mount()
    await drain()
    const input = screen.getByRole('combobox') as HTMLInputElement
    input.setSelectionRange(0, 0)
    fireEvent.keyDown(input, { key: 'ArrowLeft' })
    await drain()
    expect(api.browseDrives).toHaveBeenCalledTimes(1)
  })

  it('says so when the drive list cannot be fetched, and clears on the next listing', async () => {
    const browseSpy = vi.spyOn(api, 'browseDirs').mockResolvedValue(listing('C:\\', '', [{ name: 'Users', path: 'C:\\Users' }]))
    vi.mocked(api.browseDrives).mockRejectedValue(new Error('503'))
    mount()
    await drain()
    fireEvent.click(screen.getByRole('button', { name: 'All drives' }))
    await drain()
    // Silent failure was the defect: Back appeared to do nothing.
    expect(screen.getByTestId('pp-drives-error')).toBeTruthy()
    // The copy says which listing is still on screen and names a drive the
    // user is NOT on, so it cannot read as a stale error about these rows.
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toContain('the list below still shows C:\\')
    expect(alert.textContent).toContain('still shows C:\\')
    expect(alert.textContent).toContain('D:\\')
    // The drive root stays listed; the user can still drill or type — and the
    // directory on screen is intact, so it stays committable.
    expect(screen.getByRole('option', { name: /Users/ })).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Select' }) as HTMLButtonElement).disabled).toBe(false)

    browseSpy.mockResolvedValue(listing('C:\\Users', 'C:\\'))
    fireEvent.click(screen.getByRole('option', { name: /Users/ }))
    await drain()
    expect(screen.queryByTestId('pp-drives-error')).toBeNull()
  })

  it('a slow drive list cannot overwrite a faster drill made after it', async () => {
    const browseSpy = vi.spyOn(api, 'browseDirs').mockResolvedValue(listing('C:\\', '', [{ name: 'Users', path: 'C:\\Users' }]))
    let releaseDrives: (v: typeof DRIVES) => void = () => {}
    vi.mocked(api.browseDrives).mockImplementation(() => new Promise(res => { releaseDrives = res }))
    mount()
    await drain()
    // Back: the drive list is requested but has not answered yet...
    fireEvent.click(screen.getByRole('button', { name: 'All drives' }))
    await drain()
    // ...so the still-listed C:\ rows are used: drill into Users.
    browseSpy.mockResolvedValue(listing('C:\\Users', 'C:\\', [{ name: 'me', path: 'C:\\Users\\me' }]))
    fireEvent.click(screen.getByRole('option', { name: /Users/ }))
    await drain()
    expect(screen.getByRole('option', { name: /me/ })).toBeTruthy()
    // The late drive list must not replace the child listing.
    releaseDrives(DRIVES)
    await drain()
    expect(screen.getByRole('option', { name: /me/ })).toBeTruthy()
    expect(screen.queryByRole('option', { name: /D:\\/ })).toBeNull()
    expect((screen.getByRole('combobox') as HTMLInputElement).value).toBe('C:\\Users\\')
  })

  it('a drive list answering after a keystroke does not erase what was typed', async () => {
    vi.spyOn(api, 'browseDirs').mockResolvedValue(listing('C:\\', ''))
    let releaseDrives: (v: typeof DRIVES) => void = () => {}
    vi.mocked(api.browseDrives).mockImplementation(() => new Promise(res => { releaseDrives = res }))
    mount()
    await drain()
    fireEvent.click(screen.getByRole('button', { name: 'All drives' }))
    await drain()
    const input = screen.getByRole('combobox') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'D:\\work\\' } })
    releaseDrives(DRIVES)
    await drain()
    // The late drive list is dropped: the typed path survives for its own drill.
    expect(input.value).toBe('D:\\work\\')
    expect(screen.queryByRole('option', { name: /E:\\/ })).toBeNull()
  })

  it('cannot commit the drive list itself as a project', async () => {
    vi.spyOn(api, 'browseDirs').mockResolvedValue(listing('C:\\', ''))
    const { onSelect } = mount()
    await drain()
    fireEvent.click(screen.getByRole('button', { name: 'All drives' }))
    await drain()
    const select = screen.getByRole('button', { name: 'Select' }) as HTMLButtonElement
    expect(select.disabled).toBe(true)
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter', ctrlKey: true })
    expect(onSelect).not.toHaveBeenCalled()
  })
})

describe('ProjectPicker: typing a Windows path', () => {
  it('auto-drills on a typed trailing backslash', async () => {
    const browseSpy = vi.spyOn(api, 'browseDirs').mockResolvedValue(listing('C:\\', ''))
    mount()
    await drain()
    const input = screen.getByRole('combobox')
    browseSpy.mockClear()
    fireEvent.change(input, { target: { value: 'C:\\Users\\' } })
    expect(browseSpy).not.toHaveBeenCalled()
    await act(async () => { await vi.advanceTimersByTimeAsync(250) })
    expect(browseSpy).toHaveBeenCalledWith('C:\\Users')
  })

  it('keeps a bare drive root whole: D:\\ and D:/ browse the root, never drive-relative D:', async () => {
    const browseSpy = vi.spyOn(api, 'browseDirs').mockResolvedValue(listing('C:\\', ''))
    mount()
    await drain()
    const input = screen.getByRole('combobox')

    browseSpy.mockClear()
    fireEvent.change(input, { target: { value: 'D:\\' } })
    await act(async () => { await vi.advanceTimersByTimeAsync(250) })
    expect(browseSpy).toHaveBeenCalledWith('D:\\')

    browseSpy.mockClear()
    fireEvent.change(input, { target: { value: 'E:/' } })
    await act(async () => { await vi.advanceTimersByTimeAsync(250) })
    expect(browseSpy).toHaveBeenCalledWith('E:/')
    expect(browseSpy).not.toHaveBeenCalledWith('E:')

    // A doubled separator (stray keystroke) still browses the ROOT, never `F:`.
    browseSpy.mockClear()
    fireEvent.change(input, { target: { value: 'F:\\\\' } })
    await act(async () => { await vi.advanceTimersByTimeAsync(250) })
    expect(browseSpy).toHaveBeenCalledWith('F:\\')
    expect(browseSpy).not.toHaveBeenCalledWith('F:')
  })

  it('does not re-browse the drive already loaded when only the letter case differs', async () => {
    const browseSpy = vi.spyOn(api, 'browseDirs').mockResolvedValue(listing('C:\\', ''))
    mount()
    await drain()
    browseSpy.mockClear()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'c:\\' } })
    await act(async () => { await vi.advanceTimersByTimeAsync(250) })
    expect(browseSpy).not.toHaveBeenCalled()
  })

  it('a trailing backslash on a POSIX path is still a filename character, not a drill', async () => {
    const browseSpy = vi.spyOn(api, 'browseDirs').mockResolvedValue(listing('/home/u', '/home'))
    mount()
    await drain()
    browseSpy.mockClear()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '/home/u/odd\\' } })
    await act(async () => { await vi.advanceTimersByTimeAsync(250) })
    expect(browseSpy).not.toHaveBeenCalled()
  })

  it('filters the listed children by the last segment after a backslash', async () => {
    vi.spyOn(api, 'browseDirs').mockResolvedValue(listing('C:\\', '', [
      { name: 'Users', path: 'C:\\Users' },
      { name: 'Windows', path: 'C:\\Windows' },
    ]))
    mount()
    await drain()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'C:\\Win' } })
    expect(screen.queryByRole('option', { name: /Users/ })).toBeNull()
    expect(screen.getByRole('option', { name: /Windows/ })).toBeTruthy()
  })
})
