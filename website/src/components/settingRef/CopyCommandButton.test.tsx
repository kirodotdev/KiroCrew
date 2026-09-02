/**
 * CopyCommandButton — unit tests for copy success and failure states.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { CopyCommandButton } from './CopyCommandButton'

// Mock the clipboard utility
vi.mock('../../utils/clipboard', () => ({
  copyCode: vi.fn(),
}))

import { copyCode } from '../../utils/clipboard'
const mockedCopyCode = vi.mocked(copyCode)

afterEach(() => {
  vi.clearAllMocks()
})

describe('CopyCommandButton', () => {
  it('renders with copy icon initially', () => {
    render(<CopyCommandButton text="test command" />)
    const button = screen.getByRole('button')
    expect(button).not.toBeNull()
    expect(button.getAttribute('aria-label')).toBe('Copy command')
  })

  it('shows check icon after successful copy', async () => {
    mockedCopyCode.mockResolvedValue(undefined)
    render(<CopyCommandButton text="kirocrew config set x true" />)

    await act(async () => {
      fireEvent.click(screen.getByRole('button'))
    })

    await waitFor(() => {
      expect(screen.getByRole('button').getAttribute('aria-label')).toBe('Copied!')
    })
  })

  it('shows X icon after failed copy', async () => {
    mockedCopyCode.mockRejectedValue(new Error('Clipboard write denied'))
    render(<CopyCommandButton text="test command" />)

    await act(async () => {
      fireEvent.click(screen.getByRole('button'))
    })

    await waitFor(() => {
      const button = screen.getByRole('button')
      expect(button.getAttribute('aria-label')).toBe('Copy failed')
      expect(button.getAttribute('title')).toBe('Copy failed')
    })
  })

  it('resets to idle state after timeout on failure', async () => {
    vi.useFakeTimers()
    mockedCopyCode.mockRejectedValue(new Error('fail'))
    render(<CopyCommandButton text="test" />)

    await act(async () => {
      fireEvent.click(screen.getByRole('button'))
      // Let the rejected promise settle
      await Promise.resolve()
    })

    expect(screen.getByRole('button').getAttribute('aria-label')).toBe('Copy failed')

    // Advance past the 1500ms reset timeout
    act(() => { vi.advanceTimersByTime(1600) })

    expect(screen.getByRole('button').getAttribute('aria-label')).toBe('Copy command')
    vi.useRealTimers()
  })

  it('resets to idle state after timeout on success', async () => {
    vi.useFakeTimers()
    mockedCopyCode.mockResolvedValue(undefined)
    render(<CopyCommandButton text="test" />)

    await act(async () => {
      fireEvent.click(screen.getByRole('button'))
      // Let the resolved promise settle
      await Promise.resolve()
    })

    expect(screen.getByRole('button').getAttribute('aria-label')).toBe('Copied!')

    act(() => { vi.advanceTimersByTime(1600) })

    expect(screen.getByRole('button').getAttribute('aria-label')).toBe('Copy command')
    vi.useRealTimers()
  })

  it('has aria-live="polite" for assistive technology', () => {
    render(<CopyCommandButton text="test" />)
    const button = screen.getByRole('button')
    expect(button.getAttribute('aria-live')).toBe('polite')
  })
})
