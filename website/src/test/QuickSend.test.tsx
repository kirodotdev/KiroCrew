/**
 * Tests for Quick Send behavior in FollowUpBar.
 * Verifies that MouseEvent is passed correctly for shiftKey detection.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import FollowUpBar from '../components/FollowUpBar'

describe('FollowUpBar Quick Send support', () => {
  it('passes shiftKey=false on normal click', () => {
    const onSelect = vi.fn()
    render(<FollowUpBar options={['Go']} picked={new Set()} onSelect={onSelect} />)
    fireEvent.click(screen.getByRole('button', { name: 'Go' }))
    expect(onSelect).toHaveBeenCalledWith('Go', expect.objectContaining({ shiftKey: false }))
  })

  it('passes shiftKey=true on shift+click', () => {
    const onSelect = vi.fn()
    render(<FollowUpBar options={['Go']} picked={new Set()} onSelect={onSelect} />)
    fireEvent.click(screen.getByRole('button', { name: 'Go' }), { shiftKey: true })
    expect(onSelect).toHaveBeenCalledWith('Go', expect.objectContaining({ shiftKey: true }))
  })
})
