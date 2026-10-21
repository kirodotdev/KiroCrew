import { describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { memberMemoryState } from '../pages/KiroCrewAgentsPage'
import JobForm from '../components/JobForm'
import { wakesCrew } from '../components/crew/wakesCrew'
import type { CronJob } from '../types'

const calls = vi.hoisted(() => ({ update: vi.fn() }))
vi.mock('../api/client', () => ({ api: {
  models: vi.fn().mockResolvedValue([]),
  updateCron: calls.update,
} }))

describe('private member memory controls', () => {
  it('does not label a lost or mismatched private pointer as usable V1', () => {
    const stores = {
      default: {},
      'reviewer-private': { memory_version: 2, owner_member: 'reviewer' },
      'reviewer-archived': { memory_version: 2, owner_member: 'reviewer' },
      'writer-private': { memory_version: 2, owner_member: 'writer' },
      'legacy-notes': { memory_version: 1 },
    }
    expect(memberMemoryState('reviewer', 'default', stores)).toBe('unavailable')
    expect(memberMemoryState('reviewer', 'legacy-notes', stores)).toBe('unavailable')
    expect(memberMemoryState('reviewer', 'writer-private', stores)).toBe('ownership_mismatch')
    expect(memberMemoryState('reviewer', 'reviewer-private', stores)).toBe('private')
    expect(memberMemoryState('old-member', 'missing', stores)).toBe('unavailable')
    expect(memberMemoryState('old-member', 'legacy-notes', stores)).toBe('legacy')
    expect(memberMemoryState('old-member', 'default', stores)).toBe('legacy')
    expect(memberMemoryState('old-member', 'ownerless-private', {
      'ownerless-private': { memory_version: 2 },
    })).toBe('unavailable')
  })
})

describe('scheduled member identity', () => {
  it('preserves legacy display attribution while private jobs follow their durable member', () => {
    // The legacy branches compare the DISPLAY name, because `agent` and
    // `agent_sequence` hold template and crew names; a private job compares the
    // IMMUTABLE id. The two arguments are the same here only because these fixtures
    // use a name that is already its own slug.
    const legacy = { agent: 'reviewer' } as CronJob
    // Displaying a legacy schedule beside its agent does not grant V2 memory.
    expect(wakesCrew(legacy, 'reviewer', true, 'reviewer')).toBe(true)
    expect(wakesCrew(legacy, 'default', false, 'default')).toBe(false)
    const unbound = { agent: '' } as CronJob
    expect(wakesCrew(unbound, 'default', true, 'default')).toBe(true)
    expect(wakesCrew(unbound, 'reviewer', false, 'reviewer')).toBe(false)
    const member = { agent: 'shared-template', member_id: 'reviewer' } as CronJob
    expect(wakesCrew(member, 'reviewer', false, 'reviewer')).toBe(true)
    expect(wakesCrew(member, 'shared-template', false, 'shared-template')).toBe(false)
    expect(wakesCrew(member, 'default', true, 'default')).toBe(false)
  })

  it('reads a private job by its immutable id even when the display name differs', () => {
    // The case every fixture above misses, and the one that shipped broken: a crewmate
    // displayed as "Radar One" whose allocated id is `radar-one`. The persisted
    // `member_id` is the id, so matching on the name finds nothing.
    const job = { agent: 'shared-template', member_id: 'radar-one' } as CronJob
    expect(wakesCrew(job, 'Radar One', false, 'radar-one')).toBe(true)
    expect(wakesCrew(job, 'Radar One', false, 'Radar One')).toBe(false)
    // And the legacy branches still read the NAME, not the id.
    const legacy = { agent: 'Radar One' } as CronJob
    expect(wakesCrew(legacy, 'Radar One', false, 'radar-one')).toBe(true)
  })

  it('keeps the member immutable while editing a scheduled task', async () => {
    calls.update.mockResolvedValue({})
    const job = { id: 'job-one', name: 'Review', message: 'Review changes', agent: 'shared-template', member_id: 'reviewer', enabled: true, schedule: 'every 1h' } as CronJob
    renderWithProviders(<JobForm job={job} agents={[]} defaultAgent="default" onSaved={() => {}} />)
    expect(screen.getByTestId('jobform-locked-agent')).toHaveTextContent('reviewer')
    expect(screen.queryByLabelText('Switch agent')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Save/i }))
    await waitFor(() => expect(calls.update).toHaveBeenCalledWith('job-one', expect.objectContaining({ member_id: 'reviewer', agent: 'shared-template' })))
  })
})
