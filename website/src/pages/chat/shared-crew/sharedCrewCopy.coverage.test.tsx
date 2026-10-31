import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { OPERATION_TRANSITIONS, TERMINAL_STATES, isLegalTransition, type CapacityResponse, type SessionAttribution } from '../../../api/sharedCrew'
import { queuedReason, refusalMessage } from './overloadCopy'
import { SessionCreatorByline, SharedCrewBylineContext, creatorLabel } from '../../chat-sidebar/SessionCreatorByline'

const capacity: CapacityResponse = {
  schema_version: 1,
  principal: { active: 2, queued: 1, active_limit: 2, queue_limit: 3 },
  crew: { active: 8, queued: 4, active_limit: 8, queue_limit: 4 },
  enforcement: 'hard', limiting_scope: 'principal', reason: null, retry_after_seconds: 5,
  queue_expiry_seconds: 600, run_deadline_seconds: null,
}

describe('refusalMessage', () => {
  it.each(['principal_active_limit', 'principal_queue_full', 'crew_active_limit', 'crew_queue_full'] as const)(
    'names whose limit %s is, with the counts when they are known and without them otherwise',
    code => {
      const withCounts = refusalMessage({ code, capacity })
      const without = refusalMessage({ code })
      expect(withCounts).toBeTruthy()
      expect(without).toBeTruthy()
      expect(withCounts).not.toBe(without)
      expect(withCounts).toMatch(/\d/)
    },
  )

  it.each(['session_create_rate', 'request_too_large', 'storage_limit', 'containment_unavailable'] as const)(
    'has a line for %s', code => {
      expect(refusalMessage({ code })).toBeTruthy()
    },
  )

  it('returns null for a body that is not a Shared Crew refusal', () => {
    expect(refusalMessage({ code: 'something_else' })).toBeNull()
    expect(refusalMessage({})).toBeNull()
  })
})

describe('queuedReason', () => {
  it('says whose limit holds the queued item', () => {
    const personal = queuedReason('principal_active_limit', capacity)
    const crew = queuedReason('crew_active_limit', { ...capacity, limiting_scope: 'crew' })
    const generic = queuedReason(null, null)
    expect(new Set([personal, crew, generic]).size).toBe(3)
  })
})

describe('operation transitions', () => {
  it('allows only the listed transitions, and none out of a terminal state', () => {
    for (const [from, tos] of Object.entries(OPERATION_TRANSITIONS)) {
      for (const to of tos) expect(isLegalTransition(from as never, to)).toBe(true)
    }
    for (const state of TERMINAL_STATES) expect(OPERATION_TRANSITIONS[state]).toEqual([])
    expect(isLegalTransition('completed', 'running')).toBe(false)
  })
})

const human = (id: string, name: string) => ({ id, kind: 'human' as const, display: { name, login: '', captured_at: null } })

describe('SessionCreatorByline', () => {
  const bob: SessionAttribution = { creator: human('p_bob', 'Bob'), trigger: null } as unknown as SessionAttribution

  it('names the creator, and reads You for the viewer', () => {
    render(<SharedCrewBylineContext.Provider value={{ hidden: false, viewerId: 'p_me' }}><SessionCreatorByline attribution={bob} /></SharedCrewBylineContext.Provider>)
    expect(screen.getByTestId('session-creator-byline')).toHaveTextContent('Bob')
    expect(creatorLabel(bob, 'p_bob')).not.toBe(creatorLabel(bob, 'p_me'))
  })

  it('draws nothing on a personal Crew or without attribution', () => {
    const { container } = render(
      <SharedCrewBylineContext.Provider value={{ hidden: true, viewerId: '' }}>
        <SessionCreatorByline attribution={bob} />
        <SessionCreatorByline attribution={null} />
      </SharedCrewBylineContext.Provider>,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('says started by for unattended work', () => {
    const scheduled = { creator: { id: 'p_cron', kind: 'service', display: { name: 'nightly', login: '', captured_at: null } }, trigger: { kind: 'scheduled', name: 'nightly' } } as unknown as SessionAttribution
    render(<SessionCreatorByline attribution={scheduled} />)
    expect(screen.getByTestId('session-creator-byline').textContent).toBeTruthy()
  })
})
