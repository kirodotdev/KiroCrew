import { describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
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
    expect(screen.getByText(/own empty private memory/i)).toBeInTheDocument()
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  })

  it('keeps a legacy member on usable V1 until its owner confirms creating V2', async () => {
    const initialize = vi.fn()
    renderWithProviders(<MemoryStoreField member="reviewer" value="default" memoryState="legacy" onInitialize={initialize} />)
    expect(screen.getByText('default', { exact: true })).toBeVisible()
    const hint = screen.getByText(/This member uses its current memory \(V1\)\./)
    expect(hint).toHaveTextContent(/^This member uses its current memory \(V1\)\.$/)
    expect(screen.queryByText(/This member cannot return to its previous memory/)).toBeNull()
    expect(initialize).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Create private memory', exact: true }))
    const confirmation = await screen.findByRole('dialog', { name: 'Create private memory' })
    await waitFor(() => expect(within(confirmation).getByText(/This member cannot return to its previous memory/)).toBeVisible())
    expect(confirmation).toHaveTextContent('Private memory (V2) starts empty in a new chat')
    expect(confirmation).toHaveTextContent('Existing data and chats stay')
    expect(initialize).not.toHaveBeenCalled()
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Create private memory' })).toBeNull())
    expect(initialize).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Create private memory', exact: true }))
    const reopened = await screen.findByRole('dialog', { name: 'Create private memory' })
    fireEvent.click(within(reopened).getByRole('button', { name: 'Create private memory' }))
    expect(initialize).toHaveBeenCalledOnce()
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
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

  it('drops an open confirmation when the member identity changes', async () => {
    const initialize = vi.fn()
    const view = renderWithProviders(<MemoryStoreField member="reviewer" value="default" memoryState="legacy" onInitialize={initialize} />)
    fireEvent.click(screen.getByRole('button', { name: 'Create private memory' }))
    await screen.findByRole('dialog', { name: 'Create private memory' })
    view.rerender(<MemoryStoreField member="writer" value="default" memoryState="legacy" onInitialize={initialize} />)
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Create private memory' })).toBeNull())
    expect(initialize).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Create private memory' })).toBeEnabled()
  })

  it('announces only the private-memory request that is still in progress', () => {
    const view = renderWithProviders(<MemoryStoreField member="reviewer" value="default" memoryState="legacy" onInitialize={() => {}} busy />)
    expect(screen.queryByText('Creating private memory…')).toBeNull()
    view.rerender(<MemoryStoreField member="reviewer" value="default" memoryState="legacy" onInitialize={() => {}} busy initializing />)
    expect(screen.getByRole('status')).toHaveTextContent('Creating private memory…')
    view.rerender(<MemoryStoreField member="reviewer" value="member-reviewer-123" memoryState="private" busy initializing />)
    expect(screen.getByRole('status')).toHaveTextContent('Creating private memory…')
    view.rerender(<MemoryStoreField member="writer" value="default" memoryState="legacy" onInitialize={() => {}} busy />)
    expect(screen.queryByText('Creating private memory…')).toBeNull()
  })

  it.each([
    ['unavailable', 'This member’s configured memory store is unavailable. Inspect the cause on the gateway: kirocrew doctor'],
    ['ownership_mismatch', 'This member’s configured memory store belongs to another member. It cannot be used here. Inspect the cause on the gateway: kirocrew doctor'],
  ] as const)('does not offer V1 creation or V2 management for an %s binding', (memoryState, reason) => {
    renderWithProviders(<MemoryStoreField member="reviewer" value="missing-store" memoryState={memoryState} onInitialize={() => {}} onManage={() => {}} />)
    expect(screen.getByText(reason, { exact: true })).toBeVisible()
    expect(screen.queryByText(/Open the crew manager/i)).toBeNull()
    expect(screen.queryByText(/unavailable or belongs/i)).toBeNull()
    expect(screen.queryByText(/This member uses its current memory \(V1\)\./)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Create private memory' })).toBeNull()
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
