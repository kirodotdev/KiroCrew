import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Mock clipboard before importing the module that uses it. It resolves TRUE by
// default because that is the real contract (`Promise<boolean>`); the earlier
// `undefined` double was falsy, so it could not have distinguished a successful
// clipboard write from a failed one.
vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(true) }))

import { api } from '../api/client'
import { copyToClipboard } from '../utils/clipboard'
import { revealOrOpen } from '../components/FilePathMenu'
import { i18nT } from '../i18n/t'

describe('api.revealPath', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>
  let alertSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    vi.clearAllMocks()
    alertSpy = vi.spyOn(globalThis, 'alert').mockImplementation(() => {})
  })

  afterEach(() => {
    fetchSpy.mockRestore()
    alertSpy.mockRestore()
  })

  it('shows no confirmation on a normal (non-copy) success response', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    const result = await api.revealPath('/some/path')
    expect(result).toEqual({ ok: true })
    expect(copyToClipboard).not.toHaveBeenCalled()
    expect(alertSpy).not.toHaveBeenCalled()
  })

  it('is side-effect-free on a copy-fallback response: no clipboard write, no dialog', async () => {
    // The transport call MUST NOT touch the clipboard or pop a dialog — that is
    // the single caller `revealOrOpen`'s job now (see the block below). It only
    // returns the wire shape so the caller can act on `copy`.
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ok: true, copy: '/remote/path/file.txt' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    const result = await api.revealPath('/remote/path/file.txt')
    expect(result).toEqual({ ok: true, copy: '/remote/path/file.txt' })
    expect(copyToClipboard).not.toHaveBeenCalled()
    expect(alertSpy).not.toHaveBeenCalled()
  })

  it('sends the action parameter to the backend', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await api.revealPath('/some/file.txt', 'open')
    const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(JSON.parse(init.body as string)).toEqual({ path: '/some/file.txt', action: 'open' })
  })

  it('defaults action to reveal', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await api.revealPath('/some/file.txt')
    const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(JSON.parse(init.body as string)).toEqual({ path: '/some/file.txt', action: 'reveal' })
  })
})

// revealOrOpen is the shared failure funnel every file-location surface routes
// through. Its job on failure is to name the RIGHT cause: a sensitive-path 403
// is a deliberate policy block, not a malfunction, so it must not read as the
// generic "couldn't open" wording that invites a retry. The branch keys off the
// ApiError status, never the server's prose (which stays out of the UI).
describe('revealOrOpen failure wording', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>
  let alertSpy: ReturnType<typeof vi.spyOn>
  let onError: ReturnType<typeof vi.fn>

  beforeEach(() => {
    vi.clearAllMocks()
    // No blocking dialog exists on this path any more: every failure is handed
    // to the caller's `onError`, which renders it through the shared ErrorNotice.
    alertSpy = vi.spyOn(globalThis, 'alert').mockImplementation(() => {})
    onError = vi.fn()
  })

  afterEach(() => {
    fetchSpy.mockRestore()
    alertSpy.mockRestore()
  })

  it('reports the blocked-by-policy string on a sensitive-path 403 denial', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ error: 'access denied' }), {
        status: 403,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await revealOrOpen('/etc/shadow', 'reveal', { onError })

    expect(alertSpy).not.toHaveBeenCalled()
    expect(onError).toHaveBeenCalledTimes(1)
    // Resolve through i18nT so the assertion holds whether the test i18n setup
    // returns the English value or the raw key. The point is it is the BLOCKED
    // string, distinct from the generic failure string below.
    expect(onError).toHaveBeenCalledWith(i18nT('components.filePathMenu.reveal_blocked'))
    // The raw server prose never reaches the UI.
    expect(onError).not.toHaveBeenCalledWith(expect.stringContaining('access denied'))
  })

  it('reports the generic failure string on a non-403 malfunction', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ error: 'boom' }), {
        status: 500,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await revealOrOpen('/home/user/file.txt', 'reveal', { onError })

    expect(alertSpy).not.toHaveBeenCalled()
    expect(onError).toHaveBeenCalledTimes(1)
    expect(onError).toHaveBeenCalledWith(i18nT('components.filePathMenu.reveal_failed'))
  })

  it('keeps the generic wording for an auth-expiry 403 (has its own re-auth recovery)', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ error: 'invalid signature' }), {
        status: 403,
        headers: { 'Content-Type': 'application/json', 'X-Auth-Required': 'true' },
      }),
    )

    await revealOrOpen('/home/user/file.txt', 'reveal', { onError })

    expect(onError).toHaveBeenCalledTimes(1)
    expect(onError).toHaveBeenCalledWith(i18nT('components.filePathMenu.reveal_failed'))
  })

  // The copy-degrade is the host saying it could NOT open the path: no
  // launcher, or one that did not start. Every caller named an open or a
  // reveal, so a copy in its place is written AND said, never passed off as the
  // action ("Show in file manager" that silently copied is the bug this pins).
  // Still no blocking dialog: the caller renders the message in place.
  it('writes the copy and says so when the backend degrades to a clipboard copy', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ok: true, copy: '/remote/path/file.txt' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await revealOrOpen('/remote/path/file.txt', 'reveal', { onError })

    expect(copyToClipboard).toHaveBeenCalledWith('/remote/path/file.txt')
    expect(copyToClipboard).toHaveBeenCalledTimes(1)
    expect(onError).toHaveBeenCalledTimes(1)
    expect(onError).toHaveBeenCalledWith(i18nT('components.filePathMenu.reveal_copied_instead'))
    expect(alertSpy).not.toHaveBeenCalled()
  })

  // `copyToClipboard` reports a failed write by RETURNING false — only a genuine
  // exception from its fallback rejects (utils/clipboard.ts). Saying "copied
  // instead" regardless would have the user paste whatever the clipboard held
  // before, believing it was the path, so the plain failure is reported.
  it('reports the plain failure when the degrade copy returns false', async () => {
    ;(copyToClipboard as ReturnType<typeof vi.fn>).mockResolvedValueOnce(false)
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ok: true, copy: '/remote/path/file.txt' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await revealOrOpen('/remote/path/file.txt', 'reveal', { onError })

    expect(copyToClipboard).toHaveBeenCalledWith('/remote/path/file.txt')
    expect(onError).toHaveBeenCalledTimes(1)
    expect(onError).toHaveBeenCalledWith(i18nT('components.filePathMenu.reveal_failed'))
  })

  // The ordinary case: the backend drove a real file manager, so there was
  // nothing to copy, nothing to say, and nothing went wrong.
  it('says nothing when the host opened the path', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await revealOrOpen('/home/user/file.txt', 'reveal', { onError })

    expect(copyToClipboard).not.toHaveBeenCalled()
    expect(onError).not.toHaveBeenCalled()
  })
})
