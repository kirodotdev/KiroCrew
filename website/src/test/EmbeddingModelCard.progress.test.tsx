import { describe, it, expect } from 'vitest'
import { reembedPct, reembedBusy, reembedBar, embedModelErrorMessage } from '../pages/overview/EmbeddingModelCard'
import { i18nT } from '../i18n/t'

// The indicator has to distinguish three states that all look like "not done":
// loading the model (no denominator yet), running (n/total), and failed. A bar
// that shows 0% while loading implies work started and stalled.
describe('reembedPct', () => {
  it('is null while the model is still loading', () => {
    expect(reembedPct({ step: 'applying', done: 0, total: 0 })).toBeNull()
  })

  it('is null when running with no known total', () => {
    expect(reembedPct({ step: 'running', done: 0, total: 0 })).toBeNull()
  })

  it('computes a percentage while running', () => {
    expect(reembedPct({ step: 'running', done: 1842, total: 3148 })).toBe(59)
    expect(reembedPct({ step: 'running', done: 1, total: 4 })).toBe(25)
  })

  it('never exceeds 100 even if done overshoots total', () => {
    expect(reembedPct({ step: 'running', done: 9, total: 4 })).toBe(100)
  })

  it('is null for terminal and idle states', () => {
    expect(reembedPct({ step: 'done', done: 10, total: 10 })).toBeNull()
    expect(reembedPct({ step: 'failed', done: 3, total: 10 })).toBeNull()
    expect(reembedPct({ step: 'idle', done: 0, total: 0 })).toBeNull()
  })

  it('is null for missing state', () => {
    expect(reembedPct(undefined)).toBeNull()
  })
})

describe('reembedBusy', () => {
  it('is true while applying or running', () => {
    expect(reembedBusy({ step: 'applying' })).toBe(true)
    expect(reembedBusy({ step: 'running' })).toBe(true)
  })

  it('is false for idle and terminal states, so polling stops', () => {
    expect(reembedBusy({ step: 'idle' })).toBe(false)
    expect(reembedBusy({ step: 'done' })).toBe(false)
    expect(reembedBusy({ step: 'failed' })).toBe(false)
    expect(reembedBusy(undefined)).toBe(false)
  })
})

// The repo contract is that `code` is the wire contract and `error` prose is
// advisory: rendering English prose into a localized UI is untranslatable.
describe('embedModelErrorMessage', () => {
  it('localizes a known code rather than showing the English prose', () => {
    const msg = embedModelErrorMessage({ code: 'model_path_not_found', error: 'raw english prose' })
    expect(msg).not.toBe('raw english prose')
    expect(msg.length).toBeGreaterThan(0)
  })

  // Each code POST /api/memory/embedding-model can answer with, beyond the path
  // codes: from the owner gate, the request-path probe pool, the memory startup
  // check and the vector-store acquisition. Unmapped, each fell back to the
  // route's English `error` prose.
  const refusal = (code: string, error: string) => Object.assign(new Error(error), {
    name: 'ApiError',
    status: 503,
    body: JSON.stringify({ ok: false, error, code }),
  })

  it.each([
    ['owner_only', 'pages.overview.embedModel.err_owner_only'],
    ['path_probe_busy', 'pages.overview.embedModel.err_probe_busy'],
    ['vector_store_unavailable', 'pages.overview.embedModel.err_vector_store_unavailable'],
  ] as const)('localizes %s instead of showing the English prose', (code, key) => {
    const msg = embedModelErrorMessage(refusal(code, 'raw english prose'))
    expect(msg).toBe(i18nT(key))
    expect(msg).not.toBe('raw english prose')
  })

  // `store_unavailable` stands for memory still starting, a failed recovery or
  // a stopped startup. Only the server's sentence names which one and its
  // remedy (often a gateway restart), so the localized frame carries it.
  it('localizes store_unavailable and keeps the server cause and remedy in it', () => {
    const cause = 'Memory recovery failed: disk full Inspect or cancel the pending restore, then restart the gateway.'
    const msg = embedModelErrorMessage(refusal('store_unavailable', cause))
    expect(msg).toBe(i18nT('pages.overview.embedModel.err_memory_unavailable', { error: cause }))
    expect(msg).not.toBe(cause)
    expect(msg).toContain(cause)
  })

  it('falls back to prose for an unknown code, so nothing is swallowed', () => {
    expect(embedModelErrorMessage({ code: 'brand_new_code', error: 'something specific' }))
      .toBe('something specific')
  })

  it('falls back to prose when no code is present', () => {
    expect(embedModelErrorMessage({ error: 'plain failure' })).toBe('plain failure')
  })

  // Regression: the api client rejects with an ApiError carrying the payload as
  // a JSON STRING on .body, not as own properties. Reading err.code directly
  // found nothing and fell through to String(err), rendering the raw English
  // "ApiError: The model path points at a file that does not exist: ...".
  it('localizes from an ApiError, whose payload is a JSON string on .body', () => {
    const apiErr = Object.assign(new Error('The model path points at a file that does not exist'), {
      name: 'ApiError',
      status: 400,
      body: JSON.stringify({ ok: false, error: 'raw english prose', code: 'model_path_not_found' }),
    })
    const msg = embedModelErrorMessage(apiErr)
    expect(msg).not.toContain('ApiError')
    expect(msg).not.toBe('raw english prose')
    expect(msg.length).toBeGreaterThan(0)
  })

  it('falls back to prose when ApiError.body is not JSON', () => {
    const apiErr = Object.assign(new Error('boom'), {
      name: 'ApiError', status: 500, body: '<html>502 Bad Gateway</html>',
    })
    expect(embedModelErrorMessage(apiErr).length).toBeGreaterThan(0)
  })

  it('never returns empty for a non-object error', () => {
    expect(embedModelErrorMessage('boom').length).toBeGreaterThan(0)
  })
})

// Regression: a null percentage used to fall through to width:100%, so
// "Loading the new model..." drew a FULL bar -- visually identical to finished,
// the exact ambiguity the null percentage exists to prevent.
describe('reembedBar', () => {
  it('draws a SHORT indeterminate fill while loading, never a full one', () => {
    const bar = reembedBar({ step: 'applying', done: 0, total: 0 })
    expect(bar.indeterminate).toBe(true)
    expect(bar.widthPct).toBeGreaterThan(0)
    expect(bar.widthPct).toBeLessThan(50)
  })

  it('is indeterminate when running with no denominator yet', () => {
    const bar = reembedBar({ step: 'running', done: 0, total: 0 })
    expect(bar.indeterminate).toBe(true)
    expect(bar.widthPct).toBeLessThan(50)
  })

  it('tracks the real fraction while running', () => {
    expect(reembedBar({ step: 'running', done: 1842, total: 3148 }))
      .toEqual({ widthPct: 59, indeterminate: false })
  })

  it('stops where it stopped on failure rather than filling red', () => {
    expect(reembedBar({ step: 'failed', done: 1842, total: 3148 }))
      .toEqual({ widthPct: 59, indeterminate: false })
  })

  it('fills only when a failure has no denominator at all', () => {
    expect(reembedBar({ step: 'failed', done: 0, total: 0 }))
      .toEqual({ widthPct: 100, indeterminate: false })
  })

  it('fills on completion', () => {
    expect(reembedBar({ step: 'done', done: 10, total: 10 }).widthPct).toBe(100)
  })

  it('clamps an overshoot to 100', () => {
    expect(reembedBar({ step: 'running', done: 99, total: 10 }).widthPct).toBe(100)
  })
})
