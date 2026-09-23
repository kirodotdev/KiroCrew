// A failed folder listing and the "No subdirectories" empty-state describe the
// same list, so they must never be on screen together: the notice says the
// listing could not be read, the empty-state claims it was read and is empty.
// While a LISTING failure (`dir` / `drives`) is set only the notice speaks for
// the list; a listing that genuinely came back empty still gets the empty-state,
// including beside a preserved recent-projects notice, which is about a
// different read.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ProjectPicker from '../components/ProjectPicker'
import { api, ApiError } from '../api/client'
import { i18nT } from '../i18n/t'
import { LISTING_FAILURE_KEYS } from '../lib/searchErrorCause'

const rect = (top: number, left: number, width = 80, height = 24): DOMRect => ({
  top, left, width, height, bottom: top + height, right: left + width, x: left, y: top, toJSON: () => ({}),
} as DOMRect)

const picker = (open: boolean) => (
  <ProjectPicker open={open} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
)

function mount() {
  return renderWithProviders(picker(true))
}

beforeEach(() => {
  Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
  // No recent projects -> the picker opens straight on the Browse tab.
  vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: [] })
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('ProjectPicker: recent-project failure', () => {
  it('shows the timeout notice and still falls back to the working Browse tab', async () => {
    const timeout = Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })
    vi.mocked(api.recentProjects).mockRejectedValue(timeout)
    vi.spyOn(api, 'browseDirs').mockResolvedValue({
      path: '/home/u',
      parent: '/home',
      dirs: [{ name: 'workplace', path: '/home/u/workplace' }],
    })
    mount()

    expect(await screen.findByText('workplace')).toBeTruthy()
    const alert = screen.getByTestId('pp-recent-error')
    expect(alert.textContent).toBe(
      'Loading recent projects timed out. Type a project path above, or pick a folder from the list.',
    )
    expect(alert.textContent).not.toContain('deadline exceeded')
    expect(screen.getByPlaceholderText('/path/to/project')).toBeTruthy()
  })

  it('keeps a successful recent-project load unchanged and shows no notice', async () => {
    vi.mocked(api.recentProjects).mockResolvedValue({ dirs: ['/home/u/projA'] })
    vi.spyOn(api, 'browseDirs').mockResolvedValue({ path: '/home/u', parent: '/home', dirs: [] })
    mount()

    expect(await screen.findByText('projA')).toBeTruthy()
    expect(screen.queryByTestId('pp-recent-error')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('ignores a stale recents rejection after a reopen has loaded fresh state', async () => {
    const timeout = Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })
    let rejectStale!: (reason?: unknown) => void
    const stale = new Promise<{ dirs: string[] }>((_resolve, reject) => { rejectStale = reject })
    vi.mocked(api.recentProjects)
      .mockReturnValueOnce(stale)
      .mockResolvedValueOnce({ dirs: ['/home/u/fresh'] })
    vi.spyOn(api, 'browseDirs').mockResolvedValue({ path: '/home/u', parent: '/home', dirs: [] })

    const view = mount()
    await waitFor(() => expect(api.recentProjects).toHaveBeenCalledTimes(1))
    view.rerender(picker(false))
    view.rerender(picker(true))

    expect(await screen.findByText('fresh')).toBeTruthy()
    await act(async () => { rejectStale(timeout) })

    expect(screen.getByText('fresh')).toBeTruthy()
    expect(screen.queryByTestId('pp-recent-error')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('keeps a newer directory failure when the opening recents request rejects later', async () => {
    const timeout = Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })
    let rejectRecents!: (reason?: unknown) => void
    vi.mocked(api.recentProjects).mockReturnValue(
      new Promise<{ dirs: string[] }>((_resolve, reject) => { rejectRecents = reject }),
    )
    vi.spyOn(api, 'browseDirs')
      .mockResolvedValueOnce({
        path: '/home/u',
        parent: '/home',
        dirs: [{ name: 'broken', path: '/home/u/broken' }],
      })
      .mockRejectedValueOnce(new Error('503'))
    mount()

    fireEvent.mouseDown(screen.getByRole('button', { name: 'Browse' }))
    fireEvent.click(await screen.findByRole('option', { name: /broken/ }))
    const listingAlert = await screen.findByTestId('pp-listing-error')
    const listingMessage = listingAlert.textContent
    await act(async () => { rejectRecents(timeout) })

    expect(screen.getByTestId('pp-listing-error').textContent).toBe(listingMessage)
    expect(screen.queryByTestId('pp-recent-error')).toBeNull()
  })

  // The recents endpoint answers every read error with 200 `{"dirs": []}` and never a
  // coded body (`api_recent_projects` in chat_handlers.py), so `denied` / `root_missing`
  // are unreachable for it and own no copy. Should a coded refusal ever arrive anyway,
  // the notice degrades to the generic copy rather than a key no locale carries.
  it.each([
    ['access_denied', 403, 'Access denied'],
    ['project_not_found', 404, 'Not found'],
  ])('a recents failure coded %s takes the generic recents copy', async (code, status, text) => {
    vi.mocked(api.recentProjects).mockRejectedValue(new ApiError(status, text, JSON.stringify({ error: text, code })))
    vi.spyOn(api, 'browseDirs').mockResolvedValue({
      path: '/home/u',
      parent: '/home',
      dirs: [{ name: 'workplace', path: '/home/u/workplace' }],
    })
    mount()

    expect(await screen.findByText('workplace')).toBeTruthy()
    expect(screen.getByTestId('pp-recent-error').textContent).toBe(
      "Couldn't load recent projects. Type a project path above, or pick a folder from the list.",
    )
  })

  it('a preserved recents notice does not hide the empty-state of a directory that listed empty', async () => {
    const timeout = Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })
    vi.mocked(api.recentProjects).mockRejectedValue(timeout)
    let landListing!: (d: { path: string; parent: string; dirs: { name: string; path: string }[] }) => void
    vi.spyOn(api, 'browseDirs').mockReturnValue(new Promise(resolve => { landListing = resolve }))
    mount()

    // The recents failure is on screen BEFORE the fallback listing answers...
    expect(await screen.findByTestId('pp-recent-error')).toBeTruthy()
    await act(async () => { landListing({ path: '/home/u/empty', parent: '/home/u', dirs: [] }) })
    // ...and the listing then lands (the field names it), successfully empty, so the
    // list speaks for itself...
    await waitFor(() => expect((screen.getByRole('combobox') as HTMLInputElement).value).toBe('/home/u/empty/'))
    expect(screen.getByText('No subdirectories')).toBeTruthy()
    // ...while the recents notice, about a different read, survived that success.
    expect(screen.getByTestId('pp-recent-error').textContent).toBe(
      'Loading recent projects timed out. Type a project path above, or pick a folder from the list.',
    )
    expect(screen.queryByTestId('pp-listing-error')).toBeNull()
  })
})

describe('ProjectPicker: listing failure vs the empty-state', () => {
  it('a failed listing shows the error notice and NOT the "No subdirectories" empty-state', async () => {
    vi.spyOn(api, 'browseDirs').mockRejectedValue(new Error('503'))
    mount()
    expect(await screen.findByTestId('pp-listing-error')).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toContain("Couldn't load the folder list")
    expect(screen.queryByText('No subdirectories')).toBeNull()
    // The list itself stays mounted, just with nothing to claim about it.
    expect(screen.getByRole('listbox')).toBeTruthy()
    expect(screen.queryAllByRole('option')).toHaveLength(0)
  })

  it('a listing that genuinely came back empty still shows the empty-state, with no notice', async () => {
    vi.spyOn(api, 'browseDirs').mockResolvedValue({ path: '/home/u/empty', parent: '/home/u', dirs: [] })
    mount()
    expect(await screen.findByText('No subdirectories')).toBeTruthy()
    expect(screen.queryByTestId('pp-listing-error')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

// The notice names WHY the listing failed, through the same classifier WorkspacePicker
// and FolderPanel read, so a deadline is not reported as a bad path. Both directory
// variants are keyed: the one that names the failed path beside the listing still on
// screen, and the subject-less one for a first open that never produced a listing.
describe('ProjectPicker: the listing notice names the cause', () => {
  const timeout = () => Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })

  it('a drill that times out names the timeout, the failed path and the path still shown', async () => {
    vi.spyOn(api, 'browseDirs')
      .mockResolvedValueOnce({ path: '/home/u', parent: '/home', dirs: [{ name: 'slow', path: '/home/u/slow' }] })
      .mockRejectedValueOnce(timeout())
    mount()
    fireEvent.click(await screen.findByRole('option', { name: /slow/ }))
    const alert = await screen.findByTestId('pp-listing-error')
    expect(alert.textContent).toBe(
      'Opening /home/u/slow timed out — the list below still shows /home/u/. Try again, or pick a folder from the list.',
    )
    // The raw deadline message never reaches the user.
    expect(alert.textContent).not.toContain('deadline exceeded')
    // The listing that was on screen is still on screen.
    expect(screen.getByRole('option', { name: /slow/ })).toBeTruthy()
  })

  it('a first open that times out says so, without a path to point at', async () => {
    vi.spyOn(api, 'browseDirs').mockRejectedValue(timeout())
    mount()
    const alert = await screen.findByTestId('pp-listing-error')
    // One failed `browseDirs` read is named ONE way whichever picker the user is in: the
    // cause sentence is the shared listing-timeout string WorkspacePicker (and FolderPanel)
    // render, followed by the remedy this surface actually offers. There is no Retry
    // control here, so the copy must not tell the user to "try again" (UX review on #7068).
    expect(alert.textContent).toBe('Folder listing timed out. Type a path above.')
    expect(alert.textContent!.startsWith(i18nT(LISTING_FAILURE_KEYS.timed_out))).toBe(true)
    expect(alert.textContent).not.toMatch(/try again/i)
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })

  it('a refused drill names the refusal instead of asking the user to check the path', async () => {
    vi.spyOn(api, 'browseDirs')
      .mockResolvedValueOnce({ path: '/home/u', parent: '/home', dirs: [{ name: 'locked', path: '/home/u/locked' }] })
      .mockRejectedValueOnce(new ApiError(403, 'Access denied', JSON.stringify({ error: 'Access denied', code: 'access_denied' })))
    mount()
    fireEvent.click(await screen.findByRole('option', { name: /locked/ }))
    expect((await screen.findByTestId('pp-listing-error')).textContent).toBe(
      'No access to /home/u/locked — the list below still shows /home/u/. Type another path, or pick a folder from the list.',
    )
  })

  it('a failure with no recognised cause keeps the generic copy', async () => {
    vi.spyOn(api, 'browseDirs')
      .mockResolvedValueOnce({ path: '/home/u', parent: '/home', dirs: [{ name: 'odd', path: '/home/u/odd' }] })
      .mockRejectedValueOnce(new Error('503'))
    mount()
    fireEvent.click(await screen.findByRole('option', { name: /odd/ }))
    expect((await screen.findByTestId('pp-listing-error')).textContent).toBe(
      'Could not open /home/u/odd — the list below still shows /home/u/. Check the path, or pick a folder from the list.',
    )
  })
})

// Drive-list failures use the same cause classifier as directory failures. A timeout
// must not fall back to the generic "could not show" copy, while another recognised
// cause keeps its own arm. The one retry ProjectPicker has for the drive list is the
// "All drives" button itself, so the timeout arm names it and the refusal arm does not
// (a refusal returns the same answer).
describe('ProjectPicker: the drive-list notice names the cause', () => {
  const openDriveList = async (err: Error) => {
    vi.spyOn(api, 'browseDirs').mockResolvedValue({ path: 'C:\\', parent: '', dirs: [] })
    vi.spyOn(api, 'browseDrives').mockRejectedValue(err)
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'All drives' }))
    return screen.findByTestId('pp-drives-error')
  }

  it('a drive-list timeout names the DRIVE LIST and its retry, not the generic failure', async () => {
    // "Folder listing" was the wrong noun here: the read that timed out is the drive list,
    // and the folder listing below it is exactly what still loaded.
    const timeout = Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })
    const alert = await openDriveList(timeout)
    expect(alert.textContent).toBe(
      'Drive list timed out — the list below still shows C:\\. Type another drive above, such as D:\\, or try All drives again.',
    )
    expect(alert.textContent).not.toContain('Folder listing')
    expect(alert.textContent).not.toContain('Could not show the list of drives')
  })

  it('a refused drive-list read keeps the access-denied arm', async () => {
    const alert = await openDriveList(
      new ApiError(403, 'Access denied', JSON.stringify({ error: 'Access denied', code: 'access_denied' })),
    )
    expect(alert.textContent).toBe(
      'No access to the drive list — the list below still shows C:\\. Type another drive above, such as D:\\.',
    )
  })
})
