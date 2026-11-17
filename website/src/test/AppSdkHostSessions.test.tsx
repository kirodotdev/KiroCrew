/**
 * The App SDK's `useHostSessions` reaches the dashboard's sessions only for an
 * app that declares what each member sends.
 *
 * An external app gets a member once its `permissions.api` admits every path
 * that member sends; a member not admitted sends nothing (`sessions` is null, an
 * act rejects with `AppApiPermissionError`). A builtin page gets every member,
 * and a surface with no app identity fails closed.
 *
 * The dashboard client is real and `fetch` is stubbed, so every request a member
 * sends is checked against the declaration that admitted it: the documented list
 * covers every path the members call.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { Provider } from 'react-redux'
import '../i18n/all'
import { AppApiProvider, useHostSessions } from '../app-sdk/index'
import { AppIdentityProvider } from '../app-sdk/identity'
import { AppApiPermissionError } from '../app-sdk/apiError'
import { apiPathAllowed } from '../app-sdk/scopedApi'
import { queryClient as dashboardQueryClient } from '../api/queryClient'
import { i18nT } from '../i18n/t'
import type { RootState } from '../store'
import { createTestStore } from './helpers'

/** The declaration the API reference documents for every member. */
const DOCUMENTED = ['/api/chat/slots', '=/api/config/default-agent', '=/api/chat', '/api/approvals', '=/api/dashboard/config']

const SLOTS = [
  { key: 'chat-old', title: 'Older work', messages: 3, running: false, last_ts: '2026-11-01T10:00:00Z', agent: '', last_message: 'done' },
  {
    key: 'chat-new', title: 'chat-new', messages: 7, running: true, last_ts: '2026-11-02T10:00:00Z', agent: 'reviewer', agent_kind: '',
    last_message: 'working on it', waiting_for_input: false,
    pending_approval_info: { origin: 'native', request_id: 'r-1', request_mid: 'm-1', tool: 'shell', tool_input: '', tool_kind: 'execute' },
  },
  {
    key: 'chat-bg', title: 'Background', messages: 1, running: false, last_ts: '2026-10-30T10:00:00Z', agent: '',
    pending_approval_info: { origin: 'coordinator', request_id: 'c-9', request_instance: 'i-4', tool: 'spawn', tool_input: '', tool_kind: 'other' },
  },
]

let fetchMock: ReturnType<typeof vi.fn>
let requests: { path: string; method: string; body: unknown }[]
let decideStatus: number
let decideBody: string

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

beforeEach(() => {
  requests = []
  decideStatus = 200
  decideBody = '{"ok":true}'
  dashboardQueryClient.clear()
  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost')
    const method = (init?.method ?? 'GET').toUpperCase()
    const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined
    requests.push({ path: url.pathname, method, body })
    if (url.pathname === '/api/config/default-agent') return json({ default_agent: 'kiro', default_template: 'kiro', overridden: false })
    if (url.pathname === '/api/dashboard/config') return json({ default_memory_mode: 'standard' })
    if (url.pathname === '/api/chat') return json({ ok: true, dispatched: true })
    if (url.pathname === '/api/chat/slots' && method === 'POST') return json({ key: 'chat-made', title: 'chat-made' })
    if (url.pathname.endsWith('/approve') || url.pathname.startsWith('/api/approvals/')) {
      return new Response(decideBody, { status: decideStatus, headers: { 'Content-Type': 'application/json' } })
    }
    return json({})
  })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function storeWithSlots() {
  const defaults = createTestStore().getState()
  return createTestStore({
    dashboard: { ...defaults.dashboard, slots: SLOTS } as unknown as RootState['dashboard'],
  })
}

let appSeq = 0

function external(api: string[]) {
  const store = storeWithSlots()
  const name = `app-host-sessions-${++appSeq}`
  const wrapper = ({ children }: { children: ReactNode }) => createElement(Provider, { store },
    createElement(AppApiProvider, { appName: name, allowedApiPaths: api, allowedEvents: [] }, children))
  return renderHook(() => useHostSessions(), { wrapper })
}

function builtin() {
  const store = storeWithSlots()
  const wrapper = ({ children }: { children: ReactNode }) => createElement(Provider, { store },
    createElement(AppIdentityProvider, { appId: 'some-builtin', origin: 'builtin' }, children))
  return renderHook(() => useHostSessions(), { wrapper })
}

function bare() {
  const store = storeWithSlots()
  const wrapper = ({ children }: { children: ReactNode }) => createElement(Provider, { store }, children)
  return renderHook(() => useHostSessions(), { wrapper })
}

describe('an external app with nothing declared', () => {
  it('sees no sessions and sends nothing for any act', async () => {
    const { result } = external([])
    expect(result.current.sessions).toBeNull()
    expect(result.current.can).toEqual({ sessions: false, send: false, decide: false, create: false })
    await expect(result.current.send('chat-new', 'hi')).rejects.toBeInstanceOf(AppApiPermissionError)
    await expect(result.current.create()).rejects.toBeInstanceOf(AppApiPermissionError)
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('an external app with the documented declaration', () => {
  it('reads the live list newest first, as the session list labels it', async () => {
    const { result } = external(DOCUMENTED)
    await waitFor(() => expect(result.current.sessions?.[2]?.agentLabel).not.toBe('default'))
    const sessions = result.current.sessions!
    expect(sessions.map((s) => s.key)).toEqual(['chat-new', 'chat-old', 'chat-bg'])
    expect(sessions[0]).toMatchObject({
      title: i18nT('pages.artifactDetailPage.untitled_session'), agentLabel: 'reviewer', running: true,
      messageCount: 7, lastMessage: 'working on it', waitingForInput: false,
    })
    expect(sessions[0].pendingApproval).toMatchObject({ sessionKey: 'chat-new', tool: 'shell' })
    expect(sessions[1]).toMatchObject({ title: 'Older work', pendingApproval: null })
    expect(result.current.can).toEqual({ sessions: true, send: true, decide: true, create: true })
  })

  it('sends a reply or a steer through the dashboard send', async () => {
    const { result } = external(DOCUMENTED)
    let sent: unknown
    await act(async () => { sent = await result.current.send('chat-new', 'look at the logs', { steer: true }) })
    expect(sent).toEqual({ status: 'dispatched' })
    const post = requests.find((r) => r.path === '/api/chat')!
    expect(post.body).toMatchObject({ message: 'look at the logs', slot: 'chat-new', steer: true })
  })

  it('creates a session the person owns, not one the app owns', async () => {
    const { result } = external(DOCUMENTED)
    let made: unknown
    await act(async () => { made = await result.current.create() })
    expect(made).toEqual({ key: 'chat-made' })
    const post = requests.find((r) => r.path === '/api/chat/slots' && r.method === 'POST')!
    expect(post.body).not.toHaveProperty('embed_app')
  })

  it('decides a session request on its slot route and a background one on the approvals route', async () => {
    const { result } = external(DOCUMENTED)
    await waitFor(() => expect(result.current.sessions).not.toBeNull())
    const [running, , background] = result.current.sessions!
    let outcome: unknown
    await act(async () => { outcome = await result.current.decide(running.pendingApproval!, 'approve') })
    expect(outcome).toEqual({ outcome: 'decided' })
    expect(requests.find((r) => r.path === '/api/chat/slots/chat-new/approve')!.body)
      .toMatchObject({ action: 'approved', origin: 'native', request_id: 'r-1', request_mid: 'm-1' })
    await act(async () => { outcome = await result.current.decide(background.pendingApproval!, 'reject') })
    expect(outcome).toEqual({ outcome: 'decided' })
    expect(requests.some((r) => r.path === '/api/approvals/c-9/reject')).toBe(true)
  })

  it('every request a member sent is one the declaration admits', async () => {
    const { result } = external(DOCUMENTED)
    await waitFor(() => expect(result.current.sessions).not.toBeNull())
    const [running, , background] = result.current.sessions!
    await act(async () => {
      await result.current.send('chat-old', 'hello')
      await result.current.create()
      await result.current.decide(running.pendingApproval!, 'reject')
      await result.current.decide(background.pendingApproval!, 'approve')
    })
    expect(requests.length).toBeGreaterThanOrEqual(5)
    for (const r of requests) expect(apiPathAllowed(DOCUMENTED, r.path), r.path).toBe(true)
  })

  it('names a lost race and a gone request in the approval card words', async () => {
    const { result } = external(DOCUMENTED)
    await waitFor(() => expect(result.current.sessions).not.toBeNull())
    const approval = result.current.sessions![0].pendingApproval!
    decideStatus = 409
    decideBody = JSON.stringify({ code: 'already_resolved', decision: { decision_id: 'd-1', outcome: 'approved' } })
    let outcome: unknown
    await act(async () => { outcome = await result.current.decide(approval, 'reject') })
    expect(outcome).toEqual({ outcome: 'settled', message: i18nT('sharedCrew.already_approved') })
    decideStatus = 404
    decideBody = '{"error":"not pending"}'
    await act(async () => { outcome = await result.current.decide(approval, 'approve') })
    expect(outcome).toEqual({ outcome: 'gone', message: i18nT('components.approvalCard.approval_no_longer_pending') })
    decideStatus = 500
    decideBody = '{"error":"store unavailable"}'
    await act(async () => { outcome = await result.current.decide(approval, 'approve') })
    expect(outcome).toMatchObject({ outcome: 'failed' })
    expect((outcome as { message: string }).message).toContain('store unavailable')
  })

  it('decides nothing for an approval it did not hand out', async () => {
    const { result } = external(DOCUMENTED)
    const forged = { sessionKey: 'chat-new', tool: 'shell', key: 'x' }
    let outcome: unknown
    await act(async () => { outcome = await result.current.decide(forged, 'approve') })
    expect(outcome).toMatchObject({ outcome: 'gone' })
    expect(requests.some((r) => r.method === 'POST')).toBe(false)
  })
})

describe('each member is admitted on its own', () => {
  it('a send-only declaration sends and lists nothing', async () => {
    const { result } = external(['=/api/chat'])
    expect(result.current.sessions).toBeNull()
    expect(result.current.can).toEqual({ sessions: false, send: true, decide: false, create: false })
    await act(async () => { await result.current.send('chat-new', 'hi') })
    expect(requests.map((r) => r.path)).toEqual(['/api/chat'])
  })

  it('the list without the default agent read is not admitted', () => {
    const { result } = external(['/api/chat/slots'])
    expect(result.current.sessions).toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('a create without the memory mode read refuses before sending', async () => {
    const { result } = external(['/api/chat/slots', '=/api/config/default-agent'])
    await expect(result.current.create()).rejects.toBeInstanceOf(AppApiPermissionError)
    expect(requests.some((r) => r.method === 'POST')).toBe(false)
  })

  it('a background decide needs /api/approvals declared, a session decide does not', async () => {
    const { result } = external(['/api/chat/slots', '=/api/config/default-agent'])
    await waitFor(() => expect(result.current.sessions).not.toBeNull())
    const [running, , background] = result.current.sessions!
    await expect(result.current.decide(background.pendingApproval!, 'approve')).rejects.toBeInstanceOf(AppApiPermissionError)
    expect(requests.some((r) => r.path.startsWith('/api/approvals'))).toBe(false)
    let outcome: unknown
    await act(async () => { outcome = await result.current.decide(running.pendingApproval!, 'approve') })
    expect(outcome).toEqual({ outcome: 'decided' })
  })
})

describe('the other identities', () => {
  it('a builtin page gets every member with nothing declared', async () => {
    const { result } = builtin()
    expect(result.current.sessions?.length).toBe(3)
    expect(result.current.can).toEqual({ sessions: true, send: true, decide: true, create: true })
    await act(async () => { await result.current.send('chat-old', 'hi') })
    expect(requests.some((r) => r.path === '/api/chat')).toBe(true)
  })

  it('a surface with no app identity fails closed', async () => {
    const { result } = bare()
    expect(result.current.sessions).toBeNull()
    await expect(result.current.send('chat-new', 'hi')).rejects.toBeInstanceOf(AppApiPermissionError)
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
