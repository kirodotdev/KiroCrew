import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { IconButton } from '../components/ui'
import { OPEN_DELAY_MS } from '../components/InstantTip'

// An icon-only IconButton shows its own aria-label as an InstantTip on hover
// and on keyboard focus, so a user can read what the icon does before clicking.
describe('IconButton label tip', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('shows the aria-label on hover after the intent delay, and hides on leave', () => {
    render(<IconButton aria-label="Set a goal">*</IconButton>)
    const btn = screen.getByRole('button', { name: 'Set a goal' })
    fireEvent.mouseEnter(btn)
    expect(screen.queryByRole('tooltip')).toBeNull()
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Set a goal')
    fireEvent.mouseLeave(btn)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('shows the aria-label at once on keyboard focus, and hides on Escape', () => {
    render(<IconButton aria-label="Open activity panel">*</IconButton>)
    act(() => { screen.getByRole('button', { name: 'Open activity panel' }).focus() })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Open activity panel')
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('paints nothing on a focus the browser would not ring, such as a menu handing focus back after a mouse pick', () => {
    render(<IconButton aria-label="More actions">*</IconButton>)
    const btn = screen.getByRole('button', { name: 'More actions' })
    const matches = btn.matches.bind(btn)
    vi.spyOn(btn, 'matches').mockImplementation(sel => sel === ':focus-visible' ? false : matches(sel))
    fireEvent.focus(btn)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('does not add the hint as a description, which would repeat the name', () => {
    render(<IconButton aria-label="Set a goal">*</IconButton>)
    expect(screen.getByRole('button')).not.toHaveAttribute('aria-describedby')
  })

  it('keeps a caller title as the native tooltip and adds no bubble', () => {
    render(<IconButton aria-label="Full screen" title="Exit full screen (Esc)">*</IconButton>)
    const btn = screen.getByRole('button', { name: 'Full screen' })
    expect(btn).toHaveAttribute('title', 'Exit full screen (Esc)')
    fireEvent.focus(btn)
    fireEvent.mouseEnter(btn)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('still runs the caller handlers for the same events', () => {
    const onFocus = vi.fn()
    const onMouseEnter = vi.fn()
    const onBlur = vi.fn()
    render(<IconButton aria-label="More" onFocus={onFocus} onMouseEnter={onMouseEnter} onBlur={onBlur}>*</IconButton>)
    const btn = screen.getByRole('button', { name: 'More' })
    fireEvent.mouseEnter(btn)
    fireEvent.focus(btn)
    fireEvent.blur(btn)
    expect(onMouseEnter).toHaveBeenCalledTimes(1)
    expect(onFocus).toHaveBeenCalledTimes(1)
    expect(onBlur).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })
})
