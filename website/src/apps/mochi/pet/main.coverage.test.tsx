/**
 * Mochi pet window entry: theme and i18n seeding before mount, the presence
 * heartbeat (load, 30 s interval, visibility change), and the error boundary's
 * empty fallback with its 10 s retry. `main.tsx` mounts itself at import time,
 * so it is imported once and its effects are checked against recorded calls.
 */
import type { ReactNode } from 'react'
import { describe, it, expect, vi, beforeAll, afterAll, beforeEach, afterEach, type MockInstance } from 'vitest'
import { act, render, screen } from '@testing-library/react'

const H = vi.hoisted(() => ({
  fail: false,
  visibility: 'hidden' as 'hidden' | 'visible',
  mounted: [] as Array<{ el: Element; node: ReactNode }>,
  // Called by the root's render, so the mount has a place in the mocks' shared call order.
  rootRender: vi.fn(),
}))

vi.mock('react-dom/client', () => ({
  createRoot: (el: Element) => ({
    render: (node: ReactNode) => {
      H.rootRender()
      H.mounted.push({ el, node })
    },
  }),
}))

vi.mock('../src/shared/themes', () => ({ applyThemeVarsOnly: vi.fn() }))

vi.mock('../mochiLanguage', async () => {
  const R = await import('react')
  return {
    initMochiI18n: vi.fn(),
    MochiLocalized: ({ children }: { children: ReactNode }) =>
      R.createElement(R.Fragment, null, children),
  }
})

vi.mock('../src/renderer/PetWidget', async () => {
  const R = await import('react')
  return {
    PetWidget: () => {
      if (H.fail) throw new Error('pet crashed')
      return R.createElement('div', { 'data-testid': 'pet' }, 'pet')
    },
  }
})

import { applyThemeVarsOnly } from '../src/shared/themes'
import { initMochiI18n } from '../mochiLanguage'

const fetchMock = vi.fn(() => Promise.resolve(new Response(null)))

const lastBeat = () => {
  const call = fetchMock.mock.calls[fetchMock.mock.calls.length - 1] as unknown as [string, RequestInit]
  return { url: call[0], init: call[1], visible: JSON.parse(String(call[1].body)).visible as boolean }
}

beforeAll(async () => {
  vi.useFakeTimers() // flake-ok: main.tsx arms its interval at import, so the clock is faked once before the import and restored in afterAll
  vi.stubGlobal('fetch', fetchMock)
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => H.visibility })
  const root = document.createElement('div')
  root.id = 'root'
  document.body.appendChild(root)
  await import('./main')
})

afterAll(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  Reflect.deleteProperty(document, 'visibilityState')
  document.getElementById('root')?.remove()
})

describe('mochi pet entry: startup', () => {
  it('applies theme variables and seeds i18n before mounting', () => {
    expect(applyThemeVarsOnly).toHaveBeenCalled()
    expect(initMochiI18n).toHaveBeenCalled()
    const [mountedAt] = H.rootRender.mock.invocationCallOrder
    expect(vi.mocked(applyThemeVarsOnly).mock.invocationCallOrder[0]).toBeLessThan(mountedAt)
    expect(vi.mocked(initMochiI18n).mock.invocationCallOrder[0]).toBeLessThan(mountedAt)
  })

  it('mounts the pet tree into the #root element', () => {
    expect(H.mounted).toHaveLength(1)
    expect(H.mounted[0].el.id).toBe('root')
    render(H.mounted[0].node as Parameters<typeof render>[0])
    expect(screen.getByTestId('pet')).toBeTruthy()
  })
})

describe('mochi pet entry: presence heartbeat', () => {
  it('posts a presence beat on load, reporting a hidden window as not visible', () => {
    const first = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(first[0]).toBe('/api/apps/mochi/presence')
    expect(first[1].method).toBe('POST')
    expect(JSON.parse(String(first[1].body))).toEqual({ visible: false })
  })

  it('beats again every 30 seconds', () => {
    const before = fetchMock.mock.calls.length
    vi.advanceTimersByTime(30_000)
    expect(fetchMock.mock.calls.length).toBe(before + 1)
  })

  it('beats immediately when the window becomes visible', () => {
    H.visibility = 'visible'
    const before = fetchMock.mock.calls.length
    document.dispatchEvent(new Event('visibilitychange'))
    expect(fetchMock.mock.calls.length).toBe(before + 1)
    expect(lastBeat().visible).toBe(true)
    expect(lastBeat().url).toBe('/api/apps/mochi/presence')
  })
})

describe('mochi pet entry: error boundary', () => {
  let consoleSpy: MockInstance<typeof console.error>

  beforeEach(() => {
    consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  afterEach(() => {
    consoleSpy.mockRestore()
  })

  it('renders nothing after a crash, then retries the pet after 10 seconds', () => {
    H.fail = true
    const { container } = render(H.mounted[0].node as Parameters<typeof render>[0])
    expect(container.textContent).toBe('')
    expect(consoleSpy).toHaveBeenCalledWith(
      '[mochi-pet] render crashed; retrying shortly',
      expect.any(Error),
    )

    H.fail = false
    act(() => {
      vi.advanceTimersByTime(10_000)
    })
    expect(screen.getAllByTestId('pet').length).toBeGreaterThan(0)
  })
})
