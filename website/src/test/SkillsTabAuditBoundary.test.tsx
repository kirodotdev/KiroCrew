import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

/* ── Mocks: must run before importing the component ── */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  skill: vi.fn(),
  skillsAudit: vi.fn(),
  skillsPending: vi.fn(),
  skillPendingDetail: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi, ApiError: class ApiError extends Error {} }))
vi.mock('../providers', () => ({
  useProvider: () => ({ labels: { pluginRegistryName: 'Packages' } }),
}))
// Stands in for a stale lazy chunk: the modal fails the moment it renders.
vi.mock('../pages/overview/SkillsAuditModal', () => ({
  default: () => {
    throw new Error('Failed to fetch dynamically imported module')
  },
}))

import SkillsTab from '../pages/overview/SkillsTab'

// The audit modal sits behind SkillsTab's retryableLazy boundary
// (pages/overview/SkillsAuditModal.tsx). A cold chunk import on a loaded CI
// shard can outlast findBy*/waitFor's 1 s default, so every wait for the
// modal, its query or its contents names this timeout.
const LAZY_AUDIT_MOUNT = { timeout: 5000 }

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.skills.mockResolvedValue([])
  mockApi.skillsPending.mockResolvedValue({ pending: [] })
  mockApi.skillsAudit.mockResolvedValue({ clusters: [] })
})

describe('SkillsTab audit modal crash isolation', () => {
  it('keeps the Skills tab when the lazy audit modal fails to load', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter><SkillsTab /></MemoryRouter>
      </QueryClientProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: 'Find overlapping skills' }))

    expect(await screen.findByText('Something went wrong', undefined, LAZY_AUDIT_MOUNT)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Find overlapping skills' })).toBeTruthy()
  })

  it('offers only Retry when the modal fails while a draft is open', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    mockApi.skills.mockResolvedValue([
      { key: 'deploy-one', name: 'deploy-one', description: 'first', source: 'kirocrew', loaded_by_agents: [] },
    ])
    mockApi.skill.mockResolvedValue({
      name: 'deploy-one',
      content: '---\nname: deploy-one\ndescription: first\n---\nbody text',
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter><SkillsTab /></MemoryRouter>
      </QueryClientProvider>,
    )
    const editBtn = await screen.findByText('Edit')
    await waitFor(() => expect(editBtn).not.toBeDisabled())
    fireEvent.click(editBtn)
    await waitFor(() => expect(screen.getByText('Save')).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: 'Find overlapping skills' }))

    expect(await screen.findByText('Something went wrong', undefined, LAZY_AUDIT_MOUNT)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /agent/i })).toBeNull()
    expect(screen.queryByRole('link', { name: /agent/i })).toBeNull()
    expect(screen.getByText('Save')).toBeTruthy()
  })
})
