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

  /**
   * Paste *value* into the standing banner's field with the exchange answering
   * *exchange*, and return the field so a case can read its state after.
   */
  async function pasteIntoBanner(value: string, exchange: Response | Error) {
    const input = bannerEl()!.querySelector('input') as HTMLInputElement
    fetchMock.mockReset()
    if (exchange instanceof Error) fetchMock.mockRejectedValue(exchange)
    else fetchMock.mockResolvedValue(exchange)
    input.value = value
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled())
    return input
  }

  /** `/api/auth/me` answering 200 and naming which credential authenticated. */
  const exchanged = (tokenAccepted: boolean, ownerOk = tokenAccepted) =>
    new Response(
      JSON.stringify({
        user_id: 'same-user',
        token_accepted: tokenAccepted,
        owner_ok: ownerOk,
      }),
      { status: 200, headers: { 'content-type': 'application/json' } },
    )

  it('keeps the prompt up when the exchange authenticated on the cookie, not the pasted token', async () => {
    fetchMock.mockResolvedValueOnce(staleDenial())
    await api.chatMode('trust').catch(() => null)
    expect(bannerEl()).not.toBeNull()

    // 200 with the SAME user, because this session is authenticated -- just
    // owner-denied -- and `/api/auth/me` is not owner-gated. The gateway says
    // the pasted token is not what authenticated it, so nothing was recovered:
    // clearing here would hide the prompt while every owner-gated call kept
    // failing, and the identity is identical so it cannot be the tell.
    const input = await pasteIntoBanner('not-the-owners-token', exchanged(false))

    expect(bannerEl()).not.toBeNull()
    await vi.waitFor(() => {
      expect(bannerEl()!.querySelector('[role="status"]')!.textContent).toContain(
        'sign-in URL was not accepted',
      )
    })
    // Corrigible: the field comes back with the text still in it.
    expect(input.disabled).toBe(false)
    expect(input.value).toBe('not-the-owners-token')
  })

  it('clears the prompt once the gateway confirms the pasted token authenticated', async () => {
    fetchMock.mockResolvedValueOnce(staleDenial())
    await api.chatMode('trust').catch(() => null)
    expect(bannerEl()).not.toBeNull()

    await pasteIntoBanner('the-owners-token', exchanged(true))

    // The one event that resolves an owner denial, now established rather than
    // assumed -- so the banner goes, without the full-page reload that used to
    // take the user's unsaved input with it.
    await vi.waitFor(() => expect(bannerEl()).toBeNull())
  })

  it('keeps the prompt up for a token that is accepted but still owner-denied', async () => {
    fetchMock.mockResolvedValueOnce(staleDenial())
    await api.chatMode('trust').catch(() => null)
    expect(bannerEl()).not.toBeNull()

    // A token minted before the owner was configured is a VALID token, so it
    // authenticates and `token_accepted` is true -- while its subject is still
    // the bootstrap one the owner gate refuses. Re-pasting an old link from
    // one's own history lands exactly here, so acceptance alone must not clear
    // the prompt.
    await pasteIntoBanner('a-valid-pre-owner-token', exchanged(true, false))

    expect(bannerEl()).not.toBeNull()
    await vi.waitFor(() => {
      expect(bannerEl()!.querySelector('[role="status"]')!.textContent).toContain(
        'sign-in URL was not accepted',
      )
    })
  })

  it('says the gateway was unreachable rather than claiming the token was refused', async () => {
    fetchMock.mockResolvedValueOnce(staleDenial())
    await api.chatMode('trust').catch(() => null)
    expect(bannerEl()).not.toBeNull()

    // Nothing judged the token, so "not accepted" would assert a check that
    // never ran and send the user to re-run a command that cannot help.
    const input = await pasteIntoBanner('any-token', new TypeError('Failed to fetch'))

    expect(bannerEl()).not.toBeNull()
    await vi.waitFor(() => {
      const live = bannerEl()!.querySelector('[role="status"]')!.textContent ?? ''
      expect(live).toContain('Could not reach the gateway')
      expect(live).not.toContain('was not accepted')
    })
    expect(input.disabled).toBe(false)
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
