// The landing tab is chosen by whichever of the two opening reads settles second, so on a slow
// recents read the choice can arrive long after the user has started working in Browse. These
// tests pin the hold: an engagement with Browse retires this open's claim on the tab, and the
// first test is the control -- unengaged, the late read still decides, so the hold is not a
// blanket disable of the landing logic.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ProjectPicker from '../components/ProjectPicker'
import { api } from '../api/client'
import { canPickFolderNatively, hasNativeFolderPicker, pickFolderNatively } from '../lib/electron'

// The native chooser is offered only where the shell says a pick can work, so it is absent
// unless a test turns it on. Off by default here, leaving every other test's picker unchanged.
vi.mock('../lib/electron', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/electron')>()),
  hasNativeFolderPicker: vi.fn(() => false),
  canPickFolderNatively: vi.fn(() => Promise.resolve(false)),
  pickFolderNatively: vi.fn(() => Promise.resolve({ status: 'cancelled' as const })),
}))

const PATH_FIELD = 'Project directory path'
const FAVORITES_SEARCH = 'Search favorite projects'
const RECENT_SEARCH = 'Search recent projects'

const rect = (top: number, left: number, width = 80, height = 24): DOMRect => ({
  top, left, width, height, bottom: top + height, right: left + width, x: left, y: top, toJSON: () => ({}),
} as DOMRect)

const picker = (open: boolean) => (
  <ProjectPicker open={open} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
)

/** A recents read held open, so the landing tab is still undecided while the test acts. */
const heldRecents = () => {
  let settle: (dirs: string[]) => void = () => {}
  const held = new Promise<{ dirs: string[] }>(res => { settle = dirs => res({ dirs }) })
  vi.spyOn(api, 'recentProjects').mockReturnValue(held)
  return settle
}

/**
 * The mirror of `heldRecents`, for the affordances that live on Recent and Favourites: there the
 * recents read has already answered and it is the FAVOURITES read still out. `null` settles it as
 * a rejection, which is the only way the choice moves OFF Favourites -- a favourites read with
 * rows picks Favourites whatever the user did.
 */
const heldFavorites = () => {
  let settle: (dirs: string[] | null) => void = () => {}
  const held = new Promise<{ dirs: string[] }>((res, rej) => {
    settle = dirs => { if (dirs === null) rej(new Error('read failed')); else res({ dirs }) }
  })
  vi.spyOn(api, 'favoriteProjects').mockReturnValue(held)
  return settle
}

beforeEach(() => {
  Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
  vi.spyOn(api, 'browseDirs').mockResolvedValue({
    path: '/home/u', parent: '/home', dirs: [{ name: 'work', path: '/home/u/work' }],
  })
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('ProjectPicker: a late opening read may not move an engaged user', () => {
  it('still lands on the tab the reads choose when the user has not engaged', async () => {
    vi.spyOn(api, 'favoriteProjects').mockResolvedValue({ dirs: ['/home/u/fav'] })
    const settleRecents = heldRecents()
    renderWithProviders(picker(true))

    settleRecents([])

    expect(await screen.findByLabelText(FAVORITES_SEARCH)).toBeInTheDocument()
  })

  it('keeps the tab the user chose deliberately', async () => {
    vi.spyOn(api, 'favoriteProjects').mockResolvedValue({ dirs: ['/home/u/fav'] })
    const settleRecents = heldRecents()
    renderWithProviders(picker(true))

    fireEvent.mouseDown(screen.getByText('Browse'))
    expect(await screen.findByLabelText(PATH_FIELD)).toBeInTheDocument()

    settleRecents([])

    await waitFor(() => expect(screen.queryByLabelText(FAVORITES_SEARCH)).not.toBeInTheDocument())
    expect(screen.getByLabelText(PATH_FIELD)).toBeInTheDocument()
  })

  it('keeps Browse when the user has typed a path', async () => {
    const { rerender } = await openedOnBrowse()
    vi.spyOn(api, 'favoriteProjects').mockResolvedValue({ dirs: ['/home/u/fav'] })
    const settleRecents = heldRecents()
    rerender(picker(true))

    fireEvent.change(await screen.findByLabelText(PATH_FIELD), { target: { value: '/home/u/w' } })
    settleRecents([])

    await waitFor(() => expect(screen.queryByLabelText(FAVORITES_SEARCH)).not.toBeInTheDocument())
    expect(screen.getByLabelText(PATH_FIELD)).toBeInTheDocument()
  })

  it('keeps Browse when the user drilled into a folder with Enter', async () => {
    const { rerender } = await openedOnBrowse()
    vi.spyOn(api, 'favoriteProjects').mockResolvedValue({ dirs: ['/home/u/fav'] })
    const settleRecents = heldRecents()
    const browseDirs = vi.mocked(api.browseDirs)
    rerender(picker(true))

    const field = await screen.findByLabelText(PATH_FIELD)
    await screen.findByText('work')
    browseDirs.mockClear()
    fireEvent.keyDown(field, { key: 'Enter' })

    // The drill itself, so a keystroke that never reached `browse` cannot pass this test.
    await waitFor(() => expect(browseDirs).toHaveBeenCalledWith('/home/u/work'))
    settleRecents([])

    await waitFor(() => expect(screen.queryByLabelText(FAVORITES_SEARCH)).not.toBeInTheDocument())
    expect(screen.getByLabelText(PATH_FIELD)).toBeInTheDocument()
  })

  it('keeps Browse when the user went up a directory', async () => {
    const { rerender } = await openedOnBrowse()
    vi.spyOn(api, 'favoriteProjects').mockResolvedValue({ dirs: ['/home/u/fav'] })
    const settleRecents = heldRecents()
    rerender(picker(true))

    await screen.findByLabelText(PATH_FIELD)
    fireEvent.click(screen.getByLabelText('Back'))
    settleRecents([])

    await waitFor(() => expect(screen.queryByLabelText(FAVORITES_SEARCH)).not.toBeInTheDocument())
    expect(screen.getByLabelText(PATH_FIELD)).toBeInTheDocument()
  })

  it('keeps Browse when the user scrolled the folder list with the wheel', async () => {
    const { rerender } = await openedOnBrowse()
    vi.spyOn(api, 'favoriteProjects').mockResolvedValue({ dirs: ['/home/u/fav'] })
    const settleRecents = heldRecents()
    rerender(picker(true))

    await screen.findByLabelText(PATH_FIELD)
    fireEvent.wheel(await screen.findByText('work'), { deltaY: 120 })
    settleRecents([])

    await waitFor(() => expect(screen.queryByLabelText(FAVORITES_SEARCH)).not.toBeInTheDocument())
    expect(screen.getByLabelText(PATH_FIELD)).toBeInTheDocument()
  })

  it('keeps Browse when the user arrowed down the folder list', async () => {
    const { rerender } = await openedOnBrowse()
    vi.mocked(api.browseDirs).mockResolvedValue({
      path: '/home/u', parent: '/home',
      dirs: [{ name: 'work', path: '/home/u/work' }, { name: 'notes', path: '/home/u/notes' }],
    })
    vi.spyOn(api, 'favoriteProjects').mockResolvedValue({ dirs: ['/home/u/fav'] })
    const settleRecents = heldRecents()
    rerender(picker(true))

    const field = await screen.findByLabelText(PATH_FIELD)
    await screen.findByText('notes')
    fireEvent.keyDown(field, { key: 'ArrowDown' })

    // The highlight moved, so a keystroke the handler never saw cannot pass this test.
    await waitFor(() => expect(field).toHaveAttribute('aria-activedescendant', 'pp-dir-1'))
    settleRecents([])

    await waitFor(() => expect(screen.queryByLabelText(FAVORITES_SEARCH)).not.toBeInTheDocument())
    expect(screen.getByLabelText(PATH_FIELD)).toBeInTheDocument()
  })

  it('keeps Browse when the user favourited the directory the pane is on', async () => {
    const { rerender } = await openedOnBrowse()
    const addFavorite = vi.spyOn(api, 'addFavoriteProject').mockResolvedValue({ dirs: ['/home/u'] })
    vi.spyOn(api, 'favoriteProjects').mockResolvedValue({ dirs: ['/home/u/fav'] })
    const settleRecents = heldRecents()
    rerender(picker(true))

    await screen.findByLabelText(PATH_FIELD)
    fireEvent.mouseDown(await screen.findByTestId('pp-browse-star'))

    await waitFor(() => expect(addFavorite).toHaveBeenCalledWith('/home/u'))
    settleRecents([])

    await waitFor(() => expect(screen.queryByLabelText(FAVORITES_SEARCH)).not.toBeInTheDocument())
    expect(screen.getByLabelText(PATH_FIELD)).toBeInTheDocument()
  })

  it('keeps Browse when the user asked the recents read to retry', async () => {
    const { rerender } = await openedOnBrowse()
    const recentProjects = vi.spyOn(api, 'recentProjects').mockRejectedValue(new Error('read failed'))
    const settleFavorites = heldFavorites()
    rerender(picker(true))

    await screen.findByLabelText(PATH_FIELD)
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))

    // The re-ask went out, so a click the guard swallowed cannot pass this test.
    await waitFor(() => expect(recentProjects).toHaveBeenCalledTimes(2))
    settleFavorites(['/home/u/fav'])

    await waitFor(() => expect(screen.queryByLabelText(FAVORITES_SEARCH)).not.toBeInTheDocument())
    expect(screen.getByLabelText(PATH_FIELD)).toBeInTheDocument()
  })

  it('keeps Browse when the user asked the folder listing to retry', async () => {
    const { rerender } = await openedOnBrowse()
    const browseDirs = vi.mocked(api.browseDirs).mockRejectedValue(new Error('read failed'))
    vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: [] })
    const settleFavorites = heldFavorites()
    rerender(picker(true))

    await screen.findByLabelText(PATH_FIELD)
    const callsBeforeRetry = browseDirs.mock.calls.length
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))

    await waitFor(() => expect(browseDirs.mock.calls.length).toBeGreaterThan(callsBeforeRetry))
    settleFavorites(['/home/u/fav'])

    await waitFor(() => expect(screen.queryByLabelText(FAVORITES_SEARCH)).not.toBeInTheDocument())
    expect(screen.getByLabelText(PATH_FIELD)).toBeInTheDocument()
  })

  it('keeps Recent when the user searched the recent list', async () => {
    vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: ['/home/u/r1'] })
    const settleFavorites = heldFavorites()
    renderWithProviders(picker(true))

    fireEvent.change(await screen.findByLabelText(RECENT_SEARCH), { target: { value: 'r1' } })
    settleFavorites(['/home/u/fav'])

    await waitFor(() => expect(screen.queryByLabelText(FAVORITES_SEARCH)).not.toBeInTheDocument())
    expect(screen.getByLabelText(RECENT_SEARCH)).toBeInTheDocument()
  })

  it('keeps Favourites when the user searched the favourite list', async () => {
    const { rerender } = await openedOnFavorites()
    vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: ['/home/u/r1'] })
    const settleFavorites = heldFavorites()
    rerender(picker(true))

    fireEvent.change(await screen.findByLabelText(FAVORITES_SEARCH), { target: { value: 'fav' } })
    settleFavorites(null)

    // The read rejected, so the choice really was retaken while the user stood on Favourites.
    await waitFor(() => expect(screen.getByTestId('pp-favorites-error')).toBeInTheDocument())
    expect(document.getElementById('pp-favorite-list')).toBeInTheDocument()
    expect(document.getElementById('pp-recent-list')).toBeNull()
  })

  it('keeps Recent when the user arrowed down the recent list', async () => {
    vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: ['/home/u/r1', '/home/u/r2'] })
    const settleFavorites = heldFavorites()
    renderWithProviders(picker(true))

    const second = (await screen.findByText('/home/u/r2')).closest('[role="option"]') as HTMLElement
    // Neither list pane has an input carrying its arrows: both navigate from the nav hook's
    // own document-capture listener, which is dispatched to directly here.
    fireEvent.keyDown(document, { key: 'ArrowDown' })

    // The highlight moved, so a keystroke the nav never saw cannot pass this test.
    await waitFor(() => expect(second).toHaveAttribute('aria-selected', 'true'))

    // Settled with a path that is also a recent row, so the read has an effect visible on THIS
    // pane -- that row's star turns on. Without it a late read with rows leaves nothing to wait
    // for on the tab the user held, and the assertions below could run before it had landed.
    settleFavorites(['/home/u/r1'])
    await waitFor(() => expect(screen.getByTestId('pp-recent-star-0')).toHaveAttribute('aria-pressed', 'true'))

    expect(screen.getByLabelText(RECENT_SEARCH)).toBeInTheDocument()
    expect(screen.queryByLabelText(FAVORITES_SEARCH)).not.toBeInTheDocument()
  })

  it('keeps Favourites when the user arrowed down the favourite list', async () => {
    const { rerender } = await openedOnFavorites(['/home/u/f1', '/home/u/f2'])
    vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: ['/home/u/r1'] })
    const settleFavorites = heldFavorites()
    rerender(picker(true))

    await screen.findByLabelText(FAVORITES_SEARCH)
    const second = screen.getByText('/home/u/f2').closest('[role="option"]') as HTMLElement
    fireEvent.keyDown(document, { key: 'ArrowDown' })

    await waitFor(() => expect(second).toHaveAttribute('aria-selected', 'true'))
    settleFavorites(null)

    // The read rejected, so the choice really was retaken while the user stood on Favourites.
    await waitFor(() => expect(screen.getByTestId('pp-favorites-error')).toBeInTheDocument())
    expect(document.getElementById('pp-favorite-list')).toBeInTheDocument()
    expect(document.getElementById('pp-recent-list')).toBeNull()
  })

  it('keeps Recent when the user favourited the highlighted recent row with Alt+Enter', async () => {
    vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: ['/home/u/r1', '/home/u/r2'] })
    const settleFavorites = heldFavorites()
    // Left in flight on purpose. A write that answers retires the read still out, and then the
    // read publishes no rows -- so a landed write would remove the very pressure being tested.
    const addFavorite = vi.spyOn(api, 'addFavoriteProject').mockReturnValue(new Promise(() => {}))
    renderWithProviders(picker(true))

    await screen.findByLabelText(RECENT_SEARCH)
    // The nav hook stops propagation inside its own Enter branch, before it calls the row's
    // alternate action, so this key never descends to the picker root.
    fireEvent.keyDown(document, { key: 'Enter', altKey: true })

    // The toggle ran against the highlighted row, so a key the nav never saw cannot pass.
    await waitFor(() => expect(addFavorite).toHaveBeenCalledWith('/home/u/r1'))

    settleFavorites(['/home/u/r1'])
    await waitFor(() => expect(screen.getByTestId('pp-recent-star-0')).toHaveAttribute('aria-pressed', 'true'))

    expect(screen.getByLabelText(RECENT_SEARCH)).toBeInTheDocument()
    expect(screen.queryByLabelText(FAVORITES_SEARCH)).not.toBeInTheDocument()
  })

  it('keeps Favourites when the user un-favourited the highlighted row with Alt+Enter', async () => {
    const { rerender } = await openedOnFavorites(['/home/u/f1', '/home/u/f2'])
    vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: ['/home/u/r1'] })
    const settleFavorites = heldFavorites()
    const removeFavorite = vi.spyOn(api, 'removeFavoriteProject').mockReturnValue(new Promise(() => {}))
    rerender(picker(true))

    await screen.findByLabelText(FAVORITES_SEARCH)
    fireEvent.keyDown(document, { key: 'Enter', altKey: true })

    await waitFor(() => expect(removeFavorite).toHaveBeenCalledWith('/home/u/f1'))
    settleFavorites(null)

    await waitFor(() => expect(screen.getByTestId('pp-favorites-error')).toBeInTheDocument())
    expect(document.getElementById('pp-favorite-list')).toBeInTheDocument()
    expect(document.getElementById('pp-recent-list')).toBeNull()
  })

  it('keeps Recent when the user opened the system folder chooser and cancelled', async () => {
    vi.mocked(hasNativeFolderPicker).mockReturnValue(true)
    vi.mocked(canPickFolderNatively).mockResolvedValue(true)
    vi.mocked(pickFolderNatively).mockResolvedValue({ status: 'cancelled' })
    vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: ['/home/u/r1'] })
    const settleFavorites = heldFavorites()
    renderWithProviders(picker(true))

    await screen.findByLabelText(RECENT_SEARCH)
    fireEvent.click(await screen.findByRole('button', { name: 'Open system folder picker…' }))

    // The dialog really opened, so a click the button swallowed cannot pass this test. A cancel
    // commits nothing, which is the case that leaves the user back on the pane they started on.
    await waitFor(() => expect(pickFolderNatively).toHaveBeenCalled())

    settleFavorites(['/home/u/r1'])
    await waitFor(() => expect(screen.getByTestId('pp-recent-star-0')).toHaveAttribute('aria-pressed', 'true'))

    expect(screen.getByLabelText(RECENT_SEARCH)).toBeInTheDocument()
    expect(screen.queryByLabelText(FAVORITES_SEARCH)).not.toBeInTheDocument()
  })
})

/**
 * Reach Browse the way a returning user does, WITHOUT a tab click: an open where neither list
 * has rows lands on Browse, and the mount outlives the close, so the tab persists into the
 * reopen. A tab click would arm the hold by itself and the test could not attribute it to the
 * affordance under test.
 */
async function openedOnBrowse() {
  vi.spyOn(api, 'favoriteProjects').mockResolvedValue({ dirs: [] })
  vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: [] })
  const rendered = renderWithProviders(picker(true))
  await screen.findByLabelText(PATH_FIELD)
  rendered.rerender(picker(false))
  await waitFor(() => expect(screen.queryByLabelText(PATH_FIELD)).not.toBeInTheDocument())
  vi.restoreAllMocks()
  vi.spyOn(api, 'browseDirs').mockResolvedValue({
    path: '/home/u', parent: '/home', dirs: [{ name: 'work', path: '/home/u/work' }],
  })
  return rendered
}

/**
 * The same trick for Favourites: an open whose favourites read has rows lands there, and the
 * rows outlive the close, so the reopen starts on Favourites with its search box already
 * mounted -- before the new read has answered and could clear them.
 */
async function openedOnFavorites(dirs: string[] = ['/home/u/fav']) {
  vi.spyOn(api, 'favoriteProjects').mockResolvedValue({ dirs })
  vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: [] })
  const rendered = renderWithProviders(picker(true))
  await screen.findByLabelText(FAVORITES_SEARCH)
  rendered.rerender(picker(false))
  await waitFor(() => expect(screen.queryByLabelText(FAVORITES_SEARCH)).not.toBeInTheDocument())
  vi.restoreAllMocks()
  vi.spyOn(api, 'browseDirs').mockResolvedValue({
    path: '/home/u', parent: '/home', dirs: [{ name: 'work', path: '/home/u/work' }],
  })
  return rendered
}
