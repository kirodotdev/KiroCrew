/**
 * While PackAvatar's lazy Lottie player is still loading, the avatar box keeps
 * the pending shimmer (`.skeleton`) instead of drawing an empty circle, and the
 * player replaces it once its chunk arrives.
 */
import { act, cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'

import PackAvatar from '../components/appearancePacks/PackAvatar'
import { namedCeiling } from './namedCeiling'

// The player's module resolves only when the test says so, standing in for a
// chunk still on the wire.
const chunk = vi.hoisted(() => {
  let release: () => void = () => {}
  const arrived = new Promise<void>((resolve) => {
    release = resolve
  })
  return { arrived, release: () => release() }
})
vi.mock('../components/appearancePacks/LottieRenderer', async () => {
  await chunk.arrived
  return { LottieRenderer: () => <i data-testid="lottie-stub" /> }
})

const detail = vi.fn()
vi.mock('../api/client', () => ({
  api: { appearances: { detail: (id: string) => detail(id) } },
}))

afterEach(cleanup)

it('keeps the shimmer while the Lottie player chunk loads, then draws the player', async () => {
  detail.mockResolvedValue({ animations: { idle: { content: '{"v":"5.7.4"}', format: 'lottie' } } })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  render(
    <QueryClientProvider client={qc}>
      <PackAvatar id="aurora" state="idle" size={40} />
    </QueryClientProvider>,
  )

  // The chain: meta read -> whole-pack read -> the Lottie box renders, its
  // player suspended on the held import.
  const box = await screen.findByTestId('pack-avatar-lottie', undefined, namedCeiling('LOTTIE_BOX_WHILE_LOADING', 5000))
  const loading = screen.getByTestId('pack-avatar-player-loading')
  expect(box.contains(loading)).toBe(true)
  expect(loading.className).toContain('skeleton')
  expect(loading.style.width).toBe('40px')
  expect(loading.style.height).toBe('40px')
  expect(screen.queryByTestId('lottie-stub')).toBeNull()

  await act(async () => {
    chunk.release()
  })
  // The chain: the held import resolves -> lazy() settles -> Suspense swaps in the player.
  await screen.findByTestId('lottie-stub', undefined, namedCeiling('LOTTIE_PLAYER_AFTER_LOAD', 5000))
  expect(screen.queryByTestId('pack-avatar-player-loading')).toBeNull()
})
