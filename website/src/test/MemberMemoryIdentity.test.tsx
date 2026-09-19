import { describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { MemoryStoreField, memberMemoryState } from '../pages/KiroCrewAgentsPage'
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

  it('creates private memory automatically without a store picker', () => {
    renderWithProviders(<MemoryStoreField />)
    expect(screen.getByText(/empty member memory/i)).toBeInTheDocument()
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  })

  it('keeps existing V1 memory without offering database creation', () => {
    renderWithProviders(<MemoryStoreField member="reviewer" value="default" memoryState="legacy" />)
    expect(screen.getByText('default', { exact: true })).toBeVisible()
    expect(screen.getByText(/^This member keeps its current memory \(V1\)\. Member memory \(V2\) is only available when creating a new crew member\.$/)).toBeVisible()
    expect(screen.queryByRole('button', { name: /Create.*memory/i })).toBeNull()
    expect(screen.queryByRole('combobox')).toBeNull()
  })

  it('displays an immutable private identity and protects unsaved edits before navigation', () => {
    const manage = vi.fn()
    renderWithProviders(<MemoryStoreField member="reviewer" value="member-reviewer-123" memoryState="private" onManage={manage} manageDisabled />)
    expect(screen.getByText('member-reviewer-123')).toBeInTheDocument()
    const button = screen.getByRole('button', { name: 'Manage memory' })
    expect(button).toBeDisabled()
    expect(button).not.toHaveAttribute('title')
    expect(screen.getByText('Save or discard changes first', { exact: true })).toBeVisible()
    fireEvent.click(button)
    expect(manage).not.toHaveBeenCalled()
  })

  it.each([
    ['unavailable', 'This member’s configured memory store is unavailable. Inspect the cause on the gateway: kirocrew doctor'],
    ['ownership_mismatch', 'This member’s configured memory store belongs to another member. It cannot be used here. Inspect the cause on the gateway: kirocrew doctor'],
  ] as const)('does not offer V1 creation or V2 management for an %s binding', (memoryState, reason) => {
    renderWithProviders(<MemoryStoreField member="reviewer" value="missing-store" memoryState={memoryState} onManage={() => {}} />)
    expect(screen.getByText(reason, { exact: true })).toBeVisible()
    expect(screen.queryByText(/Open the crew manager/i)).toBeNull()
    expect(screen.queryByText(/unavailable or belongs/i)).toBeNull()
    expect(screen.queryByText(/This member keeps its current memory \(V1\)\. Member memory \(V2\) is only available when creating a new crew member\./)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Create member memory' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Manage memory' })).toBeNull()
  })
})

describe('scheduled member identity', () => {
  it('preserves legacy display attribution while private jobs follow their durable member', () => {
    const legacy = { agent: 'reviewer' } as CronJob
    // Displaying a legacy schedule beside its agent does not grant V2 memory.
    expect(wakesCrew(legacy, 'reviewer', true)).toBe(true)
    expect(wakesCrew(legacy, 'default', false)).toBe(false)
    const unbound = { agent: '' } as CronJob
    expect(wakesCrew(unbound, 'default', true)).toBe(true)
    expect(wakesCrew(unbound, 'reviewer', false)).toBe(false)
    const member = { agent: 'shared-template', member_id: 'reviewer' } as CronJob
    expect(wakesCrew(member, 'reviewer', false)).toBe(true)
    expect(wakesCrew(member, 'shared-template', false)).toBe(false)
    expect(wakesCrew(member, 'default', true)).toBe(false)
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
