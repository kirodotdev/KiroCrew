/**
 * The session-completion watcher, driven by a fake socket.
 *
 * The producing half of session notifications did not exist in this port at all:
 * the frontend could render a `session-done` bubble, the panel had a switch for it,
 * and nothing anywhere ever raised one. So these tests are about the seam that was
 * missing rather than a regression — with one exception worth naming: the gateway
 * emits NO turn-start timestamp, so "was this turn long enough to interrupt for" is
 * answerable only by having watched the start. The assumed-start group below is that
 * limit, encoded.
 */
import { describe, it, expect, vi } from 'vitest'

import { watchSessions, type SessionDone } from '../apps/crew-companion/sessionWatch'
import { TURN_NOTIFY_MIN_MS, BG_AGENT_PREFIX } from '../apps/crew-companion/completionGate'

/** A stand-in for the gateway socket that lets a test push frames by hand. */
function fakeSocket() {
  const ws = {
    onopen: null as null | (() => void),
    onmessage: null as null | ((ev: { data: string }) => void),
    onclose: null as null | (() => void),
    onerror: null as null | (() => void),
    closed: false,
    close() { this.closed = true; this.onclose?.() },
  }
  return ws
}

interface Harness {
  done: SessionDone[]
  send: (type: string, data: unknown) => void
  stop: () => void
  socket: ReturnType<typeof fakeSocket>
  setNow: (t: number) => void
  setSilent: (s: boolean) => void
}

function harness(): Harness {
  const socket = fakeSocket()
  const done: SessionDone[] = []
  let t = 1_000_000
  let silent = false
  const stop = watchSessions({
    onDone: (d) => done.push(d),
    isSilent: () => silent,
    now: () => t,
    connect: () => socket as unknown as WebSocket,
  })
  return {
    done,
    socket,
    stop,
    send: (type, data) => socket.onmessage?.({ data: JSON.stringify({ type, data }) }),
    setNow: (v) => { t = v },
    setSilent: (v) => { silent = v },
  }
}

describe('session completion → bubble', () => {
  it('reports an in-flight turn as FAILED when the socket drops', () => {
    /*
     * A gateway restart or network drop means `chat_done` will never arrive for that
     * turn. Silence would be wrong twice: the user is never told the work died, and the
     * stale start time makes the NEXT turn on that slot measure its duration from the
     * old start — reporting a three-second turn as a long one. The work stopped without
     * finishing and nobody asked for that, so it is a failure.
     */
    const h = harness()
    h.send('slots', { slots: [{ key: 'chat-1', running: false, title: 'Fix the parser' }] })
    h.send('chat_status', { slot: 'chat-1', status: 'Thinking…' })
    h.setNow(1_000_000 + TURN_NOTIFY_MIN_MS + 1)
    h.socket.onclose?.()
    expect(h.done).toHaveLength(1)
    expect(h.done[0].failed).toBe(true)
    expect(h.done[0].slot).toBe('chat-1')
    h.stop()
  })

  it('a crash is reported even with session alerts switched off', () => {
    // Same exception the error path already makes: bad news is never silenced.
    const h = harness()
    h.setSilent(true)
    h.send('slots', { slots: [{ key: 'chat-1', running: false, title: 'Fix the parser' }] })
    h.send('chat_status', { slot: 'chat-1', status: 'Thinking…' })
    h.setNow(1_000_000 + TURN_NOTIFY_MIN_MS + 1)
    h.socket.onclose?.()
    expect(h.done).toHaveLength(1)
    expect(h.done[0].failed).toBe(true)
    h.stop()
  })

  it('a deliberate Stop followed by a disconnect stays quiet', () => {
    // The stop guard must win over the crash handler — the user chose this ending.
    const h = harness()
    h.send('slots', { slots: [{ key: 'chat-1', running: false, title: 'Fix the parser' }] })
    h.send('chat_status', { slot: 'chat-1', status: 'Thinking…' })
    h.setNow(1_000_000 + TURN_NOTIFY_MIN_MS + 1)
    h.send('slots', { slots: [{ key: 'chat-1', running: true, stopping: true, title: 'Fix the parser' }] })
    h.socket.onclose?.()
    expect(h.done).toHaveLength(0)
    h.stop()
  })

  it('does not report anything when the CALLER stops watching', () => {
    // Unmounting the companion is not a crash. `stop()` detaches onclose first, which
    // is what keeps the teardown silent.
    const h = harness()
    h.send('slots', { slots: [{ key: 'chat-1', running: false, title: 'Fix the parser' }] })
    h.send('chat_status', { slot: 'chat-1', status: 'Thinking…' })
    h.setNow(1_000_000 + TURN_NOTIFY_MIN_MS + 1)
    h.stop()
    expect(h.done).toHaveLength(0)
  })

  it('stays quiet when the user pressed Stop mid-turn', () => {
    /*
     * The backend broadcasts `chat_done` for a stopped turn exactly as for a finished
     * one — `{slot}` either way — so the watcher used to celebrate an interruption as
     * a success. The only signal this socket gets is the transient `stopping: true` on
     * a slots frame while the cancel is in flight; recording it is what lets finish()
     * know this ending was chosen, not earned. Neither a hop nor an error shake is
     * right for "I changed my mind", so the turn is dropped silently.
     */
    const h = harness()
    h.send('slots', { slots: [{ key: 'chat-1', running: false, title: 'Fix the parser' }] })
    h.send('chat_status', { slot: 'chat-1', status: 'Thinking…' })
    h.setNow(1_000_000 + TURN_NOTIFY_MIN_MS + 1)
    // The user presses Stop: the cancel-in-flight flag rides a slots frame…
    h.send('slots', { slots: [{ key: 'chat-1', running: true, stopping: true, title: 'Fix the parser' }] })
    // …and the turn still terminates with an ordinary chat_done.
    h.send('chat_done', { slot: 'chat-1' })
    expect(h.done).toHaveLength(0)
    h.stop()
  })

  it('forgets a stop once that turn is over — the next completion still notifies', () => {
    // The stop must be consumed by the turn it ended, not poison the slot forever.
    const h = harness()
    h.send('slots', { slots: [{ key: 'chat-1', running: false, title: 'Fix the parser' }] })
    h.send('chat_status', { slot: 'chat-1', status: 'Thinking…' })
    h.send('slots', { slots: [{ key: 'chat-1', running: true, stopping: true, title: 'Fix the parser' }] })
    h.send('chat_done', { slot: 'chat-1' })
    expect(h.done).toHaveLength(0)
    // A fresh turn on the same slot, finished normally this time.
    h.send('chat_status', { slot: 'chat-1', status: 'Thinking…' })
    h.setNow(1_000_000 + 2 * (TURN_NOTIFY_MIN_MS + 1))
    h.send('chat_done', { slot: 'chat-1' })
    expect(h.done).toHaveLength(1)
    h.stop()
  })

  it('notifies when a watched turn ran long enough', () => {
    const h = harness()
    h.send('slots', { slots: [{ key: 'chat-1', running: false, title: 'Fix the parser' }] })
    h.send('chat_status', { slot: 'chat-1', status: 'Thinking…' })
    h.setNow(1_000_000 + TURN_NOTIFY_MIN_MS + 1)
    h.send('chat_done', { slot: 'chat-1' })
    expect(h.done).toHaveLength(1)
    expect(h.done[0].slot).toBe('chat-1')
    expect(h.done[0].title).toBe('Fix the parser')
    h.stop()
  })

  it('stays quiet for a turn shorter than the threshold', () => {
    // The desktop app's threshold was once 30s, which sat in the middle of real turn
    // durations and silently dropped about half of all completed work; 10s is the
    // ported value and the boundary is asserted rather than assumed.
    const h = harness()
    h.send('chat_status', { slot: 'chat-1' })
    h.setNow(1_000_000 + TURN_NOTIFY_MIN_MS - 1)
    h.send('chat_done', { slot: 'chat-1' })
    expect(h.done).toEqual([])
    h.stop()
  })

  it('stays quiet when the user switched session alerts off', () => {
    const h = harness()
    h.setSilent(true)
    h.send('chat_status', { slot: 'chat-1' })
    h.setNow(1_000_000 + TURN_NOTIFY_MIN_MS + 5_000)
    h.send('chat_done', { slot: 'chat-1' })
    expect(h.done).toEqual([])
    h.stop()
  })

  it('reads the preference at completion time, not at subscribe time', () => {
    // The switch has to take effect on the next completion, not the next restart.
    const h = harness()
    h.send('chat_status', { slot: 'chat-1' })
    h.setSilent(true)
    h.setNow(1_000_000 + TURN_NOTIFY_MIN_MS + 1)
    h.send('chat_done', { slot: 'chat-1' })
    expect(h.done).toEqual([])
    h.stop()
  })

  it('ignores a completion it never saw start', () => {
    // No start observed and none recoverable: the gateway sends no start timestamp.
    const h = harness()
    h.send('chat_done', { slot: 'never-seen' })
    expect(h.done).toEqual([])
    h.stop()
  })

  it("never celebrates the companion's own background agent", () => {
    const h = harness()
    const slot = `${BG_AGENT_PREFIX}-1`
    h.send('chat_status', { slot })
    h.setNow(1_000_000 + TURN_NOTIFY_MIN_MS + 60_000)
    h.send('chat_done', { slot })
    expect(h.done).toEqual([])
    h.stop()
  })

  it('treats a slot already running as an assumed start, still gated on duration', () => {
    // Joining mid-turn: the start is unknown, so the elapsed we can measure is from
    // first sight. It must still face the threshold rather than notify unconditionally.
    const h = harness()
    h.send('slots', { slots: [{ key: 'chat-2', running: true, title: 'Long job' }] })
    h.setNow(1_000_000 + 500)
    h.send('chat_done', { slot: 'chat-2' })
    expect(h.done).toEqual([])
    h.stop()
  })

  it('does not treat subagent_done as a session finishing', () => {
    const h = harness()
    h.send('chat_status', { slot: 'chat-1' })
    h.setNow(1_000_000 + TURN_NOTIFY_MIN_MS + 1)
    h.send('subagent_done', { slot: 'chat-1' })
    expect(h.done).toEqual([])
    h.stop()
  })

  it('survives a malformed frame without dying', () => {
    const h = harness()
    h.socket.onmessage?.({ data: 'not json' })
    h.send('chat_status', { slot: 'chat-1' })
    h.setNow(1_000_000 + TURN_NOTIFY_MIN_MS + 1)
    h.send('chat_done', { slot: 'chat-1' })
    expect(h.done).toHaveLength(1)
    h.stop()
  })

  it('each turn is measured on its own, not from the first one ever seen', () => {
    const h = harness()
    h.send('chat_status', { slot: 'chat-1' })
    h.setNow(1_000_000 + TURN_NOTIFY_MIN_MS + 1)
    h.send('chat_done', { slot: 'chat-1' })
    // Second turn on the same slot, this time a short one.
    h.send('chat_status', { slot: 'chat-1' })
    h.setNow(1_000_000 + TURN_NOTIFY_MIN_MS + 1_000)
    h.send('chat_done', { slot: 'chat-1' })
    expect(h.done).toHaveLength(1)
    h.stop()
  })

  it('a FAILED turn is reported, and reported as a failure', () => {
    /*
     * The gateway has no "failed" event. A broken turn emits a `chat_message` with
     * `role: 'error'` and then terminates with an ordinary `chat_done` — so a watcher
     * that only reads `chat_done` cheerfully announces work that actually stopped.
     */
    const h = harness()
    h.send('chat_status', { slot: 'chat-1' })
    h.send('chat_message', { slot: 'chat-1', role: 'error', content: 'boom' })
    h.setNow(1_000_000 + TURN_NOTIFY_MIN_MS + 1)
    h.send('chat_done', { slot: 'chat-1' })
    expect(h.done).toHaveLength(1)
    expect(h.done[0].failed).toBe(true)
    h.stop()
  })

  it('a failure is reported even when the user silenced completions', () => {
    // "Tell me when sessions are done" is a preference about good news. The app this
    // was ported from made the same exception: a failure is the case you most need.
    const h = harness()
    h.setSilent(true)
    h.send('chat_status', { slot: 'chat-1' })
    h.send('chat_message', { slot: 'chat-1', role: 'error', content: 'boom' })
    h.setNow(1_000_000 + TURN_NOTIFY_MIN_MS + 1)
    h.send('chat_done', { slot: 'chat-1' })
    expect(h.done).toHaveLength(1)
    expect(h.done[0].failed).toBe(true)
    h.stop()
  })

  it('a short failure is still too short to interrupt for', () => {
    // Bypassing SILENCE is not bypassing the duration rule.
    const h = harness()
    h.send('chat_status', { slot: 'chat-1' })
    h.send('chat_message', { slot: 'chat-1', role: 'error', content: 'boom' })
    h.setNow(1_000_000 + TURN_NOTIFY_MIN_MS - 1)
    h.send('chat_done', { slot: 'chat-1' })
    expect(h.done).toEqual([])
    h.stop()
  })

  it('a failure does not leak into the NEXT turn on the same session', () => {
    const h = harness()
    h.send('chat_status', { slot: 'chat-1' })
    h.send('chat_message', { slot: 'chat-1', role: 'error', content: 'boom' })
    h.setNow(1_000_000 + TURN_NOTIFY_MIN_MS + 1)
    h.send('chat_done', { slot: 'chat-1' })
    // Second turn, no error this time.
    h.send('chat_status', { slot: 'chat-1' })
    h.setNow(1_000_000 + 2 * TURN_NOTIFY_MIN_MS + 2)
    h.send('chat_done', { slot: 'chat-1' })
    expect(h.done.map((d) => d.failed)).toEqual([true, false])
    h.stop()
  })

  it('an ordinary assistant message is not mistaken for a failure', () => {
    const h = harness()
    h.send('chat_status', { slot: 'chat-1' })
    h.send('chat_message', { slot: 'chat-1', role: 'assistant', content: 'hello' })
    h.setNow(1_000_000 + TURN_NOTIFY_MIN_MS + 1)
    h.send('chat_done', { slot: 'chat-1' })
    expect(h.done[0].failed).toBe(false)
    h.stop()
  })

  it('work approved elsewhere is announced so the slot can be freed', () => {
    // The bubble asked a question; once it is answered in the dashboard the bubble is
    // stale, and waiting out the bounded hold blocks everything behind it.
    let resolved = 0
    const socket = fakeSocket()
    const stop = watchSessions({
      onDone: () => {},
      isSilent: () => false,
      onApprovalResolved: () => { resolved += 1 },
      connect: () => socket as unknown as WebSocket,
    })
    socket.onmessage?.({
      data: JSON.stringify({ type: 'approval_resolved', data: { id: 'a1', approved: true } }),
    })
    expect(resolved).toBe(1)
    stop()
  })

  it('the backend doorbell drains the queue at once', () => {
    // Latency, not correctness: the fire is already queued, so the poll would find
    // it eventually. The doorbell exists because "eventually" was up to the poll
    // interval ON TOP of the backend's own 1s tick, and a reminder the user set for
    // a specific moment arrived visibly late.
    let rung = 0
    const socket = fakeSocket()
    const stop = watchSessions({
      onDone: () => {},
      isSilent: () => false,
      onFireQueued: () => { rung += 1 },
      connect: () => socket as unknown as WebSocket,
    })
    const send = (type: string, data: unknown) =>
      socket.onmessage?.({ data: JSON.stringify({ type, data }) })

    // The REAL wire shape: event_bus.py puts the name in `event` and strips
    // `type`. This test used to send `type`, which is why it passed while the
    // doorbell never actually rang against a live gateway.
    send('app_event', { app: 'crew-companion', event: 'crew-companion:fire' })
    expect(rung).toBe(1)

    // Another app's events ride the same envelope and must not ring this bell.
    send('app_event', { app: 'mochi', event: 'mochi:notify' })
    send('app_event', { app: 'crew-companion', event: 'crew-companion:other' })
    expect(rung).toBe(1)

    // And the field the envelope does NOT carry must not ring it either --
    // otherwise this test would keep passing if the code regressed to `type`.
    send('app_event', { app: 'crew-companion', type: 'crew-companion:fire' })
    expect(rung).toBe(1)
    stop()
  })

  it('a pending approval is announced with the session it is blocking', () => {
    // The gateway broadcasts `approval` the moment a tool goes pending, carrying the
    // slot but no human name — the title comes from the last `slots` frame we saw.
    const approvals: { slot: string; title: string }[] = []
    const socket = fakeSocket()
    const stop = watchSessions({
      onDone: () => {},
      isSilent: () => false,
      onApproval: (a) => approvals.push(a),
      connect: () => socket as unknown as WebSocket,
    })
    const send = (type: string, data: unknown) =>
      socket.onmessage?.({ data: JSON.stringify({ type, data }) })

    send('slots', { slots: [{ key: 'chat-9', running: true, title: 'Ship the release' }] })
    // The real broadcast shape from state.py: id + slot + tool + ts, no title.
    send('approval', { id: 'a7', slot: 'chat-9', tool: 'shell', tool_purpose: 'run it', ts: 123 })
    expect(approvals).toEqual([{ slot: 'chat-9', title: 'Ship the release' }])
    stop()
  })

  it('a pending approval still fires when session-done alerts are silenced', () => {
    // "Tell me when sessions are done" is about good news. Waiting on YOU is
    // unresolved work — it must announce regardless of that switch.
    const approvals: { slot: string; title: string }[] = []
    const socket = fakeSocket()
    const stop = watchSessions({
      onDone: () => {},
      isSilent: () => true,
      onApproval: (a) => approvals.push(a),
      connect: () => socket as unknown as WebSocket,
    })
    socket.onmessage?.({
      data: JSON.stringify({ type: 'approval', data: { id: 'a1', slot: 'chat-1', ts: 1 } }),
    })
    expect(approvals).toEqual([{ slot: 'chat-1', title: '' }])
    stop()
  })

  it('an approval frame with no slot is ignored', () => {
    // Without a slot there is nothing to label or clear later, so it is not raised.
    let raised = 0
    const socket = fakeSocket()
    const stop = watchSessions({
      onDone: () => {},
      isSilent: () => false,
      onApproval: () => { raised += 1 },
      connect: () => socket as unknown as WebSocket,
    })
    socket.onmessage?.({
      data: JSON.stringify({ type: 'approval', data: { id: 'a1', ts: 1 } }),
    })
    expect(raised).toBe(0)
    stop()
  })

  it('the approval lifecycle is complete: pending raises, resolved releases', () => {
    // The two halves must pair up: the producer added here creates the sticky bubble,
    // and the resolver already present frees the slot it was holding.
    const events: string[] = []
    const socket = fakeSocket()
    const stop = watchSessions({
      onDone: () => {},
      isSilent: () => false,
      onApproval: () => events.push('raise'),
      onApprovalResolved: () => events.push('release'),
      connect: () => socket as unknown as WebSocket,
    })
    const send = (type: string, data: unknown) =>
      socket.onmessage?.({ data: JSON.stringify({ type, data }) })

    send('approval', { id: 'a1', slot: 'chat-1', ts: 1 })
    send('approval_resolved', { id: 'a1', approved: true })
    expect(events).toEqual(['raise', 'release'])
    stop()
  })

  it('stopping does not schedule a reconnect', () => {
    const h = harness()
    const spy = vi.spyOn(window, 'setTimeout')
    h.stop()
    h.socket.close()
    expect(spy).not.toHaveBeenCalled()
    spy.mockRestore()
  })
})
