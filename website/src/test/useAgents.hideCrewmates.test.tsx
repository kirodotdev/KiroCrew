/**
 * Unless the gateway's `dashboard.crewmates_in_agent_picker` config (read from
 * the shared `['kirocrewConfig']` query) is `true`, the chat agent pop-up
 * withholds a member row only when a listed template already reaches the same
 * binding. A crewmate no template covers -- made by hand with its own memory, or
 * running its own private copy -- must stay pickable, or it cannot be chosen from
 * a chat at all. With the key on, every member is listed. Either way the folded
 * `agents` list is NOT filtered -- cron, channel and project bindings still see
 * every name, and a bare name still resolves member-first -- so the picker
 * setting can never change what a name-only consumer dispatches.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { waitFor } from '@testing-library/react'
import { renderHookWithProviders } from './helpers'
import { useAgents, withoutCoveredCrewmates } from '../hooks/useAgents'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    agentCatalog: vi.fn(),
    kirocrewConfig: vi.fn(),
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

const COVERED_HIDDEN = [
  ['member', 'reviewer'],
  ['template', 'reviewer'],
  ['template', 'atlas'],
  ['template', 'kirocrew'],
  ['member', 'my-helper'],
  ['member', 'tuned'],
]

const agentsApi = vi.mocked(api.agentCatalog)
const configApi = vi.mocked(api.kirocrewConfig)

describe('useAgents keeps covered crewmates out of the picker unless the config allows them', () => {
  beforeEach(() => {
    agentsApi.mockReset()
    configApi.mockReset()
    agentsApi.mockResolvedValue({ agents: catalog, default_agent: 'default' } as never)
  })

  it.each([
    ['absent', {}],
    ['false', { dashboard: { crewmates_in_agent_picker: false } }],
    ['a non-boolean', { dashboard: { crewmates_in_agent_picker: 'yes' } }],
  ])('withholds only covered crewmates from `choices` when the key is %s', async (_label, cfg) => {
    configApi.mockResolvedValue(cfg as never)
    const { result } = renderHookWithProviders(() => useAgents(0))
    await waitFor(() => expect(configApi).toHaveBeenCalled())
    await waitFor(() => expect(result.current.choices).toHaveLength(6))

    expect(result.current.choices.map(c => [c.selection_kind, c.name])).toEqual(COVERED_HIDDEN)
  })

  it('withholds only covered crewmates from `choices` when the config read fails', async () => {
    configApi.mockRejectedValue(new Error('offline'))
    const { result } = renderHookWithProviders(() => useAgents(0))
    await waitFor(() => expect(configApi).toHaveBeenCalled())
    await waitFor(() => expect(result.current.agents).toHaveLength(6))
    expect(result.current.choices.map(c => [c.selection_kind, c.name])).toEqual(COVERED_HIDDEN)
  })

  it('offers every member and template in `choices` when the key is true', async () => {
    configApi.mockResolvedValue({ dashboard: { crewmates_in_agent_picker: true } } as never)
    const { result } = renderHookWithProviders(() => useAgents(0))
    await waitFor(() => expect(result.current.choices).toHaveLength(7))

    expect(result.current.choices.map(c => [c.selection_kind, c.name])).toEqual(
      catalog.map(c => [c.selection_kind, c.name]),
    )
  })

  it('withholds an identity-less crewmate on a listed template', () => {
    const rows = withoutCoveredCrewmates(catalog as never)
    const members = rows.filter(r => r.selection_kind === 'member').map(r => r.name)
    // `default` has no memory of its own and runs the listed `kirocrew`
    // template -- picking that template is the same binding.
    expect(members).not.toContain('default')
  })

  it('keeps a same-name crewmate that has its own memory', () => {
    // A template pick binds the shared `default` store, so the `reviewer`
    // template does not reach the `reviewer` crewmate's own memory.
    const rows = withoutCoveredCrewmates(catalog as never)
    expect(rows.filter(r => r.name === 'reviewer').map(r => r.selection_kind)).toEqual(['member', 'template'])
  })

  it('withholds a same-name crewmate with no memory of its own', () => {
    const rows = withoutCoveredCrewmates([
      { name: 'shared', kiro_agent: 'shared', memory_store: 'default', selection_kind: 'member' },
      { name: 'shared', kiro_agent: 'shared', memory_store: 'default', selection_kind: 'template' },
    ] as never)
    expect(rows.map(r => r.selection_kind)).toEqual(['template'])
  })

  it('keeps a crewmate whose own-memory binding is not a template pick', () => {
    // Without its template listed, an identity-less crewmate stays too.
    const rows = withoutCoveredCrewmates([
      { name: 'orphan', kiro_agent: 'gone', memory_store: 'default', selection_kind: 'member' },
    ] as never)
    expect(rows.map(r => r.name)).toEqual(['orphan'])
  })

  it.each([false, true])('leaves the folded name-only `agents` list member-first and complete (key=%s)', async (enabled) => {
    configApi.mockResolvedValue({ dashboard: { crewmates_in_agent_picker: enabled } } as never)
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
