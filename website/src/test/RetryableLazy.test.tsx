import { fireEvent, render, screen } from '@testing-library/react'

import { retryableLazy } from '../components/RetryableLazy'

// retryableLazy resolves its import asynchronously, and a retry re-runs it.
// A loaded CI shard can outlast findBy*'s 1 s default between the import
// settling and the boundary rendering, so these waits name this timeout.
const LAZY_AUDIT_MOUNT = { timeout: 5000 }

describe('retryableLazy', () => {
  it('runs a fresh import after the first import rejects', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const load = vi.fn()
      .mockRejectedValueOnce(new Error('stale chunk'))
      .mockResolvedValueOnce({ default: () => <div>Loaded on retry</div> })
    const LazyPanel = retryableLazy(load)

    render(<LazyPanel />)

    expect(await screen.findByText('Something went wrong', undefined, LAZY_AUDIT_MOUNT)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Try Again' }))

    expect(await screen.findByText('Loaded on retry', undefined, LAZY_AUDIT_MOUNT)).toBeTruthy()
    expect(load).toHaveBeenCalledTimes(2)
  })
})
