import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'

import { renderWithProviders, createTestStore } from '../test/helpers'
import { sseStatus, setDesktopUpdateAvailable } from '../store/dashboardSlice'
import UpdatePill from '../components/UpdatePill'
import { NavigationLeaveGuardProvider, useRegisterNavigationLeaveGuard } from '../components/NavigationLeaveGuard'
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from '../components/ui/dropdown-menu'
import type { UpdateState } from '../hooks/useUpdateSubscription'
import type { StatusData } from '../types'

const navigate = vi.fn()
vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => navigate,
}))

const pill = () => screen.queryByTestId('update-pill')

async function mount(opts: { status?: Partial<StatusData>; desktop?: boolean; state?: UpdateState } = {}) {
  const store = createTestStore()
  if (opts.status) store.dispatch(sseStatus(opts.status as StatusData))
  if (opts.desktop) store.dispatch(setDesktopUpdateAvailable(true))
  const rendered = renderWithProviders(<UpdatePill />, { store })
  if (opts.state) {
    await act(async () => { rendered.queryClient.setQueryData(['update-state'], opts.state) })
  }
  return rendered
}

beforeEach(() => navigate.mockReset())

describe('UpdatePill', () => {
  it('is absent while there is nothing to say', async () => {
    await mount()
    expect(pill()).not.toBeInTheDocument()
  })

  it('a null gateway verdict never shows it', async () => {
    await mount({ status: { update_available: null } })
    expect(pill()).not.toBeInTheDocument()
  })

  it('appears on a gateway update', async () => {
    await mount({ status: { update_available: true } })
    expect(pill()).toBeInTheDocument()
  })

  it('appears on a desktop update', async () => {
    await mount({ desktop: true })
    expect(pill()).toBeInTheDocument()
  })

  it('shows live percent while the desktop build downloads', async () => {
    await mount({ desktop: true, state: { state: 'downloading', version: '9.9.9', percent: 41.6 } })
    // React Query batches cache notifications, so the label lands a tick
    // after setQueryData — poll rather than assert synchronously.
    await waitFor(() => expect(pill()).toHaveTextContent('42'))
  })

  it('marks its label for the desktop top bar to fold it, keeping the pill named', async () => {
    // index.css's last actions-group rung (`tb-drop-update-label`) hides the
    // label so the pill cannot push the bell out; the icon and the
    // aria-label still carry the meaning.
    await mount({ status: { update_available: true } })
    const label = pill()!.querySelector('.tb-drop-update-label')
    expect(label).not.toBeNull()
    expect(label!.textContent).not.toBe('')
    expect(pill()).toHaveAccessibleName()
  })

  it('flips to update-ready once the build is staged', async () => {
    await mount({ desktop: true, state: { state: 'downloaded', version: '9.9.9' } })
    // Pinned via the catalog key's EN value so a copy tweak fails loudly here.
    await waitFor(() => expect(pill()).toHaveTextContent('Update ready'))
  })

  it('clicking deep-links to Settings › About', async () => {
    await mount({ status: { update_available: true } })
    fireEvent.click(pill()!)
    expect(navigate).toHaveBeenCalledWith('/settings/about')
  })
})

describe('UpdatePill leave guard and phone label', () => {
  function DraftPage({ ask }: { ask: () => boolean }) {
    useRegisterNavigationLeaveGuard(ask)
    return null
  }

  async function mountGuarded(ask: () => boolean, element = <UpdatePill />) {
    const store = createTestStore()
    store.dispatch(sseStatus({ update_available: true } as StatusData))
    return renderWithProviders(
      <NavigationLeaveGuardProvider><DraftPage ask={ask} />{element}</NavigationLeaveGuardProvider>,
      { store },
    )
  }

  it('asks the page first and stays put when the page keeps its draft', async () => {
    // Settings › About unmounts the page on screen: an unsaved New crewmate
    // draft must get the same confirm as every other leave.
    const ask = vi.fn(() => false)
    await mountGuarded(ask)
    fireEvent.click(pill()!)
    expect(ask).toHaveBeenCalledTimes(1)
    expect(navigate).not.toHaveBeenCalled()
  })

  it('leaves for Settings › About once the page agrees', async () => {
    const ask = vi.fn(() => true)
    await mountGuarded(ask)
    fireEvent.click(pill()!)
    expect(navigate).toHaveBeenCalledWith('/settings/about')
  })

  it('guards the overflow-menu row the same way', async () => {
    const ask = vi.fn(() => false)
    await mountGuarded(ask, (
      <DropdownMenu open>
        <DropdownMenuTrigger>menu</DropdownMenuTrigger>
        <DropdownMenuContent><UpdatePill variant="menu-item" /></DropdownMenuContent>
      </DropdownMenu>
    ))
    fireEvent.click(await screen.findByTestId('update-menu-item'))
    expect(ask).toHaveBeenCalled()
    expect(navigate).not.toHaveBeenCalled()
  })

  it('keeps its text at every width when showLabel is set (the phone Crewmates bar)', async () => {
    const store = createTestStore()
    store.dispatch(sseStatus({ update_available: true } as StatusData))
    renderWithProviders(<UpdatePill showLabel />, { store })
    expect(pill()!.querySelector('.tb-drop-update-label')).toBeNull()
    expect(pill()).toHaveTextContent('Update available')
    // Bounded, so a long label or locale truncates instead of crowding the
    // crewmate header out of a 320px bar; the full label stays in the title.
    expect(pill()!.className).toMatch(/(^|\s)max-w-\[88px\](\s|$)/)
    expect(pill()!.className).toMatch(/(^|\s)min-w-0(\s|$)/)
    expect(pill()!.querySelector('span')!.className).toMatch(/(^|\s)truncate(\s|$)/)
    expect(pill()).toHaveAttribute('title', 'Update available')
  })
})
