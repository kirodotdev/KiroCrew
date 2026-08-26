import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'

import { useFontOptions } from '../hooks/useFontOptions'

/**
 * The hook's two detection layers are stubbed at the module boundary: the probe
 * needs a font-aware canvas the test environment does not have, and the Local
 * Font Access API does not exist in it at all — so the branches are only
 * reachable through the mock.
 */
vi.mock('../utils/fontDetect', () => ({
  detectInstalledFonts: vi.fn(() => ['Menlo', 'Hack']),
  isLocalFontAccessSupported: vi.fn(() => true),
  queryLocalMonospaceFonts: vi.fn(() => Promise.resolve({ ok: true, families: ['Hack', 'Zed Mono'] })),
}))

const detect = vi.mocked(await import('../utils/fontDetect'))

beforeEach(() => {
  detect.detectInstalledFonts.mockReturnValue(['Menlo', 'Hack'])
  detect.isLocalFontAccessSupported.mockReturnValue(true)
  detect.queryLocalMonospaceFonts.mockResolvedValue({ ok: true, families: ['Hack', 'Zed Mono'] })
})

afterEach(() => { vi.clearAllMocks() })

describe('useFontOptions', () => {
  it('probes the candidate list off the render path', async () => {
    const { result } = renderHook(() => useFontOptions())
    await waitFor(() => expect(result.current.families).toEqual(['Menlo', 'Hack']))
    expect(detect.detectInstalledFonts).toHaveBeenCalledTimes(1)
  })

  it('waits for web fonts, so the dashboard\'s own face is not measured as missing', async () => {
    // A face the app loads itself resolves after first paint; probing before that
    // reports it absent and drops it from the list.
    let release = () => {}
    const ready = new Promise<void>(resolve => { release = () => resolve() })
    Object.defineProperty(document, 'fonts', { configurable: true, value: { ready } })
    try {
      const { result } = renderHook(() => useFontOptions())
      expect(detect.detectInstalledFonts).not.toHaveBeenCalled()
      await act(async () => { release(); await ready })
      await waitFor(() => expect(result.current.families).toEqual(['Menlo', 'Hack']))
    } finally {
      Reflect.deleteProperty(document, 'fonts')
    }
  })

  it('reports whether the full-enumeration action is worth offering', () => {
    detect.isLocalFontAccessSupported.mockReturnValue(false)
    const { result } = renderHook(() => useFontOptions())
    expect(result.current.accessSupported).toBe(false)
    expect(result.current.lastResult).not.toBe('denied')
  })

  it('unions the font book in after the probe, probed names first, without duplicating', async () => {
    const { result } = renderHook(() => useFontOptions())
    await waitFor(() => expect(result.current.families).toHaveLength(2))
    await act(async () => { result.current.enumerate() })
    await waitFor(() => expect(result.current.families).toEqual(['Menlo', 'Hack', 'Zed Mono']))
    expect(result.current.lastResult).not.toBe('denied')
  })

  it('keeps the probed list and flags the refusal when access is denied', async () => {
    detect.queryLocalMonospaceFonts.mockResolvedValue({ ok: false, reason: 'denied' })
    const { result } = renderHook(() => useFontOptions())
    await waitFor(() => expect(result.current.families).toHaveLength(2))
    await act(async () => { result.current.enumerate() })
    await waitFor(() => expect(result.current.lastResult).toBe('denied'))
    expect(result.current.families).toEqual(['Menlo', 'Hack'])
  })

  it('clears an earlier refusal once access is granted', async () => {
    detect.queryLocalMonospaceFonts.mockResolvedValue({ ok: false, reason: 'denied' })
    const { result } = renderHook(() => useFontOptions())
    await act(async () => { result.current.enumerate() })
    await waitFor(() => expect(result.current.lastResult).toBe('denied'))
    detect.queryLocalMonospaceFonts.mockResolvedValue({ ok: true, families: ['Zed Mono'] })
    await act(async () => { result.current.enumerate() })
    await waitFor(() => expect(result.current.lastResult).toBe('added'))
  })

  it("reports the granted-but-nothing-new branch, which otherwise looks like a dead click", async () => {
    detect.queryLocalMonospaceFonts.mockResolvedValue({ ok: true, families: ['Menlo', 'Hack'] })
    const { result } = renderHook(() => useFontOptions())
    await waitFor(() => expect(result.current.families).toHaveLength(2))
    await act(async () => { result.current.enumerate() })
    await waitFor(() => expect(result.current.lastResult).toBe('none'))
    expect(result.current.families).toEqual(['Menlo', 'Hack'])
  })

  it('starts idle, so no outcome line is claimed before the action ever ran', () => {
    const { result } = renderHook(() => useFontOptions())
    expect(result.current.lastResult).toBe('idle')
  })
})
