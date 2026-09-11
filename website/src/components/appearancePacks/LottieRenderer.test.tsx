import { cleanup, render } from '@testing-library/react'
import React from 'react'
import lottie from 'lottie-web/build/player/lottie_light'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { LottieRenderer } from './LottieRenderer'

const loadAnimation = vi.mocked(lottie.loadAnimation)

/** The smallest document `isValidLottie` accepts. */
const VALID = '{"v":"5.7.4","fr":24,"ip":0,"op":48,"layers":[]}'

/** A lottie item with recording lifecycle hooks, so destroy/listener discipline
 *  can be asserted rather than assumed. */
function fakeItem() {
  return {
    destroy: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    play: vi.fn(),
    pause: vi.fn(),
    stop: vi.fn(),
  }
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  loadAnimation.mockClear()
})

it('leaves a diagnostic when an imported animation is malformed', () => {
  const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

  render(<LottieRenderer animationData="{not json" width={64} height={64} />)

  expect(loadAnimation).not.toHaveBeenCalled()
  expect(errorSpy).toHaveBeenCalledWith(
    '[appearance-pack] lottie JSON parse failed',
    expect.objectContaining({ bytes: 9 }),
  )
})

it('reports a malformed animation to its caller, not only to the console', () => {
  // A console line alone let the caller believe it had drawn a face: the box was
  // empty, no fallback ran, and no notice appeared.
  vi.spyOn(console, 'error').mockImplementation(() => {})
  const onError = vi.fn()

  render(<LottieRenderer animationData="{not json" width={64} height={64} onError={onError} />)

  expect(onError).toHaveBeenCalledTimes(1)
})

describe('a document that is not a Lottie', () => {
  it('refuses valid JSON with none of the required fields, and reports it', () => {
    // `{}` parses, names no asset, and then fails inside the player with no event
    // anyone is listening to. The importer only checks non-emptiness.
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const onError = vi.fn()

    render(<LottieRenderer animationData="{}" width={64} height={64} onError={onError} />)

    expect(loadAnimation).not.toHaveBeenCalled()
    expect(onError).toHaveBeenCalledTimes(1)
    expect(errorSpy).toHaveBeenCalledWith(
      '[appearance-pack] lottie clip refused: not a Lottie document',
      expect.objectContaining({ bytes: 2 }),
    )
  })

  it('reports a document the player throws on, rather than crashing the tree', () => {
    // `isValidLottie` tests key PRESENCE, so `layers: null` passes it and
    // `loadAnimation` throws synchronously — before any failure listener could
    // be attached. Third-party pack art reaching this player malformed is the
    // ordinary case, so the throw is a refusal like the others: report, draw
    // nothing, and the caller falls back.
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    loadAnimation.mockImplementation(() => {
      throw new TypeError("Cannot read properties of null (reading 'length')")
    })
    const onError = vi.fn()
    const nullLayers = '{"v":"5.7.4","fr":24,"ip":0,"op":48,"layers":null}'
    expect(() =>
      render(<LottieRenderer animationData={nullLayers} width={64} height={64} onError={onError} />),
    ).not.toThrow()
    expect(loadAnimation).toHaveBeenCalledTimes(1)
    expect(onError).toHaveBeenCalledTimes(1)
    expect(errorSpy.mock.calls[0]?.[0]).toContain('the player could not build it')
    errorSpy.mockRestore()
  })

  it('routes the player own failure events to onError', () => {
    // A document that passes the shape check but that lottie-web still cannot
    // build reports through `error` / `data_failed`, never an exception.
    const item = fakeItem()
    loadAnimation.mockReturnValue(item as never)
    const onError = vi.fn()
    render(<LottieRenderer animationData={VALID} width={64} height={64} onError={onError} />)

    const listeners = Object.fromEntries(item.addEventListener.mock.calls as [string, () => void][])
    expect(Object.keys(listeners).sort()).toEqual(['DOMLoaded', 'data_failed', 'error'])
    listeners.data_failed()
    expect(onError).toHaveBeenCalledTimes(1)
  })
})

describe('a clip that would fetch', () => {
  it('refuses to load it, and reports the refusal to its caller', () => {
    // A pack's clip is third-party art on the gateway's own authenticated origin,
    // and lottie-web resolves `assets` by REQUESTING them — so an external image
    // would make the dashboard issue an attacker-chosen request the moment a crew
    // wears that pack.
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const onError = vi.fn()
    const hostile = JSON.stringify({
      v: '5.7.4',
      fr: 24,
      ip: 0,
      op: 48,
      layers: [],
      assets: [{ id: 'i0', w: 1, h: 1, u: 'https://attacker.example/', p: 'pixel.png', e: 0 }],
    })

    render(<LottieRenderer animationData={hostile} width={64} height={64} onError={onError} />)

    expect(loadAnimation).not.toHaveBeenCalled()
    expect(onError).toHaveBeenCalledTimes(1)
    expect(errorSpy).toHaveBeenCalledWith(
      '[appearance-pack] lottie clip refused: it references a remote asset',
      expect.objectContaining({ bytes: hostile.length }),
    )
  })

  it('still loads a clip whose art is embedded', () => {
    // The refusal must not cost an ordinary pack its animation.
    loadAnimation.mockReturnValue(fakeItem() as never)
    const onError = vi.fn()
    const safe = JSON.stringify({
      v: '5.7.4',
      fr: 24,
      ip: 0,
      op: 48,
      layers: [],
      assets: [{ id: 'i0', w: 1, h: 1, e: 1, p: 'data:image/png;base64,iVBORw0=' }],
    })

    render(<LottieRenderer animationData={safe} width={64} height={64} onError={onError} />)

    expect(loadAnimation).toHaveBeenCalledTimes(1)
    expect(onError).not.toHaveBeenCalled()
  })
})

it('does not report an error for a clip that loads', () => {
  loadAnimation.mockReturnValue({
    destroy: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(),
  } as never)
  const onError = vi.fn()

  render(<LottieRenderer animationData={VALID} width={64} height={64} onError={onError} />)

  expect(onError).not.toHaveBeenCalled()
})

it('loads nothing at all for empty animation data', () => {
  render(<LottieRenderer animationData="" width={64} height={64} />)
  expect(loadAnimation).not.toHaveBeenCalled()
})

describe('loading a valid clip', () => {
  it('hands lottie the parsed document and the LIGHT svg renderer', () => {
    const item = fakeItem()
    loadAnimation.mockReturnValue(item as never)

    render(<LottieRenderer animationData={VALID} width={48} height={48} />)

    expect(loadAnimation).toHaveBeenCalledTimes(1)
    const opts = loadAnimation.mock.calls[0][0]
    // Parsed, not the raw string: passing text would make lottie fetch it as a
    // path.
    expect(opts.animationData).toEqual(JSON.parse(VALID))
    // `svg`, and the module imports the light player — the pack JSON is
    // third-party, and the default build evaluates expressions.
    expect(opts.renderer).toBe('svg')
    expect(opts.loop).toBe(true)
    expect(opts.autoplay).toBe(true)
  })

  it('holds the clip on its first frame when autoplay is off', () => {
    // What a roster renders for an avatar the user cannot see: the document is
    // built, but no timeline runs.
    loadAnimation.mockReturnValue(fakeItem() as never)

    render(
      <LottieRenderer animationData={VALID} width={18} height={18} autoplay={false} loop={false} />,
    )

    expect(loadAnimation.mock.calls[0][0].autoplay).toBe(false)
    expect(loadAnimation.mock.calls[0][0].loop).toBe(false)
  })

  it('sizes its container to the box it was given', () => {
    loadAnimation.mockReturnValue(fakeItem() as never)
    const { container } = render(
      <LottieRenderer animationData={VALID} width={38} height={38} />,
    )
    const div = container.firstElementChild as HTMLElement
    expect(div.style.width).toBe('38px')
    expect(div.style.height).toBe('38px')
  })

  it('reports readiness through onReady', () => {
    const item = fakeItem()
    loadAnimation.mockReturnValue(item as never)
    const onReady = vi.fn()

    render(<LottieRenderer animationData={VALID} width={64} height={64} onReady={onReady} />)

    const [event, handler] = item.addEventListener.mock.calls[0]
    expect(event).toBe('DOMLoaded')
    ;(handler as () => void)()
    expect(onReady).toHaveBeenCalledTimes(1)
  })

  it('destroys the animation and drops its listener on unmount', () => {
    // A leaked instance keeps drawing: an avatar scrolled out of a virtualized
    // list would go on spending frames for the life of the tab.
    const item = fakeItem()
    loadAnimation.mockReturnValue(item as never)

    const view = render(<LottieRenderer animationData={VALID} width={64} height={64} />)
    view.unmount()

    expect(item.removeEventListener).toHaveBeenCalledWith('DOMLoaded', expect.any(Function))
    expect(item.destroy).toHaveBeenCalledTimes(1)
  })

  it('replaces the animation when the clip changes, destroying the old one', () => {
    // A crew moving idle -> working swaps the document under one mounted
    // instance; two live animations in one container would draw over each other.
    const first = fakeItem()
    const second = fakeItem()
    loadAnimation.mockReturnValueOnce(first as never).mockReturnValueOnce(second as never)

    const view = render(<LottieRenderer animationData={VALID} width={64} height={64} />)
    const second_doc = VALID.replace('"op":48', '"op":96')
    view.rerender(<LottieRenderer animationData={second_doc} width={64} height={64} />)

    expect(first.destroy).toHaveBeenCalled()
    expect(loadAnimation).toHaveBeenCalledTimes(2)
    expect(loadAnimation.mock.calls[1][0].animationData).toEqual(JSON.parse(second_doc))
    expect(second.destroy).not.toHaveBeenCalled()
  })
})
