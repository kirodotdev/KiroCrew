// Regression: a fresh session displayed the kiro agent FILE's model
// (~/.kiro/agents/kirocrew.json → e.g. claude-opus-4.8) while the turn actually
// ran on the configured default (Settings → Chat → Default Model, e.g.
// claude-opus-5). resolveModel must mirror ConfigLoader._acp()'s precedence so
// the composer never advertises a model the backend won't use.

vi.mock('../api/client', () => ({
  api: {
    agentDetail: vi.fn(),
    kirocrewConfig: vi.fn(),
  },
}))

import { api } from '../api/client'
import { AcpAdapter } from '../providers/adapters/acp'

const agentDetail = api.agentDetail as unknown as ReturnType<typeof vi.fn>
const kirocrewConfig = api.kirocrewConfig as unknown as ReturnType<typeof vi.fn>

describe('AcpAdapter.resolveModel — configured default vs agent file', () => {
  beforeEach(() => vi.clearAllMocks())

  it('prefers the configured default model over the builtin agent file', async () => {
    agentDetail.mockResolvedValue({ model: 'claude-opus-4.8' })
    kirocrewConfig.mockResolvedValue({ agent: { model: 'claude-opus-5' } })
    expect(await new AcpAdapter().resolveModel('kirocrew')).toBe('claude-opus-5')
  })

  it('falls back to the agent file when no default is configured', async () => {
    agentDetail.mockResolvedValue({ model: 'claude-opus-4.8' })
    kirocrewConfig.mockResolvedValue({ agent: { model: '' } })
    expect(await new AcpAdapter().resolveModel('kirocrew')).toBe('claude-opus-4.8')
  })

  it('treats "auto" as no default — the backend resolves it from the agent file', async () => {
    agentDetail.mockResolvedValue({ model: 'claude-opus-4.8' })
    kirocrewConfig.mockResolvedValue({ agent: { model: 'auto' } })
    expect(await new AcpAdapter().resolveModel('kirocrew')).toBe('claude-opus-4.8')
  })

  it('lets a named custom agent pin outrank the global default', async () => {
    agentDetail.mockResolvedValue({ model: 'claude-sonnet-4.6' })
    kirocrewConfig.mockResolvedValue({ agent: { model: 'claude-opus-5' } })
    // _resolve_named_agent_model: the agent's own model wins; the global default
    // applies only to the builtin (kirocrew) agent.
    expect(await new AcpAdapter().resolveModel('my-agent')).toBe('claude-sonnet-4.6')
    expect(kirocrewConfig).not.toHaveBeenCalled()
  })

  it('survives a config read failure by using the agent file', async () => {
    agentDetail.mockResolvedValue({ model: 'claude-opus-4.8' })
    kirocrewConfig.mockRejectedValue(new Error('503'))
    expect(await new AcpAdapter().resolveModel('kirocrew')).toBe('claude-opus-4.8')
  })

  it('survives an agent-detail failure by using the configured default', async () => {
    agentDetail.mockRejectedValue(new Error('404'))
    kirocrewConfig.mockResolvedValue({ agent: { model: 'claude-opus-5' } })
    expect(await new AcpAdapter().resolveModel('kirocrew')).toBe('claude-opus-5')
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
