import { describe, it, expect, vi, beforeEach, type Mock } from 'vitest'

// Partial mock, deliberately: it exports `api` but NOT `ApiError`. That is the
// shape the sibling AcpAdapter tests already use, and it is why the production
// predicate matches the error structurally instead of with `instanceof` — a
// class-identity check silently reads as "not this error" here and would send
// every caller down the wrong fallback.
vi.mock('../api/client', () => ({
  api: {
    models: vi.fn(),
    kiroUsage: vi.fn(),
  },
}))

import { api } from '../api/client'
import { AcpAdapter } from '../providers/adapters/acp'

const apiModelsMock = api.models as Mock
const apiKiroUsageMock = api.kiroUsage as Mock

/** The gateway's refusal when the active backend is not kiro and no live
 *  session has advertised its model namespace yet. */
function namespaceUnavailableError(
  backend?: string,
  servesAuto?: boolean,
): Error & { body: string } {
  const e = new Error('no live ACP session has advertised its models yet') as Error & {
    body: string
  }
  // The gateway reports `backend` on the FAILURE too, so a degraded answer still
  // identifies the namespace and the client can judge a cache against it.
  // `serves_auto` rides along for the same reason: this refusal IS the steady
  // state of an adapter with no live session, so a flag sent only on success
  // would be missing exactly when the picker has to decide what to offer.
  e.body = JSON.stringify({
    error: 'no live ACP session has advertised its models yet',
    code: 'acp_backend_models_unavailable',
    ...(backend === undefined ? {} : { backend }),
    ...(servesAuto === undefined ? {} : { serves_auto: servesAuto }),
  })
  return e
}

describe('AcpAdapter model fallback on a non-kiro backend', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('offers NO models rather than an unusable auto sentinel', async () => {
    // "auto" is a kiro-namespace sentinel. A spec adapter rejects it, so
    // offering it as the only row guarantees the operator picks something that
    // fails at the wire. No rows is honest; one unusable row is not.
    ;apiModelsMock.mockRejectedValue(namespaceUnavailableError())
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models).toEqual([])
    expect(models.some((m) => m.name === 'auto')).toBe(false)
  })

  it('still serves a cached live list when one exists', async () => {
    // A cache written by this backend IS in its own namespace, so it stays
    // valid — the refusal is about not knowing the namespace, not about the
    // cache being wrong. Prime through the ENVELOPE shape so the cache is
    // stamped for the same backend the later error names; priming with the
    // bare-array (kiro) shape would stamp it kiro and must NOT be served here.
    ;apiModelsMock.mockResolvedValue({
      models: [{ model_name: 'gpt-5-codex', description: 'Codex' }],
      backend: 'codex',
    })
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()

    ;apiModelsMock.mockRejectedValue(namespaceUnavailableError('codex'))
    const models = await adapter.fetchAvailableModels()
    expect(models.map((m) => m.name)).toEqual(['gpt-5-codex'])
  })

  it('keeps the auto fallback for an ordinary transient failure', async () => {
    // The kiro namespace DOES include "auto", so a 503 or cold start must keep
    // the original behaviour. Narrowing this to the new code is the point.
    ;apiModelsMock.mockRejectedValue(new Error('503 Service Unavailable'))
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models.map((m) => m.name)).toEqual(['auto'])
  })

  it('keeps the auto fallback when an error carries an unrelated code', async () => {
    const e = new Error('boom') as Error & { body: string }
    e.body = JSON.stringify({ code: 'something_else' })
    ;apiModelsMock.mockRejectedValue(e)
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models.map((m) => m.name)).toEqual(['auto'])
  })

  it('keeps the auto fallback when the body is not JSON', async () => {
    const e = new Error('boom') as Error & { body: string }
    e.body = '<html>502 Bad Gateway</html>'
    ;apiModelsMock.mockRejectedValue(e)
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models.map((m) => m.name)).toEqual(['auto'])
  })

  it('keeps the auto fallback when the error carries no body at all', async () => {
    ;apiModelsMock.mockRejectedValue({ message: 'network down' })
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models.map((m) => m.name)).toEqual(['auto'])
  })

  it('keeps auto for a non-kiro backend the gateway affirms serves it', async () => {
    // KAS is not kiro, so it takes the same degraded path as codex and claude —
    // it has no local binary to list models — yet it DOES serve the `auto`
    // sentinel. Deciding by "is the backend id non-empty" stripped the row from a
    // harness that has it, which is why the gateway states the capability instead
    // of leaving the client to infer it from the id.
    ;apiModelsMock.mockRejectedValue(namespaceUnavailableError('kas', true))
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models.map((m) => m.name)).toEqual(['auto'])
  })

  it('withholds auto for a non-kiro backend the gateway says lacks it', async () => {
    ;apiModelsMock.mockRejectedValue(namespaceUnavailableError('codex', false))
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models).toEqual([])
  })

  it('withholds auto when a namespace refusal omits the capability entirely', async () => {
    // A body from a gateway too old to send the field. Reaching this code proves
    // the backend is not kiro, so silence must not be read as the kiro answer —
    // the remembered "absent means kiro" default deliberately does not apply in
    // this branch.
    ;apiModelsMock.mockRejectedValue(namespaceUnavailableError('codex'))
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models).toEqual([])
  })

  it('withholds auto on a LATER transient failure once codex was identified', async () => {
    // The sticky half of the contract. A bodyless network blip carries no
    // backend, so the fallback consults what the gateway last reported; without
    // that memory every blip on an adapter resurrects the kiro sentinel.
    ;apiModelsMock.mockResolvedValue({
      models: [{ model_name: 'gpt-5-codex', description: 'Codex' }],
      backend: 'codex',
      serves_auto: false,
    })
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()
    localStorage.removeItem('kc.acp.models.v1') // drop the cache, keep the memory

    ;apiModelsMock.mockRejectedValue({ message: 'network down' })
    expect(await adapter.fetchAvailableModels()).toEqual([])
  })

  it('restores auto when the operator switches back to kiro', async () => {
    // The memory must be correctable in both directions, or a single session on
    // codex would suppress kiro's Auto row for the life of the browser profile.
    ;apiModelsMock.mockRejectedValue(namespaceUnavailableError('codex', false))
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()

    // Back on kiro: the BARE ARRAY shape, and an empty one so the fallback
    // (not the live list) is what answers.
    ;apiModelsMock.mockResolvedValue([])
    expect((await adapter.fetchAvailableModels()).map((m) => m.name)).toEqual(['auto'])
  })
})

/** Empty session counters, so each billing case below varies only billing. */
const EMPTY_SESSIONS = {
  total_sessions: 0,
  today: { sessions: 0, messages: 0, tool_calls: 0 },
  this_week: { sessions: 0, messages: 0, tool_calls: 0 },
  this_month: { sessions: 0, messages: 0, tool_calls: 0 },
  avg_msgs_per_session: 0,
  daily_history: [],
}

describe('AcpAdapter billing on a backend that reports none', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('suppresses billing entirely rather than reporting zero usage', async () => {
    // A non-kiro backend has no kiro-cli credit plan to parse, so the gateway
    // sends `billing: {}`. That must normalise to null — a card reading "0% of
    // 0 credits" would assert the operator has a plan and has used none of it,
    // when the truth is that this backend does not report credits at all.
    ;apiKiroUsageMock.mockResolvedValue({
      sessions: EMPTY_SESSIONS,
      billing: {},
    })
    const usage = await new AcpAdapter().fetchUsage()
    expect(usage.billing).toBeNull()
  })

  it('suppresses billing when the key is absent altogether', async () => {
    ;apiKiroUsageMock.mockResolvedValue({ sessions: EMPTY_SESSIONS })
    const usage = await new AcpAdapter().fetchUsage()
    expect(usage.billing).toBeNull()
  })

  it('leaves percentUsed undefined rather than 0 when there is no limit', async () => {
    // A plan with no credit ceiling is real (enterprise seats). Rendering 0%
    // there would read as "no usage" instead of "no ceiling to measure against".
    ;apiKiroUsageMock.mockResolvedValue({
      sessions: EMPTY_SESSIONS,
      billing: { plan: 'Enterprise', credits_used: 42, credits_plan: null },
    })
    const usage = await new AcpAdapter().fetchUsage()
    expect(usage.billing?.plan).toBe('Enterprise')
    expect(usage.billing?.percentUsed).toBeUndefined()
  })

  it('still computes a percentage when a real limit is present', async () => {
    ;apiKiroUsageMock.mockResolvedValue({
      sessions: EMPTY_SESSIONS,
      billing: { plan: 'Pro', credits_used: 25, credits_plan: 100 },
    })
    const usage = await new AcpAdapter().fetchUsage()
    expect(usage.billing?.percentUsed).toBe(25)
  })
})
