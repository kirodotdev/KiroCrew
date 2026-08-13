/**
 * panelBridge — the Mochi panel's whole transport seam.
 *
 * Every ported component talks to the gateway through this module, so the
 * behaviours worth pinning are the ones a component cannot see: which HTTP route
 * and body a call produces, what a degraded response degrades TO (an empty list,
 * a named error, a thrown refusal), and how one WebSocket frame fans out to the
 * right subscriber set.
 *
 * The module is a singleton that opens its socket at import time and captures
 * `window.mochi` once, so each test re-imports it through `loadBridge()` after
 * the socket, `fetch` and the shell table are stubbed. `fetch` is answered from a
 * per-test route table (matched on URL substring plus method, because GET and
 * POST `/api/chat/slots` mean two different things), and frames are pushed
 * through the captured socket rather than a real one — nothing here touches the
 * network, a real timer or an animation frame.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

type Bridge = typeof import('../apps/mochi/panel/panelBridge')
type ShellFn = (...args: unknown[]) => unknown
type ShellTable = Record<string, ShellFn | undefined>

/** `../apps/mochi/api` is the only collaborator module; the rest is raw fetch. */
const api = vi.hoisted(() => ({
  getWatchlist: vi.fn(async (): Promise<{ items: unknown[] }> => ({ items: [] })),
  updateWatchlist: vi.fn(async (_p: unknown): Promise<{ ok: boolean }> => ({ ok: true })),
  getPinned: vi.fn(async (): Promise<{ pins: unknown[] }> => ({ pins: [] })),
  markPinnedSeen: vi.fn(async (_p: string): Promise<{ ok: boolean }> => ({ ok: true })),
  unpinFile: vi.fn(async (_p: string): Promise<{ ok: boolean }> => ({ ok: true })),
}))

vi.mock('../apps/mochi/api', () => api)

// ── fetch route table ───────────────────────────────────────────────────────

interface Reply {
  ok?: boolean
  status?: number
  body?: unknown
  /** Response arrives but its body is not JSON. */
  jsonThrows?: boolean
  /** The request itself never completes. */
  reject?: boolean
}

interface Route {
  url: string
  method?: string
  reply: Reply
}

let routes: Route[] = []

/** Register a reply; later registrations win, so a test can override a default. */
function route(url: string, reply: Reply, method?: string): void {
  routes.unshift({ url, method, reply })
}

const fetchMock = vi.fn(async (input: unknown, init?: RequestInit) => {
  const url = String(input)
  const method = (init?.method ?? 'GET').toUpperCase()
  const hit = routes.find(
    (r) => url.includes(r.url) && (r.method === undefined || r.method === method),
  )
  const reply = hit?.reply ?? {}
  if (reply.reject === true) throw new Error('network down')
  const ok = reply.ok ?? true
  return {
    ok,
    status: reply.status ?? (ok ? 200 : 500),
    json: async () => {
      if (reply.jsonThrows === true) throw new Error('not json')
      return reply.body ?? {}
    },
  }
})

interface SeenCall {
  url: string
  init: RequestInit
}

/** Requests made to a route, in order. */
function calls(url: string, method?: string): SeenCall[] {
  return fetchMock.mock.calls
    .map((c) => ({ url: String(c[0]), init: (c[1] ?? {}) as RequestInit }))
    .filter((c) => c.url.includes(url))
    .filter((c) => method === undefined || (c.init.method ?? 'GET').toUpperCase() === method)
}

function bodyOf(call: SeenCall): Record<string, unknown> {
  return JSON.parse(String(call.init.body ?? '{}')) as Record<string, unknown>
}

// ── socket double ───────────────────────────────────────────────────────────

class MockSocket {
  static instances: MockSocket[] = []
  onopen: (() => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  send = vi.fn()
  close = vi.fn(() => {
    this.onclose?.()
  })

  constructor(public url: string) {
    MockSocket.instances.push(this)
  }
}

function socket(): MockSocket {
  const last = MockSocket.instances[MockSocket.instances.length - 1]
  if (last === undefined) throw new Error('no socket was opened')
  return last
}

/** Push a raw gateway frame through the socket the bridge holds. */
function emit(frame: unknown): void {
  socket().onmessage?.({ data: JSON.stringify(frame) } as unknown as MessageEvent)
}

/** Push an app-scoped broadcast in the wire envelope the gateway namespaces it under. */
function emitApp(event: string, payload: unknown): void {
  emit({ type: 'app_event', data: { event, data: payload } })
}

// ── module loader ───────────────────────────────────────────────────────────

async function loadBridge(shell?: ShellTable): Promise<Bridge> {
  vi.resetModules()
  MockSocket.instances.length = 0
  const win = window as unknown as { mochi?: ShellTable }
  if (shell === undefined) delete win.mochi
  else win.mochi = shell
  return await import('../apps/mochi/panel/panelBridge')
}

/** Let the module's own `void`-ed promises settle before asserting. */
async function settle(): Promise<void> {
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
}

beforeEach(() => {
  routes = []
  fetchMock.mockClear()
  api.getWatchlist.mockClear()
  api.updateWatchlist.mockClear()
  api.getPinned.mockClear()
  api.markPinnedSeen.mockClear()
  api.unpinFile.mockClear()
  api.getWatchlist.mockResolvedValue({ items: [] })
  api.getPinned.mockResolvedValue({ pins: [] })
  vi.stubGlobal('WebSocket', MockSocket)
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  const win = window as unknown as { mochi?: ShellTable }
  delete win.mochi
})

// ── trust level ─────────────────────────────────────────────────────────────

describe('panelBridge trust level is scoped to the pet slot', () => {
  it('writes the level against the slot, never the global posture', async () => {
    const bridge = await loadBridge()
    await bridge.setMochiTrustLevel('trust_reads')
    const [call] = calls('/api/chat/mode', 'POST')
    expect(bodyOf(call)).toEqual({ mode: 'trust_reads', slot: bridge.MOCHI_SLOT })
  })

  it('reads the level off the BARE ARRAY the slots route returns', async () => {
    route('/api/chat/slots', { body: [{ key: 'mochi', trust: true }] }, 'GET')
    const bridge = await loadBridge()
    expect(await bridge.getMochiTrustLevel()).toBe('trust')
  })

  it('still reads a `slots`-wrapped payload', async () => {
    route('/api/chat/slots', { body: { slots: [{ key: 'mochi', trust_reads: true }] } }, 'GET')
    const bridge = await loadBridge()
    expect(await bridge.getMochiTrustLevel()).toBe('trust_reads')
  })

  it('reports yolo from the slot mode', async () => {
    route('/api/chat/slots', { body: [{ key: 'mochi', mode: 'yolo', trust: true }] }, 'GET')
    const bridge = await loadBridge()
    expect(await bridge.getMochiTrustLevel()).toBe('yolo')
  })

  it('is normal when the slot carries no trust flags', async () => {
    route('/api/chat/slots', { body: [{ key: 'mochi' }] }, 'GET')
    const bridge = await loadBridge()
    expect(await bridge.getMochiTrustLevel()).toBe('normal')
  })

  it('is normal when our slot is absent, the read fails, or the fetch throws', async () => {
    route('/api/chat/slots', { body: [{ key: 'other', trust: true }] }, 'GET')
    let bridge = await loadBridge()
    expect(await bridge.getMochiTrustLevel()).toBe('normal')

    route('/api/chat/slots', { ok: false, status: 500 }, 'GET')
    bridge = await loadBridge()
    expect(await bridge.getMochiTrustLevel()).toBe('normal')

    route('/api/chat/slots', { reject: true }, 'GET')
    bridge = await loadBridge()
    expect(await bridge.getMochiTrustLevel()).toBe('normal')
  })
})

describe('panelBridge stat reporting', () => {
  it('posts the countable kind and cannot reject', async () => {
    route('/api/apps/mochi/stat', { reject: true })
    const bridge = await loadBridge()
    expect(() => bridge.reportStat('screenshot')).not.toThrow()
    await settle()
    expect(bodyOf(calls('/api/apps/mochi/stat')[0])).toEqual({ kind: 'screenshot' })
  })

  it('a dropped pet-event report cannot fail the turn it describes', async () => {
    route('/pet-event', { reject: true })
    const bridge = await loadBridge()
    let done = 0
    bridge.onChatDone(() => { done += 1 })
    emit({ type: 'chat_done', data: { slot: 'mochi' } })
    await settle()
    expect(done).toBe(1)
  })
})

// ── watchlist ───────────────────────────────────────────────────────────────

describe('panelBridge watchlist subscription', () => {
  it('delivers a first value immediately instead of after one poll interval', async () => {
    api.getWatchlist.mockResolvedValue({ items: [{ id: 'w1' }] })
    const bridge = await loadBridge()
    const seen: unknown[][] = []
    const off = bridge.onWatchlistChanged((items) => seen.push(items))
    await settle()
    off()
    expect(seen).toEqual([[{ id: 'w1' }]])
  })

  it('delivers the pushed items directly, without a second fetch', async () => {
    const bridge = await loadBridge()
    const seen: unknown[][] = []
    const off = bridge.onWatchlistChanged((items) => seen.push(items))
    await settle()
    const before = api.getWatchlist.mock.calls.length
    emitApp('mochi:watchlist-changed', { items: [{ id: 'pushed' }] })
    off()
    expect(seen[seen.length - 1]).toEqual([{ id: 'pushed' }])
    expect(api.getWatchlist.mock.calls.length).toBe(before)
  })

  it('skips an identical publish so the panel does not re-render mid-animation', async () => {
    const bridge = await loadBridge()
    const seen: unknown[][] = []
    const off = bridge.onWatchlistChanged((items) => seen.push(items))
    await settle()
    emitApp('mochi:watchlist-changed', { items: [{ id: 'same' }] })
    emitApp('mochi:watchlist-changed', { items: [{ id: 'same' }] })
    off()
    expect(seen.filter((s) => JSON.stringify(s) === JSON.stringify([{ id: 'same' }]))).toHaveLength(1)
  })

  it('falls back to a fetch when the publish carries no items', async () => {
    const bridge = await loadBridge()
    const off = bridge.onWatchlistChanged(() => undefined)
    await settle()
    const before = api.getWatchlist.mock.calls.length
    api.getWatchlist.mockResolvedValue({ items: [{ id: 'refetched' }] })
    emitApp('mochi:watchlist-changed', {})
    await settle()
    off()
    expect(api.getWatchlist.mock.calls.length).toBeGreaterThan(before)
  })

  it('a transient fetch failure keeps the last state instead of throwing', async () => {
    api.getWatchlist.mockRejectedValue(new Error('offline'))
    const bridge = await loadBridge()
    const seen: unknown[][] = []
    const off = bridge.onWatchlistChanged((items) => seen.push(items))
    await settle()
    off()
    expect(seen).toEqual([])
  })

  it('stops delivering once unsubscribed', async () => {
    const bridge = await loadBridge()
    const seen: unknown[][] = []
    const off = bridge.onWatchlistChanged((items) => seen.push(items))
    await settle()
    off()
    emitApp('mochi:watchlist-changed', { items: [{ id: 'after' }] })
    expect(seen.some((s) => JSON.stringify(s).includes('after'))).toBe(false)
  })

  it('exposes the raw list for a one-shot read', async () => {
    api.getWatchlist.mockResolvedValue({ items: [{ id: 'a' }, { id: 'b' }] })
    const bridge = await loadBridge()
    expect(await bridge.getWatchlistItems()).toHaveLength(2)
  })
})

describe('panelBridge watchlist writes', () => {
  it('a status change is an update, and a delete is a removal', async () => {
    const bridge = await loadBridge()
    await bridge.setWatchItemStatus('w1', 'cancelled')
    expect(api.updateWatchlist).toHaveBeenCalledWith({ update: [{ id: 'w1', status: 'cancelled' }] })

    await bridge.deleteWatchItem('w1')
    expect(api.updateWatchlist).toHaveBeenCalledWith({ remove: ['w1'] })
    await settle()
  })

  it('an arbitrary field patch merges into the item update', async () => {
    const bridge = await loadBridge()
    await bridge.updateWatchItem('w2', { label: 'renamed', intervalMins: 30 })
    expect(api.updateWatchlist).toHaveBeenCalledWith({
      update: [{ id: 'w2', label: 'renamed', intervalMins: 30 }],
    })
    await settle()
  })

  it('clear-completed reports whether the SERVER agreed', async () => {
    const bridge = await loadBridge()
    expect(await bridge.clearCompletedWatchItems()).toBe(true)

    route('/watchlist/clear-completed', { ok: false, status: 409 })
    expect(await bridge.clearCompletedWatchItems()).toBe(false)

    route('/watchlist/clear-completed', { reject: true })
    expect(await bridge.clearCompletedWatchItems()).toBe(false)
    await settle()
  })
})

// ── pinned files ────────────────────────────────────────────────────────────

describe('panelBridge pinned files', () => {
  it('reads, marks seen and unpins through the shared api module', async () => {
    api.getPinned.mockResolvedValue({ pins: [{ path: '/u/a.md' }] })
    const bridge = await loadBridge()
    expect(await bridge.getPinnedFiles()).toHaveLength(1)
    await bridge.markPinnedSeen('/u/a.md')
    expect(api.markPinnedSeen).toHaveBeenCalledWith('/u/a.md')
    await bridge.unpinFile('/u/a.md')
    expect(api.unpinFile).toHaveBeenCalledWith('/u/a.md')
  })

  it('preview reveals through the shell, and sleeps without one', async () => {
    const revealFile = vi.fn()
    let bridge = await loadBridge({ revealFile })
    bridge.previewFile('/u/a.md')
    expect(revealFile).toHaveBeenCalledWith('/u/a.md')

    bridge = await loadBridge()
    expect(() => bridge.previewFile('/u/a.md')).not.toThrow()
  })
})

// ── chat frame shaping ──────────────────────────────────────────────────────

describe('panelBridge renderable-role filter', () => {
  it('admits only the three roles the panel can draw', async () => {
    const bridge = await loadBridge()
    expect(bridge.isRenderableChatRole('user')).toBe(true)
    expect(bridge.isRenderableChatRole('assistant')).toBe(true)
    expect(bridge.isRenderableChatRole('error')).toBe(true)
    expect(bridge.isRenderableChatRole('tool')).toBe(false)
    expect(bridge.isRenderableChatRole('permission')).toBe(false)
    expect(bridge.isRenderableChatRole(undefined)).toBe(false)
  })
})

describe('panelBridge rebuilds an approval from a permission frame', () => {
  it('carries the id, title, input, scoped commands and declared purpose', async () => {
    const bridge = await loadBridge()
    const req = bridge.permissionApprovalFromFrame({
      content: 'fallback title',
      cls: JSON.stringify({
        request_id: 'req-1',
        tool_title: 'Run command',
        tool_input: JSON.stringify({ command: 'ls', __tool_use_purpose: 'list the directory' }),
        full_command: 'ls -la /tmp',
        base_command: 'ls',
      }),
    })
    expect(req).toEqual({
      id: 'req-1',
      tool: 'Run command',
      toolInput: JSON.stringify({ command: 'ls', __tool_use_purpose: 'list the directory' }),
      purpose: 'list the directory',
      fullCommand: 'ls -la /tmp',
      baseCommand: 'ls',
    })
  })

  it('falls back to the frame content when the meta names no tool title', async () => {
    const bridge = await loadBridge()
    const req = bridge.permissionApprovalFromFrame({
      content: 'execute_bash',
      cls: JSON.stringify({ request_id: 'req-2', tool_input: 'not json at all' }),
    })
    expect(req?.tool).toBe('execute_bash')
    expect(req?.toolInput).toBe('not json at all')
    // A bare shell string declares no purpose, so none is invented.
    expect(req?.purpose).toBeUndefined()
  })

  it('returns null for unparseable, shapeless, id-less and already-resolved meta', async () => {
    const bridge = await loadBridge()
    expect(bridge.permissionApprovalFromFrame({ cls: '{not json' })).toBeNull()
    expect(bridge.permissionApprovalFromFrame({ cls: '"a string"' })).toBeNull()
    expect(bridge.permissionApprovalFromFrame({ cls: JSON.stringify({ tool_title: 't' }) })).toBeNull()
    expect(bridge.permissionApprovalFromFrame({ cls: JSON.stringify({ request_id: '' }) })).toBeNull()
    expect(
      bridge.permissionApprovalFromFrame({
        cls: JSON.stringify({ request_id: 'req-3', resolved: true }),
      }),
    ).toBeNull()
  })
})

// ── WebSocket dispatcher ────────────────────────────────────────────────────

describe('panelBridge WebSocket dispatcher', () => {
  it('opens the socket against the same origin as the page', async () => {
    await loadBridge()
    expect(socket().url).toBe(`ws://${location.host}/api/ws`)
  })

  it('ignores a frame that is not JSON', async () => {
    const bridge = await loadBridge()
    const seen: string[] = []
    bridge.onChatChunk((c) => seen.push(c))
    socket().onmessage?.({ data: 'not json' } as unknown as MessageEvent)
    expect(seen).toEqual([])
  })

  it('fans out the global slots list', async () => {
    const bridge = await loadBridge()
    const seen: unknown[][] = []
    bridge.onSlotsUpdate((slots) => seen.push(slots))
    emit({ type: 'slots', data: { slots: [{ key: 'mochi' }] } })
    emit({ type: 'slots', data: {} })
    expect(seen).toEqual([[{ key: 'mochi' }], []])
  })

  it('re-arms the agent binding when a slots frame shows our slot is gone', async () => {
    route('/api/chat/slots', { body: { agent: 'mochi' } }, 'POST')
    const bridge = await loadBridge()
    await bridge.sendMessage('one')
    await bridge.sendMessage('two')
    // Idempotent: the second send reuses the bind.
    expect(calls('/api/chat/slots', 'POST')).toHaveLength(1)

    emit({ type: 'slots', data: { slots: [{ key: 'other' }] } })
    await bridge.sendMessage('three')
    expect(calls('/api/chat/slots', 'POST')).toHaveLength(2)
  })

  it('routes pet state and mood, which carry no slot', async () => {
    const bridge = await loadBridge()
    const states: string[] = []
    const moods: Array<[string, number]> = []
    bridge.onStateChange((s) => states.push(s))
    bridge.onMood((m, intensity) => moods.push([m, intensity]))
    emitApp('pet:state-change', { args: ['working'] })
    emitApp('mochi:mood', { args: ['happy', 0.5] })
    emitApp('mochi:mood', { args: ['sad'] })
    expect(states).toEqual(['working'])
    // Intensity is optional upstream; an unreported one reads as "as reported".
    expect(moods).toEqual([['happy', 0.5], ['sad', 1]])
  })

  it('delivers an approval for our slot only, and reports the pet transition', async () => {
    const bridge = await loadBridge()
    const seen: Record<string, unknown>[] = []
    bridge.onApprovalRequest((req) => seen.push(req))
    emit({ type: 'approval', data: { slot: 'other', id: 'x' } })
    emit({ type: 'approval', data: { slot: 'mochi', id: 'req-9' } })
    await settle()
    expect(seen).toHaveLength(1)
    expect(seen[0].id).toBe('req-9')
    expect(calls('/pet-event').map((c) => bodyOf(c).event)).toContain('approval_required')
  })

  it('learns about a resolution made on another surface', async () => {
    const bridge = await loadBridge()
    const seen: Record<string, unknown>[] = []
    bridge.onApprovalResolvedExternal((r) => seen.push(r))
    emit({ type: 'approval_resolved', data: { id: 'req-9', approved: true } })
    emit({ type: 'approval_resolved', data: { id: 'req-8', approved: false } })
    await settle()
    expect(seen.map((r) => r.id)).toEqual(['req-9', 'req-8'])
    const events = calls('/pet-event').map((c) => bodyOf(c).event)
    expect(events).toContain('approval_granted')
    expect(events).toContain('approval_rejected')
  })

  it('streams chunks and completes the turn', async () => {
    const bridge = await loadBridge()
    const chunks: string[] = []
    let done = 0
    bridge.onChatChunk((c) => chunks.push(c))
    bridge.onChatDone(() => { done += 1 })
    emit({ type: 'chat_chunk', data: { slot: 'mochi', content: 'hel' } })
    emit({ type: 'chat_chunk', data: { slot: 'mochi' } })
    emit({ type: 'chat_done', data: { slot: 'mochi' } })
    await settle()
    expect(chunks).toEqual(['hel', ''])
    expect(done).toBe(1)
    expect(calls('/pet-event').map((c) => bodyOf(c).event)).toContain('task_complete')
  })

  it('drops every frame belonging to another slot', async () => {
    const bridge = await loadBridge()
    const chunks: string[] = []
    bridge.onChatChunk((c) => chunks.push(c))
    emit({ type: 'chat_chunk', data: { slot: 'dashboard', content: 'not ours' } })
    expect(chunks).toEqual([])
  })

  it('reports context usage as a number', async () => {
    const bridge = await loadBridge()
    const pcts: number[] = []
    bridge.onContextUsage((p) => pcts.push(p))
    emit({ type: 'context_usage', data: { slot: 'mochi', pct: 42 } })
    emit({ type: 'context_usage', data: { slot: 'mochi' } })
    expect(pcts).toEqual([42, 0])
  })

  it('gives every chat message the id and timestamp the renderer reads unguarded', async () => {
    const bridge = await loadBridge()
    const seen: Record<string, unknown>[] = []
    bridge.onChatMessage((m) => seen.push(m))
    emit({ type: 'chat_message', data: { slot: 'mochi', role: 'assistant' } })
    emit({
      type: 'chat_message',
      data: { slot: 'mochi', role: 'user', id: 'given', content: 'hi', timestamp: 5 },
    })
    expect(String(seen[0].id).startsWith('assistant-')).toBe(true)
    expect(seen[0].content).toBe('')
    expect(typeof seen[0].timestamp).toBe('number')
    expect(seen[1]).toMatchObject({ id: 'given', content: 'hi', timestamp: 5 })
  })

  it('turns tool and error frames into pet transitions without rendering them', async () => {
    const bridge = await loadBridge()
    const seen: Record<string, unknown>[] = []
    bridge.onChatMessage((m) => seen.push(m))
    emit({ type: 'chat_message', data: { slot: 'mochi', role: 'tool', content: 'ls' } })
    emit({ type: 'chat_message', data: { slot: 'mochi', role: 'system', content: 'noise' } })
    await settle()
    const events = calls('/pet-event').map((c) => bodyOf(c).event)
    expect(events).toContain('tool_call')
    expect(seen).toHaveLength(0)
  })

  it('renders an error frame, because the panel has no other failure surface', async () => {
    const bridge = await loadBridge()
    const seen: Record<string, unknown>[] = []
    bridge.onChatMessage((m) => seen.push(m))
    emit({ type: 'chat_message', data: { slot: 'mochi', role: 'error', content: 'turn failed' } })
    await settle()
    expect(seen).toHaveLength(1)
    expect(calls('/pet-event').map((c) => bodyOf(c).event)).toContain('error')
  })

  it('turns a permission frame into the same approval card as an approval frame', async () => {
    const bridge = await loadBridge()
    const approvals: Record<string, unknown>[] = []
    const messages: Record<string, unknown>[] = []
    bridge.onApprovalRequest((r) => approvals.push(r))
    bridge.onChatMessage((m) => messages.push(m))
    emit({
      type: 'chat_message',
      data: {
        slot: 'mochi',
        role: 'permission',
        content: 'execute_bash',
        cls: JSON.stringify({ request_id: 'req-p', tool_title: 'Run command' }),
      },
    })
    // A permission frame with no usable meta must open no card at all.
    emit({
      type: 'chat_message',
      data: { slot: 'mochi', role: 'permission', content: 'x', cls: 'garbage' },
    })
    await settle()
    expect(approvals.map((r) => r.id)).toEqual(['req-p'])
    expect(messages).toHaveLength(0)
  })

  it('unsubscribing detaches every chat, turn and approval listener', async () => {
    const bridge = await loadBridge()
    const seen: string[] = []
    bridge.onChatChunk(() => seen.push('chunk'))()
    bridge.onChatMessage(() => seen.push('message'))()
    bridge.onChatDone(() => seen.push('done'))()
    bridge.onContextUsage(() => seen.push('context'))()
    bridge.onSlotsUpdate(() => seen.push('slots'))()
    bridge.onStateChange(() => seen.push('state'))()
    bridge.onMood(() => seen.push('mood'))()
    bridge.onApprovalRequest(() => seen.push('approval'))()
    bridge.onApprovalResolvedExternal(() => seen.push('resolved'))()
    emit({ type: 'chat_chunk', data: { slot: 'mochi', content: 'x' } })
    emit({ type: 'chat_message', data: { slot: 'mochi', role: 'assistant', content: 'x' } })
    emit({ type: 'chat_done', data: { slot: 'mochi' } })
    emit({ type: 'context_usage', data: { slot: 'mochi', pct: 1 } })
    emit({ type: 'slots', data: { slots: [{ key: 'mochi' }] } })
    emitApp('pet:state-change', { args: ['idle'] })
    emitApp('mochi:mood', { args: ['happy'] })
    emit({ type: 'approval', data: { slot: 'mochi', id: 'r' } })
    emit({ type: 'approval_resolved', data: { id: 'r', approved: true } })
    await settle()
    expect(seen).toEqual([])
  })
})

// ── connection status ───────────────────────────────────────────────────────

describe('panelBridge connection indicator tracks the socket', () => {
  it('is offline until the socket opens, and fires only on a transition', async () => {
    const bridge = await loadBridge()
    const seen: boolean[] = []
    const off = bridge.onBackendStatus((online) => seen.push(online))
    expect(await bridge.getBackendStatus()).toBe(false)
    socket().onopen?.()
    socket().onopen?.()
    expect(seen).toEqual([true])
    expect(await bridge.getBackendStatus()).toBe(true)
    off()
    socket().onclose?.()
    expect(seen).toEqual([true])
  })

  it('reconnects with a capped backoff after the socket drops', async () => {
    vi.useFakeTimers()
    try {
      const bridge = await loadBridge()
      bridge.onChatDone(() => undefined)
      socket().onopen?.()
      const first = MockSocket.instances.length
      socket().onclose?.()
      expect(await bridge.getBackendStatus()).toBe(false)
      vi.advanceTimersByTime(1000)
      expect(MockSocket.instances.length).toBe(first + 1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('an error closes the socket rather than leaving it half-dead', async () => {
    vi.useFakeTimers()
    try {
      await loadBridge()
      const ws = socket()
      ws.onerror?.()
      expect(ws.close).toHaveBeenCalled()
      vi.clearAllTimers()
    } finally {
      vi.useRealTimers()
    }
  })

  it('a manual retry cancels the pending backoff and reopens now', async () => {
    vi.useFakeTimers()
    try {
      const bridge = await loadBridge()
      socket().onclose?.()
      const afterClose = MockSocket.instances.length
      const result = await bridge.retryConnect()
      expect(result).toEqual({ ok: true })
      expect(MockSocket.instances.length).toBe(afterClose + 1)
      // The cancelled timer must not open a second socket later.
      vi.advanceTimersByTime(60_000)
      expect(MockSocket.instances.length).toBe(afterClose + 1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('a retry on a live socket is a no-op', async () => {
    const bridge = await loadBridge()
    const before = MockSocket.instances.length
    expect(await bridge.retryConnect()).toEqual({ ok: true })
    expect(MockSocket.instances.length).toBe(before)
  })
})

// ── send + slot binding ─────────────────────────────────────────────────────

describe('panelBridge send', () => {
  it('echoes the user message locally, because core does not echo it back', async () => {
    route('/api/chat/slots', { body: { agent: 'mochi' } }, 'POST')
    const bridge = await loadBridge()
    const seen: Record<string, unknown>[] = []
    bridge.onChatMessage((m) => seen.push(m))
    await bridge.sendMessage('hello there', 'data:image/png;base64,AAA')
    expect(seen).toHaveLength(1)
    expect(seen[0]).toMatchObject({
      role: 'user',
      content: 'hello there',
      screenshot: 'data:image/png;base64,AAA',
    })
    const body = bodyOf(calls('/api/chat?ws=1', 'POST')[0])
    expect(body).toMatchObject({
      message: 'hello there',
      slot: 'mochi',
      meta: { screenshot: 'data:image/png;base64,AAA' },
    })
  })

  it('sends without a meta block when there is no screenshot', async () => {
    route('/api/chat/slots', { body: { agent: 'mochi' } }, 'POST')
    const bridge = await loadBridge()
    await bridge.sendMessage('plain')
    expect(bodyOf(calls('/api/chat?ws=1', 'POST')[0]).meta).toBeUndefined()
  })

  it('refuses to send into a slot another agent owns', async () => {
    route('/api/chat/slots', { body: { agent: 'someone-else' } }, 'POST')
    const bridge = await loadBridge()
    await expect(bridge.ensureSlot()).rejects.toThrow(/bound to another agent/)
    expect(calls('/api/chat?ws=1', 'POST')).toHaveLength(0)
  })

  it('refuses when the binding cannot be verified at all', async () => {
    route('/api/chat/slots', { jsonThrows: true }, 'POST')
    const bridge = await loadBridge()
    await expect(bridge.ensureSlot()).rejects.toThrow(/could not verify/)
  })

  it('leaves the latch off when the bind request fails, so the next send retries', async () => {
    route('/api/chat/slots', { ok: false, status: 503 }, 'POST')
    const bridge = await loadBridge()
    await bridge.ensureSlot()
    await bridge.ensureSlot()
    expect(calls('/api/chat/slots', 'POST')).toHaveLength(2)
  })

  it('stops a running turn on the slot', async () => {
    const bridge = await loadBridge()
    await bridge.stopGeneration()
    expect(calls('/api/chat/slots/mochi/stop', 'POST')).toHaveLength(1)
  })

  it('disabling the app is the honest stand-in for quitting', async () => {
    const bridge = await loadBridge()
    await bridge.disableApp()
    expect(calls('/api/apps/mochi/disable', 'POST')).toHaveLength(1)
  })
})

// ── history and sessions ────────────────────────────────────────────────────

describe('panelBridge history', () => {
  it('filters persisted tool and system rows out of the backfill', async () => {
    route('/api/chat/slots/mochi', {
      body: {
        messages: [
          { role: 'user', content: 'hi' },
          { role: 'tool', content: 'ls' },
          { role: 'assistant', content: 'hello' },
        ],
      },
    })
    const bridge = await loadBridge()
    const history = await bridge.getChatHistory()
    expect(history.map((m) => m.role)).toEqual(['user', 'assistant'])
  })

  it('is empty for a slot that does not exist yet, a shapeless body, or a failed read', async () => {
    route('/api/chat/slots/mochi', { ok: false, status: 404 })
    let bridge = await loadBridge()
    expect(await bridge.getChatHistory()).toEqual([])

    route('/api/chat/slots/mochi', { body: { messages: 'nope' } })
    bridge = await loadBridge()
    expect(await bridge.getChatHistory()).toEqual([])

    route('/api/chat/slots/mochi', { reject: true })
    bridge = await loadBridge()
    expect(await bridge.getChatHistory()).toEqual([])
  })

  it('a new session deletes the slot and disarms the binding latch itself', async () => {
    route('/api/chat/slots', { body: { agent: 'mochi' } }, 'POST')
    const bridge = await loadBridge()
    await bridge.sendMessage('first')
    expect(calls('/api/chat/slots', 'POST')).toHaveLength(1)

    await bridge.newSession()
    expect(calls('/api/chat/slots/mochi', 'DELETE')).toHaveLength(1)

    // Without the self-disarm a socket-less panel would recreate the slot on the
    // default agent instead of the pet's own.
    await bridge.sendMessage('second')
    expect(calls('/api/chat/slots', 'POST')).toHaveLength(2)
  })

  it('an already-absent slot is a successful reset, but a real failure throws', async () => {
    route('/api/chat/slots/mochi', { ok: false, status: 404 }, 'DELETE')
    let bridge = await loadBridge()
    await expect(bridge.newSession()).resolves.toBeUndefined()

    route('/api/chat/slots/mochi', { ok: false, status: 500 }, 'DELETE')
    bridge = await loadBridge()
    await expect(bridge.clearChat()).rejects.toThrow(/newSession failed: 500/)
  })

  it('deleting history erases the session rather than archiving the slot', async () => {
    const bridge = await loadBridge()
    await bridge.deleteHistory()
    const [call] = calls('/api/sessions/', 'DELETE')
    expect(call.url).toBe(`/api/sessions/${encodeURIComponent('dashboard:mochi')}`)

    route('/api/sessions/', { ok: false, status: 500 }, 'DELETE')
    const again = await loadBridge()
    await expect(again.deleteHistory()).rejects.toThrow(/deleteHistory failed: 500/)
  })
})

// ── pet state read ──────────────────────────────────────────────────────────

describe('panelBridge pet state read', () => {
  it('returns both the state and the mood the route reports', async () => {
    route('/pet-state', { body: { state: 'working', mood: 'excited' } })
    const bridge = await loadBridge()
    expect(await bridge.getPetStateInfo()).toEqual({ state: 'working', mood: 'excited' })
    expect(await bridge.getPetState()).toBe('working')
  })

  it('degrades to the cold-start values instead of throwing', async () => {
    route('/pet-state', { body: { state: 'idle', mood: '' } })
    let bridge = await loadBridge()
    expect(await bridge.getPetStateInfo()).toEqual({ state: 'idle', mood: 'neutral' })

    route('/pet-state', { ok: false, status: 500 })
    bridge = await loadBridge()
    expect(await bridge.getPetStateInfo()).toEqual({ state: 'offline', mood: 'neutral' })

    route('/pet-state', { reject: true })
    bridge = await loadBridge()
    expect(await bridge.getPetState()).toBe('offline')
  })
})

// ── shell bridge ────────────────────────────────────────────────────────────

describe('panelBridge shell actions', () => {
  it('forwards each window action to the shell', async () => {
    const shell: ShellTable = {
      openAvatars: vi.fn(),
      openDashboard: vi.fn(),
      closeChat: vi.fn(),
      revealFile: vi.fn(),
      openExternal: vi.fn(),
      openImage: vi.fn(),
    }
    const bridge = await loadBridge(shell)
    bridge.openAvatars()
    bridge.openDashboard()
    bridge.closeChat()
    bridge.revealFile('/u/a.md')
    bridge.openExternal('https://example.com')
    bridge.openLightbox('/u/shot.png')
    expect(shell.openAvatars).toHaveBeenCalled()
    expect(shell.openDashboard).toHaveBeenCalled()
    expect(shell.closeChat).toHaveBeenCalled()
    expect(shell.revealFile).toHaveBeenCalledWith('/u/a.md')
    expect(shell.openExternal).toHaveBeenCalledWith('https://example.com')
    expect(shell.openImage).toHaveBeenCalledWith('/u/shot.png')
  })

  it('the lightbox refuses an empty target and a data URL, which the viewer cannot open', async () => {
    const openImage = vi.fn()
    const bridge = await loadBridge({ openImage })
    bridge.openLightbox('')
    bridge.openLightbox('data:image/png;base64,AAA')
    expect(openImage).not.toHaveBeenCalled()
  })

  it('every shell action degrades to a no-op in the browser preview', async () => {
    const bridge = await loadBridge()
    expect(() => {
      bridge.openAvatars()
      bridge.openDashboard()
      bridge.closeChat()
      bridge.revealFile('/u/a.md')
      bridge.openExternal('https://example.com')
      bridge.openLightbox('/u/shot.png')
    }).not.toThrow()
  })

  it('a pet menu subscription hands back the shell unsubscribe', async () => {
    const off = vi.fn()
    const shell: ShellTable = {
      onOpenMemories: vi.fn(() => off),
      onClearScreen: vi.fn(() => off),
      // A shell that returns nothing must still leave the caller an `off()`.
      onDeleteHistory: vi.fn(() => undefined),
    }
    const bridge = await loadBridge(shell)
    bridge.onOpenMemories(() => undefined)()
    bridge.onClearScreen(() => undefined)()
    expect(off).toHaveBeenCalledTimes(2)
    expect(() => bridge.onDeleteHistory(() => undefined)()).not.toThrow()
  })

  it('a pet menu subscription without a shell is inert', async () => {
    const bridge = await loadBridge()
    expect(() => bridge.onOpenMemories(() => undefined)()).not.toThrow()
  })
})

// ── appearance packs ────────────────────────────────────────────────────────

describe('panelBridge appearance packs', () => {
  it('lists packs, and reads an empty list rather than crashing on a bad body', async () => {
    route('/api/apps/mochi/packs', { body: { packs: [{ id: 'p1' }] } }, 'GET')
    let bridge = await loadBridge()
    expect(await bridge.galleryListPacks()).toHaveLength(1)

    route('/api/apps/mochi/packs', { body: {} }, 'GET')
    bridge = await loadBridge()
    expect(await bridge.galleryListPacks()).toEqual([])

    route('/api/apps/mochi/packs', { ok: false, status: 500 }, 'GET')
    bridge = await loadBridge()
    expect(await bridge.galleryListPacks()).toEqual([])
  })

  it('reads one pack manifest, and null when it is gone', async () => {
    route('/api/apps/mochi/packs/p%201', { body: { id: 'p 1', frames: {} } })
    const bridge = await loadBridge()
    expect(await bridge.galleryGetPackDetail('p 1')).toMatchObject({ id: 'p 1' })

    route('/api/apps/mochi/packs/', { ok: false, status: 404 })
    const missing = await loadBridge()
    expect(await missing.galleryGetPackDetail('nope')).toBeNull()
  })

  it('builds a directly usable image URL, encoding both segments', async () => {
    const bridge = await loadBridge()
    expect(bridge.galleryPackFileUrl('my pack', 'a b.png')).toBe(
      '/api/apps/mochi/packs/my%20pack/file/a%20b.png',
    )
  })

  it('surfaces a failed save instead of letting the user believe it stored', async () => {
    route('/api/apps/mochi/packs', { body: { packId: 'saved' } }, 'POST')
    let bridge = await loadBridge()
    expect(await bridge.gallerySaveSpritePack({ name: 'p' })).toEqual({ ok: true, packId: 'saved' })

    route('/api/apps/mochi/packs', { ok: false, status: 422, body: { error: 'bad frames' } }, 'POST')
    bridge = await loadBridge()
    expect(await bridge.gallerySaveSpritePack({})).toEqual({ ok: false, error: 'bad frames' })

    route('/api/apps/mochi/packs', { ok: false, status: 500, jsonThrows: true }, 'POST')
    bridge = await loadBridge()
    expect(await bridge.gallerySaveSpritePack({})).toEqual({ ok: false, error: 'save failed (500)' })
  })

  it('an apply confirms itself by reading the persisted value back', async () => {
    route('/api/apps/mochi/settings', { body: { activeAppearance: 'p1' } }, 'POST')
    const bridge = await loadBridge()
    await expect(bridge.gallerySetActive('p1')).resolves.toBeUndefined()
  })

  it('an apply that did not stick is a named error, not a silent success', async () => {
    route('/api/apps/mochi/settings', { body: { activeAppearance: 'other' } }, 'POST')
    let bridge = await loadBridge()
    await expect(bridge.gallerySetActive('p1')).rejects.toThrow(/did not stick/)

    route('/api/apps/mochi/settings', { ok: false, status: 500 }, 'POST')
    bridge = await loadBridge()
    await expect(bridge.gallerySetActive('p1')).rejects.toThrow(/could not apply/)
  })

  it('an unreadable settings response is accepted rather than mis-reported', async () => {
    route('/api/apps/mochi/settings', { jsonThrows: true }, 'POST')
    const bridge = await loadBridge()
    await expect(bridge.gallerySetActive('p1')).resolves.toBeUndefined()
  })

  it('reports whether a delete happened', async () => {
    route('/api/apps/mochi/packs/p1', { ok: true }, 'DELETE')
    let bridge = await loadBridge()
    expect(await bridge.galleryDeletePack('p1')).toBe(true)

    route('/api/apps/mochi/packs/p1', { ok: false, status: 404 }, 'DELETE')
    bridge = await loadBridge()
    expect(await bridge.galleryDeletePack('p1')).toBe(false)
  })
})

// ── live-refresh subscribers ────────────────────────────────────────────────

describe('panelBridge live-refresh subscribers', () => {
  it('the pin rail refreshes from the pushed list, and tolerates a shapeless one', async () => {
    const bridge = await loadBridge()
    const seen: unknown[][] = []
    const off = bridge.onPinnedFilesChanged((pins) => seen.push(pins))
    emitApp('pinned:files-changed', { args: [[{ path: '/u/a.md' }]] })
    emitApp('pinned:files-changed', { args: ['nonsense'] })
    off()
    expect(seen).toEqual([[{ path: '/u/a.md' }], []])
  })

  it('a changed pinned file carries a timestamp even when the frame omits one', async () => {
    const bridge = await loadBridge()
    const seen: Array<{ path: string; updatedAt: number }> = []
    const off = bridge.onPinnedFileUpdated((info) => seen.push(info))
    emitApp('pinned:file-updated', { args: [{ path: '/u/a.md', updatedAt: 7 }] })
    emitApp('pinned:file-updated', { args: [{ path: '/u/b.md' }] })
    off()
    expect(seen[0]).toEqual({ path: '/u/a.md', updatedAt: 7 })
    expect(seen[1].path).toBe('/u/b.md')
    expect(typeof seen[1].updatedAt).toBe('number')
  })

  it('a deleted pinned file names its path', async () => {
    const bridge = await loadBridge()
    const seen: Array<{ path: string }> = []
    const off = bridge.onPinnedFileDeleted((info) => seen.push(info))
    emitApp('pinned:file-deleted', { args: [{ path: '/u/gone.md' }] })
    off()
    expect(seen).toEqual([{ path: '/u/gone.md' }])
  })

  it('peeking is strictly boolean, whatever the frame says', async () => {
    const bridge = await loadBridge()
    const seen: boolean[] = []
    const off = bridge.onPeeking((p) => seen.push(p))
    emitApp('mochi:peeking', { args: [{ peeking: true }] })
    emitApp('mochi:peeking', { args: [{ peeking: 'yes' }] })
    emitApp('mochi:peeking', { args: [{}] })
    off()
    expect(seen).toEqual([true, false, false])
  })

  it('a notification published as a bare dict is delivered, not dropped', async () => {
    const bridge = await loadBridge()
    const seen: Array<{ title?: string }> = []
    const off = bridge.onNotification((n) => seen.push(n))
    // `publish()` sends the payload DIRECTLY — reading args[0] here handed the
    // panel undefined and its bubbles never appeared.
    emitApp('mochi:notify', { title: 'watch hit', body: 'a page changed' })
    off()
    expect(seen[0].title).toBe('watch hit')
  })

  it('an agent-pushed message reaches the transcript, and an empty one does not', async () => {
    const bridge = await loadBridge()
    const seen: Record<string, unknown>[] = []
    bridge.onChatMessage((m) => seen.push(m))
    emitApp('mochi:chat-push', { content: 'I finished the check', timestamp: 11 })
    emitApp('mochi:chat-push', { content: '' })
    emitApp('mochi:chat-push', {})
    expect(seen).toHaveLength(1)
    expect(seen[0]).toMatchObject({ role: 'assistant', content: 'I finished the check', timestamp: 11 })
    expect(String(seen[0].id).startsWith('push-')).toBe(true)
  })

  it('a pack change tells consumers to re-read the list, and a colour map carries its payload', async () => {
    const bridge = await loadBridge()
    const packSignals: string[] = []
    const maps: unknown[] = []
    const offPacks = bridge.onGalleryPacksChanged((id) => packSignals.push(id))
    const offMap = bridge.onColorMapChanged((m) => maps.push(m))
    emitApp('mochi:gallery-packs-changed', { args: [{ packId: 'p1' }] })
    emitApp('mochi:color-map-changed', { args: [{ body: '#fff' }] })
    offPacks()
    offMap()
    expect(packSignals).toEqual([''])
    expect(maps).toEqual([{ body: '#fff' }])
  })

  it('an unsubscribed app-event listener stops receiving', async () => {
    const bridge = await loadBridge()
    const seen: unknown[] = []
    const off = bridge.subscribeAppEvent('mochi:peeking', (p) => seen.push(p))
    off()
    emitApp('mochi:peeking', { args: [{ peeking: true }] })
    expect(seen).toEqual([])
  })
})

// ── approvals ───────────────────────────────────────────────────────────────

describe('panelBridge approvals', () => {
  it('rehydrates only the pet slot pending requests', async () => {
    route('/api/approvals', {
      body: [
        { id: 'a', slot: 'mochi' },
        { id: 'b', slot: 'dashboard' },
      ],
    })
    const bridge = await loadBridge()
    expect((await bridge.getPendingApprovals()).map((a) => a.id)).toEqual(['a'])
  })

  it('rehydrates empty on a shapeless body, a failed read or a dead fetch', async () => {
    route('/api/approvals', { body: { approvals: [] } })
    let bridge = await loadBridge()
    expect(await bridge.getPendingApprovals()).toEqual([])

    route('/api/approvals', { ok: false, status: 500 })
    bridge = await loadBridge()
    expect(await bridge.getPendingApprovals()).toEqual([])

    route('/api/approvals', { reject: true })
    bridge = await loadBridge()
    expect(await bridge.getPendingApprovals()).toEqual([])
  })

  it('approve and reject go to the approvals route', async () => {
    const bridge = await loadBridge()
    expect(await bridge.respondApproval('req 1', 'approve')).toEqual({ ok: true })
    expect(calls('/api/approvals/req%201/approve', 'POST')).toHaveLength(1)

    await bridge.respondApproval('req2', 'reject')
    expect(calls('/api/approvals/req2/reject', 'POST')).toHaveLength(1)
  })

  it('a scoped trust grant goes to the slot route, carrying its pattern', async () => {
    const bridge = await loadBridge()
    await bridge.respondApproval('req3', 'trust_command', 'ls -la')
    const [call] = calls('/api/chat/slots/mochi/approve', 'POST')
    expect(bodyOf(call)).toEqual({ action: 'trust_command', request_id: 'req3', pattern: 'ls -la' })

    await bridge.respondApproval('req4', 'trust')
    expect(bodyOf(calls('/api/chat/slots/mochi/approve', 'POST')[1]).pattern).toBeUndefined()
  })

  it('never claims a tool was approved when the request failed', async () => {
    route('/api/approvals/', { ok: false, status: 400 }, 'POST')
    let bridge = await loadBridge()
    expect(await bridge.respondApproval('req5', 'approve')).toEqual({
      ok: false,
      error: 'approval failed (400)',
    })

    route('/api/approvals/', { reject: true }, 'POST')
    bridge = await loadBridge()
    const result = await bridge.respondApproval('req6', 'approve')
    expect(result.ok).toBe(false)
    expect(String(result.error)).toContain('network down')
  })
})

// ── models, edit-resend, files ──────────────────────────────────────────────

describe('panelBridge model selection', () => {
  it('reads a bare array and a wrapped list of models', async () => {
    route('/api/models', { body: [{ model_name: 'a' }] })
    let bridge = await loadBridge()
    expect(await bridge.getModels()).toHaveLength(1)

    route('/api/models', { body: { models: [{ model_name: 'a' }, { model_name: 'b' }] } })
    bridge = await loadBridge()
    expect(await bridge.getModels()).toHaveLength(2)
  })

  it('hides the selector rather than offering a dropdown that cannot switch', async () => {
    route('/api/models', { ok: false, status: 503 })
    let bridge = await loadBridge()
    expect(await bridge.getModels()).toEqual([])

    route('/api/models', { body: { models: 'nope' } })
    bridge = await loadBridge()
    expect(await bridge.getModels()).toEqual([])

    route('/api/models', { reject: true })
    bridge = await loadBridge()
    expect(await bridge.getModels()).toEqual([])
  })

  it('opens on the model the slot is actually on', async () => {
    route('/api/chat/slots', { body: [{ key: 'mochi', model: 'sonnet' }] }, 'GET')
    const bridge = await loadBridge()
    expect(await bridge.getSlotModel()).toBe('sonnet')
  })

  it('reads the gateway default as an empty string in every degraded case', async () => {
    route('/api/chat/slots', { body: [{ key: 'other', model: 'sonnet' }] }, 'GET')
    let bridge = await loadBridge()
    expect(await bridge.getSlotModel()).toBe('')

    route('/api/chat/slots', { body: { slots: 'nope' } }, 'GET')
    bridge = await loadBridge()
    expect(await bridge.getSlotModel()).toBe('')

    route('/api/chat/slots', { ok: false, status: 500 }, 'GET')
    bridge = await loadBridge()
    expect(await bridge.getSlotModel()).toBe('')

    route('/api/chat/slots', { reject: true }, 'GET')
    bridge = await loadBridge()
    expect(await bridge.getSlotModel()).toBe('')
  })

  it('switches the slot model and reports failure honestly', async () => {
    const bridge = await loadBridge()
    expect(await bridge.setModel('sonnet')).toBe(true)
    expect(bodyOf(calls('/api/chat/slots/mochi/model', 'POST')[0])).toEqual({ model: 'sonnet' })

    route('/api/chat/slots/mochi/model', { ok: false, status: 400 }, 'POST')
    const failing = await loadBridge()
    expect(await failing.setModel('bogus')).toBe(false)

    route('/api/chat/slots/mochi/model', { reject: true }, 'POST')
    const offline = await loadBridge()
    expect(await offline.setModel('sonnet')).toBe(false)
  })
})

describe('panelBridge edit-and-resend and file URLs', () => {
  it('addresses the edited message by timestamp, as core requires', async () => {
    const bridge = await loadBridge()
    expect(await bridge.editResend('fixed text', '1712345678')).toEqual({ ok: true })
    expect(bodyOf(calls('/edit-resend', 'POST')[0])).toEqual({
      ts: '1712345678',
      content: 'fixed text',
    })
  })

  it('reports a failed edit so the caller can fall back to a plain send', async () => {
    route('/edit-resend', { reject: true }, 'POST')
    const bridge = await loadBridge()
    expect(await bridge.editResend('t', '1')).toEqual({ ok: false })
  })

  it('serves a local image through the guarded core route', async () => {
    const bridge = await loadBridge()
    expect(bridge.localFileUrl('/u/my shot.png')).toBe('/api/file-raw?path=%2Fu%2Fmy%20shot.png')
  })
})

// ── remote instances ────────────────────────────────────────────────────────

describe('panelBridge remote instances keep core three answers apart', () => {
  it('403 means the feature is off, not that there are no instances', async () => {
    route('/api/instances', { ok: false, status: 403 })
    const bridge = await loadBridge()
    expect(await bridge.listInstances()).toEqual({ state: 'disabled' })
  })

  it('a gateway started without the flag reads as inactive, with its list intact', async () => {
    route('/api/instances', { body: { active: false, instances: [{ id: 'i1', name: 'desk' }] } })
    const bridge = await loadBridge()
    expect(await bridge.listInstances()).toEqual({
      state: 'inactive',
      instances: [{ id: 'i1', name: 'desk' }],
    })
  })

  it('a ready gateway reports its instances from either body shape', async () => {
    route('/api/instances', { body: [{ id: 'i1', name: 'desk' }] })
    let bridge = await loadBridge()
    expect(await bridge.listInstances()).toEqual({
      state: 'ready',
      instances: [{ id: 'i1', name: 'desk' }],
    })

    route('/api/instances', { body: { instances: 'nope' } })
    bridge = await loadBridge()
    expect(await bridge.listInstances()).toEqual({ state: 'ready', instances: [] })
  })

  it('any other failure is an error state of its own', async () => {
    route('/api/instances', { ok: false, status: 500 })
    let bridge = await loadBridge()
    expect(await bridge.listInstances()).toEqual({ state: 'error' })

    route('/api/instances', { reject: true })
    bridge = await loadBridge()
    expect(await bridge.listInstances()).toEqual({ state: 'error' })
  })

  it('reads one instance status, and undefined when it cannot', async () => {
    route('/api/instances/i%201/status', { body: { state: 'connected' } })
    const bridge = await loadBridge()
    expect(await bridge.getInstanceStatus('i 1')).toEqual({ state: 'connected' })

    route('/api/instances/', { ok: false, status: 404 })
    const missing = await loadBridge()
    expect(await missing.getInstanceStatus('gone')).toBeUndefined()

    route('/api/instances/', { reject: true })
    const offline = await loadBridge()
    expect(await offline.getInstanceStatus('i1')).toBeUndefined()
  })
})

// ── speech to text ──────────────────────────────────────────────────────────

describe('panelBridge speech-to-text reuses the core stack', () => {
  it('reads the shared config, and undefined when it is unavailable', async () => {
    route('/api/config/stt', { body: { backend: 'whisper', installed: true } })
    let bridge = await loadBridge()
    expect(await bridge.getSttConfig()).toMatchObject({ backend: 'whisper' })

    route('/api/config/stt', { ok: false, status: 404 })
    bridge = await loadBridge()
    expect(await bridge.getSttConfig()).toBeUndefined()

    route('/api/config/stt', { reject: true })
    bridge = await loadBridge()
    expect(await bridge.getSttConfig()).toBeUndefined()
  })

  it('an install failure carries its reason to the settings panel', async () => {
    const bridge = await loadBridge()
    expect(await bridge.installStt()).toEqual({ ok: true })

    route('/api/stt/install', { ok: false, status: 500 }, 'POST')
    const failing = await loadBridge()
    expect(await failing.installStt()).toEqual({ ok: false, error: 'install failed (500)' })
  })

  it('transcribes a clip and defaults the mime type', async () => {
    route('/api/stt/transcribe', { body: { text: 'hello there' } }, 'POST')
    const bridge = await loadBridge()
    expect(await bridge.transcribeAudio('AAAA')).toBe('hello there')
    expect(bodyOf(calls('/api/stt/transcribe', 'POST')[0])).toEqual({
      audio: 'AAAA',
      mime: 'audio/webm',
    })
  })

  it('returns nothing when the transcription is unusable', async () => {
    route('/api/stt/transcribe', { body: {} }, 'POST')
    let bridge = await loadBridge()
    expect(await bridge.transcribeAudio('AAAA', 'audio/ogg')).toBeUndefined()

    route('/api/stt/transcribe', { ok: false, status: 500 }, 'POST')
    bridge = await loadBridge()
    expect(await bridge.transcribeAudio('AAAA')).toBeUndefined()

    route('/api/stt/transcribe', { reject: true }, 'POST')
    bridge = await loadBridge()
    expect(await bridge.transcribeAudio('AAAA')).toBeUndefined()
  })
})
