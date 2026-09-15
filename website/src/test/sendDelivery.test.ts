/**
 * Only an immediate dispatch is a receipt for the optimistic bubble.
 *
 * A `queued` response cannot stand in for one, in two independent ways. The
 * chat handler's busy branch queues only a NON-EMPTY message
 * (`if message: slot.queue_append(...)`) while answering `{ok: true, queued:
 * true}` either way, so a file-only send that races into it is dropped behind a
 * success-shaped body. And when it does queue, it broadcasts `queue_push` -- that
 * card is the server-owned representation of the message, so the bubble becomes a
 * duplicate whose fate diverges from the row's: cancelling the queued message
 * removes the card and leaves the bubble.
 *
 * Either way "confirmed" would be a claim about a message that never ran, which
 * is precisely what the 30s indicator is there to question.
 */
import { describe, it, expect, vi } from 'vitest'
import { confirmedDelivered, readSendReceipt } from '../utils/sendDelivery'

describe('confirmedDelivered', () => {
  it('accepts an immediate dispatch', () => {
    expect(confirmedDelivered({ ok: true })).toBe(true)
  })

  it('REFUSES a queued acceptance (nothing queued for an empty message; cancellable when it is)', () => {
    // The busy branch sets BOTH flags, so a predicate that reads `ok` alone
    // calls this delivered.
    expect(confirmedDelivered({ ok: true, queued: true })).toBe(false)
    expect(confirmedDelivered({ queued: true })).toBe(false)
  })

  it('refuses a rejection', () => {
    expect(confirmedDelivered({ ok: false })).toBe(false)
    expect(confirmedDelivered({})).toBe(false)
  })
})

/**
 * A receipt that could not be READ is not a receipt that said no (#4217).
 *
 * Every send path used to fold an unparsed body into `{}` and then test it for
 * the acceptance flags, so a truncated reply to an ACCEPTED post answered the
 * same as an explicit refusal — the user was told the send failed and handed the
 * payload back to retry, duplicating a turn that had gone out. The status line
 * is what survives a mangled response, so it decides: a non-2xx is a refusal
 * with or without a body, and a 2xx that will not parse is `unknown`.
 */
describe('readSendReceipt', () => {
  const res = (ok: boolean, json: () => Promise<unknown>) => ({ ok, json })

  it('reads an accepted receipt, immediate or queued', async () => {
    expect(await readSendReceipt(res(true, async () => ({ ok: true })))).toEqual({
      body: { ok: true }, outcome: 'accepted',
    })
    expect((await readSendReceipt(res(true, async () => ({ ok: true, queued: true })))).outcome).toBe('accepted')
    expect((await readSendReceipt(res(true, async () => ({ queued: true })))).outcome).toBe('accepted')
  })

  it('surfaces the server-minted mid from an immediate-dispatch receipt', async () => {
    // The mid is what the client stamps on the optimistic bubble to make the
    // just-sent message pinnable this turn; it must survive parsing.
    const receipt = await readSendReceipt(res(true, async () => ({ ok: true, slot: 's1', mid: 'm-abc123' })))
    expect(receipt.outcome).toBe('accepted')
    expect(receipt.body.mid).toBe('m-abc123')
  })

  it('reads an explicit refusal, and keeps the reason the server sent', async () => {
    const receipt = await readSendReceipt(res(false, async () => ({ ok: false, error: 'slot agent mismatch' })))
    expect(receipt.outcome).toBe('refused')
    expect(receipt.body.error).toBe('slot agent mismatch')
  })

  it('refuses a 2xx body that parsed but claims neither flag', async () => {
    // The server answered, and what it said was no. Nothing was sent, so the
    // payload is safe to hand back.
    expect((await readSendReceipt(res(true, async () => ({})))).outcome).toBe('refused')
    expect((await readSendReceipt(res(true, async () => ({ ok: false })))).outcome).toBe('refused')
  })

  it('refuses a NON-2xx with no readable body at all', async () => {
    // An unhandled backend 500 answers in HTML and a proxy 502 in its own error
    // page; neither parses. The status is the whole receipt, and it says no —
    // which is what these paths have always reported for it.
    expect((await readSendReceipt(res(false, () => Promise.reject(new Error('not json'))))).outcome).toBe('refused')
    expect((await readSendReceipt(res(false, async () => '<html>502</html>'))).outcome).toBe('refused')
  })

  it('calls a 2xx with an unreadable body UNKNOWN, never refused', async () => {
    // The defect this exists to prevent: the request WAS accepted and only its
    // answer is truncated, so the message may well have been delivered.
    expect((await readSendReceipt(res(true, () => Promise.reject(new Error('unexpected end of JSON'))))).outcome).toBe('unknown')
  })

  it('leaves a diagnostic trail on the branch that shows the user nothing', async () => {
    // `unknown` is deliberately silent on screen, so the console line is the
    // only thing that makes a receipt-mangling proxy discoverable at all.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      await readSendReceipt(res(true, () => Promise.reject(new Error('unexpected end of JSON'))))
      expect(warn).toHaveBeenCalledTimes(1)
      // ...and NOT on the outcomes the user can already see.
      warn.mockClear()
      await readSendReceipt(res(true, async () => ({ ok: true })))
      await readSendReceipt(res(false, async () => ({ ok: false })))
      expect(warn).not.toHaveBeenCalled()
    } finally {
      warn.mockRestore()
    }
  })

  it('treats a non-object JSON body as unreadable rather than as absent flags', async () => {
    // `null`, an array and a bare string carry no receipt either. Reading the
    // acceptance flags off one would call a 200 a refusal for the same wrong
    // reason a truncated body did.
    for (const value of [null, [1, 2], 'accepted', 7] as unknown[]) {
      const receipt = await readSendReceipt(res(true, async () => value))
      expect(receipt.outcome).toBe('unknown')
      expect(receipt.body).toEqual({})
    }
  })
})

/**
 * A 2xx an INTERMEDIARY wrote is not a receipt the endpoint wrote.
 *
 * An SSO/auth proxy whose session has lapsed answers the send POST itself, with a
 * login page: a 2xx whose body is not JSON. That took `unknown`'s deliberately
 * silent branch, so the composer — cleared at submit — dropped the user's text
 * with no error row and nothing on screen saying so.
 *
 * The distinguishing evidence is PROVENANCE, never "the body would not parse":
 * a truncated reply from the endpoint itself still means the turn may have run,
 * and handing that payload back duplicates it (#4217 / #5672).
 */
describe('readSendReceipt provenance', () => {
  const res = (
    ok: boolean,
    json: () => Promise<unknown>,
    extra: { redirected?: boolean; contentType?: string } = {},
  ) => ({
    ok,
    json,
    redirected: extra.redirected,
    headers: { get: (name: string) => (name.toLowerCase() === 'content-type' ? extra.contentType ?? null : null) },
  })
  const notJson = () => Promise.reject(new Error('unexpected token < in JSON'))

  it('refuses a 2xx the browser was REDIRECTED to, so the payload is handed back', async () => {
    // `POST /api/chat` never redirects, so a redirected answer came from wherever
    // the chain ended — a login form, not the gateway.
    expect((await readSendReceipt(res(true, notJson, { redirected: true }))).outcome).toBe('refused')
  })

  it('refuses a 2xx that answers in HTML, the proxy shape with no redirect', async () => {
    // Some proxies serve the login page directly on the original URL.
    expect((await readSendReceipt(res(true, notJson, { contentType: 'text/html; charset=utf-8' }))).outcome).toBe('refused')
    // Case and leading whitespace are the intermediary's choice, not a signal.
    expect((await readSendReceipt(res(true, notJson, { contentType: ' TEXT/HTML' }))).outcome).toBe('refused')
  })

  it('KEEPS unknown for an unreadable 2xx from the endpoint itself (#5672 must not regress)', async () => {
    // The truncated-JSON case: not redirected, and the content type is the
    // endpoint's own. Delivery is plausible, so the payload must NOT come back.
    expect((await readSendReceipt(res(true, notJson, { contentType: 'application/json' }))).outcome).toBe('unknown')
    // No provenance information at all is also not evidence of interception.
    expect((await readSendReceipt(res(true, notJson))).outcome).toBe('unknown')
  })

  it('leaves a readable receipt alone even when redirected', async () => {
    // The check guards only the unreadable branch: a body that parsed is the
    // endpoint's verdict and outranks any guess about who served it.
    expect((await readSendReceipt(res(true, async () => ({ ok: true }), { redirected: true }))).outcome).toBe('accepted')
  })

  it('does not warn on the intercepted branch, which the user can now see', async () => {
    // The console line exists for the branch that shows nothing. This one
    // reports an error row and refills the composer, so it earns no warning.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      await readSendReceipt(res(true, notJson, { redirected: true }))
      expect(warn).not.toHaveBeenCalled()
    } finally {
      warn.mockRestore()
    }
  })
})
