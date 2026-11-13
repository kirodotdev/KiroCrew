/**
 * The MCP staged-edits signal that lets MCP Management withhold its "Ask the
 * agent" hand-off while the sibling MCP Servers list holds unapplied edits a
 * /chat navigation would drop (#24920).
 */
import { describe, it, expect } from 'vitest'
import { render, screen, act } from '@testing-library/react'

import {
  McpStagedEditsProvider,
  useMcpHandoffWouldLoseEdits,
  useSetMcpStagedEdits,
} from '../pages/connections/mcpStagedEditsContext'

// Reads the signal and exposes the setter, so one component can drive both.
function Probe() {
  const staged = useMcpHandoffWouldLoseEdits()
  const setStaged = useSetMcpStagedEdits()
  return (
    <>
      <div data-testid="staged">{staged ? 'yes' : 'no'}</div>
      <button type="button" onClick={() => setStaged(true)}>stage</button>
      <button type="button" onClick={() => setStaged(false)}>clear</button>
    </>
  )
}

describe('mcpStagedEditsContext', () => {
  it('reads false with a no-op setter outside any provider', () => {
    // So MCP Management rendered standalone (a test, a future mount) keeps
    // offering its hand-off and the setter cannot throw.
    render(<Probe />)
    expect(screen.getByTestId('staged').textContent).toBe('no')
    act(() => { screen.getByText('stage').click() })
    expect(screen.getByTestId('staged').textContent).toBe('no')
  })

  it('publishes the staged flag to readers inside the provider', () => {
    render(<McpStagedEditsProvider><Probe /></McpStagedEditsProvider>)
    expect(screen.getByTestId('staged').textContent).toBe('no')
    act(() => { screen.getByText('stage').click() })
    expect(screen.getByTestId('staged').textContent).toBe('yes')
    act(() => { screen.getByText('clear').click() })
    expect(screen.getByTestId('staged').textContent).toBe('no')
  })
})
