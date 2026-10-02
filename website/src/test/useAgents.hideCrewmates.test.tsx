/**
 * While `HIDE_CREWMATE_CHOICES` is on, the chat agent pop-up withholds a member
 * row only when a listed template already reaches the same binding. A crewmate
 * no template covers -- made by hand with its own memory, or running its own
 * private copy -- must stay pickable, or it cannot be chosen from a chat at all. The folded `agents` list is NOT
 * filtered -- cron, channel and project bindings still see every name, and a
 * bare name still resolves member-first -- so hiding a crewmate from the picker
 * can never change what a name-only consumer dispatches.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { waitFor } from '@testing-library/react'
import { renderHookWithProviders } from './helpers'
import { HIDE_CREWMATE_CHOICES, useAgents, withoutCoveredCrewmates } from '../hooks/useAgents'
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
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'main agent', source: 'kirocrew', selection_kind: 'template' },
  // Made by hand: its own name and its own memory, on a listed template.
  { name: 'my-helper', kiro_agent: 'atlas', workspace: 'default', memory_store: 'member-my-helper', description: 'mine', source: 'kirocrew', selection_kind: 'member' },
  // Customized: runs its own private copy, which the catalog never lists.
  { name: 'tuned', kiro_agent: 'tuned-copy', workspace: 'default', memory_store: 'member-tuned', description: 'mine', source: 'kirocrew', selection_kind: 'member' },
]

const agentsApi = vi.mocked(api.agentCatalog)

describe('useAgents hides crewmates from the picker while the flag is on', () => {
  beforeEach(() => {
    agentsApi.mockReset()
    agentsApi.mockResolvedValue({ agents: catalog, default_agent: 'default' } as never)
  })

  it('withholds only crewmates a listed template covers, and keeps every template', async () => {
    // The temporary hide is what this file pins; when the flag is turned off
    // the two-group behaviour is covered by AgentDropdownList.test.tsx.
    expect(HIDE_CREWMATE_CHOICES).toBe(true)

    const { result } = renderHookWithProviders(() => useAgents(0))
    await waitFor(() => expect(result.current.choices).toHaveLength(5))

    expect(result.current.choices.map(c => [c.selection_kind, c.name])).toEqual([
      ['template', 'reviewer'],
      ['template', 'atlas'],
      ['template', 'kirocrew'],
      ['member', 'my-helper'],
      ['member', 'tuned'],
    ])
  })

  it('withholds a same-name crewmate and an identity-less one on a listed template', () => {
    const rows = withoutCoveredCrewmates(catalog as never)
    const members = rows.filter(r => r.selection_kind === 'member').map(r => r.name)
    // `reviewer` shares its name with a template; `default` has no memory of its
    // own and runs the listed `kirocrew` template -- both are the same binding.
    expect(members).not.toContain('reviewer')
    expect(members).not.toContain('default')
  })

  it('keeps a crewmate whose own-memory binding is not a template pick', () => {
    // Without its template listed, an identity-less crewmate stays too.
    const rows = withoutCoveredCrewmates([
      { name: 'orphan', kiro_agent: 'gone', memory_store: 'default', selection_kind: 'member' },
    ] as never)
    expect(rows.map(r => r.name)).toEqual(['orphan'])
  })

  it('leaves the folded name-only `agents` list member-first and complete', async () => {
    const { result } = renderHookWithProviders(() => useAgents(0))
    await waitFor(() => expect(result.current.agents).toHaveLength(6))

    // One row per name; the member still wins the fold for a shared name, so a
    // cron or channel binding to `reviewer` dispatches exactly what it did before.
    const reviewer = result.current.agents.find(a => a.name === 'reviewer')
    expect(reviewer?.selection_kind).toBe('member')
    expect(result.current.agents.map(a => a.name)).toEqual(['reviewer', 'default', 'atlas', 'kirocrew', 'my-helper', 'tuned'])
    expect(result.current.defaultAgent).toBe('default')
  })
})
