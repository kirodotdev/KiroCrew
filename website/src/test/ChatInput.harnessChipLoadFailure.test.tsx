// The model chip's harness mark is a lazy chunk inside the chip's button. When
// that chunk fails to load, the chip keeps its blank placeholder and keeps
// working; the failure never reaches the nearest boundary above the composer,
// which on the chat page is the route's and would replace the whole page.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import ErrorBoundary from '../components/ErrorBoundary'
import { stubStripHeights } from './stripHeights'
import { defaultProps } from './ChatInputTestUtils'
import { namedCeiling } from './namedCeiling'

const LAZY_LOGO = namedCeiling('LAZY_LOGO', 5000)

// The mark's chunk: an import that rejects the way a browser rejects a module
// script it could not fetch.
vi.mock('../components/HarnessLogo', () => {
  throw new TypeError('Failed to fetch dynamically imported module: /assets/HarnessLogo.js')
})

let consoleError: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  stubStripHeights()
  localStorage.clear()
  // React and the boundary log the caught render error; catching it is the point.
  consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
})
afterEach(() => { vi.restoreAllMocks() })

describe('ChatInput model chip harness mark whose chunk fails to load', () => {
  it('keeps the chip and its placeholder instead of throwing to the boundary above', async () => {
    const onModelClick = vi.fn()
    renderWithProviders(
      <ErrorBoundary scope="test-route" fallback={<div data-testid="route-boundary-fallback" />}>
        <ChatInput {...defaultProps} modelName="opus" onModelClick={onModelClick} harnessBackend="claude" />
      </ErrorBoundary>,
    )
    // The rejection has been caught and logged by a boundary, the chip's own or the one above.
    await waitFor(
      () => expect(consoleError.mock.calls.some(call => String(call[0]).startsWith('[ErrorBoundary'))).toBe(true),
      LAZY_LOGO,
    )

    expect(screen.queryByTestId('route-boundary-fallback'), 'the failure stays inside the chip').toBeNull()
    const chip = screen.getByTestId('composer-model-chip')
    const mark = screen.getByTestId('composer-model-chip-harness')
    expect(mark).toHaveAttribute('title', 'Runs on Claude Code')
    const placeholder = mark.querySelector('span[aria-hidden="true"]')
    expect(placeholder, 'the 13px placeholder keeps the chip width').not.toBeNull()
    expect(placeholder).toHaveClass('w-[13px]', 'h-[13px]')
    expect(mark.querySelector('button, [role="button"], [role="alert"]'), 'no control or notice nested in the chip').toBeNull()
    expect(chip).toHaveTextContent(/^opus$/)

    fireEvent.click(chip)
    expect(onModelClick).toHaveBeenCalledTimes(1)
  })
})
