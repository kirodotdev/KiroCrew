/**
 * The api client unwraps error envelopes into a human message, which discards
 * every other field in the body. Any structured signal the UI has to act on --
 * such as the auto-merge endpoint's `confirmationRequired` marker, which is what
 * escalates the panel to an explicit immediate-merge confirmation -- would be
 * silently lost. These tests lock the raw body onto the thrown error so the
 * friendly message and the machine-readable fields can both survive.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { api, ApiError } from '../api/client'

describe('ApiError preserves the raw error body', () => {
  let originalFetch: typeof fetch

  beforeEach(() => {
    originalFetch = globalThis.fetch
  })
  afterEach(() => {
    globalThis.fetch = originalFetch
  })

  it('keeps structured fields the friendly message drops', async () => {
    const body = JSON.stringify({
      error: 'No pipeline is pending, so GitLab would merge this merge request immediately.',
      confirmationRequired: true,
    })
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(body, { status: 400, headers: { 'content-type': 'application/json' } }),
    ) as unknown as typeof fetch

    const failure = await api
      .enablePullRequestAutoMerge('https://gitlab.com/acme/widgets/-/merge_requests/12')
      .then(() => null, (error: unknown) => error)

    expect(failure).toBeInstanceOf(ApiError)
    const error = failure as ApiError
    // The prose stays unwrapped for direct display...
    expect(error.message).toBe(
      'No pipeline is pending, so GitLab would merge this merge request immediately.',
    )
    // ...and the envelope stays available for the fields prose cannot carry.
    expect(JSON.parse(error.body)).toMatchObject({ confirmationRequired: true })
  })

  it('carries a non-JSON body through unchanged', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response('gh: not logged into github.com', { status: 503 }),
    ) as unknown as typeof fetch

    const failure = await api
      .markPullRequestReady('https://github.com/acme/widgets/pull/12')
      .then(() => null, (error: unknown) => error)

    expect((failure as ApiError).body).toBe('gh: not logged into github.com')
  })
})
