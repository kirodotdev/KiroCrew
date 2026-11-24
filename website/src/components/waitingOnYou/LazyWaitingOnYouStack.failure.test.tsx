/** A composer stack whose chunk fails to load shows the failure in its own place,
 *  above the message box, instead of throwing to the chat route's boundary. */
import { screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import LazyWaitingOnYouStack from './LazyWaitingOnYouStack'

vi.mock('./WaitingOnYouStack', () => {
  throw new Error('Failed to fetch dynamically imported module')
})

// Chain: lazyRetry -> React.lazy -> the rejected dynamic import -> the error
// boundary's commit. Under a loaded coverage run that can pass the 1000 ms
// default, so the wait allows 5000 ms.
const LAZY_CHAIN = { timeout: 5000 }

describe('the lazy composer stack', () => {
  it('shows a failed chunk load in its own place', async () => {
    renderWithProviders(
      <div data-testid="composer-dock">
        <LazyWaitingOnYouStack slotKey="chat-1" asks={[]} agent="kirocrew" />
      </div>,
    )
    const notice = await screen.findByText(i18nT('components.errorBoundary.something_went_wrong'), {}, LAZY_CHAIN)
    expect(screen.getByTestId('composer-dock')).toContainElement(notice)
    expect(screen.queryByTestId('waiting-on-you-card')).toBeNull()
  })
})
