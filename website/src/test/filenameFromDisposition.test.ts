/**
 * `filenameFromDisposition` — the client half of the download filename contract.
 *
 * This exists because the two halves went out of step once: the export endpoint
 * was changed to emit RFC 5987 `filename*=UTF-8''<percent-encoded>` so a non-Latin
 * title keeps its name hint, and the client's parser still required a literal
 * `filename=`. It matched nothing, so every export silently saved under the
 * internal slot key and re-exports of one session collided — with the server's
 * whole slug-and-stamp machinery dead behind a fallback.
 *
 * The pin is therefore on the REAL header the endpoint emits, not on a
 * hand-written approximation of it.
 */
import { describe, it, expect } from 'vitest'

import { filenameFromDisposition } from '../api/client'

const FALLBACK = 'slot-1.kcsession.json.gz'

describe('filenameFromDisposition', () => {
  it('reads the RFC 5987 form the export endpoint emits', () => {
    const header = "attachment; filename*=UTF-8''design-chat-20260909T083244Z.kcsession.json.gz"
    expect(filenameFromDisposition(header, FALLBACK))
      .toBe('design-chat-20260909T083244Z.kcsession.json.gz')
  })

  it('decodes a percent-encoded non-Latin name back to its characters', () => {
    // The whole point of the encoding: a CJK title keeps its name hint.
    const encoded = encodeURIComponent('\u4f1a\u8bdd-S.kcsession.json.gz')
    const got = filenameFromDisposition(`attachment; filename*=UTF-8''${encoded}`, FALLBACK)
    expect(got).toBe('\u4f1a\u8bdd-S.kcsession.json.gz')
  })

  it('still reads the bare filename form for anything that sends it', () => {
    expect(filenameFromDisposition('attachment; filename="report.csv"', FALLBACK))
      .toBe('report.csv')
    expect(filenameFromDisposition('attachment; filename=report.csv', FALLBACK))
      .toBe('report.csv')
  })

  it('prefers the encoded form when a sender supplies both', () => {
    const header =
      "attachment; filename=fallback.gz; filename*=UTF-8''%E4%BC%9A%E8%AF%9D.kcsession.json.gz"
    expect(filenameFromDisposition(header, FALLBACK)).toBe('\u4f1a\u8bdd.kcsession.json.gz')
  })

  it('falls back rather than throwing on a malformed percent sequence', () => {
    // decodeURIComponent raises on bad input; a broken header must not take the
    // download with it.
    expect(filenameFromDisposition("attachment; filename*=UTF-8''%E0%A4%A", FALLBACK))
      .toBe(FALLBACK)
  })

  it('falls back when the header carries no filename at all', () => {
    expect(filenameFromDisposition('attachment', FALLBACK)).toBe(FALLBACK)
    expect(filenameFromDisposition('', FALLBACK)).toBe(FALLBACK)
  })
})
