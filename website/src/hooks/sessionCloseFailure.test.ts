import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { alertSessionCloseFailed } from '../utils/sessionCloseFailure'

const said = (spy: ReturnType<typeof vi.spyOn>) => String(spy.mock.calls[0][0])
const err = (status: number) => Object.assign(new Error(`HTTP ${status}`), { status })

/** Both user close gestures must report a terminal failure, or the restored row
 *  reads as the flicker `closingSlots` removes — and the message has to be true of
 *  the branch it fires in, which is why it takes the rejection. */
describe('session close failure notice', () => {
  let alertSpy: ReturnType<typeof vi.spyOn>
  beforeEach(() => { alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {}) })
  afterEach(() => { vi.restoreAllMocks() })

  it('resolves copy rather than leaking a catalog key', () => {
    alertSessionCloseFailed(err(503))
    expect(alertSpy).toHaveBeenCalledTimes(1)
    expect(said(alertSpy)).not.toContain('useSessionActions')
    expect(said(alertSpy).length).toBeGreaterThan(10)
  })

  /** A definitive refusal is the server's considered answer, so the session is
   *  provably still there, naming it is safe — and the close aborts roll every
   *  partial step back, so the copy must name the action that is now safe. */
  it('names a refused close, with the safe next step', () => {
    alertSessionCloseFailed(err(403))
    const msg = said(alertSpy)
    expect(msg).toMatch(/refused/i)
    expect(msg).toMatch(/close it again/i)
    expect(msg).not.toMatch(/avoid closing/i)
  })

  /** THE UX FINDING — `SlotCloseError` hardcodes status 500 for EVERY close
   *  failure, so keying on the status made the refused branch unreachable and
   *  reported the hedged copy for failures the gateway had definitively refused
   *  and rolled back. The gateway's own `definitive` flag now discriminates — a
   *  wire contract, not a code list mirrored in the client. */
  it.each([
    ['forwarded on the rejection payload', { status: 500, message: 'failed', definitive: true }],
    ['unparsed on a raw ApiError body', { status: 500, body: '{"error":"failed to save history","code":"history_save_failed","definitive":true}' }],
  ])('reports a REFUSED close when the server says definitive, %s', (_label, e) => {
    alertSessionCloseFailed(e)
    const msg = said(alertSpy)
    expect(msg).toMatch(/refused/i)
    expect(msg).toMatch(/close it again/i)
    expect(msg).not.toMatch(/couldn't confirm/i)
  })

  /** A 5xx carrying NO determinism flag never reached the close path, so its outcome
   *  is genuinely unknown. An abort CODE alone no longer implies a refusal. */
  it.each([
    ['a code but no flag', { status: 500, code: 'history_save_failed' }],
    ['an unrecognised code', { status: 500, code: 'something_else' }],
    ['a bare 500', { status: 500, message: 'boom' }],
    ['an unparseable body', { status: 500, body: '<html>502</html>' }],
    ['definitive false', { status: 500, definitive: false }],
  ])('still reports an unknown outcome for %s', (_label, e) => {
    alertSessionCloseFailed(e)
    expect(said(alertSpy)).toMatch(/couldn't confirm/i)
  })

  /** Every other failure leaves the outcome UNKNOWN — the DELETE may have completed
   *  and slot keys are reusable, so the copy must neither claim the session survived
   *  nor invite a second close at whatever now holds the key. */
  it.each([
    ['no status', new Error('network down')],
    ['a timeout', err(408)],
    ['a rate limit', err(429)],
    ['a 5xx', err(503)],
  ])('reports an unknown outcome for %s', (_label, e) => {
    alertSessionCloseFailed(e)
    const msg = said(alertSpy)
    expect(msg).toMatch(/couldn't confirm/i)
    expect(msg).toMatch(/avoid closing it again/i)
    expect(msg).not.toMatch(/still open/i)
    expect(msg).not.toMatch(/try again in a moment/i)
    expect(msg).not.toMatch(/unreachable|couldn't reach/i)
  })

  /** The two outcomes must be distinguishable, or the parameter buys nothing. */
  it('says something different for refused than for unknown', () => {
    alertSessionCloseFailed(err(403))
    const refused = said(alertSpy)
    alertSpy.mockClear()
    alertSessionCloseFailed(err(503))
    expect(said(alertSpy)).not.toBe(refused)
  })

})
