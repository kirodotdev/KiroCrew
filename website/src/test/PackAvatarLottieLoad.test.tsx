/**
 * PackAvatar loads the Lottie player on demand, off the first load. When that
 * chunk will not load (a dropped connection, a stale deploy), the avatar must
 * report the failure and draw its fallback face, the same as malformed art,
 * rather than leave a blank box or throw to the nearest error boundary.
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'

import PackAvatar from '../components/appearancePacks/PackAvatar'
import { namedCeiling } from './namedCeiling'

// A mock factory that throws rejects the import() of this module, as a failed
// chunk fetch does in the browser.
vi.mock('../components/appearancePacks/LottieRenderer', () => {
  throw new Error('Failed to fetch dynamically imported module')
})

const detail = vi.fn()
vi.mock('../api/client', () => ({
  api: { appearances: { detail: (id: string) => detail(id) } },
}))

afterEach(cleanup)

it('falls back and reports when the Lottie player chunk fails to load', async () => {
  detail.mockResolvedValue({ animations: { idle: { content: '{"v":"5.7.4"}', format: 'lottie' } } })
  const onError = vi.fn()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  render(
    <QueryClientProvider client={qc}>
      <PackAvatar id="aurora" state="idle" size={40} onError={onError} fallback={<i data-testid="ghost" />} />
    </QueryClientProvider>,
  )

  // The chain: meta read -> whole-pack read -> the lazy import rejects ->
  // LottieLoadFailed's effect calls onError -> the avatar swaps in its fallback.
  expect(await screen.findByTestId('ghost', undefined, namedCeiling('LOTTIE_LOAD_FAILED_FALLBACK', 5000))).toBeTruthy()
  // Same commit as the fallback, so no further chain; the ceiling only names a starved run.
  await waitFor(() => expect(onError).toHaveBeenCalledTimes(1), namedCeiling('LOTTIE_LOAD_FAILED_REPORT', 5000))
})
