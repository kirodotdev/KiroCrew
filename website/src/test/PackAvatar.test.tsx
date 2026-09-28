/**
 * The pack tier's renderer: which player draws which format, which slot a state
 * resolves to, that a pack is read once however many avatars wear it, that a
 * broken pack reports rather than rendering blank, and that an avatar the user
 * cannot see holds still.
 *
 * The players themselves are stubbed. `LottieRenderer` loads through lottie-web
 * and `SpriteRenderer` decodes through `new Image()` + a canvas, neither of which
 * happens in this environment — what is under test here is the DISPATCH, so the
 * stubs report the props they were handed.
 */
import { act, cleanup, render as rtlRender, renderHook, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import PackAvatar from '../components/appearancePacks/PackAvatar'
import { useInvalidatePackDetail } from '../hooks/usePackDetail'

/** One client per test, so a cached pack never leaks between cases. The retry
 *  delay is shortened so the retry cases run in real time. */
let qc: QueryClient
function render(ui: React.ReactElement) {
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

/** Forget one pack the way the Library tab does — through the hook, so a test
 *  cannot pass against a key the tab no longer targets. */
async function invalidate(id: string) {
  const { result } = renderHook(() => useInvalidatePackDetail(), {
    wrapper: ({ children }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>,
  })
  await act(async () => {
    result.current(id)
  })
}

vi.mock('../components/appearancePacks/LottieRenderer', () => ({
  LottieRenderer: ({ animationData, autoplay, loop, onError }: {
    animationData: string; autoplay?: boolean; loop?: boolean; onError?: () => void
  }) => (
    <button
      type="button"
      aria-label="lottie stub"
      data-testid="lottie-stub"
      data-bytes={String(animationData.length)}
      data-autoplay={String(autoplay)}
      data-loop={String(loop)}
      onClick={() => onError?.()}
    />
  ),
}))

vi.mock('../components/appearancePacks/SpriteRenderer', () => ({
  SpriteRenderer: ({ src, row, playing, fps, frameWidth, onError }: {
    src: string; row?: number; playing?: boolean; fps?: number; frameWidth?: number; onError?: () => void
  }) => (
    <button
      type="button"
      aria-label="sprite stub"
      data-testid="sprite-stub"
      data-src={src}
      data-row={String(row)}
      data-playing={String(playing)}
      data-fps={String(fps)}
      data-frame-width={String(frameWidth)}
      onClick={() => onError?.()}
    />
  ),
}))

const detail = vi.fn()
vi.mock('../api/client', () => ({
  api: { appearances: { detail: (id: string) => detail(id) } },
}))

/** An IntersectionObserver whose callback this test fires by hand, so the
 *  off-screen path is reachable (the suite's default stub always intersects). */
class ManualObserver {
  static instances: ManualObserver[] = []
  private readonly cb: IntersectionObserverCallback
  constructor(cb: IntersectionObserverCallback) {
    this.cb = cb
    ManualObserver.instances.push(this)
  }
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords(): IntersectionObserverEntry[] { return [] }
  fire(isIntersecting: boolean) {
    this.cb(
      [{ isIntersecting } as unknown as IntersectionObserverEntry],
      this as unknown as IntersectionObserver,
    )
  }
}

const originalObserver = globalThis.IntersectionObserver

function useManualObserver() {
  ManualObserver.instances = []
  ;(globalThis as unknown as { IntersectionObserver: unknown }).IntersectionObserver = ManualObserver
  ;(window as unknown as { IntersectionObserver: unknown }).IntersectionObserver = ManualObserver
}

beforeEach(() => {
  qc = new QueryClient({
    defaultOptions: { queries: { retry: 1, retryDelay: 20, staleTime: Infinity } },
  })
  detail.mockReset()
})

afterEach(() => {
  cleanup()
  ;(globalThis as unknown as { IntersectionObserver: unknown }).IntersectionObserver = originalObserver
  ;(window as unknown as { IntersectionObserver: unknown }).IntersectionObserver = originalObserver
})

describe('PackAvatar format dispatch', () => {
  it('draws an svg slot as an <img> on the per-slot route', async () => {
    detail.mockResolvedValue({ animations: { idle: { content: '<svg/>', format: 'svg' } } })
    render(<PackAvatar id="aurora" state="idle" size={40} />)

    const box = await screen.findByTestId('pack-avatar-svg')
    const img = box.querySelector('img')
    expect(img?.getAttribute('src')).toBe('/api/appearances/aurora/slot/idle')
    // Contain, not cover: a pack's art is drawn to its own frame.
    expect(img?.style.objectFit).toBe('contain')
  })

  it('draws a lottie slot through LottieRenderer, looping', async () => {
    detail.mockResolvedValue({
      animations: { idle: { content: '{"v":"5.7.4"}', format: 'lottie' } },
    })
    render(<PackAvatar id="aurora" state="idle" size={40} />)

    await screen.findByTestId('pack-avatar-lottie')
    const stub = screen.getByTestId('lottie-stub')
    expect(stub.getAttribute('data-bytes')).toBe('13')
    expect(stub.getAttribute('data-loop')).toBe('true')
  })

  it('draws a sprite slot through SpriteRenderer on the row its slot is assigned', async () => {
    detail.mockResolvedValue({
      animations: {
        idle: { content: 'base64', format: 'sprite' },
        working: { content: 'base64', format: 'sprite' },
      },
      sprite: { frameWidth: 16, frameHeight: 16, fps: 4, rowAssignments: { idle: 0, working: 1 } },
    })
    render(<PackAvatar id="aurora" state="working" size={40} />)

    const box = await screen.findByTestId('pack-avatar-sprite')
    expect(box.getAttribute('data-pack-slot')).toBe('working')
    const stub = screen.getByTestId('sprite-stub')
    expect(stub.getAttribute('data-row')).toBe('1')
    expect(stub.getAttribute('data-fps')).toBe('4')
    expect(stub.getAttribute('data-frame-width')).toBe('16')
    // The sheet comes from the slot route, which base64-DECODES it; a canvas
    // cannot draw the inlined base64 text.
    expect(stub.getAttribute('data-src')).toBe('/api/appearances/aurora/slot/working')
  })

  it('falls back to row 0 when the sheet assigns this slot no row', async () => {
    detail.mockResolvedValue({
      animations: { idle: { content: 'base64', format: 'sprite' } },
      sprite: { frameWidth: 16, frameHeight: 16, fps: 8 },
    })
    render(<PackAvatar id="aurora" state="idle" size={40} />)

    await screen.findByTestId('pack-avatar-sprite')
    expect(screen.getByTestId('sprite-stub').getAttribute('data-row')).toBe('0')
  })
})

describe('PackAvatar slot fallback', () => {
  it('resolves working through the pack own chain when it draws only idle', async () => {
    detail.mockResolvedValue({ animations: { idle: { content: '<svg/>', format: 'svg' } } })
    render(<PackAvatar id="aurora" state="working" size={40} />)

    const box = await screen.findByTestId('pack-avatar-svg')
    expect(box.getAttribute('data-pack-slot')).toBe('idle')
    expect(box.querySelector('img')?.getAttribute('src')).toBe('/api/appearances/aurora/slot/idle')
  })

  it('prefers loading over idle for working, matching the server chain', async () => {
    detail.mockResolvedValue({
      animations: {
        idle: { content: '<svg/>', format: 'svg' },
        loading: { content: '{"v":1}', format: 'lottie' },
      },
    })
    render(<PackAvatar id="aurora" state="working" size={40} />)

    const box = await screen.findByTestId('pack-avatar-lottie')
    expect(box.getAttribute('data-pack-slot')).toBe('loading')
  })
})

describe('PackAvatar pack reads', () => {
  it('reads a pack once however many avatars wear it', async () => {
    detail.mockResolvedValue({ animations: { idle: { content: '<svg/>', format: 'svg' } } })
    render(
      <>
        <PackAvatar id="aurora" state="idle" size={40} />
        <PackAvatar id="aurora" state="working" size={22} />
        <PackAvatar id="aurora" state="done" size={18} />
      </>,
    )

    await waitFor(() => expect(screen.getAllByTestId('pack-avatar-svg')).toHaveLength(3))
    expect(detail).toHaveBeenCalledTimes(1)
  })

  it('re-reads after the library invalidates the pack, redrawing a MOUNTED avatar', async () => {
    // The Library tab re-imports a pack under the same id while a roster row is
    // wearing it. The row must pick up the new art without remounting.
    detail
      .mockResolvedValueOnce({ animations: { idle: { content: '<svg/>', format: 'svg' } } })
      .mockResolvedValueOnce({ animations: { idle: { content: '{"v":1}', format: 'lottie' } } })
    render(<PackAvatar id="aurora" state="idle" size={40} />)
    await screen.findByTestId('pack-avatar-svg')

    await invalidate('aurora')

    await screen.findByTestId('pack-avatar-lottie')
    expect(detail).toHaveBeenCalledTimes(2)
  })
})

describe('PackAvatar failure reporting', () => {
  it('reports onError and renders nothing when the pack cannot be read', async () => {
    detail.mockRejectedValue(new Error('pack_not_found'))
    const onError = vi.fn()
    render(<PackAvatar id="gone" state="idle" size={40} onError={onError} />)

    // Past the one retry: a real 404 fails both reads.
    await waitFor(() => expect(onError).toHaveBeenCalled())
    expect(detail).toHaveBeenCalledTimes(2)
    expect(screen.queryByTestId('pack-avatar-svg')).toBeNull()
    expect(screen.queryByTestId('pack-avatar-pending')).toBeNull()
  })

  it('draws the caller fallback in place while broken, for every kind of failure', async () => {
    // Read failure, no resolvable slot, renderer refusal: each shows the fallback
    // with this component still mounted, which is what keeps the query observed.
    const cases: Array<[string, () => void]> = [
      ['read', () => detail.mockRejectedValue(new Error('pack_not_found'))],
      ['no slot', () => detail.mockResolvedValue({ animations: { walking: { content: '<svg/>', format: 'svg' } } })],
      ['renderer', () => detail.mockResolvedValue({ animations: { idle: { content: 'nope', format: 'lottie' } } })],
    ]
    for (const [label, arrange] of cases) {
      detail.mockReset()
      arrange()
      // One id per case: the client caches by id for the whole test.
      const { unmount } = render(
        <PackAvatar id={`pack-${label}`} state="done" size={40} fallback={<i data-testid="fallback">{label}</i>} />,
      )
      if (label === 'renderer') {
        await screen.findByTestId('lottie-stub')
        act(() => screen.getByTestId('lottie-stub').click())
      }
      await screen.findByTestId('fallback')
      expect(screen.queryByTestId('pack-avatar-pending')).toBeNull()
      unmount()
    }
  })

  it('gives a re-imported pack a fresh chance after its previous art was refused', async () => {
    // The renderer refused the OLD art. The Library re-imports the pack under the
    // same id and invalidates; the new art must be tried, not inherit the verdict.
    detail
      .mockResolvedValueOnce({ animations: { idle: { content: 'nope', format: 'lottie' } } })
      .mockResolvedValueOnce({ animations: { idle: { content: '<svg/>', format: 'svg' } } })
    render(<PackAvatar id="aurora" state="idle" size={40} fallback={<i data-testid="fallback" />} />)
    await screen.findByTestId('lottie-stub')
    act(() => screen.getByTestId('lottie-stub').click())
    await screen.findByTestId('fallback')

    await invalidate('aurora')
    await screen.findByTestId('pack-avatar-svg')
    expect(screen.queryByTestId('fallback')).toBeNull()
  })

  it('reports onError when the pack draws nothing for any state', async () => {
    // A pack whose only art is a random clip: `done` resolves to done|idle, and
    // it carries neither, so there is no frame to draw.
    detail.mockResolvedValue({ animations: { walking: { content: '<svg/>', format: 'svg' } } })
    const onError = vi.fn()
    render(<PackAvatar id="aurora" state="done" size={40} onError={onError} />)

    await waitFor(() => expect(onError).toHaveBeenCalled())
  })

  it('reports onError when a lottie clip cannot be parsed', async () => {
    // Malformed art is a load failure like a missing file: the importer only
    // checks that a pack's `.json` is non-empty, so an unparseable clip gets
    // here and must fall back rather than leave an empty box.
    detail.mockResolvedValue({ animations: { idle: { content: '{bad', format: 'lottie' } } })
    const onError = vi.fn()
    render(<PackAvatar id="aurora" state="idle" size={40} onError={onError} />)

    const stub = await screen.findByTestId('lottie-stub')
    act(() => stub.click())
    await waitFor(() => expect(onError).toHaveBeenCalled())
    expect(screen.queryByTestId('pack-avatar-lottie')).toBeNull()
  })

  it('re-reads once before giving up, so a blip is not a permanent ghost', async () => {
    detail
      .mockRejectedValueOnce(new Error('ECONNREFUSED'))
      .mockResolvedValue({ animations: { idle: { content: '<svg/>', format: 'svg' } } })
    const onError = vi.fn()
    render(<PackAvatar id="aurora" state="idle" size={40} onError={onError} />)

    await screen.findByTestId('pack-avatar-svg')
    expect(detail).toHaveBeenCalledTimes(2)
    expect(onError).not.toHaveBeenCalled()
  })

  it('recovers a pack that failed both reads once the query is refetched', async () => {
    // A query left in error state has no data, so React Query refetches it on the
    // next focus / reconnect / mount. A gateway blip is therefore not a permanent
    // ghost even after the retry budget is spent.
    detail
      .mockRejectedValueOnce(new Error('ECONNREFUSED'))
      .mockRejectedValueOnce(new Error('ECONNREFUSED'))
      .mockResolvedValue({ animations: { idle: { content: '<svg/>', format: 'svg' } } })
    const onError = vi.fn()
    render(<PackAvatar id="aurora" state="idle" size={40} onError={onError} />)
    await waitFor(() => expect(onError).toHaveBeenCalled())

    // Invalidating an errored, still-mounted query refetches it — the same path
    // the Library's re-import takes; focus and reconnect drive the same refetch.
    await invalidate('aurora')
    await screen.findByTestId('pack-avatar-svg')
  })

  it('reports onError when a sprite sheet cannot be decoded', async () => {
    detail.mockResolvedValue({
      animations: { idle: { content: 'b64', format: 'sprite' } },
      sprite: { frameWidth: 16, frameHeight: 16, fps: 4 },
    })
    const onError = vi.fn()
    render(<PackAvatar id="aurora" state="idle" size={40} onError={onError} />)

    const stub = await screen.findByTestId('sprite-stub')
    act(() => stub.click())
    await waitFor(() => expect(onError).toHaveBeenCalled())
    expect(screen.queryByTestId('pack-avatar-sprite')).toBeNull()
  })

  it('reports onError when the served image itself fails to load', async () => {
    detail.mockResolvedValue({ animations: { idle: { content: '<svg/>', format: 'svg' } } })
    const onError = vi.fn()
    render(<PackAvatar id="aurora" state="idle" size={40} onError={onError} />)

    const img = (await screen.findByTestId('pack-avatar-svg')).querySelector('img')!
    img.dispatchEvent(new Event('error'))
    await waitFor(() => expect(onError).toHaveBeenCalled())
  })
})

describe('PackAvatar animation is bounded by visibility', () => {
  it('holds a lottie avatar on its first frame while it is off screen', async () => {
    useManualObserver()
    detail.mockResolvedValue({
      animations: { idle: { content: '{"v":1}', format: 'lottie' } },
    })
    render(<PackAvatar id="aurora" state="idle" size={38} />)

    // No observer callback has fired, so nothing is known to be visible yet.
    const box = await screen.findByTestId('pack-avatar-lottie')
    expect(box.getAttribute('data-pack-playing')).toBe('false')
    expect(screen.getByTestId('lottie-stub').getAttribute('data-autoplay')).toBe('false')
  })

  it('plays once the avatar scrolls into view, and stops when it leaves', async () => {
    useManualObserver()
    detail.mockResolvedValue({
      animations: { working: { content: 'base64', format: 'sprite' } },
      sprite: { frameWidth: 16, frameHeight: 16, fps: 4 },
    })
    render(<PackAvatar id="aurora" state="working" size={38} />)
    await screen.findByTestId('pack-avatar-sprite')

    const observer = ManualObserver.instances.at(-1)!
    observer.fire(true)
    await waitFor(() =>
      expect(screen.getByTestId('sprite-stub').getAttribute('data-playing')).toBe('true'),
    )

    observer.fire(false)
    await waitFor(() =>
      expect(screen.getByTestId('sprite-stub').getAttribute('data-playing')).toBe('false'),
    )
  })

  it('holds every frame when the user prefers reduced motion, even on screen', async () => {
    // Both players are JS-driven, so the stylesheet's reduced-motion rule cannot
    // reach them; the preference is read here and overrides visibility.
    const originalMatch = window.matchMedia
    const listeners: Array<() => void> = []
    let reduce = true
    // A real MediaQueryList's `matches` is LIVE, so the mock's must be too.
    ;(window as unknown as { matchMedia: unknown }).matchMedia = (q: string) => ({
      get matches() {
        return q.includes('reduced-motion') ? reduce : false
      },
      media: q,
      addEventListener: (_: string, cb: () => void) => listeners.push(cb),
      removeEventListener: () => {},
    })
    try {
      detail.mockResolvedValue({ animations: { working: { content: '{"v":1}', format: 'lottie' } } })
      render(<PackAvatar id="aurora" state="working" size={40} />)
      // The suite's observer stub reports intersecting immediately, so without the
      // preference this would be playing.
      const box = await screen.findByTestId('pack-avatar-lottie')
      expect(box.getAttribute('data-pack-playing')).toBe('false')
      expect(screen.getByTestId('lottie-stub').getAttribute('data-autoplay')).toBe('false')

      // The preference is a live system setting: flipping it resumes the timeline.
      reduce = false
      act(() => listeners.forEach((cb) => cb()))
      await waitFor(() => expect(box.getAttribute('data-pack-playing')).toBe('true'))
    } finally {
      window.matchMedia = originalMatch
    }
  })

  it('shows the pending box as a loading skeleton, not a blank tile', async () => {
    detail.mockReturnValue(new Promise(() => {}))
    render(<PackAvatar id="aurora" state="idle" size={40} />)
    expect(screen.getByTestId('pack-avatar-pending').className).toContain('skeleton')
  })

  it('animates where the engine has no IntersectionObserver at all', async () => {
    ;(globalThis as unknown as { IntersectionObserver: unknown }).IntersectionObserver = undefined
    ;(window as unknown as { IntersectionObserver: unknown }).IntersectionObserver = undefined
    detail.mockResolvedValue({
      animations: { working: { content: '{"v":1}', format: 'lottie' } },
    })
    render(<PackAvatar id="aurora" state="working" size={18} />)

    const box = await screen.findByTestId('pack-avatar-lottie')
    expect(box.getAttribute('data-pack-playing')).toBe('true')
  })

  it('re-observes the box a recovery mounts, so the visibility bound survives a failure', async () => {
    // While the fallback shows, the box is unmounted; a recovery mounts a NEW
    // span. Observing only the first mount left `visible` frozen at whatever it
    // last said — a recovered avatar playing off screen, or still on it.
    useManualObserver()
    detail
      .mockResolvedValueOnce({ animations: { working: { content: 'nope', format: 'lottie' } } })
      .mockResolvedValueOnce({ animations: { working: { content: '{"v":1}', format: 'lottie' } } })
    render(<PackAvatar id="aurora" state="working" size={40} fallback={<i data-testid="fallback" />} />)
    await screen.findByTestId('lottie-stub')
    const first = ManualObserver.instances.at(-1)!
    first.fire(true)
    await waitFor(() =>
      expect(screen.getByTestId('pack-avatar-lottie').getAttribute('data-pack-playing')).toBe('true'),
    )

    // The renderer refuses the art; the fallback replaces the box.
    act(() => screen.getByTestId('lottie-stub').click())
    await screen.findByTestId('fallback')

    // Recovery mounts a fresh box, which must get a fresh observer.
    await invalidate('aurora')
    const box = await screen.findByTestId('pack-avatar-lottie')
    const second = ManualObserver.instances.at(-1)!
    expect(second).not.toBe(first)
    second.fire(false)
    await waitFor(() => expect(box.getAttribute('data-pack-playing')).toBe('false'))
    second.fire(true)
    await waitFor(() => expect(box.getAttribute('data-pack-playing')).toBe('true'))
  })

  it('holds idle on its first frame even on screen; motion is for a reaction', async () => {
    // A roster holds dozens of these faces. A dozen looping idles make motion
    // the wallpaper of the page, so idle is still and a state change moves —
    // which is also what tells the eye something happened.
    detail.mockResolvedValue({
      animations: {
        idle: { content: '{"v":1}', format: 'lottie' },
        working: { content: '{"v":2}', format: 'lottie' },
      },
    })
    const { rerender } = render(<PackAvatar id="aurora" state="idle" size={40} />)
    const box = await screen.findByTestId('pack-avatar-lottie')
    // The suite's observer stub reports intersecting immediately.
    expect(box.getAttribute('data-pack-playing')).toBe('false')
    expect(screen.getByTestId('lottie-stub').getAttribute('data-autoplay')).toBe('false')

    rerender(
      <QueryClientProvider client={qc}>
        <PackAvatar id="aurora" state="working" size={40} />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(box.getAttribute('data-pack-playing')).toBe('true'))
  })

  it('plays a reaction that resolves to the idle clip: the state decides, not the art', async () => {
    // A pack that draws only idle still answers `working` with that clip, and
    // while a turn runs the clip plays: the motion says "working", whatever art
    // carries it.
    detail.mockResolvedValue({ animations: { idle: { content: '{"v":1}', format: 'lottie' } } })
    render(<PackAvatar id="aurora" state="working" size={40} />)
    const box = await screen.findByTestId('pack-avatar-lottie')
    expect(box.getAttribute('data-pack-playing')).toBe('true')
  })
})
