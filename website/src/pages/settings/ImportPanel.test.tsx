import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ImportPanel } from './ImportPanel'

describe('ImportPanel', () => {
  it('reopens the foreign-agent import flow', () => {
    const listener = vi.fn()
    window.addEventListener('mc-start-import', listener)
    render(<ImportPanel />)

    fireEvent.click(screen.getByRole('button', { name: /import from another agent/i }))

    expect(listener).toHaveBeenCalledOnce()
    window.removeEventListener('mc-start-import', listener)
  })
})
