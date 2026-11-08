import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, __resetAuthRecoveryStateForTests } from '../api/client'
import { sideTurnWire } from '../pages/chat/sideTurnWire'
import { __resetErrorJournalForTests } from '../utils/errorReport'
import { okJson } from './ApiClient.coverageTestUtils'

const SLOT = 'side-close-fence-slot'

function deferredResponse() {
  let resolve: (value: Response) => void = () => {}
  let reject: (reason: Error) => void = () => {}
  const promise = new Promise<Response>((accept, decline) => {
    resolve = accept
    reject = decline
  })
  return { promise, resolve, reject }
}

describe('sideTurnWire close fence', () => {
  const fetchMock = vi.fn()

  beforeEach(() => {
    fetchMock.mockReset()
    vi.stubGlobal('fetch', fetchMock)
    __resetAuthRecoveryStateForTests()
    __resetErrorJournalForTests()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    __resetAuthRecoveryStateForTests()
  })

  it('waits for the same slot close before opening and sending', async () => {
    const closeResponse = deferredResponse()
    fetchMock
      .mockImplementationOnce(() => closeResponse.promise)
      .mockResolvedValue(okJson())

    const close = api.sideClose(SLOT)
    const send = sideTurnWire(SLOT)({ message: 'question' }, new AbortController().signal)

    expect(fetchMock).toHaveBeenCalledTimes(1)
    closeResponse.resolve(okJson({ ok: true, was_open: true }))
    await close
    await send

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      `/api/chat/slots/${SLOT}/side/close`,
      `/api/chat/slots/${SLOT}/side/open`,
      `/api/chat/slots/${SLOT}/side/turn`,
    ])
  })

  it('sends after the same slot close rejects', async () => {
    const closeResponse = deferredResponse()
    fetchMock
      .mockImplementationOnce(() => closeResponse.promise)
      .mockResolvedValue(okJson())

    const close = api.sideClose(SLOT)
    const send = sideTurnWire(SLOT)({ message: 'question' }, new AbortController().signal)

    expect(fetchMock).toHaveBeenCalledTimes(1)
    closeResponse.reject(new Error('close failed'))
    await expect(close).rejects.toThrow('close failed')
    await send

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      `/api/chat/slots/${SLOT}/side/close`,
      `/api/chat/slots/${SLOT}/side/open`,
      `/api/chat/slots/${SLOT}/side/turn`,
    ])
  })

  it('keeps later sends fenced until the same-slot close settles', async () => {
    const closeResponse = deferredResponse()
    vi.useFakeTimers()
    try {
      fetchMock
        .mockImplementationOnce(() => closeResponse.promise)
        .mockResolvedValue(okJson())

      const close = api.sideClose(SLOT)
      const send = sideTurnWire(SLOT)({ message: 'question' }, new AbortController().signal)
      await vi.advanceTimersByTimeAsync(3_001)
      expect(fetchMock).toHaveBeenCalledTimes(1)

      closeResponse.resolve(okJson({ ok: true, was_open: true }))
      await close
      await send

      expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
        `/api/chat/slots/${SLOT}/side/close`,
        `/api/chat/slots/${SLOT}/side/open`,
        `/api/chat/slots/${SLOT}/side/turn`,
      ])
    } finally {
      vi.useRealTimers()
    }
  })
})
