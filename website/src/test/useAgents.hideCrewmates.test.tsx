/**
 * While `HIDE_CREWMATE_CHOICES` is on, the chat agent pop-up offers templates
 * only: `choices` withholds every member row. The folded `agents` list is NOT
 * filtered -- cron, channel and project bindings still see every name, and a
 * bare name still resolves member-first -- so hiding a crewmate from the picker
 * can never change what a name-only consumer dispatches.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { waitFor } from '@testing-library/react'
import { renderHookWithProviders } from './helpers'
import { HIDE_CREWMATE_CHOICES, useAgents } from '../hooks/useAgents'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    agentCatalog: vi.fn(),
  },
}))

const catalog = [
  { name: 'reviewer', kiro_agent: 'reviewer', workspace: 'default', memory_store: 'member-reviewer', description: 'My reviewer', source: 'kirocrew', selection_kind: 'member' },
  { name: 'reviewer', kiro_agent: 'reviewer', workspace: 'default', memory_store: 'default', description: 'Shared reviewer', source: 'package', selection_kind: 'template' },
  { name: 'default', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'built-in', source: 'kirocrew', selection_kind: 'member' },
  { name: 'atlas', kiro_agent: 'atlas', workspace: 'default', memory_store: 'default', description: 'package agent', source: 'package', selection_kind: 'template' },
]

const agentsApi = vi.mocked(api.agentCatalog)

describe('useAgents hides crewmates from the picker while the flag is on', () => {
  beforeEach(() => {
    agentsApi.mockReset()
    agentsApi.mockResolvedValue({ agents: catalog, default_agent: 'default' } as never)
  })

  it('withholds member rows from `choices` and keeps every template', async () => {
    // The temporary hide is what this file pins; when the flag is turned off
    // the two-group behaviour is covered by AgentDropdownList.test.tsx.
    expect(HIDE_CREWMATE_CHOICES).toBe(true)

    const { result } = renderHookWithProviders(() => useAgents(0))
    await waitFor(() => expect(result.current.choices).toHaveLength(2))

    expect(result.current.choices.map(c => [c.selection_kind, c.name])).toEqual([
      ['template', 'reviewer'],
      ['template', 'atlas'],
    ])
    expect(result.current.choices.some(c => c.selection_kind === 'member')).toBe(false)
  })

  it('leaves the folded name-only `agents` list member-first and complete', async () => {
    const { result } = renderHookWithProviders(() => useAgents(0))
    await waitFor(() => expect(result.current.agents).toHaveLength(3))

    // One row per name; the member still wins the fold for a shared name, so a
    // cron or channel binding to `reviewer` dispatches exactly what it did before.
    const reviewer = result.current.agents.find(a => a.name === 'reviewer')
    expect(reviewer?.selection_kind).toBe('member')
    expect(result.current.agents.map(a => a.name)).toEqual(['reviewer', 'default', 'atlas'])
    expect(result.current.defaultAgent).toBe('default')
  })
})
