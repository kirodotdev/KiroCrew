import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { sendCardReply } from './sendCardReply'

const reply = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

afterEach(() => vi.restoreAllMocks())

describe('sendCardReply', () => {
  it('posts the text to the tab\'s own slot with the card origin and a send id', async () => {
    const send = vi.spyOn(api, 'sendChat').mockResolvedValue(reply(200, { ok: true }))
    expect(await sendCardReply('member-ada', 'Approve it')).toBe('sent')
    const args = send.mock.calls[0]
    expect(args[0]).toBe('Approve it')
    expect(args[1]).toBe('member-ada')
    expect((args[4] as { sendId: string }).sendId).toMatch(/^[A-Za-z0-9_-]{1,128}$/)
    expect(args[5]).toBeUndefined() // never a steer
    expect(args[7]).toBe('dashboard_card')
  })

  it('reports the switch the person turned off as off, so the caller fills the composer without an error', async () => {
    vi.spyOn(api, 'sendChat').mockResolvedValue(reply(403, { error: 'no', code: 'card_send_off' }))
    expect(await sendCardReply('member-ada', 'x')).toBe('off')
  })

  it('reports the gateway\'s other "not now" as refused, so the caller fills the composer and says why', async () => {
    for (const [status, code] of [[409, 'card_send_slot_busy'], [409, 'card_send_pending_decision'], [429, 'card_send_rate_limited'], [400, 'card_text_invalid']] as const) {
      vi.spyOn(api, 'sendChat').mockResolvedValue(reply(status, { error: 'no', code }))
      expect(await sendCardReply('member-ada', 'x')).toBe('refused')
    }
  })

  it('reports any other error as failed', async () => {
    for (const [status, body] of [[500, { error: 'boom' }], [401, { error: 'expired', code: 'session_expired' }], [404, {}]] as const) {
      vi.spyOn(api, 'sendChat').mockResolvedValue(reply(status, body))
      expect(await sendCardReply('member-ada', 'x')).toBe('failed')
    }
  })

  it('reports a dropped connection as sent, because the POST may have landed and a resend would run the turn twice', async () => {
    vi.spyOn(api, 'sendChat').mockRejectedValue(new TypeError('Failed to fetch'))
    expect(await sendCardReply('member-ada', 'x')).toBe('sent')
  })

  it('does not report a late answer as a failure, since it may have been delivered', async () => {
    vi.spyOn(api, 'sendChat').mockRejectedValue(new DOMException('aborted', 'AbortError'))
    expect(await sendCardReply('member-ada', 'x')).toBe('sent')
  })

  it('sends the origin on the wire body and nowhere a typed send does', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(reply(200, { ok: true }))
    await api.sendChat('hi', 'member-ada', undefined, undefined, undefined, undefined, undefined, 'dashboard_card')
    await api.sendChat('hi', 'member-ada')
    const bodies = fetchSpy.mock.calls.map((c) => JSON.parse(String((c[1] as RequestInit).body)))
    expect(bodies[0].origin).toBe('dashboard_card')
    expect('origin' in bodies[1]).toBe(false)
  })
})
