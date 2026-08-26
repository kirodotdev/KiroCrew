import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import { acquireMicStream, setPreferredMicId } from '../hooks/mic'

/** Install a getUserMedia mock; returns the spy for constraint assertions. */
function mockGetUserMedia(impl: (c: MediaStreamConstraints) => Promise<unknown>) {
  const gum = vi.fn(impl)
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia: gum },
  })
  return gum
}

function namedError(name: string): Error {
  const e = new Error(name)
  e.name = name
  return e
}

const STREAM = { fake: 'stream' } as unknown as MediaStream

describe('acquireMicStream', () => {
  beforeEach(() => localStorage.clear())
  afterEach(() => vi.restoreAllMocks())

  it('requests the saved device with `exact`, not `ideal`', async () => {
    // `ideal` is the bug this function replaces: the browser may silently hand
    // back the previous device, so "switch mic" moved the dropdown but not the
    // audio. `exact` makes the choice either real or loudly impossible.
    setPreferredMicId('airpods')
    const gum = mockGetUserMedia(() => Promise.resolve(STREAM))
    await acquireMicStream()
    expect(gum).toHaveBeenCalledWith({ audio: { deviceId: { exact: 'airpods' } } })
  })

  it('asks for the plain default when no device is saved', async () => {
    const gum = mockGetUserMedia(() => Promise.resolve(STREAM))
    await acquireMicStream()
    expect(gum).toHaveBeenCalledWith({ audio: true })
  })

  it.each(['OverconstrainedError', 'NotFoundError', 'NotReadableError', 'AbortError'])(
    'session start falls back to the default when the saved device fails with %s',
    async name => {
      // Unplugged (Overconstrained/NotFound), held by another app
      // (NotReadable), or a driver/hardware failure while opening (Abort):
      // voice input must still start — the picker now renders the LIVE device,
      // so the fallback is visible instead of silent.
      setPreferredMicId('gone')
      const gum = mockGetUserMedia(c =>
        JSON.stringify(c).includes('exact') ? Promise.reject(namedError(name)) : Promise.resolve(STREAM),
      )
      await expect(acquireMicStream()).resolves.toBe(STREAM)
      expect(gum).toHaveBeenLastCalledWith({ audio: true })
    },
  )

  it('falls back for exactly the errors humanizeMicError calls device-unavailable', async () => {
    // The two classifications drifted once: AbortError was grouped with
    // NotReadableError in humanizeMicError ("another app may be using it") but
    // missing from the fallback set, so a device that merely failed to OPEN made
    // voice input unstartable until the user cleared the preference. Pin them
    // together — a name added to one must be added to the other.
    const deviceUnavailable = ['OverconstrainedError', 'NotFoundError', 'NotReadableError', 'AbortError']
    const permissionDenied = ['NotAllowedError', 'SecurityError']

    for (const name of deviceUnavailable) {
      setPreferredMicId('gone')
      mockGetUserMedia(c =>
        JSON.stringify(c).includes('exact') ? Promise.reject(namedError(name)) : Promise.resolve(STREAM),
      )
      await expect(acquireMicStream(), `${name} must fall back`).resolves.toBe(STREAM)
    }
    for (const name of permissionDenied) {
      setPreferredMicId('gone')
      mockGetUserMedia(() => Promise.reject(namedError(name)))
      await expect(acquireMicStream(), `${name} must propagate`).rejects.toMatchObject({ name })
    }
  })

  it('propagates a permission denial without retrying', async () => {
    // A denial must never be laundered into a different constraint: retrying
    // would double the OS prompt surface and hide the real problem.
    setPreferredMicId('airpods')
    const gum = mockGetUserMedia(() => Promise.reject(namedError('NotAllowedError')))
    await expect(acquireMicStream()).rejects.toMatchObject({ name: 'NotAllowedError' })
    expect(gum).toHaveBeenCalledTimes(1)
  })

  it('an EXPLICIT pick fails loudly instead of falling back', async () => {
    // This is a live device switch: pretending it worked by capturing from
    // something else is exactly the lie the user reported. The caller keeps
    // the old stream and surfaces the error.
    const gum = mockGetUserMedia(() => Promise.reject(namedError('NotReadableError')))
    await expect(acquireMicStream('busy-device')).rejects.toMatchObject({ name: 'NotReadableError' })
    expect(gum).toHaveBeenCalledTimes(1)
    expect(gum).toHaveBeenCalledWith({ audio: { deviceId: { exact: 'busy-device' } } })
  })

  it("an explicit '' means the system default", async () => {
    setPreferredMicId('airpods') // must be ignored: the explicit pick wins
    const gum = mockGetUserMedia(() => Promise.resolve(STREAM))
    await acquireMicStream('')
    expect(gum).toHaveBeenCalledWith({ audio: true })
  })
})
