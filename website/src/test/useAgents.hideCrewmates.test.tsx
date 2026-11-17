/**
 * Unless the gateway's `dashboard.crewmates_in_agent_picker` config (read from
 * the shared `['kirocrewConfig']` query) is `true`, the chat agent pop-up
 * withholds a member row only when a listed template already reaches the same
 * binding. A crewmate no template covers -- made by hand with its own memory, or
 * running its own private copy -- must stay pickable, or it cannot be chosen from
 * a chat at all. No crewmate is exempt for being the default: a new session runs
 * the default template, whose own row is the way back. With the key on,
 * every member is listed. Either way the folded
 * `agents` list is NOT filtered -- cron, channel and project bindings still see
 * every name, and a bare name still resolves member-first -- so the picker
 * setting can never change what a name-only consumer dispatches.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { waitFor } from '@testing-library/react'
import { renderHookWithProviders } from './helpers'
import { useAgents, withoutCoveredCrewmates } from '../hooks/useAgents'
import { defaultWriteKind, isCrewmateChat } from '../components/AgentDropdownList'
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
  // The stock shape the loader seeds: a template, a workspace and a store, nothing else.
  { name: 'default', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: '', source: 'kirocrew', selection_kind: 'member' },
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
  // The built-in default crew is covered by the listed `kirocrew` template and
  // is withheld like any covered crewmate: a new session runs the default
  // TEMPLATE, whose own row is the way back from any switch.
  ['template', 'atlas'],
  ['template', 'kirocrew'],
  ['member', 'my-helper'],
  ['member', 'tuned'],
]

const agentsApi = vi.mocked(api.agentCatalog)
const configApi = vi.mocked(api.kirocrewConfig)

describe('useAgents keeps covered mates out of the picker unless the config allows them', () => {
  beforeEach(() => {
    agentsApi.mockReset()
    configApi.mockReset()
    agentsApi.mockResolvedValue({ agents: catalog, default_agent: 'kirocrew' } as never)
  })

  it.each([
    ['absent', {}],
    ['false', { dashboard: { crewmates_in_agent_picker: false } }],
    ['a non-boolean', { dashboard: { crewmates_in_agent_picker: 'yes' } }],
  ])('withholds only covered mates from `choices` when the key is %s', async (_label, cfg) => {
    configApi.mockResolvedValue(cfg as never)
    const { result } = renderHookWithProviders(() => useAgents(0))
    await waitFor(() => expect(configApi).toHaveBeenCalled())
    await waitFor(() => expect(result.current.choices).toHaveLength(COVERED_HIDDEN.length))

    expect(result.current.choices.map(c => [c.selection_kind, c.name])).toEqual(COVERED_HIDDEN)
  })

  it('withholds only covered mates from `choices` when the config read fails', async () => {
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

  it('withholds an identity-less mate on a listed template', () => {
    // `default` has no memory of its own and runs the listed `kirocrew`
    // template -- the same binding -- and no crewmate is exempt for being "the
    // default": a new session runs the default TEMPLATE, whose own row is the
    // way back from any switch.
    const rows = withoutCoveredCrewmates(catalog as never)
    const members = rows.filter(r => r.selection_kind === 'member').map(r => r.name)
    expect(members).not.toContain('default')
  })

  it('keeps a same-name mate that has its own memory', () => {
    // A template pick binds the shared `default` store, so the `reviewer`
    // template does not reach the `reviewer` crewmate's own memory.
    const rows = withoutCoveredCrewmates(catalog as never)
    expect(rows.filter(r => r.name === 'reviewer').map(r => r.selection_kind)).toEqual(['member', 'template'])
  })

  it('withholds a same-name mate with no memory of its own', () => {
    const rows = withoutCoveredCrewmates([
      { name: 'shared', kiro_agent: 'shared', memory_store: 'default', selection_kind: 'member' },
      { name: 'shared', kiro_agent: 'shared', memory_store: 'default', selection_kind: 'template' },
    ] as never)
    expect(rows.map(r => r.selection_kind)).toEqual(['template'])
  })

  describe('a mate on the shared store and a listed template', () => {
    const template = { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'main agent', source: 'kirocrew', selection_kind: 'template' }
    const mate = (name: string, own: Record<string, unknown> = {}) => ({
      name, kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: '', source: 'kirocrew', selection_kind: 'member', ...own,
    })
    // An install upgraded from shared memory: three mates set up for their own
    // work beside the stock `default`, which has nothing of its own. `tracker`
    // keeps the default workspace and its row carries nothing; its permanent
    // rules and briefing are on disk, which the server reports as `own_setup`.
    const upgraded = [
      template,
      mate('default', { own_setup: false }),
      mate('reports', { workspace: 'reports', description: 'Builds the weekly reports', own_setup: false }),
      mate('review-drafts', { workspace: 'review-drafts' }),
      mate('tracker', { own_setup: true }),
    ]

    it('offers the mates with a setup of their own and withholds the stock one', async () => {
      agentsApi.mockResolvedValue({ agents: upgraded, default_agent: 'kirocrew' } as never)
      configApi.mockResolvedValue({ default_workspace: 'default' } as never)
      const { result } = renderHookWithProviders(() => useAgents(0))
      await waitFor(() => expect(result.current.choices).toHaveLength(4))

      // A template pick runs on the default workspace with no identity or rules,
      // so it does not reach a mate's own workspace, description or rules.
      expect(result.current.choices.map(c => [c.selection_kind, c.name])).toEqual([
        ['template', 'kirocrew'],
        ['member', 'reports'],
        ['member', 'review-drafts'],
        ['member', 'tracker'],
      ])
      expect(result.current.displayChoices.map(c => c.name)).toEqual(['kirocrew', 'reports', 'review-drafts', 'tracker'])
    })

    it.each([
      ['workspace', { workspace: 'reports' }],
      ['model', { model: 'some-model' }],
      ['reasoning effort', { reasoning_effort: 'high' }],
      ['display name', { display_name: 'Reports' }],
      ['triggers', { triggers: 'weekly report' }],
      ['description', { description: 'Builds the weekly reports' }],
      ['rules, goals or briefing, as the server reports them', { own_setup: true }],
    ])('keeps a mate with its own %s', (_label, own) => {
      const rows = withoutCoveredCrewmates([template, mate('mine', own)] as never)
      expect(rows.map(r => [r.selection_kind, r.name])).toEqual([['template', 'kirocrew'], ['member', 'mine']])
    })

    it.each([
      ['nothing of its own', {}],
      ['the template\'s own description (a synced template)', { description: 'main agent' }],
      ['the "auto" model, which pins nothing', { model: 'auto' }],
      ['blank text only', { description: '  ', triggers: ' ', display_name: '' }],
      ['no other setup by the server\'s count', { own_setup: false }],
    ])('withholds a mate with %s', (_label, own) => {
      const rows = withoutCoveredCrewmates([template, mate('same', own)] as never)
      expect(rows.map(r => r.selection_kind)).toEqual(['template'])
    })

    it('reads the workspace a template pick runs on from `default_workspace`', async () => {
      agentsApi.mockResolvedValue({ agents: [template, mate('default'), mate('on-main', { workspace: 'main' })], default_agent: 'kirocrew' } as never)
      configApi.mockResolvedValue({ default_workspace: 'main' } as never)
      const { result } = renderHookWithProviders(() => useAgents(0))
      await waitFor(() => expect(result.current.choices.map(c => c.name)).toEqual(['kirocrew', 'default']))
    })
  })

  it('keeps a mate whose own-memory binding is not a template pick', () => {
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
    expect(result.current.defaultAgent).toBe('kirocrew')
  })

  describe('the "default for new sessions" row on a chat of unknown kind', () => {
    const roster = [
      { name: 'research', selection_kind: 'member' },
      { name: 'atlas', selection_kind: 'template' },
    ] as never

    it('is a mate chat when the slot says member, or a kind-less slot names a mate', () => {
      expect(isCrewmateChat('member', 'atlas', roster)).toBe(true)
      // Saved before kinds existed: the roster decides, so a crewmate's chat
      // never offers to make a crewmate the default custom agent.
      expect(isCrewmateChat('', 'research', roster)).toBe(true)
      expect(isCrewmateChat(undefined, 'research', roster)).toBe(true)
    })

    it('is a template chat when the slot says template, or a kind-less slot names no mate', () => {
      expect(isCrewmateChat('template', 'research', roster)).toBe(false)
      expect(isCrewmateChat('', 'atlas', roster)).toBe(false)
      expect(isCrewmateChat(undefined, 'unknown', roster)).toBe(false)
    })

    it('writes in the template namespace only when the slot is stamped template', () => {
      // A kind-less write lets the server route the name: an alias stays a
      // roster-default write, never a same-named template behind its back.
      expect(defaultWriteKind('template')).toBe('template')
      expect(defaultWriteKind('')).toBeUndefined()
      expect(defaultWriteKind(undefined)).toBeUndefined()
    })
  })
})
