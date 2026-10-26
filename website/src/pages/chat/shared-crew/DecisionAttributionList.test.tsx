import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { PublicPrincipal, SharedDecision } from '../../../api/sharedCrew'
import { DecisionAttributionList } from './DecisionAttributionList'

const person = (id: string, name: string, via?: string): PublicPrincipal => ({
  id, kind: 'human', display: { name, login: '', captured_at: null }, ...(via ? { via } : {}),
})

const decision = (over: Partial<SharedDecision>): SharedDecision => ({
  decision_id: 'approval-channel:9:slack_1.1:r1', kind: 'approval', session_key: 'slack_1.1',
  state: 'resolved', revision: 2, initiator: person('p_a', 'Alice Example', 'slack'),
  responder: person('p_b', 'Bob Example', 'slack'), outcome: 'approved', resolved_at: 10, ...over,
})

describe('DecisionAttributionList for a decision answered in Slack', () => {
  it('names the Slack clicker with the Slack mark, and the Slack asker', () => {
    render(<DecisionAttributionList items={[decision({})]} />)
    const line = screen.getByTestId('decision-attribution').querySelector('li') as HTMLElement
    expect(line.textContent).toContain('Approved by Bob Example')
    expect(line.textContent).toContain("asked during Alice Example's turn")
    expect(screen.getByTestId('decision-via').getAttribute('data-via')).toBe('slack')
  })

  it('draws no mark for a dashboard answer or a decision still waiting', () => {
    render(
      <DecisionAttributionList
        items={[
          decision({ decision_id: 'd1', responder: person('p_b', 'Bob') }),
          decision({ decision_id: 'd2', state: 'pending', responder: null, resolved_at: null }),
        ]}
      />,
    )
    expect(screen.queryByTestId('decision-via')).toBeNull()
  })
})
