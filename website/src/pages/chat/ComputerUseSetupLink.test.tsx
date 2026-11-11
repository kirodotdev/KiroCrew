import { describe, it, expect } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { ComputerUseSetupLink, computerUseSetupNeeded } from './ComputerUseSetupLink'

const REFUSAL =
  'Error: computer use is disabled. Enable it in the KiroCrew dashboard under Settings -> Computer Use; it cannot be enabled by an agent.'

describe('computerUseSetupNeeded', () => {
  it('fires for a computer-use refusal that names the setup page', () => {
    expect(computerUseSetupNeeded({ mcpServer: 'kirocrew-computer', trustedToolName: 'computer_get_state', output: REFUSAL })).toBe(true)
    expect(computerUseSetupNeeded({ mcpServer: '', trustedToolName: 'computer_click', output: `  ${REFUSAL}` })).toBe(true)
  })

  it('ignores other tools, successes and unrelated errors', () => {
    expect(computerUseSetupNeeded({ mcpServer: 'kirocrew-core', trustedToolName: 'browser', output: REFUSAL })).toBe(false)
    expect(computerUseSetupNeeded({ mcpServer: 'kirocrew-computer', trustedToolName: 'computer_get_state', output: 'Window Notes: see Settings -> Computer Use' })).toBe(false)
    expect(computerUseSetupNeeded({ mcpServer: 'kirocrew-computer', trustedToolName: 'computer_click', output: "Error: no state for 'Notes'. Call computer_get_state first." })).toBe(false)
  })

  it('does not trust a model-written title as identity', () => {
    // The display title is not an input at all; only the transport fields are.
    expect(computerUseSetupNeeded({ mcpServer: 'other-server', trustedToolName: 'shell', output: REFUSAL })).toBe(false)
  })
})

function Where() {
  const loc = useLocation()
  return <div data-testid="where">{loc.pathname + loc.search}</div>
}

describe('ComputerUseSetupLink', () => {
  it('opens Settings -> Computer Use with the setup dialog requested', () => {
    render(
      <MemoryRouter initialEntries={['/chat']}>
        <Routes>
          <Route path="/chat" element={<ComputerUseSetupLink />} />
          <Route path="/settings/*" element={<Where />} />
        </Routes>
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByRole('button', { name: /set up computer use/i }))
    expect(screen.getByTestId('where')).toHaveTextContent('/settings/computer-use?setup=1')
  })
})
