/**
 * Behavior pins for mochi's LottieRenderer diagnostics under the LIGHT player.
 *
 * The renderer's job splits in two: hand a sanitized clip to
 * `lottie.loadAnimation`, and leave a console breadcrumb for every way a clip
 * can degrade silently (expressions the light player cannot run, SVG effects it
 * cannot draw, JSON that does not parse, a load that throws, a load that
 * succeeds but paints nothing). Each pin here renders the real component
 * against the suite's lottie mock (integration/setup.ts mocks BOTH specifiers)
 * and asserts the observable outcome: what reached `loadAnimation` and what
 * landed on the console — including that the effect warning fires ONCE per
 * distinct clip, not once per mount or per state swap.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, cleanup, act } from '@testing-library/react'
import React from 'react'

import lottie from 'lottie-web/build/player/lottie_light'

import { LottieRenderer } from '../src/renderer/LottieRenderer'

const loadAnimation = vi.mocked(lottie.loadAnimation)

/** A minimal well-formed clip; `seed` makes each test's clip key distinct so
 *  the module-level once-per-clip warn dedupe cannot leak between tests. */
function clip(seed: string, extra: Record<string, unknown> = {}): string {
  return JSON.stringify({ v: '5.13.0', seed, op: 60, layers: [], ...extra })
}

/** The slice of a sanitized clip these pins read back off `loadAnimation`: the
 *  one transform property the expression strip touches. `p` stays an open bag
 *  because the point of the assertion is whether the `x` key is still there. */
type StrippedClip = { layers: { ks: { p: Record<string, unknown> } }[] }

describe('mochi LottieRenderer', () => {
  let warnSpy: ReturnType<typeof vi.spyOn>
  let errorSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  afterEach(() => {
    cleanup()
    warnSpy.mockRestore()
    errorSpy.mockRestore()
    loadAnimation.mockClear()
  })

  it('loads a clean clip with the svg renderer and no warnings', () => {
    render(<LottieRenderer animationData={clip('clean')} width={64} height={64} />)
    expect(loadAnimation).toHaveBeenCalledTimes(1)
    expect(loadAnimation.mock.calls[0][0]).toMatchObject({ renderer: 'svg', loop: true })
    expect(warnSpy).not.toHaveBeenCalled()
    expect(errorSpy).not.toHaveBeenCalled()
  })

  it('strips a string-x expression before loading and warns with the light-player reason', () => {
    render(
      <LottieRenderer
        animationData={clip('expr', { layers: [{ ks: { p: { x: 'loopOut()' } } }] })}
        width={64}
        height={64}
      />,
    )
    const passed = loadAnimation.mock.calls[0][0].animationData as StrippedClip
    expect(passed.layers[0].ks.p).not.toHaveProperty('x')
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining('removed 1 lottie expression(s)'),
    )
    // A numeric/array x is a coordinate, not an expression — it must survive.
    expect(clip('expr')).toBeTruthy()
  })

  it('keeps non-string x keys: coordinates and easing handles survive the strip', () => {
    render(
      <LottieRenderer
        animationData={clip('data-x', { layers: [{ ks: { p: { x: [0.5], y: 1 } } }] })}
        width={64}
        height={64}
      />,
    )
    const passed = loadAnimation.mock.calls[0][0].animationData as StrippedClip
    expect(passed.layers[0].ks.p.x).toEqual([0.5])
    expect(warnSpy).not.toHaveBeenCalled()
  })

  it('warns ONCE per distinct effect-bearing clip, across remounts and data swaps', () => {
    const effectClip = clip('fx', { layers: [{ ef: [{ ty: 25, ef: [{ v: { k: 1 } }] }] }] })
    const { rerender, unmount } = render(
      <LottieRenderer animationData={effectClip} width={64} height={64} />,
    )
    const effectWarns = () =>
      warnSpy.mock.calls.filter(
        (c) => typeof c[0] === 'string' && c[0].includes('effect-bearing node(s)'),
      )
    expect(effectWarns()).toHaveLength(1)
    // The nested parameter `ef` inside the counted effect must not double-count.
    expect(effectWarns()[0][0]).toContain('carries 1 effect-bearing node(s)')

    // Remount with the same clip (PetWidget swaps animationData every state
    // change; GalleryPanel mounts one renderer per tile): still one warn.
    rerender(<LottieRenderer animationData={clip('other')} width={64} height={64} />)
    rerender(<LottieRenderer animationData={effectClip} width={64} height={64} />)
    unmount()
    render(<LottieRenderer animationData={effectClip} width={64} height={64} />)
    expect(effectWarns()).toHaveLength(1)

    // A DIFFERENT effect-bearing clip is new information: it gets its own warn.
    render(
      <LottieRenderer
        animationData={clip('fx2', { layers: [{ ef: [{ ty: 21 }] }] })}
        width={64}
        height={64}
      />,
    )
    expect(effectWarns()).toHaveLength(2)
  })

  it('leaves a parse-failure breadcrumb instead of an invisible blank', () => {
    render(<LottieRenderer animationData="{not json" width={64} height={64} />)
    expect(loadAnimation).not.toHaveBeenCalled()
    expect(errorSpy).toHaveBeenCalledWith(
      '[mochi] lottie JSON parse failed',
      expect.objectContaining({ bytes: 9 }),
      expect.anything(),
    )
  })

  it('leaves a load-failure breadcrumb when loadAnimation throws', () => {
    loadAnimation.mockImplementationOnce(() => {
      throw new Error('unsupported layer')
    })
    render(<LottieRenderer animationData={clip('boom')} width={64} height={64} />)
    expect(errorSpy).toHaveBeenCalledWith(
      '[mochi] lottie loadAnimation failed',
      expect.objectContaining({ bytes: expect.any(Number) }),
      expect.any(Error),
    )
  })

  it('reports a load that succeeded but painted nothing, then calls onReady', () => {
    let ready: (() => void) | undefined
    loadAnimation.mockImplementationOnce(
      () =>
        ({
          destroy: () => {},
          addEventListener: (event: string, cb: () => void) => {
            if (event === 'DOMLoaded') ready = cb
          },
          removeEventListener: () => {},
        }) as never,
    )
    const onReady = vi.fn()
    render(
      <LottieRenderer
        animationData={clip('empty-paint')}
        width={64}
        height={64}
        onReady={onReady}
      />,
    )
    expect(ready).toBeDefined()
    act(() => ready!())
    expect(errorSpy).toHaveBeenCalledWith(
      '[mochi] lottie loaded but painted nothing',
      expect.objectContaining({ hasSvg: false }),
    )
    expect(onReady).toHaveBeenCalledTimes(1)
  })

  it('destroys the animation and detaches the DOMLoaded listener on unmount', () => {
    const destroy = vi.fn()
    const removeEventListener = vi.fn()
    loadAnimation.mockImplementationOnce(
      () =>
        ({
          destroy,
          addEventListener: () => {},
          removeEventListener,
        }) as never,
    )
    const { unmount } = render(
      <LottieRenderer animationData={clip('teardown')} width={64} height={64} />,
    )
    unmount()
    expect(destroy).toHaveBeenCalled()
    expect(removeEventListener).toHaveBeenCalledWith('DOMLoaded', expect.any(Function))
  })
})
