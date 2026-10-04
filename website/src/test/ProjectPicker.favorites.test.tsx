// The picker's Favourites tab: the list, the star toggle on both list panes and on the
// browsed directory, the landing tab decided from BOTH list reads, and the two failure
// notices (a failed READ of the list, a refused WRITE of one entry).
import { screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ProjectPicker from '../components/ProjectPicker'
import { api } from '../api/client'

const rect = (top: number, left: number, width = 80, height = 24): DOMRect => ({
  top, left, width, height,
  bottom: top + height,
  right: left + width,
  x: left, y: top,
  toJSON: () => ({}),
} as DOMRect)

const open = (onSelect = vi.fn()) => renderWithProviders(
  <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={onSelect} />,
)

beforeEach(() => {
  vi.spyOn(api, 'browseDirs').mockResolvedValue({ path: '/home/u', parent: '/home', dirs: [] })
  vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: ['/home/u/projA', '/home/u/projB'] })
  vi.spyOn(api, 'favoriteProjects').mockResolvedValue({ dirs: ['/home/u/projB'] })
})

afterEach(() => { vi.restoreAllMocks() })

describe('ProjectPicker favourites', () => {
  it('opens on Favourites when there is at least one, and lists them', async () => {
    open()
    expect(await screen.findByRole('listbox', { name: 'Favorite projects' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /projB/ })).toBeInTheDocument()
    // The favourites list is its own read: a recent that is not favourited is not here.
    expect(screen.queryByRole('option', { name: /projA/ })).not.toBeInTheDocument()
  })

  it('falls back to Recent when there are no favourites', async () => {
    vi.mocked(api.favoriteProjects).mockResolvedValue({ dirs: [] })
    open()
    expect(await screen.findByRole('listbox', { name: 'Recent projects' })).toBeInTheDocument()
  })

  it('falls back to Browse when neither list has rows', async () => {
    vi.mocked(api.favoriteProjects).mockResolvedValue({ dirs: [] })
    vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
    open()
    expect(await screen.findByRole('combobox')).toBeInTheDocument()
  })

  it('lands on Favourites even when the favourites read settles LAST', async () => {
    // The landing tab is decided by whichever read settles second, so the order cannot
    // change the answer. Recents resolves immediately; favourites after a tick.
    vi.mocked(api.favoriteProjects).mockImplementation(
      () => new Promise(res => setTimeout(() => res({ dirs: ['/home/u/projB'] }), 10)),
    )
    open()
    expect(await screen.findByRole('listbox', { name: 'Favorite projects' })).toBeInTheDocument()
  })

  it('still leaves Recent reachable, with its rows', async () => {
    open()
    fireEvent.mouseDown(await screen.findByText('Recent'))
    expect(await screen.findByRole('listbox', { name: 'Recent projects' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /projA/ })).toBeInTheDocument()
  })

  it('selecting a favourite commits it and does not write to the list', async () => {
    const onSelect = vi.fn()
    const add = vi.spyOn(api, 'addFavoriteProject')
    open(onSelect)
    fireEvent.mouseDown(await screen.findByRole('option', { name: /projB/ }))
    expect(onSelect).toHaveBeenCalledWith('/home/u/projB')
    expect(add).not.toHaveBeenCalled()
  })

  it('stars an unfavourited recent and renders the list the server returns', async () => {
    const add = vi.spyOn(api, 'addFavoriteProject')
      .mockResolvedValue({ dirs: ['/home/u/projB', '/home/u/projA'] })
    open()
    fireEvent.mouseDown(await screen.findByText('Recent'))
    const star = await screen.findByTestId('pp-recent-star-0')      // projA, not favourited
    expect(star).toHaveAttribute('aria-pressed', 'false')
    fireEvent.mouseDown(star)
    expect(add).toHaveBeenCalledWith('/home/u/projA')
    await waitFor(() => expect(screen.getByTestId('pp-recent-star-0')).toHaveAttribute('aria-pressed', 'true'))
  })

  it('shows an already-favourited recent as pressed, and un-stars it', async () => {
    const remove = vi.spyOn(api, 'removeFavoriteProject').mockResolvedValue({ dirs: [] })
    open()
    fireEvent.mouseDown(await screen.findByText('Recent'))
    const star = await screen.findByTestId('pp-recent-star-1')      // projB, favourited
    expect(star).toHaveAttribute('aria-pressed', 'true')
    fireEvent.mouseDown(star)
    expect(remove).toHaveBeenCalledWith('/home/u/projB')
    await waitFor(() => expect(screen.getByTestId('pp-recent-star-1')).toHaveAttribute('aria-pressed', 'false'))
  })

  it('un-stars from the Favourites pane itself, dropping the row', async () => {
    vi.spyOn(api, 'removeFavoriteProject').mockResolvedValue({ dirs: [] })
    open()
    fireEvent.mouseDown(await screen.findByTestId('pp-favorite-star-0'))
    await waitFor(() => expect(screen.queryByRole('option', { name: /projB/ })).not.toBeInTheDocument())
    expect(screen.getByText(/No favorite projects yet/)).toBeInTheDocument()
  })

  it('a star click does not also commit the row', async () => {
    const onSelect = vi.fn()
    vi.spyOn(api, 'removeFavoriteProject').mockResolvedValue({ dirs: [] })
    open(onSelect)
    fireEvent.mouseDown(await screen.findByTestId('pp-favorite-star-0'))
    await waitFor(() => expect(api.removeFavoriteProject).toHaveBeenCalled())
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('favourites the directory the Browse pane is on', async () => {
    const add = vi.spyOn(api, 'addFavoriteProject').mockResolvedValue({ dirs: ['/home/u'] })
    open()
    fireEvent.mouseDown(await screen.findByText('Browse'))
    fireEvent.mouseDown(await screen.findByTestId('pp-browse-star'))
    expect(add).toHaveBeenCalledWith('/home/u')
  })

  it('tells a failed write without discarding the rows', async () => {
    vi.spyOn(api, 'addFavoriteProject').mockRejectedValue(new Error('Not a directory'))
    open()
    fireEvent.mouseDown(await screen.findByText('Recent'))
    fireEvent.mouseDown(await screen.findByTestId('pp-recent-star-0'))
    expect(await screen.findByTestId('pp-favorite-write-error')).toHaveTextContent(/Not a directory/)
    // The list the server last gave is still on screen -- a refused write changed nothing.
    expect(screen.getByRole('option', { name: /projA/ })).toBeInTheDocument()
    // No hand-off unless the mount opts in: most mounts float over an unsaved draft.
    expect(screen.queryByRole('button', { name: 'Ask the agent' })).toBeNull()
  })

  it('does not let an opening read that answers late overwrite a newer write', async () => {
    let answerRead: (v: { dirs: string[] }) => void = () => {}
    vi.mocked(api.favoriteProjects).mockReturnValue(new Promise(r => { answerRead = r }))
    vi.spyOn(api, 'addFavoriteProject').mockResolvedValue({ dirs: ['/home/u/projA'] })
    open()
    fireEvent.mouseDown(await screen.findByText('Recent'))
    fireEvent.mouseDown(await screen.findByTestId('pp-recent-star-0'))
    await waitFor(() => expect(screen.getByTestId('pp-recent-star-0')).toHaveAttribute('aria-pressed', 'true'))
    // The read started before the write and holds the older list. It still settles the
    // landing tab (Favorites, since it has rows), but the rows shown are the write's.
    await act(async () => { answerRead({ dirs: ['/home/u/projB'] }) })
    const favs = await screen.findByRole('listbox', { name: 'Favorite projects' })
    expect(within(favs).getByRole('option', { name: /projA/ })).toBeInTheDocument()
    expect(within(favs).queryByRole('option', { name: /projB/ })).toBeNull()
  })

  it('offers the agent hand-off on a failed write when the mount opts in', async () => {
    vi.spyOn(api, 'addFavoriteProject').mockRejectedValue(new Error('Not a directory'))
    const onOpenChange = vi.fn()
    renderWithProviders(
      <ProjectPicker open={true} onOpenChange={onOpenChange} anchorRect={rect(100, 50)} onSelect={vi.fn()} errorHandoff />,
    )
    fireEvent.mouseDown(await screen.findByText('Recent'))
    fireEvent.mouseDown(await screen.findByTestId('pp-recent-star-0'))
    const notice = await screen.findByTestId('pp-favorite-write-error')
    fireEvent.click(within(notice).getByRole('button', { name: 'Ask the agent' }))
    // The popover closes itself so it does not float over the chat it hands to.
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('keeps the Browse favourite toggle out of the Back / Select row', async () => {
    open()
    fireEvent.mouseDown(await screen.findByText('Browse'))
    const star = await screen.findByTestId('pp-browse-star')
    const select = screen.getByRole('button', { name: 'Select' })
    expect(star.parentElement).not.toBe(select.parentElement)
    // Labelled, since it no longer sits beside anything that explains it.
    expect(star).toHaveTextContent('Add /home/u to favorites')
  })

  it('tells a failed favourites READ instead of claiming the list is empty', async () => {
    vi.mocked(api.favoriteProjects).mockRejectedValue(new Error('boom'))
    open()
    // A failed favourites read lands the picker on Recent (its list has rows), so the
    // notice is read on the Favourites pane the user goes to.
    fireEvent.mouseDown(await screen.findByText('Favorites'))
    expect(await screen.findByTestId('pp-favorites-error')).toBeInTheDocument()
    expect(screen.queryByText(/No favorite projects yet/)).not.toBeInTheDocument()
  })

  it('filters the favourites list', async () => {
    vi.mocked(api.favoriteProjects).mockResolvedValue({ dirs: ['/home/u/projB', '/work/other'] })
    open()
    fireEvent.change(await screen.findByLabelText('Search favorite projects'), { target: { value: 'other' } })
    expect(screen.getByRole('option', { name: /other/ })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /projB/ })).not.toBeInTheDocument()
  })

  it('toggles the highlighted row with Alt+Enter, the keyboard path to the star', async () => {
    const remove = vi.spyOn(api, 'removeFavoriteProject').mockResolvedValue({ dirs: [] })
    open()
    await screen.findByRole('option', { name: /projB/ })
    fireEvent.keyDown(document, { key: 'Enter', altKey: true })
    expect(remove).toHaveBeenCalledWith('/home/u/projB')
  })

  it('Alt+Enter on the Recent pane stars the highlighted row', async () => {
    const add = vi.spyOn(api, 'addFavoriteProject').mockResolvedValue({ dirs: ['/home/u/projA'] })
    open()
    fireEvent.mouseDown(await screen.findByText('Recent'))
    await screen.findByRole('option', { name: /projA/ })
    fireEvent.keyDown(document, { key: 'Enter', altKey: true })
    expect(add).toHaveBeenCalledWith('/home/u/projA')
  })

  it('plain Enter on a favourite commits it rather than toggling it', async () => {
    const onSelect = vi.fn()
    const remove = vi.spyOn(api, 'removeFavoriteProject')
    open(onSelect)
    await screen.findByRole('option', { name: /projB/ })
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(onSelect).toHaveBeenCalledWith('/home/u/projB')
    expect(remove).not.toHaveBeenCalled()
  })

  it('ignores a second toggle while the first write is still out', async () => {
    // Every write answers with the WHOLE list, so two overlapping writes would race to
    // be the list on screen. The star goes inert rather than queueing.
    let release = (_: { dirs: string[] }) => {}
    const remove = vi.spyOn(api, 'removeFavoriteProject')
      .mockImplementation(() => new Promise(res => { release = res }))
    open()
    const star = await screen.findByTestId('pp-favorite-star-0')
    fireEvent.mouseDown(star)
    await waitFor(() => expect(star).toHaveAttribute('aria-disabled', 'true'))
    fireEvent.mouseDown(star)
    fireEvent.mouseDown(star)
    expect(remove).toHaveBeenCalledTimes(1)
    release({ dirs: [] })
    await waitFor(() => expect(screen.getByText(/No favorite projects yet/)).toBeInTheDocument())
  })

  it('ignores an Alt+Enter toggle while a write is still out', async () => {
    // The keyboard path does NOT consult the star's own inert state, so the guard
    // inside the toggle is what keeps two overlapping writes from racing to be the
    // list on screen. Mutating that guard away leaves this the only failing test.
    let release = (_: { dirs: string[] }) => {}
    const remove = vi.spyOn(api, 'removeFavoriteProject')
      .mockImplementation(() => new Promise(res => { release = res }))
    open()
    await screen.findByRole('option', { name: /projB/ })
    fireEvent.keyDown(document, { key: 'Enter', altKey: true })
    await waitFor(() => expect(remove).toHaveBeenCalledTimes(1))
    fireEvent.keyDown(document, { key: 'Enter', altKey: true })
    fireEvent.keyDown(document, { key: 'Enter', altKey: true })
    expect(remove).toHaveBeenCalledTimes(1)
    release({ dirs: [] })
    await waitFor(() => expect(screen.getByText(/No favorite projects yet/)).toBeInTheDocument())
  })

  it('offers no star on the Browse drive list, which has no path of its own', async () => {
    vi.mocked(api.favoriteProjects).mockResolvedValue({ dirs: [] })
    vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
    vi.spyOn(api, 'browseDrives').mockResolvedValue({ path: '', parent: '', dirs: [{ name: 'D:\\', path: 'D:\\' }] })
    vi.mocked(api.browseDirs).mockResolvedValue({ path: 'C:\\', parent: '', dirs: [] })
    open()
    // Back from a drive root lists the drives; that listing carries no path to favourite.
    fireEvent.click(await screen.findByLabelText('All drives'))
    await waitFor(() => expect(api.browseDrives).toHaveBeenCalled())
    expect(screen.queryByTestId('pp-browse-star')).not.toBeInTheDocument()
  })

  it('a write that answers clears a stale read notice above the rows', async () => {
    // A read that failed and a write that then succeeded cannot both be true of the
    // same list, so the notice must not outlive the rows the write delivered.
    vi.mocked(api.favoriteProjects).mockRejectedValue(new Error('boom'))
    vi.spyOn(api, 'addFavoriteProject').mockResolvedValue({ dirs: ['/home/u/projA'] })
    open()
    fireEvent.mouseDown(await screen.findByText('Favorites'))
    expect(await screen.findByTestId('pp-favorites-error')).toBeInTheDocument()
    fireEvent.mouseDown(screen.getByText('Recent'))
    fireEvent.mouseDown(await screen.findByTestId('pp-recent-star-0'))
    await waitFor(() => expect(api.addFavoriteProject).toHaveBeenCalled())
    fireEvent.mouseDown(screen.getByText('Favorites'))
    expect(screen.queryByTestId('pp-favorites-error')).not.toBeInTheDocument()
    expect(await screen.findByRole('option', { name: /projA/ })).toBeInTheDocument()
  })

  it('says so when the filter matches no favourite', async () => {
    open()
    fireEvent.change(await screen.findByLabelText('Search favorite projects'), { target: { value: 'zzz' } })
    expect(screen.getByText('No matching projects')).toBeInTheDocument()
  })
})
