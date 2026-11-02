import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  opsApi,
  OpsApiError,
  describeVerification,
  type Incident,
  type Signal,
} from './api'

const BASE = '/api/apps/ops-mission-control'

const fetchMock = vi.fn()

function reply(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function lastCall(): { url: string; init: RequestInit } {
  const call = fetchMock.mock.calls.at(-1)
  if (!call) throw new Error('fetch was not called')
  return { url: call[0] as string, init: (call[1] ?? {}) as RequestInit }
}

function bodyOf(init: RequestInit): Record<string, unknown> {
  return JSON.parse(String(init.body)) as Record<string, unknown>
}

beforeEach(() => {
  fetchMock.mockReset()
  fetchMock.mockImplementation(() => Promise.resolve(reply({ ok: true })))
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ops api req() error handling', () => {
  it('throws an OpsApiError carrying the backend error text and code', async () => {
    fetchMock.mockResolvedValueOnce(reply({ error: 'autonomy gate: no rule', code: 'AUTONOMY' }, 403))
    const err = await opsApi.state().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(OpsApiError)
    expect((err as OpsApiError).message).toBe('autonomy gate: no rule')
    expect((err as OpsApiError).code).toBe('AUTONOMY')
    expect((err as OpsApiError).name).toBe('OpsApiError')
  })

  it('falls back to the HTTP status when the error body is not JSON', async () => {
    fetchMock.mockResolvedValueOnce(new Response('<html>bad gateway</html>', { status: 502 }))
    const err = (await opsApi.state().catch((e: unknown) => e)) as OpsApiError
    expect(err.message).toBe('HTTP 502')
    expect(err.code).toBeUndefined()
  })

  it('keeps the HTTP status when a JSON error body has no error field', async () => {
    fetchMock.mockResolvedValueOnce(reply({ detail: 'nope' }, 500))
    const err = (await opsApi.providers().catch((e: unknown) => e)) as OpsApiError
    expect(err.message).toBe('HTTP 500')
  })

  it('returns the parsed body on success under the app base path', async () => {
    fetchMock.mockResolvedValueOnce(reply({ providers: [{ id: 'slack' }] }))
    const out = await opsApi.providers()
    expect(out).toEqual({ providers: [{ id: 'slack' }] })
    expect(lastCall().url).toBe(`${BASE}/providers`)
  })
})

describe('ops api incident reads and writes', () => {
  it('lists incidents without a status filter when none is given', async () => {
    await opsApi.incidents()
    expect(lastCall().url.startsWith(`${BASE}/incidents`)).toBe(true)
    expect(lastCall().url).not.toContain('status=')
  })

  it('filters incidents by status', async () => {
    await opsApi.incidents('needs_human')
    expect(lastCall().url).toBe(`${BASE}/incidents?status=needs_human`)
  })

  it('encodes the incident id in the lookup', async () => {
    await opsApi.incident('a/b c')
    expect(lastCall().url).toBe(`${BASE}/incident?id=a%2Fb%20c`)
  })

  it('posts a transition with its extra fields merged into the body', async () => {
    await opsApi.transition('inc-1', 'resolved', { note: 'fixed' })
    const { url, init } = lastCall()
    expect(url).toBe(`${BASE}/incident/transition`)
    expect(init.method).toBe('POST')
    expect(bodyOf(init)).toEqual({ id: 'inc-1', status: 'resolved', note: 'fixed' })
  })

  it('posts an action with its options', async () => {
    await opsApi.action('inc-2', 'silence', { duration_secs: 600, note: 'deploy' })
    const { url, init } = lastCall()
    expect(url).toBe(`${BASE}/incident/action`)
    expect(init.method).toBe('POST')
    expect(bodyOf(init)).toEqual({ id: 'inc-2', action: 'silence', duration_secs: 600, note: 'deploy' })
  })

  it('posts a claim carrying the whole signal', async () => {
    const signal = { id: 'sig-9', source: 'alertmanager' } as unknown as Signal
    await opsApi.claim(signal)
    const { url, init } = lastCall()
    expect(url).toBe(`${BASE}/incident/claim`)
    expect(init.method).toBe('POST')
    expect(bodyOf(init)).toEqual({ signal })
  })
})

describe('ops api proposal decisions', () => {
  it('reads the pending proposal queue', async () => {
    await opsApi.proposals()
    expect(lastCall().url).toBe(`${BASE}/proposals`)
  })

  it('sends the digest the operator saw on an approval', async () => {
    await opsApi.decideProposal('p-1', true, 'abc123')
    const { url, init } = lastCall()
    expect(url).toBe(`${BASE}/incident/proposal/decide`)
    expect(bodyOf(init)).toEqual({ id: 'p-1', approve: true, digest: 'abc123' })
  })

  it('omits the digest on a rejection, which authorizes nothing', async () => {
    await opsApi.decideProposal('p-2', false)
    expect(bodyOf(lastCall().init)).toEqual({ id: 'p-2', approve: false })
  })
})

describe('ops api board reads', () => {
  it('fetches the handover digest', async () => {
    await opsApi.handover()
    expect(lastCall().url).toBe(`${BASE}/handover`)
  })

  it('fetches the raw signals list', async () => {
    await opsApi.signals()
    expect(lastCall().url).toBe(`${BASE}/signals`)
  })

  it('runs one dispatch cycle with POST', async () => {
    await opsApi.dispatch()
    expect(lastCall().url).toBe(`${BASE}/dispatch`)
    expect(lastCall().init.method).toBe('POST')
  })

  it('fetches the rotation roster and the ledger', async () => {
    await opsApi.rotation()
    expect(lastCall().url).toBe(`${BASE}/rotation`)
    await opsApi.ledger()
    expect(lastCall().url).toBe(`${BASE}/ledger`)
  })
})

describe('ops api settings and secrets', () => {
  it('puts provider config under an encoded provider id', async () => {
    await opsApi.putProviderConfig('zab bix', { url: 'https://z' })
    const { url, init } = lastCall()
    expect(url).toBe(`${BASE}/providers/zab%20bix/config`)
    expect(init.method).toBe('PUT')
    expect(bodyOf(init)).toEqual({ url: 'https://z' })
  })

  it('puts app settings', async () => {
    await opsApi.putSettings({ notify_enabled: true })
    const { url, init } = lastCall()
    expect(url).toBe(`${BASE}/settings`)
    expect(init.method).toBe('PUT')
    expect(bodyOf(init)).toEqual({ notify_enabled: true })
  })

  it('writes a secret field and deletes a provider secret', async () => {
    await opsApi.putSecret('slack', 'token', 'xoxb-test')
    expect(lastCall().url).toBe(`${BASE}/providers/slack/secret`)
    expect(lastCall().init.method).toBe('PUT')
    expect(bodyOf(lastCall().init)).toEqual({ field: 'token', value: 'xoxb-test' })
    await opsApi.deleteSecret('slack')
    expect(lastCall().url).toBe(`${BASE}/providers/slack/secret`)
    expect(lastCall().init.method).toBe('DELETE')
  })
})

describe('ops api ledger maintenance', () => {
  it('runs the hygiene pass with POST', async () => {
    await opsApi.ledgerHygiene()
    expect(lastCall().url).toBe(`${BASE}/ledger/hygiene`)
    expect(lastCall().init.method).toBe('POST')
  })

  it('reads ledger contradictions', async () => {
    await opsApi.ledgerContradictions()
    expect(lastCall().url).toBe(`${BASE}/ledger/contradictions`)
  })

  it('adds a ledger entry with its fields as the body', async () => {
    await opsApi.addLedgerEntry({ pattern: 'disk full', fix: 'rotate logs', confidence: 'high' })
    const { url, init } = lastCall()
    expect(url).toBe(`${BASE}/ledger`)
    expect(init.method).toBe('POST')
    expect(bodyOf(init)).toEqual({ pattern: 'disk full', fix: 'rotate logs', confidence: 'high' })
  })

  it('removes a ledger entry by encoded id', async () => {
    await opsApi.removeLedgerEntry('L 1/2')
    const { url, init } = lastCall()
    expect(url).toBe(`${BASE}/ledger?id=L%201%2F2`)
    expect(init.method).toBe('DELETE')
  })
})

describe('describeVerification', () => {
  const base = { last_action: 'ack' } as unknown as Incident

  it('returns null when no verdict has been recorded', () => {
    expect(describeVerification({ ...base, verification: '' } as Incident)).toBeNull()
  })

  it('flags a still-firing alarm as an error', () => {
    const view = describeVerification({ ...base, verification: 'still_firing' } as Incident)
    expect(view?.variant).toBe('err')
    expect(view?.label).toBeTruthy()
  })

  it('reports a confirmed recovery as ok', () => {
    const view = describeVerification({ ...base, verification: 'cleared' } as Incident)
    expect(view?.variant).toBe('ok')
  })

  it('reports a pending check as muted, with its due time when one is set', () => {
    const at = describeVerification({ ...base, verification: 'pending', verify_after: '14:05Z' } as Incident)
    const bare = describeVerification({ ...base, verification: 'pending' } as Incident)
    expect(at?.variant).toBe('muted')
    expect(bare?.variant).toBe('muted')
    expect(at?.meaning).not.toBe(bare?.meaning)
  })

  it('reports an unreadable recheck as warn, never as a failure', () => {
    const view = describeVerification({ ...base, verification: 'unknown' } as Incident)
    expect(view?.variant).toBe('warn')
  })
})
