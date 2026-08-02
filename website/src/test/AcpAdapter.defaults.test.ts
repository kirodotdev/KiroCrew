// Regression: a fresh session displayed the kiro agent FILE's model
// (~/.kiro/agents/kirocrew.json → e.g. claude-opus-4.8) while the turn actually
// ran on the configured default (e.g. claude-opus-5). The precedence chain is
// now four tiers deep (the KiroCrew agent's own model, the bound kiro agent's
// pin, the global fallback, the installed agent file) and ONE backend resolver
// owns it — resolveModel must delegate rather than keep a second copy that can
// drift, which is what produced the original mismatch.

vi.mock('../api/client', () => ({
  api: {
    agentDetail: vi.fn(),
    agentResolvedModel: vi.fn(),
    kirocrewConfig: vi.fn(),
  },
}))

import { api } from '../api/client'
import { AcpAdapter } from '../providers/adapters/acp'

const agentDetail = api.agentDetail as unknown as ReturnType<typeof vi.fn>
const agentResolvedModel = api.agentResolvedModel as unknown as ReturnType<typeof vi.fn>
const kirocrewConfig = api.kirocrewConfig as unknown as ReturnType<typeof vi.fn>

describe('AcpAdapter.resolveModel — delegates to the backend resolver', () => {
  beforeEach(() => vi.clearAllMocks())

  it('returns the model the backend resolved for the agent', async () => {
    agentResolvedModel.mockResolvedValue({ model: 'claude-opus-5' })
    expect(await new AcpAdapter().resolveModel('oncall')).toBe('claude-opus-5')
  })

  it('passes the KiroCrew agent name through, not a kiro template name', async () => {
    // Several agents can share one template, so the agent name is the only
    // input that can select a per-agent pin.
    agentResolvedModel.mockResolvedValue({ model: 'claude-opus-5' })
    await new AcpAdapter().resolveModel('oncall')
    expect(agentResolvedModel).toHaveBeenCalledWith('oncall')
  })

  it('does NOT re-derive the precedence client-side', async () => {
    agentResolvedModel.mockResolvedValue({ model: 'claude-opus-5' })
    await new AcpAdapter().resolveModel('oncall')
    expect(agentDetail).not.toHaveBeenCalled()
    expect(kirocrewConfig).not.toHaveBeenCalled()
  })

  it('reports "" when every tier defers, so callers keep the backend-picks semantics', async () => {
    agentResolvedModel.mockResolvedValue({ model: '' })
    expect(await new AcpAdapter().resolveModel('oncall')).toBe('')
  })

  it('survives a resolver failure by reporting no model rather than throwing', async () => {
    agentResolvedModel.mockRejectedValue(new Error('503'))
    expect(await new AcpAdapter().resolveModel('oncall')).toBe('')
  })

  it('survives a partially-mocked api client without throwing', async () => {
    // Many suites mock ../api/client partially, so a newly-added method is
    // undefined there and calling it raises synchronously. A mount-time resolve
    // must degrade to "" rather than crash the tree.
    agentResolvedModel.mockImplementation(() => {
      throw new TypeError('api.agentResolvedModel is not a function')
    })
    expect(await new AcpAdapter().resolveModel('oncall')).toBe('')
  })
})

describe('AcpAdapter.resolveDefaultEffort', () => {
  beforeEach(() => vi.clearAllMocks())

  it('returns the configured default effort', async () => {
    kirocrewConfig.mockResolvedValue({ agent: { reasoning_effort: 'high' } })
    expect(await new AcpAdapter().resolveDefaultEffort()).toBe('high')
  })

  it('returns "" when unset, so callers keep the model-default semantics', async () => {
    kirocrewConfig.mockResolvedValue({ agent: {} })
    expect(await new AcpAdapter().resolveDefaultEffort()).toBe('')
  })

  it('returns "" on a failed config read rather than throwing', async () => {
    kirocrewConfig.mockRejectedValue(new Error('boom'))
    expect(await new AcpAdapter().resolveDefaultEffort()).toBe('')
  })
})
