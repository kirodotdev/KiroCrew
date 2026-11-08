/**
 * Mate (the first crewmate, key `mate`) is created on every install and listed
 * like any other crewmate in every agent list `useAgents` feeds -- the chat
 * pop-up (`choices`), the name-only pickers (`agents`) and the display-only
 * tint list (`displayAgents`).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { waitFor } from '@testing-library/react'
import { renderHookWithProviders } from './helpers'
import { useAgents } from '../hooks/useAgents'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    agentCatalog: vi.fn(),
    kirocrewConfig: vi.fn(),
  },
}))

const catalog = [
  { name: 'default', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'built-in', source: 'kirocrew', selection_kind: 'member' },
  { name: 'mate', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'member-mate', description: '', source: 'builtin', selection_kind: 'member' },
  { name: 'helper', kiro_agent: 'helper-copy', workspace: 'default', memory_store: 'member-helper', description: 'mine', source: 'kirocrew', selection_kind: 'member' },
]

const names = (rows: { name: string }[]) => rows.map(r => r.name)

describe('useAgents lists Mate on a default install', () => {
  beforeEach(() => {
    vi.mocked(api.agentCatalog).mockReset().mockResolvedValue({ agents: catalog, default_agent: 'default' } as never)
    vi.mocked(api.kirocrewConfig).mockReset().mockResolvedValue({ dashboard: { crewmates_in_agent_picker: true } } as never)
  })

  it('Mate is listed like any other mate, with no opt-in', async () => {
    const { result } = renderHookWithProviders(() => useAgents(0))
    await waitFor(() => expect(names(result.current.choices)).toEqual(['default', 'mate', 'helper']))
    expect(names(result.current.agents)).toEqual(['default', 'mate', 'helper'])
    expect(names(result.current.displayAgents)).toEqual(['default', 'mate', 'helper'])
  })
})
