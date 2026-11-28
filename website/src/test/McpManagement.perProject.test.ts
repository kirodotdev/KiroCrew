import { describe, expect, it } from 'vitest'

import { ApiError, type McpManagedServer } from '../api/client'
import {
  autoShareCandidates,
  detailsWhyKey,
  rowState,
  stateLabelKey,
  stubErrorKey,
} from '../pages/settings/mcpManagementModel'

function server(over: Partial<McpManagedServer> = {}): McpManagedServer {
  return {
    name: 'files',
    stub: false,
    can_stub: true,
    in_allowlist: false,
    entry_poolable: false,
    agents: ['a'],
    transport: 'stdio',
    denylisted: false,
    ...over,
  } as McpManagedServer
}

describe('a per-project server', () => {
  it('reads shared in project while sharing is on, whatever the Share mark says', () => {
    for (const stub of [false, true]) {
      const s = server({ per_project: true, stub, in_allowlist: stub })
      expect(rowState(s, true)).toBe('project')
      expect(stateLabelKey(rowState(s, true))).toBe('pages.mcpManagement.state_project')
      expect(detailsWhyKey(s, 'project')).toBe('pages.mcpManagement.why_project')
    }
  })

  it('reads direct with sharing off, and says it shares once sharing is on', () => {
    const s = server({ per_project: true })
    expect(rowState(s, false)).toBe('direct')
    expect(detailsWhyKey(s, 'direct')).toBe('pages.mcpManagement.why_project_off')
  })

  it('reads one per chat with Share off, and says each chat launches its own', () => {
    const s = server({ per_project: true, declined: true })
    expect(rowState(s, true)).toBe('direct')
    expect(detailsWhyKey(s, 'direct')).toBe('pages.mcpManagement.why_dont_share')
  })

  it('reads one per chat, with the env reason, when a declared key keeps it per chat', () => {
    const s = server({ per_project: true, pooling_blocked_by_env: true })
    expect(rowState(s, true)).toBe('direct_env')
    expect(stateLabelKey(rowState(s, true))).toBe('pages.mcpManagement.state_direct')
    expect(detailsWhyKey(s, 'direct_env')).toBe('pages.mcpManagement.state_direct_env_reason')
  })

  it('is never a Share candidate, since it needs no mark', () => {
    expect(autoShareCandidates([server({ per_project: true }), server({ name: 'free' })]).map(s => s.name)).toEqual([
      'free',
    ])
  })

  it('names its own refusal of the Share press', () => {
    const err = new ApiError(409, 'shared per project', '{"code":"per_project"}')
    expect(stubErrorKey(err)).toBe('pages.mcpManagement.stub_error_per_project')
    const perChat = new ApiError(409, 'per chat', '{"code":"per_chat"}')
    expect(stubErrorKey(perChat)).toBe('pages.mcpManagement.stub_error_per_chat')
  })
})
