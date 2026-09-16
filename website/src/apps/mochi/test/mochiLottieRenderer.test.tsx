/**
 * Mochi draws Lottie through CORE's fenced player.
 *
 * `renderer/LottieRenderer.tsx` is a one-line re-export of
 * `components/appearancePacks/LottieRenderer.tsx`, the same shape as the
 * vendored `SpriteRenderer` shim. Before that, Mochi carried its own copy and
 * it was the one `loadAnimation` call site in the tree with no remote-asset
 * fence: a clip naming a remote image or font went straight to the player,
 * which resolves those by REQUESTING them from the dashboard's own
 * authenticated origin. Mochi renders bundled presets today, so nothing
 * crossed that gap yet — but a Mochi that accepts an imported pack must
 * already stand behind the fence, and one player means one fence.
 *
 * Pins here read the component through MOCHI's import path, so they hold the
 * shim to the contract rather than trusting the file is still one line:
 * a remote reference is refused before `loadAnimation`, a clean clip still
 * loads with the options Mochi's callers rely on, and every shipped preset
 * passes the fence core adds (the shape check and the asset check the old copy
 * never ran), so the switch cannot have blanked the built-in ghost.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import React from 'react'

import lottie from 'lottie-web/build/player/lottie_light'

import { LottieRenderer } from '../src/renderer/LottieRenderer'
import { LottieRenderer as CoreLottieRenderer } from '../../../components/appearancePacks/LottieRenderer'

import ghostIdle from '../assets/animations/kiro_idle_mid.json'
import ghostIdleBlink from '../assets/animations/kiro_idle_mid_blink.json'
import ghostFlying from '../assets/animations/kiro_flying.json'
import ghostStaticBlink from '../assets/animations/kiro_static_blink.json'

const loadAnimation = vi.mocked(lottie.loadAnimation)

const SHIPPED: [string, unknown][] = [
  ['kiro_idle_mid', ghostIdle],
  ['kiro_idle_mid_blink', ghostIdleBlink],
  ['kiro_flying', ghostFlying],
  ['kiro_static_blink', ghostStaticBlink],
]

/** A minimal document that passes core's shape check (`isValidLottie` wants
 *  `v`/`fr`/`ip`/`op`/`layers`); `extra` layers the case under test on top. */
function clip(extra: Record<string, unknown> = {}): string {
  return JSON.stringify({ v: '5.13.0', fr: 30, ip: 0, op: 60, layers: [], ...extra })
}

describe('mochi LottieRenderer', () => {
  let errorSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  afterEach(() => {
    cleanup()
    errorSpy.mockRestore()
    loadAnimation.mockClear()
  })

  it('is core’s player, not a copy of it', () => {
    // The fence lives in one file. A second implementation under Mochi — even a
    // faithful one — is a second place the fence has to be kept, which is how
    // the unfenced copy came to exist in the first place.
    expect(LottieRenderer).toBe(CoreLottieRenderer)
  })

  it('refuses a clip that names a remote image before loadAnimation', () => {
    render(
      <LottieRenderer
        animationData={clip({
          assets: [{ id: 'img_0', w: 1, h: 1, u: 'https://attacker.example/', p: 'pixel.png' }],
        })}
        width={64}
        height={64}
      />,
    )
    expect(loadAnimation).not.toHaveBeenCalled()
    expect(errorSpy).toHaveBeenCalledWith(
      expect.stringContaining('references a remote asset'),
      expect.anything(),
    )
  })

  it('refuses a clip that names a webfont before loadAnimation', () => {
    render(
      <LottieRenderer
        animationData={clip({
          fonts: { list: [{ fName: 'Inter', fFamily: 'Inter', fStyle: 'Regular', origin: 3, fPath: 'https://attacker.example/inter.woff2' }] },
        })}
        width={64}
        height={64}
      />,
    )
    expect(loadAnimation).not.toHaveBeenCalled()
    expect(errorSpy).toHaveBeenCalledWith(
      expect.stringContaining('references a remote asset'),
      expect.anything(),
    )
  })

  it('loads a clean clip with the svg renderer, looping, the way PetWidget and GalleryPanel call it', () => {
    render(<LottieRenderer animationData={clip()} width={64} height={64} />)
    expect(loadAnimation).toHaveBeenCalledTimes(1)
    expect(loadAnimation.mock.calls[0][0]).toMatchObject({
      renderer: 'svg',
      loop: true,
      autoplay: true,
    })
    expect(errorSpy).not.toHaveBeenCalled()
  })

  it.each(SHIPPED)('%s passes the fence and reaches the player', (_name, doc) => {
    // Core checks two things the old Mochi copy never did — the document's
    // shape and its asset references — so each built-in preset has to be shown
    // to clear both, or the switch would have turned the ghost into an empty box.
    render(<LottieRenderer animationData={JSON.stringify(doc)} width={64} height={64} />)
    expect(errorSpy).not.toHaveBeenCalled()
    expect(loadAnimation).toHaveBeenCalledTimes(1)
  })
})
