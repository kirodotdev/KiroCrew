import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import HoverTip from '../components/HoverTip'
import { OPEN_DELAY_MS } from '../components/InstantTip'
import ReadingWidthToggle from '../components/ReadingWidthToggle'

describe('HoverTip', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('shows the bubble after the short intent delay, not the OS title delay', () => {
    render(<HoverTip label="Download"><button type="button" aria-label="Download">D</button></HoverTip>)
    const btn = screen.getByRole('button', { name: 'Download' })
    fireEvent.mouseEnter(btn.parentElement as HTMLElement)
    expect(screen.queryByRole('tooltip')).toBeNull()
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Download')
    fireEvent.mouseLeave(btn.parentElement as HTMLElement)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('keeps the description on the control while the bubble is closed', () => {
    render(<HoverTip label="Publish this artifact"><button type="button">Publish</button></HoverTip>)
    expect(screen.getByRole('button', { name: 'Publish', description: 'Publish this artifact' })).not.toHaveAttribute('title')
  })

  it('keeps the description out of the reading order, so it is not read twice as page text', () => {
    render(<h1><span>Notes</span><HoverTip label="Rename this artifact"><button type="button">Rename</button></HoverTip></h1>)
    expect(screen.getByRole('heading', { name: 'Notes Rename' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Rename', description: 'Rename this artifact' })).toBeInTheDocument()
  })

  it('opens synchronously on keyboard focus', () => {
    render(<HoverTip label="Copy"><button type="button">C</button></HoverTip>)
    fireEvent.focus(screen.getByRole('button'))
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copy')
  })

  it('without a label, leaves the control as it is inside the same wrapper', () => {
    const { rerender } = render(<HoverTip label="Click to hide"><button type="button">M</button></HoverTip>)
    const btn = screen.getByRole('button')
    rerender(<HoverTip><button type="button" aria-describedby="card">M</button></HoverTip>)
    expect(screen.getByRole('button')).toBe(btn)
    expect(btn).toHaveAttribute('aria-describedby', 'card')
    expect(btn.parentElement).toHaveAttribute('data-hover-tip')
    fireEvent.mouseEnter(btn.parentElement as HTMLElement)
    fireEvent.focus(btn)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })
})

describe('ReadingWidthToggle', () => {
  it('renders an icon, not a letter, and no native title', () => {
    render(<ReadingWidthToggle value="md" onToggle={() => {}} />)
    const btn = screen.getByRole('button', { name: 'Medium width' })
    expect(btn).toHaveTextContent('')
    expect(btn.querySelector('svg')).not.toBeNull()
    expect(btn).not.toHaveAttribute('title')
  })
})
