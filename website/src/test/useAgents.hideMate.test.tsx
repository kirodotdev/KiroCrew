/**
 * Mate (the first crewmate, key `mate`) is created in the background on every
 * install, but nothing about it is shown until the Crewmates preview is on. So
 * every agent list `useAgents` feeds -- the chat pop-up (`choices`), the
 * name-only pickers (`agents`) and the display-only tint list (`displayAgents`)
 * -- leaves it out while the preview is off, and lists it once it is on.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { waitFor } from '@testing-library/react'
import { renderHookWithProviders } from './helpers'
import { useAgents } from '../hooks/useAgents'
import { api } from '../api/client'
import { PREVIEW_CREW } from '../utils/previewFlags'

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

describe('useAgents keeps Mate out of every agent list while the Crewmates preview is off', () => {
  beforeEach(() => {
    vi.mocked(api.agentCatalog).mockReset().mockResolvedValue({ agents: catalog, default_agent: 'default' } as never)
    vi.mocked(api.kirocrewConfig).mockReset().mockResolvedValue({ dashboard: { crewmates_in_agent_picker: true } } as never)
  })
  afterEach(() => localStorage.removeItem(PREVIEW_CREW))

  it('preview off: no list offers Mate', async () => {
    const { result } = renderHookWithProviders(() => useAgents(0))
    await waitFor(() => expect(result.current.agents.length).toBeGreaterThan(0))
    await waitFor(() => expect(names(result.current.choices)).toEqual(['default', 'helper']))
    expect(names(result.current.agents)).toEqual(['default', 'helper'])
    expect(names(result.current.displayAgents)).toEqual(['default', 'helper'])
  })

  it('preview on: Mate is listed like any other crewmate', async () => {
    localStorage.setItem(PREVIEW_CREW, '1')
    const { result } = renderHookWithProviders(() => useAgents(0))
    await waitFor(() => expect(names(result.current.choices)).toEqual(['default', 'mate', 'helper']))
    expect(names(result.current.agents)).toEqual(['default', 'mate', 'helper'])
  })
})
