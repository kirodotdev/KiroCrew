import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { OPEN_DELAY_MS } from '../components/InstantTip'
import TruncatedText from '../components/TruncatedText'

describe('TruncatedText', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('shows the full text in the bubble on hover, with no native title', () => {
    render(<TruncatedText full="telemetry.startup.cold_ms" />)
    const line = screen.getByText('telemetry.startup.cold_ms')
    expect(line).not.toHaveAttribute('title')
    fireEvent.mouseEnter(line)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('telemetry.startup.cold_ms')
    fireEvent.mouseLeave(line)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('is a tab stop whose focus opens the bubble at once, and Escape closes it', () => {
    render(<TruncatedText full="/home/me/work/a-very-long-folder-name" />)
    const line = screen.getByText('/home/me/work/a-very-long-folder-name')
    expect(line).toHaveAttribute('tabindex', '0')
    fireEvent.focus(line)
    expect(screen.getByRole('tooltip')).toHaveTextContent('/home/me/work/a-very-long-folder-name')
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('points the bubble at its own line, which a stacked list would otherwise read as the row above', () => {
    render(<TruncatedText full="telemetry.startup.cold_ms" />)
    fireEvent.focus(screen.getByText('telemetry.startup.cold_ms'))
    expect(screen.getByRole('tooltip')).toContainElement(screen.getByTestId('instant-tip-arrow'))
  })

  it('starts the bubble of a label shown over a raw value with that label, so it names its own row', () => {
    render(<TruncatedText full="chat-7-1700009999">Untitled · 11/14/2023</TruncatedText>)
    fireEvent.focus(screen.getByText('Untitled · 11/14/2023'))
    expect(screen.getByRole('tooltip')).toHaveTextContent(/^Untitled · 11\/14\/2023chat-7-1700009999$/)
  })

  it('describes a label shown over a raw value by that value, and its own text by nothing', () => {
    const { rerender } = render(<TruncatedText full="chat-1-1700000000">Untitled conversation</TruncatedText>)
    const label = screen.getByText('Untitled conversation')
    const desc = document.getElementById(label.getAttribute('aria-describedby') ?? '')
    expect(desc).toHaveTextContent('chat-1-1700000000')
    expect(desc).not.toBeVisible()
    rerender(<TruncatedText full="chat-1-1700000000" />)
    expect(screen.getByText('chat-1-1700000000')).not.toHaveAttribute('aria-describedby')
    // A label that happens to equal the raw value (an unknown category) is not
    // described by a second copy of itself.
    rerender(<TruncatedText full="chat-1-1700000000">chat-1-1700000000</TruncatedText>)
    expect(screen.getByText('chat-1-1700000000')).not.toHaveAttribute('aria-describedby')
  })
})
