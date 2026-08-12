import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Search } from 'lucide-react'

import AgentSessionButton from '../apps/issue-radar/components/AgentSessionButton'
import type { InvestigationRecord } from '../apps/issue-radar/api'

// The in-progress pill is a claim that work is UNDER WAY. `status` alone cannot carry
// that claim: it outlives whatever it described, and the backend stamps `investigating`
// on any record it creates -- so a reservation whose session never started leaves a
// record that would otherwise read as permanently investigating with nothing running.
// The link is the only thing that makes "in progress" true.
const base = {
  icon: Search,
  label: 'Investigate',
  busy: false,
  error: null,
  onClick: () => {},
  startHint: 'start',
  resumeHint: 'resume',
  pendingLabel: 'Investigating',
  donePillLabel: 'Done',
  showStatus: true,
}

const record = (over: Partial<InvestigationRecord>): InvestigationRecord =>
  ({ owner: 'o', repo: 'r', number: 1, slot_key: null, folder_id: null,
     status: 'investigating', started_at: '', last_opened_at: '', findings: null,
     ...over } as InvestigationRecord)

describe('the in-progress pill requires a live session, not just a status', () => {
  it('hides the pill for a record whose session never started', () => {
    render(<AgentSessionButton {...base} record={record({ slot_key: null })} />)
    expect(screen.queryByText('Investigating')).not.toBeInTheDocument()
  })

  it('shows the pill once the record is linked to a session', () => {
    render(<AgentSessionButton {...base} record={record({ slot_key: 'chat-1' })} />)
    expect(screen.getByText('Investigating')).toBeInTheDocument()
  })

  it('still shows a finished verdict after the session is gone', () => {
    // A resolved record is exempt: the verdict is the durable result, and losing it
    // when the chat session is deleted would discard the only record of the work.
    render(
      <AgentSessionButton
        {...base}
        record={record({ slot_key: null, status: 'resolved' })}
      />,
    )
    expect(screen.getByText('Done')).toBeInTheDocument()
  })
})
