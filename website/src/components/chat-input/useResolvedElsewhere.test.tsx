import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { SharedDecision } from '../../api/sharedCrew'
import { api } from '../../api/client'
import { applySharedCrewFrame, sharedCrewKeys } from '../../hooks/sharedCrew/sharedCrewSocket'
import { useSessionDecisions } from '../../hooks/sharedCrew/useSharedCrew'
import { useResolvedElsewhere } from './useResolvedElsewhere'

vi.mock('../../api/client', () => ({ api: { sharedCrewDecisions: vi.fn() } }))

const person = (id: string, name: string) => ({ id, kind: 'human' as const, display: { name, login: '', captured_at: null } })
const decision = (over: Partial<SharedDecision>): SharedDecision => ({
  decision_id: 'approval:3:s1:ap-1', kind: 'approval', session_key: 's1', state: 'pending', revision: 1,
  initiator: person('p_me', 'Me'), responder: null, outcome: '', resolved_at: null, ...over,
})
const resolved = (over: Partial<SharedDecision> = {}) =>
  decision({ state: 'resolved', outcome: 'approved', responder: person('p_kyle', 'Kyle'), resolved_at: 5, ...over })

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const seen = vi.fn()
let markSeen: (d: SharedDecision) => void = () => {}
function Probe({ viewerId = 'p_me', cards = ['ap-1'], sessionKey = 's1' }: { viewerId?: string; cards?: string[]; sessionKey?: string }) {
  markSeen = useResolvedElsewhere(sessionKey, true, viewerId, cards, seen)
  // What this render has read, so a test knows the hook has seen a frame.
  const items = useSessionDecisions(sessionKey).data?.items ?? []
  return <span data-testid="read">{items.map(d => `${d.state}:${d.resolved_at}`).join(',')}</span>
}
const mount = (props: Parameters<typeof Probe>[0] = {}) => {
  const view = render(<QueryClientProvider client={qc}><Probe {...props} /></QueryClientProvider>)
  const rerender = (next: Parameters<typeof Probe>[0]) => view.rerender(<QueryClientProvider client={qc}><Probe {...next} /></QueryClientProvider>)
  return { ...view, rerender }
}
const read = (...items: SharedDecision[]) => waitFor(() => expect(screen.getByTestId('read')).toHaveTextContent(items.map(d => `${d.state}:${d.resolved_at}`).join(',')))
const firstRead = () => read(decision({}))
/** A decision frame lands in the cache and the hook has read it. */
const frame = async (item: SharedDecision) => {
  act(() => {
    qc.setQueryData(sharedCrewKeys.decisions('s1'), (prev: { items: SharedDecision[] } | undefined) => ({ items: [...(prev?.items ?? []).filter(d => d.decision_id !== item.decision_id), item] }))
  })
  await read(item)
}

beforeEach(() => {
  qc.clear()
  seen.mockClear()
  vi.mocked(api.sharedCrewDecisions).mockResolvedValue({ schema_version: 1, items: [decision({})] } as never)
})

describe('useResolvedElsewhere', () => {
  it('says how a card ended when someone else settled it', async () => {
    mount()
    await firstRead()
    await frame(resolved())
    expect(seen).toHaveBeenCalledExactlyOnceWith('Kyle approved', expect.objectContaining({ state: 'resolved' }))
  })

  it('names a settled question as answered', async () => {
    vi.mocked(api.sharedCrewDecisions).mockResolvedValue({ schema_version: 1, items: [decision({ decision_id: 'question:ask-1', kind: 'question' })] } as never)
    mount({ cards: ['ask-1'] })
    await read(decision({}))
    await frame(resolved({ decision_id: 'question:ask-1', kind: 'question', outcome: 'answered' }))
    expect(seen).toHaveBeenCalledExactlyOnceWith('Kyle answered', expect.objectContaining({ kind: 'question' }))
  })

  it('says nothing for a question somebody dismissed', async () => {
    vi.mocked(api.sharedCrewDecisions).mockResolvedValue({ schema_version: 1, items: [decision({ decision_id: 'question:ask-1', kind: 'question' })] } as never)
    mount({ cards: ['ask-1'] })
    await read(decision({}))
    await frame(resolved({ decision_id: 'question:ask-1', kind: 'question', outcome: 'dismissed' }))
    expect(seen).not.toHaveBeenCalled()
  })

  it.each([
    ['the viewer settled it', resolved({ responder: person('p_me', 'Me') })],
    ['the gateway settled it by its approval mode', resolved({ settled_by: 'host' })],
    ['nobody is named', resolved({ responder: null })],
  ])('says nothing when %s', async (_why, item) => {
    mount()
    await firstRead()
    await frame(item)
    expect(seen).not.toHaveBeenCalled()
  })

  it('says nothing about a decision for a card this tab never showed', async () => {
    mount({ cards: ['ap-other'] })
    await firstRead()
    await frame(resolved())
    expect(seen).not.toHaveBeenCalled()
  })

  it('does not replay decisions that were already settled when the session opened', async () => {
    vi.mocked(api.sharedCrewDecisions).mockResolvedValue({ schema_version: 1, items: [resolved()] } as never)
    mount({ cards: [] })
    await read(resolved())
    await frame(resolved({ resolved_at: 6 }))
    expect(seen).not.toHaveBeenCalled()
  })

  it('reports a card settled while the first read was still in flight', async () => {
    let landFullRead: (v: unknown) => void = () => {}
    vi.mocked(api.sharedCrewDecisions)
      .mockImplementationOnce(() => new Promise(() => {}))
      .mockImplementationOnce(() => new Promise(r => { landFullRead = r }) as never)
    mount()
    act(() => { applySharedCrewFrame(qc, { type: 'shared_crew.decision', schema_version: 1, decision: resolved() }) })
    await read(resolved())
    landFullRead({ schema_version: 1, items: [resolved()] })
    await read(resolved())
    expect(seen).toHaveBeenCalledExactlyOnceWith('Kyle approved', expect.objectContaining({ state: 'resolved' }))
  })

  it('does not report older decisions that arrive with the first full read', async () => {
    const older = resolved({ decision_id: 'approval:1:s1:old', resolved_at: 3 })
    mount()
    await firstRead()
    await frame(older)
    expect(seen).not.toHaveBeenCalled()
  })

  it('stays silent about a decision the caller already reported itself', async () => {
    mount()
    await firstRead()
    markSeen(resolved())
    await frame(resolved())
    expect(seen).not.toHaveBeenCalled()
  })

  it('reports a second settlement of a recurring request id, told apart by revision', async () => {
    mount()
    await firstRead()
    await frame(resolved({ revision: 2 }))
    expect(seen).toHaveBeenCalledTimes(1)
    await frame(decision({ revision: 3 }))
    await frame(resolved({ revision: 4, responder: person('p_mia', 'Mia') }))
    expect(seen).toHaveBeenCalledTimes(2)
    expect(seen).toHaveBeenLastCalledWith('Mia approved', expect.objectContaining({ revision: 4 }))
  })

  it('a settlement already in the cache when its card appears belongs to an earlier use of the id', async () => {
    vi.mocked(api.sharedCrewDecisions).mockResolvedValue({ schema_version: 1, items: [resolved({ revision: 2 })] } as never)
    const { rerender } = mount({ cards: [] })
    await read(resolved())
    rerender({ cards: ['ap-1'] })
    await frame(resolved({ revision: 2, resolved_at: 6 }))
    expect(seen).not.toHaveBeenCalled()
  })

  it('forgets the cards of the session it leaves', async () => {
    const { rerender } = mount()
    await firstRead()
    rerender({ sessionKey: 's2', cards: [] })
    rerender({ sessionKey: 's1', cards: [] })
    await firstRead()
    await frame(resolved())
    expect(seen).not.toHaveBeenCalled()
  })

  it('says nothing while the viewer is unknown', async () => {
    mount({ viewerId: '' })
    await firstRead()
    await frame(resolved())
    expect(seen).not.toHaveBeenCalled()
  })
})
