/**
 * Pins the Mochi panel bridge's chat transport contract.
 *
 * The load-bearing behavior is slot filtering: the panel shares the dashboard's
 * global WebSocket, so without the filter the pet would render turns belonging
 * to whatever slot the user has open in the dashboard.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

class FakeWebSocket {
  static last: FakeWebSocket | null = null
  onopen: (() => void) | null = null
  onmessage: ((ev: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  readonly url: string
  closed = false

  constructor(url: string) {
    this.url = url
    FakeWebSocket.last = this
  }

  close(): void {
    this.closed = true
  }

  emit(type: string, data: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify({ type, data }) })
  }

  /** The REAL wire shape for an app-scoped event: namespaced under `app_event`
   *  with the true event name + payload in the envelope (see apps/event_bus.py). */
  emitAppEvent(event: string, payload: Record<string, unknown>): void {
    this.onmessage?.({
      data: JSON.stringify({
        type: 'app_event',
        data: { event, app: 'mochi', data: payload },
      }),
    })
  }

  emitRaw(payload: string): void {
    this.onmessage?.({ data: payload })
  }
}

describe('mochi panelBridge chat transport', () => {
  let bridge: typeof import('../panel/panelBridge')

  beforeEach(async () => {
    vi.resetModules()
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
    FakeWebSocket.last = null
    bridge = await import('../panel/panelBridge')
  })

  it('connects to the same-origin dashboard websocket', () => {
    bridge.onChatDone(() => {})
    expect(FakeWebSocket.last?.url).toContain('/api/ws')
  })

  it('forwards mochi:chat-push into the transcript listeners as a pet message', () => {
    // The backend publishes chat-push only AFTER its re-notify guard accepts
    // the push (hooks.py _push_to_chat), so the bridge renders every arrival.
    // It must land in the SAME listener set as real chat frames — ChatPanel
    // has no second path.
    const messages: Record<string, unknown>[] = []
    bridge.onChatMessage((m) => messages.push(m))
    const ws = FakeWebSocket.last!
    ws.emit('mochi:chat-push', { content: 'the long story', timestamp: 42 })
    expect(messages).toHaveLength(1)
    expect(messages[0].role).toBe('assistant')
    expect(messages[0].content).toBe('the long story')
    expect(messages[0].timestamp).toBe(42)
    // An empty push must not flash an empty transcript row.
    ws.emit('mochi:chat-push', { content: '' })
    expect(messages).toHaveLength(1)
  })

  it('unwraps the namespaced app_event wire frame to the real event', () => {
    // App events ship on the wire as {type:"app_event", data:{event, app, data}}
    // (apps/event_bus.py). If the bridge does not unwrap that, EVERY app-scoped
    // event (notify bubble, appearance/colour-map, watchlist, pinned, chat-push)
    // silently stops — the regression this pins.
    const messages: Record<string, unknown>[] = []
    bridge.onChatMessage((m) => messages.push(m))
    const notes: unknown[] = []
    bridge.subscribeAppEvent('mochi:notify', (p) => notes.push(p))
    const ws = FakeWebSocket.last!

    ws.emitAppEvent('mochi:chat-push', { content: 'wrapped story', timestamp: 7 })
    ws.emitAppEvent('mochi:notify', { summary: 'hi' })

    expect(messages).toHaveLength(1)
    expect(messages[0].content).toBe('wrapped story')
    expect(notes).toEqual([{ summary: 'hi' }])
  })

  it('delivers chunks, messages and done for the mochi slot', () => {
    const chunks: string[] = []
    const messages: Record<string, unknown>[] = []
    let doneCount = 0
    bridge.onChatChunk((c) => chunks.push(c))
    bridge.onChatMessage((m) => messages.push(m))
    bridge.onChatDone(() => {
      doneCount += 1
    })

    const ws = FakeWebSocket.last!
    ws.emit('chat_chunk', { slot: bridge.MOCHI_SLOT, content: 'he' })
    ws.emit('chat_chunk', { slot: bridge.MOCHI_SLOT, content: 'llo' })
    ws.emit('chat_message', {
      slot: bridge.MOCHI_SLOT,
      role: 'assistant',
      content: 'hello',
    })
    ws.emit('chat_done', { slot: bridge.MOCHI_SLOT })

    expect(chunks).toEqual(['he', 'llo'])
    expect(messages).toHaveLength(1)
    expect(messages[0].role).toBe('assistant')
    expect(doneCount).toBe(1)
  })

  it('ignores chat events belonging to another slot', () => {
    const chunks: string[] = []
    let doneCount = 0
    bridge.onChatChunk((c) => chunks.push(c))
    bridge.onChatDone(() => {
      doneCount += 1
    })

    const ws = FakeWebSocket.last!
    ws.emit('chat_chunk', { slot: 'default', content: 'not mine' })
    ws.emit('chat_message', { slot: 'default', role: 'assistant', content: 'x' })
    ws.emit('chat_done', { slot: 'default' })

    expect(chunks).toEqual([])
    expect(doneCount).toBe(0)
  })

  it('passes slots updates through unfiltered (they are global)', () => {
    const seen: unknown[] = []
    bridge.onSlotsUpdate((s) => seen.push(s))
    FakeWebSocket.last!.emit('slots', { slots: [{ key: 'default' }] })
    expect(seen).toHaveLength(1)
  })

  it('survives malformed frames without throwing', () => {
    const chunks: string[] = []
    bridge.onChatChunk((c) => chunks.push(c))
    const ws = FakeWebSocket.last!
    expect(() => ws.emitRaw('not json')).not.toThrow()
    ws.emit('chat_chunk', { slot: bridge.MOCHI_SLOT, content: 'ok' })
    expect(chunks).toEqual(['ok'])
  })

  it('unsubscribing stops delivery', () => {
    const chunks: string[] = []
    const off = bridge.onChatChunk((c) => chunks.push(c))
    const ws = FakeWebSocket.last!
    ws.emit('chat_chunk', { slot: bridge.MOCHI_SLOT, content: 'a' })
    off()
    ws.emit('chat_chunk', { slot: bridge.MOCHI_SLOT, content: 'b' })
    expect(chunks).toEqual(['a'])
  })

  it('sendMessage posts to the mochi slot with ws fan-out', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ agent: 'mochi' }) })
    vi.stubGlobal('fetch', fetchMock)

    await bridge.sendMessage('hi')

    // ensureSlot binds the slot first, then the chat turn is posted.
    const chatCall = fetchMock.mock.calls.find((c) => c[0] === '/api/chat?ws=1')!
    expect(chatCall).toBeTruthy()
    expect(JSON.parse(chatCall[1].body)).toEqual({ message: 'hi', slot: bridge.MOCHI_SLOT })
  })

  it('sendMessage carries a screenshot as meta when present', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ agent: 'mochi' }) })
    vi.stubGlobal('fetch', fetchMock)

    await bridge.sendMessage('look', 'data:image/png;base64,AAA')

    const chatCall = fetchMock.mock.calls.find((c) => c[0] === '/api/chat?ws=1')!
    const body = JSON.parse(chatCall[1].body)
    expect(body.meta).toEqual({ screenshot: 'data:image/png;base64,AAA' })
  })

  // Agent binding — the pet's slot MUST be bound to the mochi agent, or the
  // ambient dashboard agent answers its chat.
  it('binds the app slot to an app-id-prefixed agent name', () => {
    expect(bridge.MOCHI_SLOT).toBe('mochi')
    expect(bridge.MOCHI_AGENT).toBe('mochi')
    // kiro-cli keys agents by the JSON "name" field, not by the namespaced link
    // filename the app bridge writes, so agent names are ONE FLAT GLOBAL
    // namespace. 'mochi-pet' is claimed by an unrelated standalone build on some
    // machines; the agent name must stay app-id-prefixed so it cannot collide.
    expect(bridge.MOCHI_AGENT.startsWith('mochi')).toBe(true)
    expect(bridge.MOCHI_AGENT).not.toBe('mochi-pet')
  })

  it('sendMessage binds the slot to the mochi agent before the first send', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ agent: 'mochi' }) })
    vi.stubGlobal('fetch', fetchMock)
    await bridge.sendMessage('hi')
    const createCall = fetchMock.mock.calls.find((c) => c[0] === '/api/chat/slots')!
    expect(createCall).toBeTruthy()
    expect(createCall[1].method).toBe('POST')
    expect(JSON.parse(createCall[1].body)).toEqual({ name: 'mochi', agent: 'mochi' })
  })

  it('re-binds the agent when the slot is deleted elsewhere', async () => {
    // Closing the slot from the dashboard used to leave this page latched, so the
    // next send created a slot with the DEFAULT agent -- taking the pet's prompt,
    // skills, MCP and context-usage reporting with it, silently.
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ agent: 'mochi' }) })
    vi.stubGlobal('fetch', fetchMock)
    await bridge.sendMessage('first')
    const binds = () => fetchMock.mock.calls.filter((c) => c[0] === '/api/chat/slots').length
    expect(binds()).toBe(1)

    await bridge.sendMessage('second')
    expect(binds()).toBe(1) // still latched — no redundant POST

    bridge.onSlotsUpdate(() => {}) // opens the socket, as the panel does
    FakeWebSocket.last!.emit('slots', { slots: [{ key: 'someone-else' }] }) // ours is gone
    await bridge.sendMessage('third')
    expect(binds()).toBe(2) // re-bound
  })

  it('drops core-internal roles so tool calls are not drawn as pet replies', () => {
    // Core appends tool/done/chunk/system/permission/notice rows to the slot and
    // broadcasts each as a chat_message. Upstream Mochi never saw them; rendering
    // them produced the "🔧 Running: …" bubbles AND suppressed the waiting paws
    // (any streamed text hides them).
    for (const role of ['tool', 'done', 'chunk', 'system', 'permission', 'notice']) {
      expect(bridge.isRenderableChatRole(role)).toBe(false)
    }
  })

  it('keeps user, assistant and error visible', () => {
    // error is kept deliberately: the panel has no other surface for a failed turn.
    for (const role of ['user', 'assistant', 'error']) {
      expect(bridge.isRenderableChatRole(role)).toBe(true)
    }
    expect(bridge.isRenderableChatRole(undefined)).toBe(false)
  })

  it('binds the slot only once across sends (idempotent)', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ agent: 'mochi' }) })
    vi.stubGlobal('fetch', fetchMock)
    await bridge.sendMessage('one')
    await bridge.sendMessage('two')
    const createCalls = fetchMock.mock.calls.filter((c) => c[0] === '/api/chat/slots')
    expect(createCalls).toHaveLength(1)
  })

  it('re-binds on the next send if the bind failed', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, status: 503 }) // ensureSlot fails
      .mockResolvedValue({ ok: true, json: async () => ({ agent: 'mochi' }) })
    vi.stubGlobal('fetch', fetchMock)
    // A failed slot-create must ABORT the send (same invariant as the rebind
    // branches): a bare return fell through to the chat POST, so the turn
    // landed in an unbound slot and the ambient dashboard agent answered.
    await expect(bridge.sendMessage('one')).rejects.toThrow(/could not create the pet slot/)
    expect(fetchMock.mock.calls.some((c) => c[0] === '/api/chat?ws=1')).toBe(false)
    // The latch never tripped, so the next send retries the bind and goes through.
    await bridge.sendMessage('two')
    const createCalls = fetchMock.mock.calls.filter((c) => c[0] === '/api/chat/slots')
    expect(createCalls.length).toBeGreaterThanOrEqual(2)
    expect(fetchMock.mock.calls.some((c) => c[0] === '/api/chat?ws=1')).toBe(true)
  })

  it('refuses to adopt an empty-agent slot that has a turn running', async () => {
    // Adoption rebinds, and a rebind resets the slot session: rebinding a busy
    // slot would tear down the turn already streaming in it. The endpoint
    // enforces this atomically (409 `slot_running`, covered below), but the
    // `running` flag rides along on the get_or_create response, so ensureSlot
    // refuses BEFORE the rebind POST as a free fast path — no extra request,
    // no round-trip wasted on a refusal we can already predict.
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url === '/api/chat/slots') {
        return Promise.resolve({ ok: true, json: async () => ({ agent: '', running: true }) })
      }
      return Promise.resolve({ ok: true, json: async () => ({}) })
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(bridge.sendMessage('hi')).rejects.toThrow(/busy/)
    // Neither the rebind nor the chat turn was posted.
    expect(
      fetchMock.mock.calls.some((c) => c[0] === `/api/chat/slots/${bridge.MOCHI_SLOT}/agent`),
    ).toBe(false)
    expect(fetchMock.mock.calls.some((c) => c[0] === '/api/chat?ws=1')).toBe(false)

    // Once the running turn ends, the retry adopts and sends (latch never tripped).
    fetchMock.mockImplementation((url: string) => {
      if (url === '/api/chat/slots') {
        return Promise.resolve({ ok: true, json: async () => ({ agent: '', running: false }) })
      }
      if (url === `/api/chat/slots/${bridge.MOCHI_SLOT}/agent`) {
        return Promise.resolve({ ok: true, json: async () => ({ ok: true, agent: 'mochi' }) })
      }
      return Promise.resolve({ ok: true, json: async () => ({}) })
    })
    await bridge.sendMessage('hi again')
    expect(fetchMock.mock.calls.some((c) => c[0] === '/api/chat?ws=1')).toBe(true)
  })

  it('surfaces the server-side busy refusal of the rebind as the busy bind error', async () => {
    // The client's `running` check reads a lagging snapshot: a turn can start
    // between the get_or_create response and the rebind POST. The endpoint
    // re-checks atomically under the slot's turn lock and refuses with 409
    // `slot_running` instead of tearing the turn down — ensureSlot must
    // surface that as the same "busy, retry in a moment" bind error as the
    // fast path (NOT the generic bind failure), abort the send, and leave the
    // latch false so the next send retries.
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url === '/api/chat/slots') {
        // Snapshot reads idle — the race window the server guard closes.
        return Promise.resolve({ ok: true, json: async () => ({ agent: '', running: false }) })
      }
      if (url === `/api/chat/slots/${bridge.MOCHI_SLOT}/agent`) {
        return Promise.resolve({
          ok: false,
          status: 409,
          json: async () => ({ error: 'slot is running', code: 'slot_running' }),
        })
      }
      return Promise.resolve({ ok: true, json: async () => ({}) })
    })
    vi.stubGlobal('fetch', fetchMock)

    const err = await bridge.sendMessage('hi').catch((e: unknown) => e)
    expect(bridge.isMochiBindError(err)).toBe(true)
    expect(String(err)).toMatch(/busy/)
    // The refused send never reached the chat POST.
    expect(fetchMock.mock.calls.some((c) => c[0] === '/api/chat?ws=1')).toBe(false)

    // Once the turn ends the retry adopts and sends (latch never tripped).
    fetchMock.mockImplementation((url: string) => {
      if (url === '/api/chat/slots') {
        return Promise.resolve({ ok: true, json: async () => ({ agent: '', running: false }) })
      }
      if (url === `/api/chat/slots/${bridge.MOCHI_SLOT}/agent`) {
        return Promise.resolve({ ok: true, json: async () => ({ ok: true, agent: 'mochi' }) })
      }
      return Promise.resolve({ ok: true, json: async () => ({}) })
    })
    await bridge.sendMessage('hi again')
    expect(fetchMock.mock.calls.some((c) => c[0] === '/api/chat?ws=1')).toBe(true)
  })

  it('keeps non-busy rebind refusals on the generic bind error', async () => {
    // Only the structured 409 `slot_running` refusal means "busy, retry in a
    // moment"; any other rebind failure (here a 409 without the code and an
    // unparseable body) must stay on the generic bind error so a real binding
    // defect is not misreported as transient busyness.
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url === '/api/chat/slots') {
        return Promise.resolve({ ok: true, json: async () => ({ agent: '', running: false }) })
      }
      if (url === `/api/chat/slots/${bridge.MOCHI_SLOT}/agent`) {
        return Promise.resolve({
          ok: false,
          status: 409,
          json: async () => {
            throw new Error('no body')
          },
        })
      }
      return Promise.resolve({ ok: true, json: async () => ({}) })
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(bridge.sendMessage('hi')).rejects.toThrow(/could not bind the pet agent/)
    expect(fetchMock.mock.calls.some((c) => c[0] === '/api/chat?ws=1')).toBe(false)
  })

  it('classifies every ensureSlot failure as a bind error, distinct from transport failures', async () => {
    // ChatPanel keys its error string off this classification: bind failures say
    // "couldn't connect the pet's agent", NOT "check your connection" — blaming
    // the network for a server-side binding refusal sends the user debugging
    // the wrong thing.
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => ({ agent: '', running: true }) })
    vi.stubGlobal('fetch', fetchMock)
    const err = await bridge.sendMessage('hi').catch((e: unknown) => e)
    expect(bridge.isMochiBindError(err)).toBe(true)
    // A plain transport error is NOT a bind error.
    expect(bridge.isMochiBindError(new TypeError('Failed to fetch'))).toBe(false)
  })

  it('refuses to send when the slot is owned by another agent', async () => {
    // get_or_create returns an existing foreign-agent slot UNCHANGED; sending the
    // pet's turn into it would hijack/corrupt that session. ensureSlot must abort.
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => ({ agent: 'someone-else' }) })
    vi.stubGlobal('fetch', fetchMock)
    await expect(bridge.sendMessage('hi')).rejects.toThrow(/another agent/)
    // The chat turn was NOT posted into the foreign slot.
    expect(fetchMock.mock.calls.some((c) => c[0] === '/api/chat?ws=1')).toBe(false)
  })

  it('adopts a pre-existing mochi slot whose agent binding is empty', async () => {
    // A `mochi` slot persisted before the agent-binding feature (or restored from
    // open_slots.json at boot) comes back from get_or_create with an EMPTY agent,
    // so the ambient dashboard default answers the pet — the reported bug. Because
    // it is OUR OWN slot (not a foreign named agent), ensureSlot must ADOPT it via
    // the explicit rebind endpoint rather than refusing, then send.
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url === '/api/chat/slots') {
        return Promise.resolve({ ok: true, json: async () => ({ agent: '' }) })
      }
      if (url === `/api/chat/slots/${bridge.MOCHI_SLOT}/agent`) {
        return Promise.resolve({ ok: true, json: async () => ({ ok: true, agent: 'mochi' }) })
      }
      return Promise.resolve({ ok: true, json: async () => ({}) })
    })
    vi.stubGlobal('fetch', fetchMock)

    await bridge.sendMessage('hi')

    const rebind = fetchMock.mock.calls.find(
      (c) => c[0] === `/api/chat/slots/${bridge.MOCHI_SLOT}/agent`,
    )!
    expect(rebind).toBeTruthy()
    expect(rebind[1].method).toBe('POST')
    expect(JSON.parse(rebind[1].body)).toEqual({ agent: bridge.MOCHI_AGENT })
    // The chat turn WAS posted, into the now-rebound slot.
    expect(fetchMock.mock.calls.some((c) => c[0] === '/api/chat?ws=1')).toBe(true)
  })

  it('REFUSES a POPULATED empty-agent slot — never deletes, never rebinds, never sends', async () => {
    // A populated empty-agent slot carries a foreign transcript (rebinding
    // would replay it into the pet's fresh session), and the prior answer —
    // DELETE + recreate — was unsafe: the `running` check is a lagging
    // snapshot, so a turn another tab started after it would be cancelled
    // outright by the DELETE. GPT round-9: refuse instead of deleting.
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url === '/api/chat/slots') {
        // The persisted, populated, unbound slot.
        return Promise.resolve({ ok: true, json: async () => ({ agent: '', messages: 7 }) })
      }
      return Promise.resolve({ ok: true, json: async () => ({}) })
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(bridge.sendMessage('hi')).rejects.toThrow(/occupied by an existing conversation/)
    // No DELETE, no recreate, no rebind, no turn posted.
    expect(
      fetchMock.mock.calls.some((c) => c[1] && (c[1] as RequestInit).method === 'DELETE'),
    ).toBe(false)
    expect(fetchMock.mock.calls.filter((c) => c[0] === '/api/chat/slots').length).toBe(1)
    expect(
      fetchMock.mock.calls.some((c) => c[0] === `/api/chat/slots/${bridge.MOCHI_SLOT}/agent`),
    ).toBe(false)
    expect(fetchMock.mock.calls.some((c) => c[0] === '/api/chat?ws=1')).toBe(false)
  })

  it('classifies the occupied refusal distinctly from transient bind errors', async () => {
    // ChatPanel keys a SEPARATE string off this classification: the occupied
    // refusal is permanent until the user clears the populated slot, so the
    // bind_failed "try again in a moment" copy would loop the user on advice
    // that can never work (Design/UX watch item on this PR).
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url === '/api/chat/slots') {
        return Promise.resolve({ ok: true, json: async () => ({ agent: '', messages: 7 }) })
      }
      return Promise.resolve({ ok: true, json: async () => ({}) })
    })
    vi.stubGlobal('fetch', fetchMock)

    const err = await bridge.sendMessage('hi').catch((e: unknown) => e)
    // Still a bind error (the name is the classification key)…
    expect(bridge.isMochiBindError(err)).toBe(true)
    // …but carrying the occupied discriminator.
    expect(bridge.isMochiSlotOccupiedError(err)).toBe(true)

    // Transient refusals do NOT carry it: a busy slot is genuinely retryable.
    const busyMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => ({ agent: '', running: true }) })
    vi.stubGlobal('fetch', busyMock)
    const busyErr = await bridge.sendMessage('hi').catch((e: unknown) => e)
    expect(bridge.isMochiBindError(busyErr)).toBe(true)
    expect(bridge.isMochiSlotOccupiedError(busyErr)).toBe(false)
    // Nor do plain transport errors.
    expect(bridge.isMochiSlotOccupiedError(new TypeError('Failed to fetch'))).toBe(false)
  })

  it('the populated refusal keeps the latch false so the next send retries adoption', async () => {
    // First send refuses on the populated slot; once the slot is empty (user
    // cleared it), the next send must re-run the adoption from the top and
    // bind normally — the refusal must not latch the bridge shut.
    let slotCalls = 0
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url === '/api/chat/slots') {
        slotCalls += 1
        // Populated on the first adoption attempt, empty afterwards.
        return Promise.resolve({
          ok: true,
          json: async () => (slotCalls === 1 ? { agent: '', messages: 7 } : { agent: '', messages: 0 }),
        })
      }
      if (url === `/api/chat/slots/${bridge.MOCHI_SLOT}/agent`) {
        return Promise.resolve({ ok: true, json: async () => ({ ok: true, agent: 'mochi' }) })
      }
      return Promise.resolve({ ok: true, json: async () => ({}) })
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(bridge.sendMessage('hi')).rejects.toThrow(/occupied by an existing conversation/)
    await bridge.sendMessage('hi again')
    expect(
      fetchMock.mock.calls.some((c) => c[0] === `/api/chat/slots/${bridge.MOCHI_SLOT}/agent`),
    ).toBe(true)
    expect(fetchMock.mock.calls.some((c) => c[0] === '/api/chat?ws=1')).toBe(true)
  })

  it('does not send when adopting an empty-agent slot fails to rebind', async () => {
    // If the rebind POST fails, ensureSlot must THROW so the send aborts — a bare
    // return left the flag false but sendMessage still POSTed the turn to the
    // unbound slot, where the ambient dashboard agent answered the pet. The throw
    // also keeps the flag false, so the next send retries the bind.
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url === '/api/chat/slots') {
        return Promise.resolve({ ok: true, json: async () => ({ agent: '' }) })
      }
      if (url === `/api/chat/slots/${bridge.MOCHI_SLOT}/agent`) {
        return Promise.resolve({ ok: false, status: 503 })
      }
      return Promise.resolve({ ok: true, json: async () => ({}) })
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(bridge.sendMessage('hi')).rejects.toThrow(/could not bind the pet agent/)
    // The turn was NOT posted to the unbound slot.
    expect(fetchMock.mock.calls.some((c) => c[0] === '/api/chat?ws=1')).toBe(false)

    // A second send retries the bind (flag never latched).
    await expect(bridge.sendMessage('hi again')).rejects.toThrow(/could not bind the pet agent/)
    const rebinds = fetchMock.mock.calls.filter(
      (c) => c[0] === `/api/chat/slots/${bridge.MOCHI_SLOT}/agent`,
    )
    expect(rebinds.length).toBe(2)
    // Still no chat turn posted after the retry either.
    expect(fetchMock.mock.calls.some((c) => c[0] === '/api/chat?ws=1')).toBe(false)
  })

  it('stopGeneration targets the mochi slot', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ agent: 'mochi' }) })
    vi.stubGlobal('fetch', fetchMock)

    await bridge.stopGeneration()

    expect(fetchMock.mock.calls[0][0]).toBe(`/api/chat/slots/${bridge.MOCHI_SLOT}/stop`)
  })

  it('forwards context usage percentage for the mochi slot only', () => {
    const seen: number[] = []
    bridge.onContextUsage((p) => seen.push(p))
    const ws = FakeWebSocket.last!
    ws.emit('context_usage', { slot: 'default', pct: 90 })
    ws.emit('context_usage', { slot: bridge.MOCHI_SLOT, pct: 42.5 })
    expect(seen).toEqual([42.5])
  })

  it('getChatHistory returns the slot messages', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ messages: [{ role: 'user', content: 'hi' }] }),
      }),
    )
    await expect(bridge.getChatHistory()).resolves.toEqual([
      { role: 'user', content: 'hi' },
    ])
  })

  it('getChatHistory yields an empty array when the slot does not exist yet', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404 }))
    await expect(bridge.getChatHistory()).resolves.toEqual([])
  })

  it('getChatHistory swallows transport failures', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    await expect(bridge.getChatHistory()).resolves.toEqual([])
  })

  it('newSession deletes the slot and tolerates a missing one', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ agent: 'mochi' }) })
    vi.stubGlobal('fetch', fetchMock)
    await bridge.newSession()
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(`/api/chat/slots/${bridge.MOCHI_SLOT}`)
    expect(init.method).toBe('DELETE')

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404 }))
    await expect(bridge.newSession()).resolves.toBeUndefined()
  })

  it('newSession surfaces real server errors to the caller', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 500 }))
    await expect(bridge.newSession()).rejects.toThrow(/newSession failed: 500/)
  })

  // Connection indicator — the builtin's "online" is the WebSocket, not a
  // separate gateway process (see panelBridge's DECISION note).
  it('getBackendStatus resolves the live socket state', async () => {
    await expect(bridge.getBackendStatus()).resolves.toBe(false)
    FakeWebSocket.last!.onopen?.()
    await expect(bridge.getBackendStatus()).resolves.toBe(true)
  })

  it('onBackendStatus fires on open and close transitions only', () => {
    const seen: boolean[] = []
    bridge.onBackendStatus((o) => seen.push(o))
    const ws = FakeWebSocket.last!
    ws.onopen?.()
    ws.onopen?.() // duplicate open — must not re-fire
    ws.onclose?.()
    expect(seen).toEqual([true, false])
  })

  it('retryConnect cancels backoff and reopens the socket immediately', () => {
    vi.useFakeTimers()
    try {
      const seen: boolean[] = []
      bridge.onBackendStatus((o) => seen.push(o))
      const ws1 = FakeWebSocket.last!
      ws1.onopen?.()
      ws1.onclose?.() // schedules a backoff reconnect
      expect(seen).toEqual([true, false])

      void bridge.retryConnect()
      const ws2 = FakeWebSocket.last!
      expect(ws2).not.toBe(ws1)
      ws2.onopen?.()
      expect(seen).toEqual([true, false, true])
      vi.clearAllTimers()
    } finally {
      vi.useRealTimers()
    }
  })

  // Pet state / mood — the backend PetStateManager is the source of truth. The
  // panel/pet read the current value once (getPetState) then track live changes
  // over the shared WS. These events are app-scoped ({args:[value]}), not
  // slot-scoped chat frames, so they must survive the slot filter.
  it('getPetState reads the backend pet-state route', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ state: 'thinking', mood: 'happy' }),
      }),
    )
    await expect(bridge.getPetState()).resolves.toBe('thinking')
  })

  it('getPetState falls back to offline on a missing route or transport error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404 }))
    await expect(bridge.getPetState()).resolves.toBe('offline')
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    await expect(bridge.getPetState()).resolves.toBe('offline')
  })

  it('onStateChange delivers backend pet:state-change events (args payload)', () => {
    const seen: string[] = []
    bridge.onStateChange((s) => seen.push(s))
    FakeWebSocket.last!.emit('pet:state-change', { args: ['working'] })
    expect(seen).toEqual(['working'])
  })

  it('onMood delivers backend mochi:mood events (args payload)', () => {
    const seen: string[] = []
    bridge.onMood((m) => seen.push(m))
    FakeWebSocket.last!.emit('mochi:mood', { args: ['curious'] })
    expect(seen).toEqual(['curious'])
  })

  it('pet state/mood events are not dropped by the slot filter', () => {
    const states: string[] = []
    const chunks: string[] = []
    bridge.onStateChange((s) => states.push(s))
    bridge.onChatChunk((c) => chunks.push(c))
    const ws = FakeWebSocket.last!
    // No `slot` field on the pet event — must still be delivered.
    ws.emit('pet:state-change', { args: ['idle'] })
    expect(states).toEqual(['idle'])
    expect(chunks).toEqual([])
  })
})

/**
 * The live-refresh and approval branches.
 *
 * All of these frames were already arriving on this socket; `ws.onmessage` had
 * no branch for them, so they were parsed and thrown away — which is why the pin
 * rail polled, the appearance needed a pet reopen, and tool approvals never
 * appeared in the pet's chat at all.
 */
describe('mochi panelBridge live events', () => {
  let bridge: typeof import('../panel/panelBridge')

  beforeEach(async () => {
    vi.resetModules()
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }))
    FakeWebSocket.last = null
    bridge = await import('../panel/panelBridge')
  })

  it('delivers the pinned:* family with its payload', () => {
    const changed: unknown[] = []
    const updated: unknown[] = []
    const deleted: unknown[] = []
    bridge.onPinnedFilesChanged((p) => changed.push(p))
    bridge.onPinnedFileUpdated((p) => updated.push(p))
    bridge.onPinnedFileDeleted((p) => deleted.push(p))
    const ws = FakeWebSocket.last!
    ws.emit('pinned:files-changed', { args: [[{ path: '/a' }]] })
    ws.emit('pinned:file-updated', { args: [{ path: '/a', updatedAt: 7 }] })
    ws.emit('pinned:file-deleted', { args: [{ path: '/a' }] })
    expect(changed).toEqual([[{ path: '/a' }]])
    expect(updated).toEqual([{ path: '/a', updatedAt: 7 }])
    expect(deleted).toEqual([{ path: '/a' }])
  })

  it('delivers notifications and the appearance broadcasts', () => {
    const notes: unknown[] = []
    const packs: number[] = []
    const maps: unknown[] = []
    bridge.onNotification((n) => notes.push(n))
    bridge.onGalleryPacksChanged(() => packs.push(1))
    bridge.onColorMapChanged((m) => maps.push(m))
    const ws = FakeWebSocket.last!
    ws.emit('mochi:notify', { args: [{ summary: 'hi' }] })
    ws.emit('mochi:gallery-packs-changed', { args: [{ packId: 'p1' }] })
    ws.emit('mochi:color-map-changed', { args: [{ catPreset: 'calico' }] })
    expect(notes).toEqual([{ summary: 'hi' }])
    expect(packs).toEqual([1])
    expect(maps).toEqual([{ catPreset: 'calico' }])
  })

  it('delivers approval requests for the pet slot only', () => {
    const seen: Record<string, unknown>[] = []
    bridge.onApprovalRequest((r) => seen.push(r))
    const ws = FakeWebSocket.last!
    ws.emit('approval', { slot: 'mochi', id: 'a1', tool: 'fs_write' })
    ws.emit('approval', { slot: 'other', id: 'a2', tool: 'fs_write' })
    expect(seen.map((r) => r.id)).toEqual(['a1'])
  })

  it('renders a permission-role chat frame as an approval card (path B)', () => {
    // The pet's own turn surfaces approvals as a `permission` chat_message whose
    // `cls` holds the meta — NOT a dedicated `approval` frame. Without rebuilding
    // it the pet showed nothing while the dashboard rendered the same card.
    const seen: Record<string, unknown>[] = []
    bridge.onApprovalRequest((r) => seen.push(r))
    const cls = JSON.stringify({
      request_id: 'req-9',
      tool_title: 'fs_read',
      tool_input: '/tmp/x',
    })
    FakeWebSocket.last!.emit('chat_message', { slot: 'mochi', role: 'permission', content: 'fs_read', cls })
    expect(seen).toEqual([{ id: 'req-9', tool: 'fs_read', toolInput: '/tmp/x' }])
  })

  it('does NOT re-open a permission frame already resolved (history replay)', () => {
    const seen: Record<string, unknown>[] = []
    bridge.onApprovalRequest((r) => seen.push(r))
    const cls = JSON.stringify({ request_id: 'req-9', tool_title: 'fs_read', resolved: 'approved' })
    FakeWebSocket.last!.emit('chat_message', { slot: 'mochi', role: 'permission', content: 'fs_read', cls })
    expect(seen).toEqual([])
  })

  it('ignores a permission frame for another slot, and never renders it as a bubble', () => {
    const approvals: Record<string, unknown>[] = []
    const messages: Record<string, unknown>[] = []
    bridge.onApprovalRequest((r) => approvals.push(r))
    bridge.onChatMessage((m) => messages.push(m))
    const cls = JSON.stringify({ request_id: 'req-1', tool_title: 'fs_read' })
    const ws = FakeWebSocket.last!
    ws.emit('chat_message', { slot: 'other', role: 'permission', content: 'fs_read', cls })
    ws.emit('chat_message', { slot: 'mochi', role: 'permission', content: 'fs_read', cls })
    expect(approvals.map((r) => r.id)).toEqual(['req-1']) // only the mochi one
    expect(messages).toEqual([]) // permission is never a renderable bubble
  })

  it('delivers approval_resolved even though it carries no slot', () => {
    // The gateway's resolve frame has no slot; requiring one would mean the pet
    // never learns that the dashboard already answered, leaving a dead card.
    const seen: Record<string, unknown>[] = []
    bridge.onApprovalResolvedExternal((r) => seen.push(r))
    FakeWebSocket.last!.emit('approval_resolved', { id: 'a1', approved: true })
    expect(seen).toEqual([{ id: 'a1', approved: true }])
  })

  it('respondApproval routes approve/reject and trust to DIFFERENT endpoints', async () => {
    const calls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        calls.push(url)
        return Promise.resolve({ ok: true, json: async () => ({}) })
      }),
    )
    await bridge.respondApproval('a1', 'approve')
    await bridge.respondApproval('a2', 'reject')
    await bridge.respondApproval('a3', 'trust')
    expect(calls[0]).toBe('/api/approvals/a1/approve')
    expect(calls[1]).toBe('/api/approvals/a2/reject')
    expect(calls[2]).toBe('/api/chat/slots/mochi/approve')
  })

  it('respondApproval reports failure instead of claiming success', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 400 }))
    await expect(bridge.respondApproval('a1', 'approve')).resolves.toMatchObject({ ok: false })
  })

  it('localFileUrl points at core file-raw rather than base64 over IPC', () => {
    expect(bridge.localFileUrl('/tmp/a b.png')).toBe('/api/file-raw?path=%2Ftmp%2Fa%20b.png')
  })

  // clearCompleted is a permanent bulk delete, and the caller drops the rows from
  // its own list. Reporting a rejected request as success therefore looked like a
  // successful delete until the next refresh brought every item back.
  it('clearCompletedWatchItems reports a rejected request as failure', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 500 }))
    await expect(bridge.clearCompletedWatchItems()).resolves.toBe(false)
  })

  it('clearCompletedWatchItems reports a network error as failure, not a throw', async () => {
    // The caller awaits this inside a click handler; an unhandled rejection there
    // leaves the dialog stuck on "working" with nothing said.
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    await expect(bridge.clearCompletedWatchItems()).resolves.toBe(false)
  })

  it('clearCompletedWatchItems reports success and still re-reads the list', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ items: [] }) })
    vi.stubGlobal('fetch', fetchMock)

    await expect(bridge.clearCompletedWatchItems()).resolves.toBe(true)
    const posted = fetchMock.mock.calls.find(
      (c) => c[0] === '/api/apps/mochi/watchlist/clear-completed',
    )
    expect(posted).toBeTruthy()
  })
})
