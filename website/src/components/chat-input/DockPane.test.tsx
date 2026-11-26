import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { DockPane } from './DockPane'

describe('DockPane', () => {
  it('is a thick glass pane that keeps the row\'s own attributes', () => {
    render(<DockPane role="status" data-testid="row" aria-label="Row" className="mb-1 px-4">Text</DockPane>)
    const pane = screen.getByTestId('row')
    expect(pane.className).toMatch(/\bliquid-glass\b/)
    expect(pane.className).toMatch(/\bglass-thick\b/)
    expect(pane.className).toMatch(/\bmb-1 px-4\b/)
    expect(pane).toHaveAttribute('role', 'status')
    expect(pane).toHaveAttribute('aria-label', 'Row')
    expect(pane).toHaveTextContent('Text')
  })

  it('mixes a tone into the tint after the step class, and none without one', () => {
    const { rerender } = render(<DockPane data-testid="row" tone="danger">x</DockPane>)
    expect(screen.getByTestId('row').className).toMatch(/\bglass-thick glass-danger\b/)
    rerender(<DockPane data-testid="row" tone="warn">x</DockPane>)
    expect(screen.getByTestId('row').className).toMatch(/\bglass-thick glass-warn\b/)
    rerender(<DockPane data-testid="row" tone="accent">x</DockPane>)
    expect(screen.getByTestId('row').className).toMatch(/\bglass-thick glass-accent\b/)
    rerender(<DockPane data-testid="row">x</DockPane>)
    expect(screen.getByTestId('row').className).not.toMatch(/glass-(danger|warn|accent)/)
  })
})
