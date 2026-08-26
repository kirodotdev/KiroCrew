import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'

import { renderWithProviders, createTestStore } from '../test/helpers'
import { sseStatus, setDesktopUpdateAvailable } from '../store/dashboardSlice'
import UpdatePill from '../components/UpdatePill'
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

  it('flips to update-ready once the build is staged', async () => {
    await mount({ desktop: true, state: { state: 'downloaded', version: '9.9.9' } })
    // Pinned via the catalog key's EN value so a copy tweak fails loudly here.
    await waitFor(() => expect(pill()).toHaveTextContent('Update ready'))
  })

  it('clicking deep-links to Settings › About', async () => {
    await mount({ status: { update_available: true } })
    fireEvent.click(pill()!)
    expect(navigate).toHaveBeenCalledWith('/settings?tab=about')
  })
})
