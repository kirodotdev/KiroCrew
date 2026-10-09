// The recents read is the one list read in this picker with no re-ask of its own: a user
// whose recents fail can otherwise only get the list back by closing and reopening. The
// Retry beside the Browse notice is that re-ask, and it is deliberately the ONLY placement
// (see `noticeRetry` in ProjectPicker.tsx) -- the Recent pane's nav hook owns Enter and Tab
// at document capture while its list is empty, which is exactly when the read has failed.
//
// The landing tab is the second property pinned here. `settle` picks a tab once both list
// reads answer, so a read that answers late would otherwise move a user who has already
// started working in Browse.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ProjectPicker from '../components/ProjectPicker'
import { api } from '../api/client'

const BROWSE_NOTICE = "Couldn't load recent projects. Type a project path above, or pick a folder from the list."
const RECENT_TAB_NOTICE = "Couldn't load recent projects. Pick a folder from Browse."
const PATH_FIELD = '/path/to/project'

const rect = (top: number, left: number, width = 80, height = 24): DOMRect => ({
  top, left, width, height, bottom: top + height, right: left + width, x: left, y: top, toJSON: () => ({}),
} as DOMRect)

const mount = () => renderWithProviders(
  <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
)

beforeEach(() => {
  Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
  vi.spyOn(api, 'favoriteProjects').mockResolvedValue({ dirs: [] })
  vi.spyOn(api, 'browseDirs').mockResolvedValue({ path: '/home/u', parent: '/home', dirs: [{ name: 'work', path: '/home/u/work' }] })
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('ProjectPicker: re-asking a failed recents read', () => {
  it('re-reads the list from the Browse notice and drops the notice when it lands', async () => {
    const recents = vi.spyOn(api, 'recentProjects').mockRejectedValue(new Error('gateway down'))
    mount()
    expect(await screen.findByText(BROWSE_NOTICE)).toBeInTheDocument()

    recents.mockResolvedValue({ dirs: ['/home/u/projA'] })
    fireEvent.click(screen.getByText('Retry'))

    await waitFor(() => expect(screen.queryByText(BROWSE_NOTICE)).not.toBeInTheDocument())
    expect(recents).toHaveBeenCalledTimes(2)
  })

  it('keeps the notice and returns the control to Retry when the re-ask fails again', async () => {
    const recents = vi.spyOn(api, 'recentProjects').mockRejectedValue(new Error('gateway down'))
    mount()
    await screen.findByText(BROWSE_NOTICE)

    fireEvent.click(screen.getByText('Retry'))

    await waitFor(() => expect(recents).toHaveBeenCalledTimes(2))
    expect(screen.getByText(BROWSE_NOTICE)).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('Retry')).not.toHaveAttribute('aria-disabled'))
  })

  it('is an inert "Retrying…" while the re-ask is out, and a second press sends nothing', async () => {
    const recents = vi.spyOn(api, 'recentProjects').mockRejectedValue(new Error('gateway down'))
    mount()
    await screen.findByText(BROWSE_NOTICE)

    let land: (v: { dirs: string[] }) => void = () => {}
    recents.mockReturnValue(new Promise<{ dirs: string[] }>(r => { land = r }))
    fireEvent.click(screen.getByText('Retry'))

    const busy = await screen.findByText('Retrying…')
    expect(busy).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(busy)
    expect(recents).toHaveBeenCalledTimes(2)

    await act(async () => { land({ dirs: ['/home/u/projA'] }) })
    await waitFor(() => expect(screen.queryByText(BROWSE_NOTICE)).not.toBeInTheDocument())
  })

  it('offers no Retry on the Recent pane, whose own copy sends the user to Browse', async () => {
    // The nav hook claims Enter and Tab at document capture while the list is empty, so a
    // control rendered here could be reached by mouse only. Asserting its ABSENCE is what
    // keeps a later edit from reintroducing an unreachable one.
    vi.spyOn(api, 'recentProjects').mockRejectedValue(new Error('gateway down'))
    mount()
    await screen.findByText(BROWSE_NOTICE)

    fireEvent.mouseDown(screen.getByText('Recent'))

    expect(await screen.findByText(RECENT_TAB_NOTICE)).toBeInTheDocument()
    expect(screen.queryByText('Retry')).not.toBeInTheDocument()
    expect(screen.queryByText('Retrying…')).not.toBeInTheDocument()
  })
})

describe('ProjectPicker: a late list read cannot move the tab the user is working in', () => {
  const picker = (open: boolean) => (
    <ProjectPicker open={open} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
  )

  // Reach Browse WITHOUT a tab click, so these tests can only pass through the affordance
  // each one exercises: an install with neither list lands on Browse, and the mount outlives
  // the close, so the reopen starts there. A tab choice arms the same guard, which is why
  // clicking Browse here would make every assertion below vacuous.
  async function reopenOnBrowse() {
    vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: [] })
    const view = renderWithProviders(picker(true))
    expect(await screen.findByPlaceholderText(PATH_FIELD)).toBeInTheDocument()
    view.rerender(picker(false))

    let land: (v: { dirs: string[] }) => void = () => {}
    vi.mocked(api.recentProjects).mockReturnValue(new Promise<{ dirs: string[] }>(r => { land = r }))
    vi.mocked(api.favoriteProjects).mockResolvedValue({ dirs: ['/home/u/fav'] })
    view.rerender(picker(true))
    await screen.findByPlaceholderText(PATH_FIELD)
    return async () => { await act(async () => { land({ dirs: [] }) }) }
  }

  it('moves to the list it found when the user has not touched the picker', async () => {
    // The negative control for both assertions below: it is what proves the landing still
    // works, so a guard left permanently armed fails here instead of passing everywhere.
    const landRecents = await reopenOnBrowse()
    await landRecents()
    expect(await screen.findByRole('option', { name: /fav/ })).toBeInTheDocument()
  })

  it('holds Browse after the user has typed a path', async () => {
    const landRecents = await reopenOnBrowse()
    fireEvent.change(screen.getByPlaceholderText(PATH_FIELD), { target: { value: '/home/u/w' } })

    await landRecents()

    expect(screen.getByPlaceholderText(PATH_FIELD)).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /fav/ })).not.toBeInTheDocument()
  })

  it('holds Browse after the user has gone up a level', async () => {
    const landRecents = await reopenOnBrowse()
    fireEvent.click(await screen.findByLabelText('Back'))

    await landRecents()

    expect(screen.getByPlaceholderText(PATH_FIELD)).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /fav/ })).not.toBeInTheDocument()
  })
})
