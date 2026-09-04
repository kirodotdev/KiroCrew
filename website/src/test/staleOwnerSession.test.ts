/**
 * Tests for the stale pre-owner session re-auth prompt.
 *
 * A session signed in before `KIROCREW_OWNER_ID` was configured carries a
 * bootstrap token subject forever (refresh re-mints from the incoming subject),
 * so the backend labels its owner-gate denial `401 stale_session_reauth`. On
 * that signal — and ONLY that signal — the client must prompt re-authentication
 * instead of failing silently, and must NOT attempt the silent refresh (a
 * "successful" refresh would rotate the cookie and keep the stale subject).
 *
 * The banner also must survive 2xx responses: unlike a fully expired session,
 * this one still authenticates against everything the owner gate does not
 * front, so background polls keep succeeding and the `j` wrapper's
 * clear-banner-on-2xx self-dismissal must not remove the prompt.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  api,
  ApiError,
  checkSessionExpired,
  isAuthExpiredError,
  STALE_OWNER_SESSION_CODE,
  __resetAuthRecoveryStateForTests,
} from '../api/client'
import { noteStaleOwnerResponse } from '../api/staleOwnerSignal'
import { respondApproval } from '../apps/mochi/panel/panelBridge'

const staleDenial = (): Response =>
  new Response(
    JSON.stringify({
      error: 'this session predates the configured owner; sign in again',
      code: STALE_OWNER_SESSION_CODE,
    }),
    { status: 401, headers: { 'content-type': 'application/json' } },
  )

const bannerEl = (): HTMLElement | null => document.getElementById('mc-session-expired')

describe('stale pre-owner session re-auth prompt', () => {
  let fetchMock: ReturnType<typeof vi.fn>
  let originalFetch: typeof fetch

  beforeEach(() => {
    __resetAuthRecoveryStateForTests()
    fetchMock = vi.fn()
    originalFetch = globalThis.fetch
    globalThis.fetch = fetchMock as unknown as typeof fetch
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
    __resetAuthRecoveryStateForTests()
  })

  it('prompts re-auth on the stale signal, without attempting a silent refresh', async () => {
    fetchMock.mockResolvedValue(staleDenial())

    const err = await api.chatMode('trust').then(
      () => null,
      (e: unknown) => e,
    )

    expect(err).toBeInstanceOf(ApiError)
    const apiErr = err as ApiError
    expect(apiErr.status).toBe(401)
    // Only a re-sign-in recovers, so call sites must drop retry affordances.
    expect(isAuthExpiredError(apiErr)).toBe(true)
    // The display message is the recovery instruction, not the raw label.
    expect(apiErr.message).toContain('kirocrew token')
    expect(apiErr.message.toLowerCase()).toContain('owner')

    // The banner names the stale-owner cause, not plain expiry.
    expect(bannerEl()).not.toBeNull()
    expect(bannerEl()!.textContent).toContain('predates the configured owner')

    // The silent refresh must NOT fire: refresh preserves the stale subject.
    const refreshCalls = fetchMock.mock.calls.filter((c) => c[0] === '/api/auth/refresh')
    expect(refreshCalls.length).toBe(0)
  })

  it('does NOT prompt on a generic 403 (existing handling untouched)', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ error: 'forbidden' }), {
        status: 403,
        headers: { 'content-type': 'application/json' },
      }),
    )

    const err = await api.chatMode('trust').then(
      () => null,
      (e: unknown) => e,
    )

    expect((err as ApiError).status).toBe(403)
    expect(isAuthExpiredError(err)).toBe(false)
    expect(bannerEl()).toBeNull()
  })

  it('does NOT prompt on a 401 without the stale code', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ error: 'authentication required', code: 'auth_required' }), {
        status: 401,
        headers: { 'content-type': 'application/json' },
      }),
    )

    const err = await api.chatMode('trust').then(
      () => null,
      (e: unknown) => e,
    )

    expect((err as ApiError).status).toBe(401)
    expect(isAuthExpiredError(err)).toBe(false)
    expect(bannerEl()).toBeNull()
  })

  it('keeps the banner up across later 2xx responses, until dismissed by hand', async () => {
    fetchMock.mockResolvedValueOnce(staleDenial())
    await api.chatMode('trust').catch(() => null)
    expect(bannerEl()).not.toBeNull()

    // A background poll succeeding must not clear the prompt: this session
    // still authenticates for non-owner-gated routes.
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ slots: [] }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )
    await api.chatSlots().catch(() => null)
    expect(bannerEl()).not.toBeNull()

    // The ✕ dismiss still works, and re-arms detection for the next denial.
    bannerEl()!.querySelector('button')!.click()
    expect(bannerEl()).toBeNull()

    fetchMock.mockResolvedValueOnce(staleDenial())
    await api.chatMode('trust').catch(() => null)
    expect(bannerEl()).not.toBeNull()
  })

  it('upgrades an already-showing plain-expiry banner to stale-owner lifetime rules', async () => {
    // Raise the generic banner first: access-cookie lapse with the silent
    // refresh terminally exhausted (refresh answers 401).
    fetchMock.mockImplementation((url: string) =>
      Promise.resolve(
        url === '/api/auth/refresh'
          ? new Response('{}', { status: 401 })
          : new Response('{}', { status: 200 }),
      ),
    )
    checkSessionExpired(
      new Response(JSON.stringify({ error: 'Token required' }), {
        status: 403,
        headers: { 'content-type': 'application/json', 'X-Auth-Required': 'true' },
      }),
    )
    await new Promise((r) => setTimeout(r, 0))
    expect(bannerEl()).not.toBeNull()

    // The stale denial arrives while that banner is up: the latch must still
    // engage, so the next 2xx may NOT clear the prompt.
    fetchMock.mockResolvedValueOnce(staleDenial())
    await api.chatMode('trust').catch(() => null)
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ slots: [] }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )
    await api.chatSlots().catch(() => null)
    expect(bannerEl()).not.toBeNull()
  })

  it("raises the prompt from Mochi's direct-fetch approval bridge and names the cause", async () => {
    fetchMock.mockResolvedValueOnce(staleDenial())
    const out = await respondApproval('req-1', 'approve')
    expect(out.ok).toBe(false)
    // The flag is what the panel's UI branches on for its localized remedy.
    expect(out.staleOwnerSession).toBe(true)
    expect(out.error).toContain('predates the configured owner')
    expect(bannerEl()).not.toBeNull()
  })

  it('keeps the terse status form on a non-stale approval failure', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ error: 'forbidden' }), {
        status: 403,
        headers: { 'content-type': 'application/json' },
      }),
    )
    const out = await respondApproval('req-2', 'approve')
    expect(out).toEqual({ ok: false, error: 'approval failed (403)' })
    expect(bannerEl()).toBeNull()
  })

  it('noteStaleOwnerResponse matches string and parsed bodies, and only the exact signal', () => {
    const body = JSON.stringify({ code: STALE_OWNER_SESSION_CODE })
    expect(noteStaleOwnerResponse(401, body)).toBe(true)
    expect(noteStaleOwnerResponse(401, { code: STALE_OWNER_SESSION_CODE })).toBe(true)
    expect(noteStaleOwnerResponse(403, body)).toBe(false)
    expect(noteStaleOwnerResponse(401, JSON.stringify({ code: 'auth_required' }))).toBe(false)
    expect(noteStaleOwnerResponse(401, 'not json')).toBe(false)
    expect(noteStaleOwnerResponse(401, null)).toBe(false)
  })
})
