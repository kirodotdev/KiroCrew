import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../api/client', () => ({
  api: {
    kirocrewConfig: vi.fn(),
    kiroHooks: vi.fn(),
    capabilityAgentsList: vi.fn(),
    capabilitySkillsList: vi.fn(),
    capabilityMcpList: vi.fn(),
    capabilityAgentsInstall: vi.fn(),
    capabilitySkillsInstall: vi.fn(),
    capabilityMcpInstall: vi.fn(),
    capabilityAgentsUninstall: vi.fn(),
    capabilitySkillsUninstall: vi.fn(),
    capabilityMcpUninstall: vi.fn(),
  },
}))

import { api } from '../api/client'
import { AcpAdapter } from '../providers/adapters/acp'

const m = (fn: unknown) => fn as ReturnType<typeof vi.fn>

beforeEach(() => {
  vi.clearAllMocks()
})

describe('AcpAdapter.resolveAgentTemplate', () => {
  it('uses the bound kiro agent template when one is set', () => {
    expect(new AcpAdapter().resolveAgentTemplate({ name: 'oncall', kiro_agent: 'sre-template' } as never)).toBe('sre-template')
  })

  it('falls back to the Kiro Crew agent name when no template is bound', () => {
    expect(new AcpAdapter().resolveAgentTemplate({ name: 'oncall' } as never)).toBe('oncall')
  })
})

describe('AcpAdapter.resolveDefaultModel', () => {
  it('returns the configured default model', async () => {
    m(api.kirocrewConfig).mockResolvedValue({ agent: { model: 'claude-opus-5' } })
    expect(await new AcpAdapter().resolveDefaultModel()).toBe('claude-opus-5')
  })

  it('treats "auto" as no explicit default', async () => {
    m(api.kirocrewConfig).mockResolvedValue({ agent: { model: 'auto' } })
    expect(await new AcpAdapter().resolveDefaultModel()).toBe('')
  })

  it('returns an empty default when the config read fails', async () => {
    m(api.kirocrewConfig).mockRejectedValue(new Error('gateway down'))
    expect(await new AcpAdapter().resolveDefaultModel()).toBe('')
  })
})

describe('AcpAdapter.fetchProviderHooks', () => {
  it('groups each hook under its event with the event attached', async () => {
    m(api.kiroHooks).mockResolvedValue({
      hooks: { preToolUse: [{ command: 'lint', matcher: 'write', source: 'workspace' }] },
    })
    expect(await new AcpAdapter().fetchProviderHooks()).toEqual({
      preToolUse: [{ event: 'preToolUse', command: 'lint', matcher: 'write', source: 'workspace' }],
    })
  })

  it('returns an empty map when the gateway reports no hooks', async () => {
    m(api.kiroHooks).mockResolvedValue({})
    expect(await new AcpAdapter().fetchProviderHooks()).toEqual({})
  })
})

describe('AcpAdapter.fetchUnsentProviderHooks', () => {
  it('maps each unsent hook, defaulting missing fields to empty strings', async () => {
    m(api.kiroHooks).mockResolvedValue({
      unsent: [{ name: 'notify', trigger: 'stop', action_type: 'command', command: 'say', reason: 'too long' }],
    })
    expect(await new AcpAdapter().fetchUnsentProviderHooks()).toEqual([
      { name: 'notify', trigger: 'stop', matcher: '', actionType: 'command', command: 'say', reason: 'too long' },
    ])
  })

  it('reads an older gateway that sends no unsent list as nothing left out', async () => {
    m(api.kiroHooks).mockResolvedValue({})
    expect(await new AcpAdapter().fetchUnsentProviderHooks()).toEqual([])
  })
})

describe('AcpAdapter.listPlugins', () => {
  it('lists agents, skills and MCP servers with their package ids', async () => {
    m(api.capabilityAgentsList).mockResolvedValue([{ name: 'a1', package: 'pkg-a', version: '1.0' }])
    m(api.capabilitySkillsList).mockResolvedValue([{ name: 's1', version: '2.0' }])
    m(api.capabilityMcpList).mockResolvedValue([{ name: 'm1', server_id: 'srv-m', version: '3.0' }])
    expect(await new AcpAdapter().listPlugins()).toEqual([
      { id: 'pkg-a', name: 'a1', type: 'agent', source: 'package', version: '1.0' },
      { id: 's1', name: 's1', type: 'skill', source: 'package', version: '2.0' },
      { id: 'srv-m', name: 'm1', type: 'mcp', source: 'package', version: '3.0' },
    ])
  })

  it('treats a failed list as empty instead of failing the whole read', async () => {
    m(api.capabilityAgentsList).mockRejectedValue(new Error('503'))
    m(api.capabilitySkillsList).mockResolvedValue([{ name: 's2' }])
    m(api.capabilityMcpList).mockResolvedValue([])
    const plugins = await new AcpAdapter().listPlugins()
    expect(plugins.map(p => p.id)).toEqual(['s2'])
  })
})

describe('AcpAdapter plugin install and uninstall routing', () => {
  it('routes install by plugin type', async () => {
    const adapter = new AcpAdapter()
    await adapter.installPlugin('pkg-s', 'skill')
    await adapter.installPlugin('pkg-m', 'mcp')
    await adapter.installPlugin('pkg-a', 'agent')
    expect(api.capabilitySkillsInstall).toHaveBeenCalledWith('pkg-s')
    expect(api.capabilityMcpInstall).toHaveBeenCalledWith('pkg-m')
    expect(api.capabilityAgentsInstall).toHaveBeenCalledWith('pkg-a')
  })

  it('routes uninstall by plugin type', async () => {
    const adapter = new AcpAdapter()
    await adapter.uninstallPlugin('pkg-s', 'skill')
    await adapter.uninstallPlugin('pkg-m', 'mcp')
    await adapter.uninstallPlugin('pkg-a', 'agent')
    expect(api.capabilitySkillsUninstall).toHaveBeenCalledWith('pkg-s')
    expect(api.capabilityMcpUninstall).toHaveBeenCalledWith('pkg-m')
    expect(api.capabilityAgentsUninstall).toHaveBeenCalledWith('pkg-a')
  })

  it('reports bulk plugin update as unsupported without calling the gateway', async () => {
    expect(await new AcpAdapter().updatePlugins('skill')).toEqual({
      ok: false,
      error: 'plugin update is not supported',
    })
    expect(api.capabilitySkillsList).not.toHaveBeenCalled()
  })
})

describe('AcpAdapter static choices', () => {
  it('defaults the model to auto', () => {
    expect(new AcpAdapter().getDefaultModel()).toBe('auto')
  })

  it('offers the permission modes from least to most trusting, ending with yolo', () => {
    expect(new AcpAdapter().getPermissionModes().map(p => p.id)).toEqual(['normal', 'trust_reads', 'trust', 'yolo'])
  })
})
