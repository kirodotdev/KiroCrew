/**
 * Change cards, drawn in the conversation at the row the agent proposed them in:
 * every kind has a fixed renderer, the widen gate, the secret value reaching
 * only `/api/secrets`, plan steps sent in order with the card headers, the
 * result line's undo and focus, an expired card disabled, the browser never
 * claiming success on its own, and a row whose card the store no longer holds
 * drawing its own recorded outcome.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MEMBERS_ROSTER_QUERY_KEY } from '../api/membersQuery'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { store as reduxStore } from '../store'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import '../api/client'
import { i18nT } from '../i18n/t'
import { applyCardUpdate, cardsQueryKey, CARD_KINDS, type Card } from '../api/cards'
import { CARD_REGISTRY } from './cardRegistry'
import { fillBody } from './runPlan'
import ConversationCard, { readCardRef } from './ConversationCard'
import type { ChatMessage } from '../types'
import { CardAuthorContext } from './cardAuthor'

const L = (k: string, v?: Record<string, unknown>) => i18nT(`components.changeCards.${k}`, v)
const SLOT = 'slot-A'
/** The frozen wall clock every test runs at (noon UTC, so "tomorrow" is a day out in every zone). */
const NOW = Date.UTC(2026, 0, 15, 12)
const SECRET = 'sk-live-THE-VALUE-123'

const card = (over: Partial<Card> = {}): Card => ({
  id: 'c1',
  slot_key: SLOT,
  kind: 'setting.change',
  revision: 1,
  status: 'pending',
  risk: 'normal',
  title: 'Shorter replies',
  changes: [{ label: 'Reply length', before: 'standard', after: 'brief' }],
  editable: [],
  params: { path: 'chat.verbosity', value: 'brief' },
  plan: {
    apply: [{ method: 'PATCH', path: '/api/config/kirocrew', body: { path: 'chat.verbosity', value: 'brief' } }],
    undo: [{ method: 'PATCH', path: '/api/config/kirocrew', body: { path: 'chat.verbosity', value: 'standard' } }],
  },
  created_at: 1_700_000_000,
  expires_at: NOW + 3_600_000,
  ...over,
})

type Call = { method: string; url: string; body: string; headers: Record<string, string> }
let calls: Call[]
let pending: Card[]
let fetchSpy: ReturnType<typeof vi.spyOn>
let qc: QueryClient

/** The transcript row the gateway writes for a proposed card. */
const row = (c: Pick<Card, 'id' | 'kind' | 'title' | 'status'> & { slot_key?: string }, over: Record<string, unknown> = {}): ChatMessage => ({
  role: 'card',
  content: c.title,
  cls: 'msg msg-card',
  ts: '2026-10-03T10:00:00Z',
  meta: { mid: `m-${c.id}`, card: { surface: 'change', id: c.id, slot: c.slot_key ?? SLOT, kind: c.kind, title: c.title, status: c.status, ...over } },
})

/** One conversation row per card the store serves, as the transcript holds them. */
function renderCards(rows: ChatMessage[] = pending.map(c => row(c))) {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={reduxStore}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          {rows.map(m => <ConversationCard key={String(m.meta?.mid)} message={m} slot={SLOT} />)}
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

/** Every request the page made, captured at the fetch boundary. */
function recordFetches() {
  const real = globalThis.fetch
  fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
    const headers: Record<string, string> = {}
    new Headers(init?.headers).forEach((v, k) => { headers[k.toLowerCase()] = v })
    calls.push({ method: (init?.method ?? 'GET').toUpperCase(), url, body: typeof init?.body === 'string' ? init.body : '', headers })
    return real(input, init)
  })
}

const stepCalls = () => calls.filter(c => c.headers['x-card-id'])

beforeEach(() => {
  // One frozen clock for every test: the card reads Date for its expiry and run time.
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
  calls = []
  pending = []
  server.use(
    http.get('/api/cards/pending', () => HttpResponse.json({ cards: pending })),
    http.post('/api/cards/:id/preview', async ({ request, params }) => {
      const body = (await request.json()) as { params: Record<string, unknown> }
      const prev = pending.find(c => c.id === params.id) as Card
      const next = { ...prev, revision: prev.revision + 1, params: body.params }
      pending = [next]
      return HttpResponse.json({ card: next })
    }),
    http.post('/api/cards/:id/cancel', ({ params }) => {
      const next = { ...(pending.find(c => c.id === params.id) as Card), status: 'cancelled' as const }
      pending = [next]
      return HttpResponse.json({ card: next })
    }),
    http.patch('/api/config/kirocrew', () => HttpResponse.json({ ok: true })),
    http.post('/api/agents', () => HttpResponse.json({ ok: true })),
    http.post('/api/crons', () => HttpResponse.json({ ok: true, id: 'job1' })),
    http.post('/api/secrets', () => HttpResponse.json({ ok: true })),
  )
  recordFetches()
})

afterEach(() => {
  fetchSpy.mockRestore()
  vi.useRealTimers()
})

describe('card registry', () => {
  it('has a fixed renderer and wording for every contract kind', () => {
    expect(Object.keys(CARD_REGISTRY).sort()).toEqual([...CARD_KINDS].sort())
    for (const kind of CARD_KINDS) {
      const spec = CARD_REGISTRY[kind]
      expect(spec.Body, kind).toBeTypeOf('function')
      for (const label of [spec.label(), spec.primary()]) {
        expect(label, kind).not.toMatch(/^components\.changeCards\./)
        expect(label.length, kind).toBeGreaterThan(0)
      }
    }
  })

  it('renders every kind as a card with its own primary action', async () => {
    pending = CARD_KINDS.map((kind, i) => card({ id: `k${i}`, kind, created_at: 1_700_000_000 + i }))
    renderCards()
    await waitFor(() => expect(screen.getAllByTestId('change-card')).toHaveLength(CARD_KINDS.length))
    for (const kind of CARD_KINDS) expect(screen.getAllByText(CARD_REGISTRY[kind].primary()).length).toBeGreaterThan(0)
  })

  it('fills only the declared fields, from the user or an earlier step response', () => {
    const step = {
      method: 'POST', path: '/api/crons',
      body: { name: 'radar', member_id: '{{step0.member_id}}', note: '{{user:value}}', keep: '{{user:value}}' },
      fill: [{ field: 'member_id', source: 'step' as const, step: 0, key: 'member_id' }, { field: 'note', source: 'user' as const }],
    }
    expect(fillBody(step, { value: SECRET, note: 'typed' }, [{ member_id: 'm-7', other: 'x' }]))
      .toEqual({ name: 'radar', member_id: 'm-7', note: 'typed', keep: '{{user:value}}' })
    const plain = { method: 'POST', path: '/api/agents', body: { a: 1 } }
    expect(fillBody(plain, { value: SECRET }, [])).toBe(plain.body)
  })
})

describe('card layout', () => {
  const INSET = /(^|\s)(?:[a-z0-9@[\]:-]*:)?-?(?:p[xlrse]|m[xlrse])-/

  it('names the crewmate that proposed it, and Kiro Crew outside a crewmate chat', async () => {
    pending = [card({ reason: 'You asked for shorter replies.' })]
    const rows = pending.map(c => row(c))
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const view = render(
      <Provider store={reduxStore}>
        <QueryClientProvider client={qc}>
          <MemoryRouter>
            <CardAuthorContext.Provider value="Pebble">
              {rows.map(m => <ConversationCard key={String(m.meta?.mid)} message={m} slot={SLOT} />)}
            </CardAuthorContext.Provider>
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    expect(await view.findByText(L('reason_label', { name: 'Pebble' }))).toBeInTheDocument()
    view.unmount()
    renderCards()
    expect(await screen.findByText(L('reason_label', { name: 'Kiro Crew' }))).toBeInTheDocument()
  })

  it('puts every block of a card on one left edge: no child adds its own inset', async () => {
    pending = [card({ risk: 'widen', reason: 'You asked for shorter replies.', changes: [{ label: 'Hidden models', add: [], remove: ['m-1'] }] })]
    renderCards()
    const body = await screen.findByTestId('change-card-body')
    const footer = screen.getByTestId('change-card-footer')
    // The body and the footer are siblings with the same inset; the footer's
    // divider is its own top border, so it spans the full card width.
    expect(body.parentElement).toBe(footer.parentElement)
    expect(body.className).toMatch(/(^|\s)px-4(\s|$)/)
    expect(footer.className).toMatch(/(^|\s)px-4(\s|$)/)
    expect(footer.className).toContain('border-t')
    const blocks = [
      screen.getByTestId('change-card-kind'),
      screen.getByRole('heading', { level: 3 }).parentElement as HTMLElement,
      screen.getByTestId('change-card-rows'),
      screen.getByTestId('change-card-reason'),
      ...screen.getAllByTestId('change-card-rows').flatMap(r => Array.from(r.querySelectorAll<HTMLElement>('[data-card-row]'))),
    ]
    for (const el of blocks) {
      expect(el.parentElement === body || el.closest('[data-testid="change-card-rows"]')).toBeTruthy()
      expect(el.className, el.outerHTML.slice(0, 80)).not.toMatch(INSET)
    }
    // The risk callout is the one boxed block: its BOX starts on the same edge
    // (no margin of its own), and the widen acknowledgment sits inside it.
    const risk = screen.getByTestId('change-card-risk')
    expect(risk.parentElement).toBe(body)
    expect(risk.className).not.toMatch(/(^|\s)-?m[xlrse]-/)
    expect(risk.contains(screen.getByTestId('change-card-widen-ack'))).toBe(true)
    // The reason is plain text on the card, not a boxed inset.
    expect(screen.getByTestId('change-card-reason').className).not.toMatch(/bg-|rounded/)
  })

  it('adds no horizontal margin at the conversation row: the row is the message column', async () => {
    pending = [card()]
    renderCards()
    const root = await screen.findByTestId('conversation-card')
    expect(root.className).not.toMatch(INSET)
    expect(root.contains(await screen.findByTestId('change-card'))).toBe(true)
  })
})

describe('change card', () => {
  it('re-reads the crewmate roster once a crewmate card has run, so the sidebar count moves', async () => {
    pending = [card({
      kind: 'crewmate.create',
      plan: { apply: [{ method: 'POST', path: '/api/agents', body: { name: 'radar' } }], undo: null },
    })]
    renderCards()
    const spy = vi.spyOn(qc, 'invalidateQueries')
    fireEvent.click(await screen.findByTestId('change-card-apply'))
    await waitFor(() => expect(spy.mock.calls.some(([f]) =>
      JSON.stringify((f as { queryKey?: unknown })?.queryKey) === JSON.stringify(MEMBERS_ROSTER_QUERY_KEY))).toBe(true))
  })

  it('sends the plan steps in order with the card headers and waits for the gateway verdict', async () => {
    pending = [card({
      kind: 'crewmate.create',
      revision: 3,
      plan: {
        apply: [
          { method: 'POST', path: '/api/agents', body: { name: 'radar' } },
          { method: 'POST', path: '/api/crons', body: { name: 'radar', cron_expr: '0 9 * * 1-5' } },
        ],
        undo: null,
      },
    })]
    renderCards()
    fireEvent.click(await screen.findByTestId('change-card-apply'))
    await waitFor(() => expect(stepCalls()).toHaveLength(2))
    const [a, b] = stepCalls()
    expect([a.method, a.url, JSON.parse(a.body)]).toEqual(['POST', '/api/agents', { name: 'radar' }])
    expect([b.method, b.url]).toEqual(['POST', '/api/crons'])
    expect(a.headers).toMatchObject({ 'x-card-id': 'c1', 'x-card-revision': '3', 'x-card-op': 'apply', 'x-card-step': '0' })
    expect(b.headers).toMatchObject({ 'x-card-op': 'apply', 'x-card-step': '1' })
    // Nothing local marks it done: still the full card until the gateway says so.
    await waitFor(() => expect(screen.getByTestId('change-card-apply')).toBeTruthy())
    expect(screen.queryByTestId('change-card-result')).toBeNull()
    act(() => applyCardUpdate(qc, { ...pending[0], status: 'applied', result: { summary: 'Created radar' } }))
    // The applied line is the dashboard's, never the gateway's English summary.
    expect(await screen.findByText(L('result_applied', { title: 'Shorter replies' }))).toBeTruthy()
    expect(screen.queryByText('Created radar')).toBeNull()
  })

  it('stops at the first refused step', async () => {
    server.use(http.post('/api/agents', () => HttpResponse.json({ error: 'name taken' }, { status: 409 })))
    pending = [card({
      kind: 'crewmate.create',
      plan: { apply: [{ method: 'POST', path: '/api/agents', body: {} }, { method: 'POST', path: '/api/crons', body: {} }], undo: null },
    })]
    renderCards()
    fireEvent.click(await screen.findByTestId('change-card-apply'))
    await waitFor(() => expect(screen.getByTestId('change-card-error').textContent).toBeTruthy())
    expect(stepCalls().map(c => c.url)).toEqual(['/api/agents'])
  })

  it('fills a later step from the earlier step response', async () => {
    server.use(http.post('/api/agents', () => HttpResponse.json({ ok: true, member_id: 'm-42', name: 'radar' })))
    pending = [card({
      kind: 'crewmate.create',
      plan: {
        apply: [
          { method: 'POST', path: '/api/agents', body: { name: 'radar' } },
          { method: 'POST', path: '/api/crons', body: { name: 'radar', member_id: '{{step0.member_id}}' }, fill: [{ field: 'member_id', source: 'step', step: 0, key: 'member_id' }] },
        ],
        undo: null,
      },
    })]
    renderCards()
    fireEvent.click(await screen.findByTestId('change-card-apply'))
    await waitFor(() => expect(stepCalls()).toHaveLength(2))
    expect(JSON.parse(stepCalls()[1].body)).toEqual({ name: 'radar', member_id: 'm-42' })
  })

  it('resends a repeat step with the card headers until the gateway stops waiting', async () => {
    let polls = 0
    const waiting = { status: 'applying' as const, progress: { op: 'apply' as const, done: 1, total: 2, waiting: true } }
    server.use(
      http.post('/api/connections/mint', () => HttpResponse.json({ ok: true, state: 'minting' })),
      http.get('/api/connections/mint', () => {
        polls += 1
        if (polls >= 2) pending = [{ ...pending[0], status: 'applied', progress: null, result: { summary: 'Connected github' } }]
        return HttpResponse.json({ slug: 'github', state: polls >= 2 ? 'granted' : 'waiting', oauth_url: 'https://example.test/auth' })
      }),
    )
    pending = [card({
      kind: 'connection.connect',
      plan: {
        apply: [
          { method: 'POST', path: '/api/connections/mint', body: { slug: 'github' } },
          { method: 'GET', path: '/api/connections/mint?slug=github', body: null, repeat: true },
        ],
        undo: null,
      },
    })]
    renderCards()
    fireEvent.click(await screen.findByTestId('change-card-apply'))
    act(() => applyCardUpdate(qc, { ...pending[0], ...waiting }))
    pending = [{ ...pending[0], ...waiting }]
    await waitFor(() => expect(stepCalls().filter(c => c.url.startsWith('/api/connections/mint?')).length).toBe(2), { timeout: 5000 })
    const reads = stepCalls().filter(c => c.method === 'GET')
    for (const r of reads) expect(r.headers).toMatchObject({ 'x-card-op': 'apply', 'x-card-step': '1', 'x-card-id': 'c1' })
    expect(await screen.findByText(L('result_applied', { title: 'Shorter replies' }))).toBeTruthy()
    // No separate, un-headered poll of the mint route.
    expect(calls.filter(c => c.url.startsWith('/api/connections/mint') && !c.headers['x-card-id'])).toHaveLength(0)
  })

  it('shows the error when the card cannot be re-read between polls, and frees the button', async () => {
    let failReads = false
    server.use(
      http.post('/api/connections/mint', () => HttpResponse.json({ ok: true, state: 'minting' })),
      http.get('/api/connections/mint', () => {
        failReads = true
        return HttpResponse.json({ slug: 'github', state: 'granted' })
      }),
      http.get('/api/cards/pending', () => (failReads
        ? HttpResponse.json({ error: 'store unreadable', code: 'read_failed' }, { status: 500 })
        : HttpResponse.json({ cards: pending }))),
    )
    pending = [card({
      kind: 'connection.connect',
      plan: {
        apply: [
          { method: 'POST', path: '/api/connections/mint', body: { slug: 'github' } },
          { method: 'GET', path: '/api/connections/mint?slug=github', body: null, repeat: true },
        ],
        undo: null,
      },
    })]
    renderCards()
    fireEvent.click(await screen.findByTestId('change-card-apply'))
    await waitFor(() => expect(screen.getByTestId('change-card-error')).toBeTruthy())
    expect(stepCalls().map(c => c.method)).toEqual(['POST', 'GET'])
    await waitFor(() => expect((screen.getByTestId('change-card-apply') as HTMLButtonElement).disabled).toBe(false))
  })

  it('stops on a replayed apply and shows the recorded card', async () => {
    server.use(http.post('/api/agents', () => {
      pending = [{ ...pending[0], status: 'applied', result: { summary: 'Already created radar' } }]
      return HttpResponse.json({ ok: true, card_replay: true, card: pending[0] })
    }))
    pending = [card({
      kind: 'crewmate.create',
      plan: { apply: [{ method: 'POST', path: '/api/agents', body: {} }, { method: 'POST', path: '/api/crons', body: {} }], undo: null },
    })]
    renderCards()
    fireEvent.click(await screen.findByTestId('change-card-apply'))
    expect(await screen.findByText(L('result_applied', { title: 'Shorter replies' }))).toBeTruthy()
    expect(stepCalls().map(c => c.url)).toEqual(['/api/agents'])
  })

  it('maps a refusal code to its message and offers a refreshed preview', async () => {
    server.use(http.patch('/api/config/kirocrew', () => HttpResponse.json({ error: 'raw', code: 'changed_since_preview' }, { status: 409 })))
    pending = [card()]
    renderCards()
    fireEvent.click(await screen.findByTestId('change-card-apply'))
    await waitFor(() => expect(screen.getByTestId('change-card-error').textContent).toContain(L('refusal_changed_since_preview')))
    fireEvent.click(screen.getByTestId('change-card-refresh'))
    await waitFor(() => expect(calls.filter(c => c.url.endsWith('/preview'))).toHaveLength(1))
    expect(JSON.parse(calls.find(c => c.url.endsWith('/preview'))!.body)).toEqual({ params: pending[0].params, revision: 1 })
  })

  it('an undo refused because the thing changed offers no Refresh, which could not make it valid again', async () => {
    const { wantsRefresh } = await import('./refusals')
    expect(wantsRefresh('changed_since_apply')).toBe(false)
    expect(wantsRefresh('changed_since_preview')).toBe(true)
    expect(L('refusal_changed_since_apply')).not.toMatch(/Refresh/)
  })

  it('keeps a widening card disabled until the risk is acknowledged', async () => {
    pending = [card({ kind: 'trust.app', risk: 'widen' })]
    renderCards()
    const apply = await screen.findByTestId('change-card-apply') as HTMLButtonElement
    expect(screen.getByTestId('change-card-risk').textContent).toContain(L('risk_widen'))
    expect(apply.disabled).toBe(true)
    fireEvent.click(apply)
    expect(stepCalls()).toHaveLength(0)
    fireEvent.click(screen.getByTestId('change-card-widen-ack'))
    expect(apply.disabled).toBe(false)
    // A new revision (a re-preview) asks again.
    act(() => applyCardUpdate(qc, { ...pending[0], revision: 2 }))
    await waitFor(() => expect((screen.getByTestId('change-card-apply') as HTMLButtonElement).disabled).toBe(true))
  })

  it('shows the risk bar for code-running kinds without a checkbox gate', async () => {
    pending = [card({ kind: 'mcp.install', risk: 'code_exec' })]
    renderCards()
    expect((await screen.findByTestId('change-card-risk')).textContent).toContain(L('risk_code_exec'))
    expect(screen.queryByTestId('change-card-widen-ack')).toBeNull()
    expect((screen.getByTestId('change-card-apply') as HTMLButtonElement).disabled).toBe(false)
  })

  it('still gates on the acknowledgement when a code_exec card also carries the widen flag', async () => {
    // A capabilities card that BOTH launches a server and expands auto-approval
    // reads risk: code_exec (the worst label) yet carries widen separately, so
    // the "runs without asking" acknowledgement is still required.
    pending = [card({ kind: 'crewmate.capabilities', risk: 'code_exec', widen: true })]
    renderCards()
    const apply = await screen.findByTestId('change-card-apply') as HTMLButtonElement
    // The badge is the code_exec message (the worst label wins).
    expect(screen.getByTestId('change-card-risk').textContent).toContain(L('risk_code_exec'))
    // But the acknowledgement checkbox is present and gates Apply.
    const ack = screen.getByTestId('change-card-widen-ack') as HTMLInputElement
    expect(apply.disabled).toBe(true)
    fireEvent.click(ack)
    expect(apply.disabled).toBe(false)
    // A re-preview (new revision) asks for the acknowledgement again.
    act(() => applyCardUpdate(qc, { ...pending[0], revision: 2 }))
    await waitFor(() => expect((screen.getByTestId('change-card-apply') as HTMLButtonElement).disabled).toBe(true))
  })

  it('sends the typed secret only to /api/secrets and clears it afterwards', async () => {
    pending = [card({
      kind: 'secret.save',
      title: 'Save GITHUB_TOKEN',
      editable: ['value'],
      params: { name: 'GITHUB_TOKEN' },
      plan: {
        apply: [{ method: 'POST', path: '/api/secrets', body: { name: 'GITHUB_TOKEN', value: '{{user:value}}' }, fill: [{ field: 'value', source: 'user' }] }],
        undo: [{ method: 'DELETE', path: '/api/secrets/GITHUB_TOKEN' }],
      },
    })]
    renderCards()
    const apply = await screen.findByTestId('change-card-apply') as HTMLButtonElement
    expect(apply.disabled).toBe(true)
    const input = screen.getByTestId('change-card-input-value') as HTMLInputElement
    expect(input.type).toBe('password')
    fireEvent.change(input, { target: { value: SECRET } })
    fireEvent.click(apply)
    await waitFor(() => expect(stepCalls()).toHaveLength(1))
    await waitFor(() => expect(calls.some(c => c.url.startsWith('/api/cards/pending') && calls.indexOf(c) > 0)).toBe(true))
    const carrying = calls.filter(c => c.body.includes(SECRET) || c.url.includes(SECRET) || Object.values(c.headers).some(v => v.includes(SECRET)))
    expect(carrying.map(c => [c.method, c.url])).toEqual([['POST', '/api/secrets']])
    expect(JSON.parse(carrying[0].body)).toEqual({ name: 'GITHUB_TOKEN', value: SECRET })
    await waitFor(() => expect((screen.getByTestId('change-card-input-value') as HTMLInputElement).value).toBe(''))
    expect(JSON.stringify(qc.getQueryData(cardsQueryKey(SLOT)))).not.toContain(SECRET)
  })

  it('offers no free-text Edit: a value is changed by asking Mate for a new card', async () => {
    pending = [card({ kind: 'setting.change', editable: ['value'], params: { path: 'agent.fallback_model', value: 'x' } })]
    renderCards()
    await screen.findByTestId('change-card-apply')
    expect(screen.queryByTestId('change-card-edit')).toBeNull()
    expect(screen.queryByRole('textbox')).toBeNull()
  })

  it('continues an apply a reload interrupted, from the step the gateway expects', async () => {
    pending = [card({
      kind: 'crewmate.create',
      title: 'Create crewmate Scout',
      status: 'applying',
      params: { name: 'Scout', goal: 'g', schedule: { cron_expr: '0 9 * * *' } },
      plan: {
        apply: [
          { method: 'POST', path: '/api/agents', body: { name: 'Scout' } },
          { method: 'POST', path: '/api/crons', body: { member_id: '', cron_expr: '0 9 * * *' }, fill: [{ field: 'member_id', source: 'step', step: 0, key: 'member_id' }] },
        ],
        undo: null,
      },
      progress: { op: 'apply', done: 1, total: 2 },
      resume: { step: 1, responses: [{ member_id: 'm-7', name: 'scout' }] },
    })]
    renderCards()
    const go = await screen.findByTestId('change-card-apply') as HTMLButtonElement
    expect(go.textContent).toBe(L('continue_apply'))
    expect(go.disabled).toBe(false)
    fireEvent.click(go)
    await waitFor(() => expect(stepCalls()).toHaveLength(1))
    const sent = stepCalls()[0]
    expect([sent.method, sent.url, sent.headers['x-card-step']]).toEqual(['POST', '/api/crons', '1'])
    expect(JSON.parse(sent.body)).toEqual({ member_id: 'm-7', cron_expr: '0 9 * * *' })
  })

  it('offers Continue on a card left waiting on an approval poll', async () => {
    pending = [card({
      kind: 'connection.connect',
      title: 'Connect GitHub',
      status: 'applying',
      params: { slug: 'github' },
      plan: {
        apply: [
          { method: 'POST', path: '/api/agents', body: {} },
          { method: 'POST', path: '/api/crons', body: {}, repeat: true },
        ],
        undo: null,
      },
      progress: { op: 'apply', done: 1, total: 2, waiting: true },
      resume: { step: 1, responses: [{}] },
    })]
    renderCards()
    const go = await screen.findByTestId('change-card-apply') as HTMLButtonElement
    expect(go.textContent).toBe(L('continue_apply'))
    expect(go.disabled).toBe(false)
  })

  it('continues an Undo that stopped between its steps', async () => {
    server.use(http.delete('/api/agents/:name', () => HttpResponse.json({ ok: true })))
    pending = [card({
      kind: 'crewmate.create',
      title: 'Create crewmate Scout',
      status: 'applied',
      params: { name: 'Scout', goal: 'g', schedule: { cron_expr: '0 9 * * *' } },
      plan: {
        apply: [{ method: 'POST', path: '/api/agents', body: { name: 'Scout' } }],
        undo: [
          { method: 'DELETE', path: '/api/crons/job1' },
          { method: 'DELETE', path: '/api/agents/scout' },
        ],
      },
      undo_resume: { step: 1, responses: [{}] },
    })]
    renderCards()
    fireEvent.click(await screen.findByTestId('change-card-undo'))
    await waitFor(() => expect(stepCalls()).toHaveLength(1))
    const sent = stepCalls()[0]
    expect([sent.method, sent.url, sent.headers['x-card-op'], sent.headers['x-card-step']]).toEqual(['DELETE', '/api/agents/scout', 'undo', '1'])
  })

  it('keeps a closed undo error closed after the card refetches', async () => {
    pending = [card({ status: 'applied', error: { code: 'changed_since_apply', message: 'changed', op: 'undo' } })]
    renderCards()
    const notice = await screen.findByTestId('change-card-undo-error')
    fireEvent.click(within(notice).getByRole('button'))
    await waitFor(() => expect(screen.queryByTestId('change-card-undo-error')).toBeNull())
  })

  it('disables an expired card and says how to get a new one', async () => {
    pending = [card({ expires_at: NOW - 1000 })]
    renderCards()
    expect(await screen.findByTestId('change-card-expired')).toBeTruthy()
    expect((screen.getByTestId('change-card-apply') as HTMLButtonElement).disabled).toBe(true)
    expect(screen.queryByTestId('change-card-cancel')).toBeNull()
  })

  it('cancels through the gateway and shows the muted result line', async () => {
    pending = [card()]
    renderCards()
    fireEvent.click(await screen.findByTestId('change-card-cancel'))
    expect(await screen.findByText(L('result_cancelled', { title: 'Shorter replies' }))).toBeTruthy()
    expect(screen.queryByTestId('change-card-undo')).toBeNull()
  })
})

describe('result line', () => {
  it('takes focus when the card the user acted on collapses into it', async () => {
    pending = [card()]
    renderCards()
    const apply = await screen.findByTestId('change-card-apply')
    apply.focus()
    fireEvent.click(apply)
    await waitFor(() => expect(stepCalls()).toHaveLength(1))
    act(() => applyCardUpdate(qc, { ...pending[0], status: 'applied', result: { summary: 'Replies are brief now' } }))
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId('change-card-result')))
  })

  it('undoes through the undo plan with undo headers', async () => {
    pending = [card({ status: 'applied', result: { summary: 'Replies are brief now' } })]
    renderCards()
    fireEvent.click(await screen.findByTestId('change-card-undo'))
    await waitFor(() => expect(stepCalls()).toHaveLength(1))
    const [u] = stepCalls()
    expect([u.method, u.url, JSON.parse(u.body)]).toEqual(['PATCH', '/api/config/kirocrew', { path: 'chat.verbosity', value: 'standard' }])
    expect(u.headers).toMatchObject({ 'x-card-op': 'undo', 'x-card-step': '0' })
  })

  it('names the undo for what it does and says when undo is unavailable', async () => {
    pending = [
      card({ id: 'a', kind: 'secret.save', status: 'applied', undo_label: 'delete_secret', created_at: 1 }),
      card({ id: 'b', kind: 'secret.save', status: 'applied', plan: { apply: [], undo: null }, undo_unavailable_reason: 'overwrites_existing', created_at: 2 }),
    ]
    renderCards()
    // Each finished card is its own one-line row, at its own place: no stack.
    expect(await screen.findAllByTestId('change-card-result')).toHaveLength(2)
    expect((await screen.findByTestId('change-card-undo')).textContent).toBe(L('undo_delete_secret'))
    expect(screen.getByTestId('change-card-undo-unavailable').textContent).toBe(L('undo_unavailable_overwrites'))
  })

  it('reads an undone card as restored, with no undo', async () => {
    pending = [card({ status: 'undone' })]
    renderCards()
    expect(await screen.findByText(L('result_undone', { title: 'Shorter replies' }))).toBeTruthy()
    expect(screen.queryByTestId('change-card-undo')).toBeNull()
  })
})

describe('one-shot schedule', () => {
  const LA = 'America/Los_Angeles'
  // Exactly a day out is tomorrow in every zone, so the assertion holds on any host clock.
  const tomorrow = () => Math.floor(NOW / 1000) + 86_400
  const once = (over: Partial<Card> = {}) => card({
    kind: 'schedule.create',
    title: 'Call the vet',
    once: true,
    timezone: LA,
    next_run_at: tomorrow(),
    params: { name: 'call-vet', at: 'tomorrow 9am', timezone: LA, once: true },
    changes: [{ field: 'name', label: 'Name', after: 'call-vet' }],
    editable: ['at'],
    ...over,
  })

  it('says it runs once, with the date, and no recurrence or next run', async () => {
    pending = [once()]
    renderCards()
    const row = await screen.findByTestId('change-card-once')
    expect(row.textContent).toMatch(/^Tomorrow, .+ · .+ P[DS]T$/)
    expect(screen.getByText(L('schedule_once'))).toBeTruthy()
    expect(screen.queryByText(L('schedule_next_run'))).toBeNull()
    expect(screen.queryByText(L('field_cron_expr'))).toBeNull()
  })

  it('names the `at` row "Runs once" and drops a cron row', async () => {
    pending = [once({
      changes: [
        { field: 'at', label: 'at', after: new Date(tomorrow() * 1000).toISOString() },
        { field: 'cron_expr', label: 'cron', after: '0 9 * * 1-5' },
      ],
    })]
    renderCards()
    await screen.findByTestId('change-card')
    expect(screen.getByText(L('schedule_once'))).toBeTruthy()
    expect(screen.getAllByTestId('change-card-value').map(v => v.textContent)).toEqual([expect.stringMatching(/^Tomorrow, /)])
    expect(screen.queryByTestId('change-card-once')).toBeNull()
    expect(screen.queryByText(L('field_cron_expr'))).toBeNull()
  })

  it('collapses to a result line that says the schedule is created and when it runs', async () => {
    pending = [once({ status: 'applied', result: { summary: 'Done: Call the vet' } })]
    renderCards()
    const line = await screen.findByText(/call-vet/)
    expect(line.textContent).toMatch(/^Schedule created: call-vet · runs once, tomorrow, .+ · .+ P[DS]T$/)
  })

  it('names its button for a reminder and counts down to the run time', async () => {
    pending = [once({ next_run_at: Math.floor(NOW / 1000) + 150 })]
    renderCards()
    const apply = await screen.findByTestId('change-card-apply')
    expect(apply.textContent).toBe(L('primary_schedule_once'))
    expect(screen.getByTestId('change-card-due').textContent).toMatch(/2 minutes/)
    expect(screen.queryByTestId('change-card-expires')).toBeNull()
  })

  it('takes the button away the moment the run time passes, says so aloud, and keeps focus on the card', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true, now: NOW })
    try {
      // 2s ahead: well inside one clock tick, so only the exact deadline timer can catch it.
      const at = NOW + 2_000
      pending = [once({ next_run_at: at / 1000 })]
      renderCards()
      const apply = await screen.findByTestId('change-card-apply')
      act(() => apply.focus())
      await act(async () => { vi.advanceTimersByTime(2_100) })
      expect(screen.queryByTestId('change-card-apply')).toBeNull()
      const status = screen.getByTestId('change-card-status')
      expect(status.textContent).toBe(L('time_passed', { name: 'Kiro Crew' }))
      expect(status.getAttribute('aria-live')).toBe('polite')
      expect(document.activeElement).toBe(screen.getByTestId('change-card-cancel'))
    } finally {
      vi.useRealTimers()
    }
  })

  it('re-reads the clock at the press, so a just-passed reminder sends nothing', async () => {
    const at = NOW + 60_000
    pending = [once({ next_run_at: at / 1000 })]
    renderCards()
    const apply = await screen.findByTestId('change-card-apply')
    vi.setSystemTime(at + 1)
    calls = []
    await act(async () => { fireEvent.click(apply) })
    expect(calls.filter(c => c.method !== 'GET')).toEqual([])
    expect(screen.queryByTestId('change-card-apply')).toBeNull()
  })

  it('keeps "Create schedule" on a recurring schedule', async () => {
    pending = [card({ kind: 'schedule.create', params: { cron_expr: '0 9 * * *' } })]
    renderCards()
    expect((await screen.findByTestId('change-card-apply')).textContent).toBe(L('primary_schedule_create'))
  })

  it('leaves a recurring schedule on its next run', async () => {
    pending = [card({ kind: 'schedule.create', timezone: LA, next_run_at: tomorrow(), params: { cron_expr: '0 9 * * *' } })]
    renderCards()
    expect(await screen.findByText(L('schedule_next_run'))).toBeTruthy()
    expect(screen.queryByTestId('change-card-once')).toBeNull()
  })

  it('shows a changed time zone once, in its change row', async () => {
    pending = [card({
      kind: 'schedule.update', timezone: LA, next_run_at: tomorrow(),
      params: { job_id: 'job-1', cron_expr: '30 8 * * 1-5', timezone: LA },
      changes: [{ field: 'timezone', label: 'Time zone', before: 'UTC', after: LA }],
    })]
    renderCards()
    await screen.findByText(L('schedule_next_run'))
    expect(screen.getAllByText(LA)).toHaveLength(1)
  })

  it('keeps the time zone row when the change leaves the zone alone', async () => {
    pending = [card({ kind: 'schedule.create', timezone: LA, next_run_at: tomorrow(), params: { cron_expr: '0 9 * * *', timezone: LA } })]
    renderCards()
    expect(await screen.findByText(L('schedule_timezone'))).toBeTruthy()
    expect(screen.getByText(LA)).toBeTruthy()
  })
})

describe('a card in the conversation', () => {
  it('turns into its result line IN PLACE when the gateway reports it applied', async () => {
    pending = [card()]
    renderCards()
    const host = await screen.findByTestId('conversation-card')
    expect(host.contains(await screen.findByTestId('change-card-apply'))).toBe(true)
    act(() => applyCardUpdate(qc, { ...pending[0], status: 'applied', result: { summary: 'x' } }))
    const line = await screen.findByTestId('change-card-result')
    // Same conversation row: the card did not move to another place on the page.
    expect(screen.getByTestId('conversation-card')).toBe(host)
    expect(host.contains(line)).toBe(true)
    expect(screen.queryByTestId('change-card-apply')).toBeNull()
  })

  it('draws a finished card the store no longer holds from its own row, with no actions', async () => {
    pending = []
    renderCards([row(card({ status: 'applied' }))])
    const line = await screen.findByTestId('change-card-record')
    expect(line.getAttribute('data-card-status')).toBe('applied')
    expect(line.textContent).toBe(L('result_applied', { title: 'Shorter replies' }))
    expect(screen.queryByTestId('change-card-apply')).toBeNull()
    expect(screen.queryByTestId('change-card-undo')).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('reads a proposal the store forgot as expired, once the store has answered', async () => {
    pending = []
    renderCards([row(card({ status: 'pending' }))])
    const line = await screen.findByTestId('change-card-record')
    expect(line.getAttribute('data-card-status')).toBe('expired')
    expect(line.textContent).toBe(L('result_expired', { title: 'Shorter replies' }))
  })

  it('says the card read failed instead of calling an unfinished proposal expired', async () => {
    server.use(http.get('/api/cards/pending', () => HttpResponse.json({ error: 'card store unavailable', code: 'x' }, { status: 500 })))
    renderCards([row(card({ status: 'pending' }))])
    const notice = await screen.findByTestId('change-card-read-error')
    expect(notice.textContent).toContain('card store unavailable')
    expect(screen.queryByTestId('change-card-record')).toBeNull()
    // No hand-off: the row sits beside the composer's unsent draft.
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('keeps a finished record line beside the read failure', async () => {
    server.use(http.get('/api/cards/pending', () => HttpResponse.json({ error: 'card store unavailable', code: 'x' }, { status: 500 })))
    renderCards([row(card({ status: 'applied' }))])
    expect(await screen.findByTestId('change-card-read-error')).toBeTruthy()
    expect(screen.getByTestId('change-card-record').getAttribute('data-card-status')).toBe('applied')
  })

  it('never flashes a live proposal as expired while the store is still answering', async () => {
    pending = [card()]
    renderCards()
    // First paint, before the card read resolves: nothing rather than a wrong verdict.
    expect(screen.queryByTestId('change-card-record')).toBeNull()
    expect(await screen.findByTestId('change-card')).toBeTruthy()
    expect(screen.queryByTestId('change-card-record')).toBeNull()
  })

  it('never draws another slot\'s live card from a row that names it', async () => {
    // The transcript is agent-writable: a row in this chat naming a card of
    // another conversation draws only its own text, never that card's controls.
    pending = [card({ slot_key: 'slot-B' })]
    renderCards([row(card())])
    expect(await screen.findByTestId('change-card-record')).toBeTruthy()
    expect(screen.queryByTestId('change-card')).toBeNull()
    expect(stepCalls()).toHaveLength(0)
  })

  it('draws only the record line for a row that claims another slot', async () => {
    pending = [card()]
    renderCards([row(card(), { slot: 'slot-B' })])
    expect(await screen.findByTestId('change-card-record')).toBeTruthy()
    expect(screen.queryByTestId('change-card')).toBeNull()
  })

  it('gives a finished result line no close control: it is the record of the change', async () => {
    pending = [card({ status: 'applied', result: { summary: 'x' } })]
    renderCards()
    await screen.findByTestId('change-card-result')
    expect(screen.queryByRole('button', { name: L('dismiss') })).toBeNull()
  })

  it('reads only a well-formed reference off a card row', () => {
    const base = row(card())
    expect(readCardRef(base)).toMatchObject({ surface: 'change', id: 'c1', slot: SLOT, status: 'pending' })
    expect(readCardRef({ ...base, role: 'assistant' })).toBeNull()
    expect(readCardRef({ ...base, meta: { card: { surface: 'other', id: 'c1' } } })).toBeNull()
    expect(readCardRef({ ...base, meta: { card: { surface: 'change' } } })).toBeNull()
    expect(readCardRef({ ...base, meta: { card: 'c1' } })).toBeNull()
  })
})
