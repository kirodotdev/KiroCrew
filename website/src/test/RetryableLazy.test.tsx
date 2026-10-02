import { fireEvent, render, screen } from '@testing-library/react'

import { retryableLazy } from '../components/RetryableLazy'

describe('retryableLazy', () => {
  it('runs a fresh import after the first import rejects', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const load = vi.fn()
      .mockRejectedValueOnce(new Error('stale chunk'))
      .mockResolvedValueOnce({ default: () => <div>Loaded on retry</div> })
    const LazyPanel = retryableLazy(load)

    render(<LazyPanel />)

    expect(await screen.findByText('Something went wrong')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Try Again' }))

    expect(await screen.findByText('Loaded on retry')).toBeTruthy()
    expect(load).toHaveBeenCalledTimes(2)
  })
})
