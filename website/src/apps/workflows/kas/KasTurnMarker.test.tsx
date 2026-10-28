import { describe, expect, it } from 'vitest'
import { screen } from '@testing-library/react'

import { renderWithProviders } from '../../../test/helpers'
import { KasTurnMarkerRow, parseKasTurnMarker } from './KasTurnMarker'

const TOKEN = 'a'.repeat(32)
const user = (content: string) => ({ role: 'user' as const, content })

describe('parseKasTurnMarker', () => {
  it("reads the router's three marker shapes", () => {
    expect(parseKasTurnMarker(user(`[Workflow \`investigate\` completed] Kiro Agent's reply follows (observe ${TOKEN})`))).toEqual({
      kind: 'observe',
      name: 'investigate',
      status: 'completed',
    })
    expect(parseKasTurnMarker(user(`Start workflow \`triage\` in \`/repo\` (launch ${TOKEN})`))).toEqual({ kind: 'launch', name: 'triage' })
    expect(parseKasTurnMarker(user(`Reconnect workflow runs (reconnect ${TOKEN})`))).toEqual({ kind: 'reconnect' })
  })

  it('leaves everything else to the ordinary user bubble', () => {
    expect(parseKasTurnMarker(user('re-run the workflow'))).toBeNull()
    expect(parseKasTurnMarker(user('[Workflow `x` completed] see (observe 123)'))).toBeNull()
    expect(parseKasTurnMarker({ role: 'assistant', content: `Reconnect workflow runs (reconnect ${TOKEN})` })).toBeNull()
  })
})

describe('KasTurnMarkerRow', () => {
  it('draws one notice line without the token', () => {
    renderWithProviders(<KasTurnMarkerRow marker={{ kind: 'observe', name: 'investigate', status: 'failed' }} />)
    const row = screen.getByTestId('kas-turn-marker')
    expect(row).toHaveTextContent('Workflow investigate failed')
    expect(row.textContent).not.toContain('observe')
  })

  it('renders the observe-turn verbs a step raises: update, waiting, error', () => {
    const line = (status: string) => {
      const { unmount } = renderWithProviders(<KasTurnMarkerRow marker={{ kind: 'observe', name: 'triage', status }} />)
      const text = screen.getByTestId('kas-turn-marker').textContent
      unmount()
      return text
    }
    expect(line('update')).toBe('Update from workflow triage')
    expect(line('waiting')).toBe('Workflow triage is waiting for you')
    expect(line('error')).toBe('A step in workflow triage failed')
  })
})
