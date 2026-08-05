import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import TypewriterText from '../components/TypewriterText'

describe('TypewriterText', () => {
  it('renders text immediately when not animating', () => {
    render(<TypewriterText text="Hello world" />)
    expect(screen.getByText('Hello world')).toBeInTheDocument()
  })

  it('applies className', () => {
    render(<TypewriterText text="Test" className="custom-class" />)
    expect(screen.getByText('Test')).toHaveClass('custom-class')
  })

  it('applies title attribute', () => {
    render(<TypewriterText text="Test" title="tooltip" />)
    expect(screen.getByTitle('tooltip')).toBeInTheDocument()
  })
})
