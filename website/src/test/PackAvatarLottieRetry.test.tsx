/**
 * A failed load of PackAvatar's lazy Lottie player is not kept for the life of
 * the page: the avatar that hit it falls back, and the next mount imports the
 * player again and draws it.
 */
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'

import PackAvatar from '../components/appearancePacks/PackAvatar'
import { namedCeiling } from './namedCeiling'

// The first import of the player fails, as a dropped chunk fetch does; later
// imports succeed.
const attempts = vi.hoisted(() => ({ count: 0 }))
vi.mock('../components/appearancePacks/LottieRenderer', () => {
  attempts.count += 1
  if (attempts.count === 1) throw new Error('Failed to fetch dynamically imported module')
  return { LottieRenderer: () => <i data-testid="lottie-stub" /> }
})

const detail = vi.fn()
vi.mock('../api/client', () => ({
  api: { appearances: { detail: (id: string) => detail(id) } },
}))

afterEach(cleanup)

function mount(onError: () => void) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(
    <QueryClientProvider client={qc}>
      <PackAvatar id="aurora" state="idle" size={40} onError={onError} fallback={<i data-testid="ghost" />} />
    </QueryClientProvider>,
  )
}

it('imports the player again after a failed load, so the next avatar draws its art', async () => {
  detail.mockResolvedValue({ animations: { idle: { content: '{"v":"5.7.4"}', format: 'lottie' } } })

  const first = vi.fn()
  mount(first)
  // The chain: meta read -> whole-pack read -> the first import rejects ->
  // LottieLoadFailed reports -> the avatar swaps in its fallback.
  await screen.findByTestId('ghost', undefined, namedCeiling('FIRST_LOAD_FAILS_TO_FALLBACK', 5000))
  expect(first).toHaveBeenCalledTimes(1)
  cleanup()

  const second = vi.fn()
  mount(second)
  // The chain: meta read -> whole-pack read -> a fresh lazy() imports again ->
  // the player draws.
  await screen.findByTestId('lottie-stub', undefined, namedCeiling('SECOND_LOAD_DRAWS_PLAYER', 5000))
  expect(screen.queryByTestId('ghost')).toBeNull()
  expect(second).not.toHaveBeenCalled()
  expect(attempts.count).toBe(2)
})
