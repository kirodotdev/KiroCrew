/**
 * System > Services > "Leaked agent runtimes".
 *
 * Pins that the card shows the reconciler's count and memory, hides when nothing
 * is leaked or the gateway cannot answer, and that Reclaim needs two clicks: the
 * first only arms, the second is the one request that ends anything.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import type { LeakedRuntimes, LeakedRuntimesReclaim } from '../api/client/system'

const leakedRuntimes = vi.fn<() => Promise<LeakedRuntimes>>()
const reclaimLeakedRuntimes = vi.fn<() => Promise<LeakedRuntimesReclaim>>()

vi.mock('../api/client', () => ({
  api: {
    leakedRuntimes: () => leakedRuntimes(),
    reclaimLeakedRuntimes: () => reclaimLeakedRuntimes(),
  },
}))

import LeakedRuntimesCard from '../pages/system/LeakedRuntimesCard'

const LEAKED: LeakedRuntimes = {
  supported: true,
  count: 3,
  rss_bytes: 1_500_000_000,
  runtimes: [{ pid: 4242, rss_bytes: 1_500_000_000 }],
}

beforeEach(() => {
  leakedRuntimes.mockReset()
  reclaimLeakedRuntimes.mockReset()
})

describe('LeakedRuntimesCard', () => {
  it('shows the leaked count and the memory they hold', async () => {
    leakedRuntimes.mockResolvedValue(LEAKED)
    renderWithProviders(<LeakedRuntimesCard />)
    expect(await screen.findByTestId('leaked-runtimes-count')).toHaveTextContent('3')
    expect(screen.getByTestId('leaked-runtimes-rss')).toHaveTextContent('1.5GB')
    expect(screen.getByTestId('leaked-runtimes-no-loss')).toHaveTextContent('loses no work')
  })

  it('renders nothing when nothing is leaked or the gateway cannot answer', async () => {
    leakedRuntimes.mockResolvedValue({ ...LEAKED, count: 0, rss_bytes: 0, runtimes: [] })
    const { container } = renderWithProviders(<LeakedRuntimesCard />)
    await waitFor(() => expect(leakedRuntimes).toHaveBeenCalled())
    expect(container.querySelector('[data-testid="leaked-runtimes-card"]')).toBeNull()
  })

  it('hides when the route is absent but shows any other read failure', async () => {
    leakedRuntimes.mockRejectedValue(Object.assign(new Error('Not Found'), { status: 404 }))
    const { container, unmount } = renderWithProviders(<LeakedRuntimesCard />)
    await waitFor(() => expect(leakedRuntimes).toHaveBeenCalled())
    expect(container.querySelector('[data-testid="leaked-runtimes-card"]')).toBeNull()
    unmount()
    leakedRuntimes.mockRejectedValue(Object.assign(new Error('gateway exploded'), { status: 500 }))
    renderWithProviders(<LeakedRuntimesCard />)
    expect(await screen.findByTestId('leaked-runtimes-load-error')).toHaveTextContent('Could not read leaked runtimes')
  })

  it('the first click only arms; the second sends the one reclaim', async () => {
    leakedRuntimes.mockResolvedValue(LEAKED)
    reclaimLeakedRuntimes.mockResolvedValue({ killed: [4242], refused: [{ pid: 4300, reason: 'x' }] })
    renderWithProviders(<LeakedRuntimesCard />)
    const button = await screen.findByTestId('leaked-runtimes-reclaim')
    fireEvent.click(button)
    expect(reclaimLeakedRuntimes).not.toHaveBeenCalled()
    expect(button).toHaveTextContent('Reclaim now? No work is lost')
    expect(button).not.toHaveAttribute('aria-label')
    fireEvent.click(button)
    await waitFor(() => expect(reclaimLeakedRuntimes).toHaveBeenCalledTimes(1))
    expect(await screen.findByTestId('leaked-runtimes-result')).toHaveTextContent('Ended 1 · kept 1')
  })

  it('a refused reclaim shows its reason through ErrorNotice', async () => {
    leakedRuntimes.mockResolvedValue(LEAKED)
    reclaimLeakedRuntimes.mockRejectedValue(
      Object.assign(new Error('owner authorization required'), { status: 403, body: '{"code": "owner_only"}' }),
    )
    renderWithProviders(<LeakedRuntimesCard />)
    const button = await screen.findByTestId('leaked-runtimes-reclaim')
    fireEvent.click(button)
    fireEvent.click(button)
    expect(await screen.findByTestId('leaked-runtimes-error'))
      .toHaveTextContent('Only the dashboard owner can reclaim leaked runtimes.')
  })
})
