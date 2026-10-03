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
import {
  __resetErrorJournalForTests,
  __resetNavSeamForTests,
  attachReport,
  consumeChatHandoff,
  installSoftNavigate,
  recentErrors,
  recordError,
} from '../utils/errorReport'

const rect = (top: number, left: number, width = 80, height = 24): DOMRect => ({
  top, left, width, height, bottom: top + height, right: left + width, x: left, y: top, toJSON: () => ({}),
} as DOMRect)

const picker = (open: boolean, errorHandoff = false) => (
  <ProjectPicker open={open} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} errorHandoff={errorHandoff} />
)

function mount(errorHandoff = false) {
  return renderWithProviders(picker(true, errorHandoff))
}

beforeEach(() => {
  Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  sessionStorage.clear()
  installSoftNavigate(() => {})
  // No recent projects -> the picker opens straight on the Browse tab.
  vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: [] })
  vi.spyOn(api, 'favoriteProjects').mockResolvedValue({ dirs: [] })
})

afterEach(() => {
  __resetNavSeamForTests()
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

  it('states the failure on the Recent tab itself, never a false "No recent projects"', async () => {
    // The recents read rejects and the picker falls back to Browse -- but the Recent tab button
    // stays mounted, and clicking it rendered the EMPTY state, claiming a read that never landed
    // came back empty. The Recent pane needs its own record of the failure, with copy that fits
    // that pane: it has no path field and no folder list, so the Browse notice's remedy is false
    // there.
    const timeout = Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })
    vi.mocked(api.recentProjects).mockRejectedValue(timeout)
    vi.spyOn(api, 'browseDirs').mockResolvedValue({ path: '/home/u', parent: '/home', dirs: [] })
    mount()
    await screen.findByTestId('pp-recent-error')

    fireEvent.mouseDown(screen.getByRole('button', { name: 'Recent' }))

    const alert = await screen.findByTestId('pp-recent-tab-error')
    expect(alert.textContent).toBe('Loading recent projects timed out. Pick a folder from Browse.')
    expect(screen.queryByText('No recent projects')).toBeNull()
    // Same hand-off contract as the Browse notice: off unless the mount opts in.
    expect(screen.queryByRole('button', { name: 'Ask the agent' })).toBeNull()
  })

  it('names the generic recents failure on the Recent tab too, with the hand-off when opted in', async () => {
    recordError({ source: 'api', message: 'boom', status: 500, endpoint: '/api/recent-projects' })
    vi.mocked(api.recentProjects).mockRejectedValue(new ApiError(500, 'boom'))
    vi.spyOn(api, 'browseDirs').mockResolvedValue({ path: '/home/u', parent: '/home', dirs: [] })
    mount(true)
    await screen.findByTestId('pp-recent-error')

    fireEvent.mouseDown(screen.getByRole('button', { name: 'Recent' }))

    const alert = await screen.findByTestId('pp-recent-tab-error')
    expect(alert.textContent).toContain("Couldn't load recent projects. Pick a folder from Browse.")
    expect(screen.queryByText('No recent projects')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Ask the agent' }))
    const prompt = consumeChatHandoff() ?? ''
    expect(prompt).toContain('/api/recent-projects')
    expect(prompt).toContain('HTTP 500')
  })

  it('keeps the recents failure on the Recent tab when a directory failure outranks it on Browse', async () => {
    // Browse tells a directory failure before the recents one, so its notice says nothing
    // about the recents read. The Recent tab must still state its own read's failure.
    const browseTimeout = Object.assign(new Error('browse deadline exceeded'), { name: 'TimeoutError' })
    const recentsTimeout = Object.assign(new Error('recents deadline exceeded'), { name: 'TimeoutError' })
    vi.spyOn(api, 'browseDirs').mockRejectedValue(browseTimeout)
    vi.mocked(api.recentProjects).mockRejectedValue(recentsTimeout)
    mount()

    expect(await screen.findByTestId('pp-listing-error')).toBeTruthy()
    expect(screen.queryByTestId('pp-recent-error')).toBeNull()

    fireEvent.mouseDown(screen.getByRole('button', { name: 'Recent' }))

    const alert = await screen.findByTestId('pp-recent-tab-error')
    expect(alert.textContent).toBe('Loading recent projects timed out. Pick a folder from Browse.')
    expect(screen.queryByText('No recent projects')).toBeNull()
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

  it('a reopen keeps the last open\'s rows on screen while its recents read is still pending', async () => {
    // ChatPage keeps the picker mounted and only toggles `open`, and the Recent tab is the one
    // persisted after a load. Clearing the rows before the reopen's read answers painted
    // "No recent projects" for the whole round trip -- the full browse bound on a wedged
    // gateway -- and the search box, gated on rows, remounted and took focus when they landed.
    let landFresh!: (d: { dirs: string[] }) => void
    const pending = new Promise<{ dirs: string[] }>(resolve => { landFresh = resolve })
    vi.mocked(api.recentProjects)
      .mockResolvedValueOnce({ dirs: ['/home/u/kept'] })
      .mockReturnValueOnce(pending)
    vi.spyOn(api, 'browseDirs').mockResolvedValue({ path: '/home/u', parent: '/home', dirs: [] })

    const view = mount()
    expect(await screen.findByText('kept')).toBeTruthy()
    view.rerender(picker(false))
    view.rerender(picker(true))
    await waitFor(() => expect(api.recentProjects).toHaveBeenCalledTimes(2))

    // The pending window: nothing claims an empty read, the old rows are still listed and
    // their search box is already mounted.
    expect(screen.queryByText('No recent projects')).toBeNull()
    expect(screen.getByText('kept')).toBeTruthy()
    const search = screen.getByLabelText('Search recent projects')
    expect(screen.queryByTestId('pp-recent-tab-error')).toBeNull()

    await act(async () => { landFresh({ dirs: ['/home/u/fresh'] }) })

    // The fresh rows replace the old ones, under the SAME search box: it did not remount
    // (a remount is what re-fired its autoFocus and stole the caret).
    expect(await screen.findByText('fresh')).toBeTruthy()
    expect(screen.queryByText('kept')).toBeNull()
    expect(screen.getByLabelText('Search recent projects')).toBe(search)
    expect(screen.queryByText('No recent projects')).toBeNull()
  })

  it('a reopen whose recents read rejects shows none of the previous open\'s rows under the notice', async () => {
    // ChatPage keeps the picker mounted and only toggles `open`, so the rows the last open
    // loaded survive the close in state. When the reopen's read rejects, the Recent pane
    // must state that failure alone: the old rows and their search box rendered under it
    // would claim the read landed.
    const timeout = Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })
    vi.mocked(api.recentProjects)
      .mockResolvedValueOnce({ dirs: ['/home/u/stale'] })
      .mockRejectedValueOnce(timeout)
    vi.spyOn(api, 'browseDirs').mockResolvedValue({ path: '/home/u', parent: '/home', dirs: [] })

    const view = mount()
    expect(await screen.findByText('stale')).toBeTruthy()
    view.rerender(picker(false))
    view.rerender(picker(true))

    await screen.findByTestId('pp-recent-error')
    fireEvent.mouseDown(screen.getByRole('button', { name: 'Recent' }))

    expect(await screen.findByTestId('pp-recent-tab-error')).toBeTruthy()
    expect(screen.queryByText('stale')).toBeNull()
    expect(screen.queryByLabelText('Search recent projects')).toBeNull()
    expect(screen.queryByText('No recent projects')).toBeNull()
  })

  it('keeps the opening directory failure when both opening reads time out', async () => {
    const browseTimeout = Object.assign(new Error('browse deadline exceeded'), { name: 'TimeoutError' })
    const recentsTimeout = Object.assign(new Error('recents deadline exceeded'), { name: 'TimeoutError' })
    recordError({
      source: 'api',
      message: browseTimeout.message,
      endpoint: '/api/files/list?path=/expected/root',
      detail: 'Requested path: /expected/root',
    })
    recordError({
      source: 'api',
      message: recentsTimeout.message,
      endpoint: '/api/recent-projects',
    })
    let rejectBrowse!: (reason?: unknown) => void
    let rejectRecents!: (reason?: unknown) => void
    vi.spyOn(api, 'browseDirs').mockReturnValue(
      new Promise<{ path: string; parent: string; dirs: { name: string; path: string }[] }>((_resolve, reject) => { rejectBrowse = reject }),
    )
    vi.mocked(api.recentProjects).mockReturnValue(
      new Promise<{ dirs: string[] }>((_resolve, reject) => { rejectRecents = reject }),
    )
    mount(true)

    await act(async () => { rejectBrowse(browseTimeout) })
    await act(async () => { rejectRecents(recentsTimeout) })

    expect(await screen.findByTestId('pp-listing-error')).toBeTruthy()
    expect(screen.getByText('Folder listing timed out. Type a path above.')).toBeTruthy()
    expect(screen.queryByTestId('pp-recent-error')).toBeNull()
    expect(screen.queryByText('No subdirectories')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Ask the agent' }))
    const prompt = consumeChatHandoff() ?? ''
    expect(prompt).toContain('/api/files/list?path=/expected/root')
    expect(prompt).toContain('Requested path: /expected/root')
    expect(prompt).not.toContain('/api/recent-projects')
  })

  it('lets the opening directory failure replace a recents failure that lands first', async () => {
    const browseTimeout = Object.assign(new Error('browse deadline exceeded'), { name: 'TimeoutError' })
    const recentsTimeout = Object.assign(new Error('recents deadline exceeded'), { name: 'TimeoutError' })
    let rejectBrowse!: (reason?: unknown) => void
    let rejectRecents!: (reason?: unknown) => void
    vi.spyOn(api, 'browseDirs').mockReturnValue(
      new Promise<{ path: string; parent: string; dirs: { name: string; path: string }[] }>((_resolve, reject) => { rejectBrowse = reject }),
    )
    vi.mocked(api.recentProjects).mockReturnValue(
      new Promise<{ dirs: string[] }>((_resolve, reject) => { rejectRecents = reject }),
    )
    mount()

    await act(async () => { rejectRecents(recentsTimeout) })
    expect(await screen.findByTestId('pp-recent-error')).toBeTruthy()

    await act(async () => { rejectBrowse(browseTimeout) })

    expect((await screen.findByTestId('pp-listing-error')).textContent).toBe(
      'Folder listing timed out. Type a path above.',
    )
    expect(screen.queryByTestId('pp-recent-error')).toBeNull()
    expect(screen.queryByText('No subdirectories')).toBeNull()
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

// WHICH failure hides the empty-state: the failure of the listing on screen, not any listing
// failure. A directory that listed successfully empty keeps "No subdirectories" when a read of
// something ELSE then fails -- the drive list, or a different typed path -- because the notice
// for those says the list below still shows that directory, and a blank region under it said
// nothing at all. It stays hidden when the failed read IS the shown listing: the opening read
// of a reopen (recorded with no path) over the last open's rows, and a typed drill before any
// listing landed (there is no listing to be empty).
describe('ProjectPicker: which failure hides the empty-state', () => {
  const timeout = () => Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })

  it('a failed drive list does not hide the empty-state of the directory still shown', async () => {
    vi.spyOn(api, 'browseDirs').mockResolvedValue({ path: 'C:\\', parent: '', dirs: [] })
    vi.spyOn(api, 'browseDrives').mockRejectedValue(timeout())
    mount()
    // "No subdirectories" also paints at mount, before the opening read lands, so it is no
    // proof the listing is on screen; the All drives control is, gated on the landed root.
    expect(await screen.findByRole('button', { name: 'All drives' })).toBeTruthy()
    expect(screen.getByText('No subdirectories')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'All drives' }))

    const alert = await screen.findByTestId('pp-drives-error')
    expect(alert.textContent).toContain('the list below still shows C:\\')
    expect(screen.getByText('No subdirectories')).toBeTruthy()
    expect(screen.queryByTestId('pp-listing-error')).toBeNull()
  })

  it('a different typed path that fails does not hide the empty-state of the directory still shown', async () => {
    vi.spyOn(api, 'browseDirs')
      .mockResolvedValueOnce({ path: '/home/u/empty', parent: '/home/u', dirs: [] })
      .mockRejectedValueOnce(timeout())
    mount()
    // Wait for the listing to land (the field names it) before typing, or the typed drill
    // would be the read the mock rejects while the opening one is still in flight.
    await waitFor(() => expect((screen.getByRole('combobox') as HTMLInputElement).value).toBe('/home/u/empty/'))
    expect(screen.getByText('No subdirectories')).toBeTruthy()

    fireEvent.change(screen.getByRole('combobox'), { target: { value: '/home/u/other/' } })
    await waitFor(() => expect(api.browseDirs).toHaveBeenCalledWith('/home/u/other'))

    const alert = await screen.findByTestId('pp-listing-error')
    expect(alert.textContent).toBe(
      'Opening /home/u/other timed out — the list below still shows /home/u/empty/. Open it again, or pick a folder from the list.',
    )
    expect(screen.getByText('No subdirectories')).toBeTruthy()
  })

  it('a reopen whose opening read fails hides the empty-state of the last open\'s empty rows', async () => {
    // The opposite failure mode, guarded: the opening read is `browse()` with no path, so its
    // record does not name the shown listing by string -- but it IS that listing's re-read, and
    // the notice above it is the no-path copy, which "No subdirectories" would contradict.
    vi.spyOn(api, 'browseDirs')
      .mockResolvedValueOnce({ path: '/home/u/empty', parent: '/home/u', dirs: [] })
      .mockRejectedValueOnce(timeout())
    const view = mount()
    // Wait for the first read to land (the field names it) before closing, or the reopen's
    // read would be the one the mock rejects while the first is still in flight.
    await waitFor(() => expect((screen.getByRole('combobox') as HTMLInputElement).value).toBe('/home/u/empty/'))
    expect(screen.getByText('No subdirectories')).toBeTruthy()

    view.rerender(picker(false))
    view.rerender(picker(true))

    expect((await screen.findByTestId('pp-listing-error')).textContent).toBe('Folder listing timed out. Type a path above.')
    expect(screen.queryByText('No subdirectories')).toBeNull()
  })

  it('a typed drill that fails before any listing landed claims no empty directory', async () => {
    // The mirror guard: the failed path IS known but nothing was ever read, so there is no
    // listing for the empty-state to describe -- and the notice names the path alone.
    vi.spyOn(api, 'browseDirs').mockRejectedValue(timeout())
    mount()
    await screen.findByTestId('pp-listing-error')

    fireEvent.change(screen.getByRole('combobox'), { target: { value: '/home/u/typed/' } })
    await waitFor(() => expect(api.browseDirs).toHaveBeenCalledWith('/home/u/typed'))

    expect(screen.getByTestId('pp-listing-error').textContent).toBe('Opening /home/u/typed timed out. Type a path above.')
    expect(screen.queryByText('No subdirectories')).toBeNull()
  })
})

// The notice names WHY the listing failed, through the same classifier WorkspacePicker
// and FolderPanel read, so a deadline is not reported as a bad path. Three directory
// variants are keyed: the one that names the failed path beside the listing still on
// screen, the one that names the failed path alone when no listing is on screen, and the
// subject-less one for an opening read that had no path of its own.
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
      'Opening /home/u/slow timed out — the list below still shows /home/u/. Open it again, or pick a folder from the list.',
    )
    // The raw deadline message never reaches the user.
    expect(alert.textContent).not.toContain('deadline exceeded')
    // The copy names the gesture the user has (open the folder again), never "try again": the
    // re-ask is the Retry control beside the notice, which the copy need not name.
    expect(alert.textContent).not.toMatch(/try again/i)
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
    // The listing that was on screen is still on screen.
    expect(screen.getByRole('option', { name: /slow/ })).toBeTruthy()
  })

  it('a first open that times out says so, without a path to point at', async () => {
    vi.spyOn(api, 'browseDirs').mockRejectedValue(timeout())
    mount()
    const alert = await screen.findByTestId('pp-listing-error')
    // One failed `browseDirs` read is named ONE way whichever picker the user is in: the
    // cause sentence is the shared listing-timeout string WorkspacePicker (and FolderPanel)
    // render, followed by the remedy this surface offers. The re-ask is the Retry control
    // beside the notice, so the copy itself does not tell the user to "try again".
    expect(alert.textContent).toBe('Folder listing timed out. Type a path above.')
    expect(alert.textContent!.startsWith(i18nT(LISTING_FAILURE_KEYS.timed_out))).toBe(true)
    expect(alert.textContent).not.toMatch(/try again/i)
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
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

  // The path-naming arm interpolates two values: the path that failed and the one still
  // shown. Copy that names a value the picker does not have is never selected: with no failed
  // path there is nothing to name, and with a failed path but no shown path (`browsePath` is '':
  // nothing has landed yet, or the drive list is up and its rows carry no path) the notice names
  // the failed path alone. The cases below each leave exactly one of them empty.
  it('a reopen whose opening listing times out never names an empty folder beside the old listing', async () => {
    // ChatPage keeps the picker mounted and only toggles `open`, so the last open's
    // `browsePath` survives the close. The opening read is `browse()` with no path, and
    // its rejection records `path: ''`; with the old listing still in state the notice
    // used to read "Opening  timed out — the list below still shows /home/u/." -- a blank
    // folder name and a doubled space, in every locale.
    vi.spyOn(api, 'browseDirs')
      .mockResolvedValueOnce({ path: '/home/u', parent: '/home', dirs: [{ name: 'workplace', path: '/home/u/workplace' }] })
      .mockRejectedValueOnce(timeout())
    const view = mount()
    expect(await screen.findByText('workplace')).toBeTruthy()

    view.rerender(picker(false))
    view.rerender(picker(true))

    const alert = await screen.findByTestId('pp-listing-error')
    expect(alert.textContent).toBe('Folder listing timed out. Type a path above.')
    expect(alert.textContent).not.toContain('still shows')
    expect(alert.textContent).not.toMatch(/Opening\s+timed out/)
    expect(alert.textContent).not.toContain('  ')
  })

  it('a typed drill that times out before any listing landed names the failed path, and no listing', async () => {
    // The mirror: `browsePath` is '' (the opening read rejected, so nothing is on screen)
    // while the failed path IS known (the auto-drill passes the typed target). The notice
    // names that path -- it is what the user asked for -- but has no listing to point at,
    // so it must not read "the list below still shows " with nothing after it.
    vi.spyOn(api, 'browseDirs').mockRejectedValue(timeout())
    mount()
    expect((await screen.findByTestId('pp-listing-error')).textContent).toBe('Folder listing timed out. Type a path above.')

    // A trailing separator on a path that differs from the (empty) shown one auto-drills
    // after the 250ms debounce; that read rejects too, recording `/home/u/typed` as the failure.
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '/home/u/typed/' } })
    await waitFor(() => expect(api.browseDirs).toHaveBeenCalledWith('/home/u/typed'))

    const alert = await screen.findByTestId('pp-listing-error')
    expect(alert.textContent).toBe('Opening /home/u/typed timed out. Type a path above.')
    expect(alert.textContent).not.toContain('still shows')
  })

  it('a timed-out click on a drive in the Windows drive list names that drive', async () => {
    // On the drive list `browsePath` and the input are both '' (browseDrives clears them), so
    // the drive rows on screen carry no path for the notice to point at -- but the click asked
    // for `D:\`, and a notice that said only "Couldn't load the folder list" hid which drive it
    // was talking about.
    vi.spyOn(api, 'browseDrives').mockResolvedValue({ path: '', parent: '', dirs: [{ name: 'C:\\', path: 'C:\\' }, { name: 'D:\\', path: 'D:\\' }] })
    const browseSpy = vi.spyOn(api, 'browseDirs').mockResolvedValue({ path: 'C:\\', parent: '', dirs: [] })
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'All drives' }))
    await screen.findByRole('option', { name: /D:\\/ })
    expect((screen.getByRole('combobox') as HTMLInputElement).value).toBe('')

    browseSpy.mockRejectedValue(timeout())
    fireEvent.click(screen.getByRole('option', { name: /D:\\/ }))
    await waitFor(() => expect(api.browseDirs).toHaveBeenLastCalledWith('D:\\'))

    const alert = await screen.findByTestId('pp-listing-error')
    expect(alert.textContent).toBe('Opening D:\\ timed out. Type a path above.')
    expect(alert.textContent).not.toContain('still shows')
  })
})

// Drive-list failures use the same cause classifier as directory failures. A timeout
// must not fall back to the generic "could not show" copy, while another recognised
// cause keeps its own arm. The drive-list copy names "All drives" as the way to re-ask (the
// Retry beside the notice does the same for a cause re-asking can fix), so the timeout arm
// names it and the refusal arm does not (a refusal returns the same answer).
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

/**
 * Every bounded picker read journals the one contract line (`deadline exceeded`), and the journal
 * resolves by exact message, newest first. So the directory notice's hand-off used to name whichever
 * bounded read timed out LAST -- another surface's endpoint for this picker's failure. The transport
 * pins each report to its own rejection; the picker reads that before it consults the journal.
 */
describe('ProjectPicker: the listing notice hands off ITS OWN deadline report', () => {
  it('names /api/browse-dirs when an unrelated deadline is the newer entry under the same message', async () => {
    // The picker captures the report the moment its read rejects, so the aliasing case is a
    // DIFFERENT bounded read whose deadline landed first: its entry is the newest match when this
    // one is looked up. Two reads on one wedged gateway make that ordering a coin toss.
    const browseTimeout = Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })
    // What `withJournaledDeadline` does for the real read: journal, then pin the entry to the error.
    attachReport(browseTimeout, recordError({
      source: 'api',
      message: browseTimeout.message,
      code: 'timeout',
      endpoint: '/api/browse-dirs',
    }))
    let rejectBrowse!: (reason?: unknown) => void
    vi.spyOn(api, 'browseDirs').mockReturnValue(
      new Promise<{ path: string; parent: string; dirs: { name: string; path: string }[] }>((_resolve, reject) => { rejectBrowse = reject }),
    )
    mount(true)

    // Another bounded read's deadline is journaled AFTER this one, so it is the newest match.
    recordError({ source: 'api', message: 'deadline exceeded', code: 'timeout', endpoint: '/api/project/tree' })
    expect(recentErrors()[0].endpoint).toBe('/api/project/tree')
    await act(async () => { rejectBrowse(browseTimeout) })
    expect(await screen.findByTestId('pp-listing-error')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Ask the agent' }))
    const prompt = consumeChatHandoff() ?? ''
    expect(prompt).toContain('/api/browse-dirs')
    expect(prompt).not.toContain('/api/project/tree')
  })
})

// The same Retry WorkspacePicker offers beside a failed listing, so both pickers recover one way.
// It re-asks EXACTLY the read that failed -- the failed path, the pathless opening read, or the
// drive list -- and only for a cause re-asking can fix. While the re-ask is out it is an inert
// "Retrying…" that keeps focus, so the wait is visible and a second press restarts nothing.
describe('ProjectPicker: Retry beside a failed listing', () => {
  const timeout = () => Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })
  const home = { path: '/home/u', parent: '/home', dirs: [{ name: 'slow', path: '/home/u/slow' }] }

  it('re-lists the path a timed-out drill asked for, and clears the notice when it lands', async () => {
    const browse = vi.spyOn(api, 'browseDirs')
      .mockResolvedValueOnce(home)
      .mockRejectedValueOnce(timeout())
      .mockResolvedValueOnce({ path: '/home/u/slow', parent: '/home/u', dirs: [{ name: 'deep', path: '/home/u/slow/deep' }] })
    mount()
    fireEvent.click(await screen.findByRole('option', { name: /slow/ }))
    await screen.findByTestId('pp-listing-error')

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    expect(await screen.findByRole('option', { name: /deep/ })).toBeTruthy()
    expect(browse).toHaveBeenCalledTimes(3)
    expect(browse).toHaveBeenLastCalledWith('/home/u/slow')
    expect(screen.queryByTestId('pp-listing-error')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    expect((screen.getByRole('combobox') as HTMLInputElement).value).toBe('/home/u/slow/')
  })

  it('re-runs the pathless opening read when that is the read that failed', async () => {
    const browse = vi.spyOn(api, 'browseDirs')
      .mockRejectedValueOnce(timeout())
      .mockResolvedValueOnce(home)
    mount()
    await screen.findByTestId('pp-listing-error')

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    expect(await screen.findByRole('option', { name: /slow/ })).toBeTruthy()
    expect(browse.mock.calls).toEqual([[undefined], [undefined]])
    expect(screen.queryByTestId('pp-listing-error')).toBeNull()
  })

  it('re-lists the drives when the drive list is the read that failed', async () => {
    vi.spyOn(api, 'browseDirs').mockResolvedValue({ path: 'C:\\', parent: '', dirs: [] })
    const drives = vi.spyOn(api, 'browseDrives')
      .mockRejectedValueOnce(timeout())
      .mockResolvedValueOnce({ path: '', parent: '', dirs: [{ name: 'D:\\', path: 'D:\\' }] })
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'All drives' }))
    await screen.findByTestId('pp-drives-error')

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    expect(await screen.findByRole('option', { name: /D:\\/ })).toBeTruthy()
    expect(drives).toHaveBeenCalledTimes(2)
    expect(screen.queryByTestId('pp-drives-error')).toBeNull()
  })

  it.each([
    ['a refused', new ApiError(403, 'Access denied', JSON.stringify({ error: 'Access denied', code: 'access_denied' }))],
    ['a missing', new ApiError(404, 'nope', JSON.stringify({ error: 'nope', code: 'project_not_found' }))],
  ])('offers no Retry beside %s listing, which re-asking cannot fix', async (_label, err) => {
    vi.spyOn(api, 'browseDirs').mockResolvedValueOnce(home).mockRejectedValueOnce(err)
    mount()
    fireEvent.click(await screen.findByRole('option', { name: /slow/ }))
    await screen.findByTestId('pp-listing-error')
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retrying…' })).toBeNull()
  })

  it('is an inert, focused "Retrying…" while its re-ask is in flight, and a second press sends nothing', async () => {
    let land!: (v: typeof home) => void
    const browse = vi.spyOn(api, 'browseDirs')
      .mockResolvedValueOnce(home)
      .mockRejectedValueOnce(timeout())
      .mockReturnValueOnce(new Promise(resolve => { land = resolve }))
    mount()
    fireEvent.click(await screen.findByRole('option', { name: /slow/ }))
    await screen.findByTestId('pp-listing-error')

    const retry = screen.getByRole('button', { name: 'Retry' })
    retry.focus()
    fireEvent.click(retry)

    expect(await screen.findByRole('button', { name: 'Retrying…' })).toBe(retry)
    expect(retry).toHaveAttribute('aria-disabled', 'true')
    expect(retry).not.toBeDisabled()
    expect(document.activeElement).toBe(retry)
    fireEvent.click(retry)
    expect(browse).toHaveBeenCalledTimes(3)

    await act(async () => { land({ path: '/home/u/slow', parent: '/home/u', dirs: [] }) })
    expect(screen.queryByTestId('pp-listing-error')).toBeNull()
    expect(browse).toHaveBeenCalledTimes(3)
  })

  it('never re-asks a path the user has typed over: a keystroke drops the notice, its Retry and the re-ask', async () => {
    let land!: (v: typeof home) => void
    vi.spyOn(api, 'browseDirs')
      .mockResolvedValueOnce(home)
      .mockRejectedValueOnce(timeout())
      .mockReturnValueOnce(new Promise(resolve => { land = resolve }))
    mount()
    fireEvent.click(await screen.findByRole('option', { name: /slow/ }))
    await screen.findByTestId('pp-listing-error')
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findByRole('button', { name: 'Retrying…' })

    fireEvent.change(screen.getByRole('combobox'), { target: { value: '/home/u/other' } })
    expect(screen.queryByTestId('pp-listing-error')).toBeNull()
    expect(screen.queryByRole('button', { name: /^Retry/ })).toBeNull()

    // The retired re-ask landing late neither lists nor re-seeds the field over what was typed.
    await act(async () => { land({ path: '/home/u/slow', parent: '/home/u', dirs: [{ name: 'deep', path: '/home/u/slow/deep' }] }) })
    expect(screen.queryByRole('option', { name: /deep/ })).toBeNull()
    expect((screen.getByRole('combobox') as HTMLInputElement).value).toBe('/home/u/other')
  })
})
