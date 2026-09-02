import { render, screen, fireEvent } from '@testing-library/react'
import ToolInputPreview from './ToolInputPreview'

describe('ToolInputPreview', () => {
  it('renders short input in full with no expand affordance', () => {
    render(<ToolInputPreview toolInput="zzq-short" />)
    expect(screen.getByText(/zzq-short/)).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('truncates past the threshold and expands on demand', () => {
    render(<ToolInputPreview toolInput={'z'.repeat(30)} threshold={10} />)

    const expand = screen.getByRole('button', { name: /show full command/i })
    expect(document.querySelector('pre')!.textContent).toBe('z'.repeat(10) + '…')

    fireEvent.click(expand)
    expect(document.querySelector('pre')!.textContent).toBe('z'.repeat(30))
    expect(screen.getByRole('button', { name: /collapse/i })).toBeInTheDocument()
  })

  it('collapses back to the truncated preview', () => {
    render(<ToolInputPreview toolInput={'z'.repeat(30)} threshold={10} />)
    fireEvent.click(screen.getByRole('button', { name: /show full command/i }))
    fireEvent.click(screen.getByRole('button', { name: /collapse/i }))
    expect(document.querySelector('pre')!.textContent).toBe('z'.repeat(10) + '…')
  })

  it('input exactly at the threshold is not considered truncatable', () => {
    render(<ToolInputPreview toolInput={'z'.repeat(10)} threshold={10} />)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })
})
