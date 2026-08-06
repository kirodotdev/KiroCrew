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

  it('is named by a short phrase, and describes itself with the tip text', () => {
    // The visible glyph is a bare "?", which assistive technology would
    // otherwise announce as "question mark" — a control with no discoverable
    // purpose, and a blocking a11y rule. The tip prose is the DESCRIPTION, not
    // the name: a name is read on every visit, and it is also the handle every
    // other control is queried by, so a paragraph-length one both talks over
    // the user and collides with real actions named inside that prose.
    render(<InfoTip text="What this binding does" />)
    const btn = screen.getByRole('button', { name: 'More information' })
    expect(btn).toHaveAttribute('aria-expanded', 'false')
    expect(btn).not.toHaveAttribute('aria-describedby')

    fireEvent.click(btn)
    expect(btn).toHaveAttribute('aria-expanded', 'true')
    const tip = screen.getByRole('tooltip')
    expect(tip).toHaveTextContent('What this binding does')
    expect(btn.getAttribute('aria-describedby')).toBe(tip.id)
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
