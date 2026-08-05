import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import InfoTip from '../components/InfoTip'

describe('InfoTip', () => {
  it('renders a ? button with title', () => {
    render(<InfoTip text="Help text" />)
    const btn = screen.getByTitle('Help text')
    expect(btn).toBeInTheDocument()
    expect(btn).toHaveTextContent('?')
  })

  it('shows tooltip on click', () => {
    render(<InfoTip text="Detailed help" />)
    fireEvent.click(screen.getByTitle('Detailed help'))
    expect(screen.getByText('Detailed help')).toBeInTheDocument()
  })

  it('hides tooltip on outside click', () => {
    render(<InfoTip text="Tip content" />)
    fireEvent.click(screen.getByTitle('Tip content'))
    expect(screen.getByText('Tip content')).toBeInTheDocument()
    fireEvent.mouseDown(document.body)
    expect(screen.queryByText('Tip content')).not.toBeInTheDocument()
  })
})
