/**
 * The prop layer must draw the REAL art, for every prop the picker offers.
 *
 * Two failure shapes are guarded here, and this port has produced both:
 *
 * 1. A table with no art behind it. `GHOST_ACCESSORIES` and `CELEBRATE_PROPS` were
 *    ported early and the component that draws them was not, so the gallery listed
 *    props the companion could never wear. Nothing errored — the layer simply did
 *    not exist.
 * 2. Substituted art. A first attempt at the component inlined `data:` URIs and,
 *    unable to carry 216 KB of traced Figma vector, shipped simplified stand-ins.
 *    The mascot is the product's face, so an approximation reads as a bug nobody
 *    can name. Six props are asset files and this asserts they are LOADED, not
 *    redrawn: an `<img>` whose src resolves to a bundled file.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import React from 'react'
import { describe, it, expect, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'

import GhostAccessoryLayer from '../apps/crew-companion/GhostAccessoryLayer'
import {
  GHOST_ACCESSORIES,
  CELEBRATE_PROPS,
  HIDES_EYES,
  type GhostAccessory,
} from '../apps/crew-companion/ghostAccessories'

afterEach(cleanup)

/** Props whose art is a bundled SVG file rather than inline JSX. */
const ASSET_BACKED: GhostAccessory[] = [
  'flower', 'partyhat', 'antenna', 'sleepmask', 'popper', 'coffee',
]

describe('ghost accessory layer', () => {
  it('draws nothing at all for "none"', () => {
    // 'none' is a real, common outcome — it is in CELEBRATE_PROPS on purpose so a
    // plain hop stays frequent. It must render empty, not an empty box.
    const { container } = render(<GhostAccessoryLayer id="none" pose="primary" />)
    expect(container.innerHTML).toBe('')
  })

  it('draws real art for every accessory the picker offers', () => {
    for (const { id } of GHOST_ACCESSORIES) {
      if (id === 'none') continue
      const { container, unmount } = render(<GhostAccessoryLayer id={id} pose="primary" />)
      const drawn = container.querySelector('svg, img')
      expect(drawn, `accessory "${id}" renders no art`).not.toBeNull()
      unmount()
    }
  })

  it('loads the asset-backed props from files instead of redrawing them', () => {
    // What this guards is that the art comes from a FILE, not that it hand-draws
    // paths in the .tsx — a sub-agent once "ported" these props by inventing
    // simplified vectors inline, and they shipped looking almost right.
    //
    // So the assertion reads the IMPORTS, not the resolved URL. It used to reject a
    // `data:` src, which was wrong: Vite inlines any asset under
    // `assetsInlineLimit` (4 KB), so `antenna.svg` (1.9 KB) and `sunglasses.svg`
    // (1.3 KB) legitimately become base64 in a real build — the test passed under
    // vitest and failed in CI for a difference that is not a defect.
    const source = readFileSync(
      join(__dirname, '..', 'apps', 'crew-companion', 'GhostAccessoryLayer.tsx'),
      'utf8',
    )
    for (const id of ASSET_BACKED) {
      expect(
        source,
        `"${id}" must be imported from assets/props, not drawn inline`,
      ).toMatch(new RegExp(`import\\s+\\w+\\s+from\\s+'\\./assets/props/${id}\\.svg'`))

      const { container, unmount } = render(<GhostAccessoryLayer id={id} pose="primary" />)
      const img = container.querySelector('img')
      expect(img, `"${id}" should be an <img> onto its bundled SVG`).not.toBeNull()
      expect(img!.getAttribute('src') ?? '', `"${id}" has no src`).not.toBe('')
      unmount()
    }
  })

  it('never intercepts a click meant for the companion or its bubble', () => {
    // The overlay is click-through except for reported hitboxes; decoration that
    // swallowed a click would make the companion unclickable in a way that looks
    // like the hitbox is broken.
    for (const { id } of GHOST_ACCESSORIES) {
      if (id === 'none') continue
      const { container, unmount } = render(<GhostAccessoryLayer id={id} pose="primary" />)
      const host = container.firstElementChild as HTMLElement
      expect(host.style.pointerEvents, `"${id}" is not click-through`).toBe('none')
      unmount()
    }
  })

  it('follows the eyes: a different pose places the prop differently', () => {
    // The whole point of deriving position from the eye pair is that a posed eye
    // position carries its props with it.
    const a = render(<GhostAccessoryLayer id="partyhat" pose="primary" />)
    const left1 = (a.container.firstElementChild as HTMLElement).style.left
    a.unmount()
    const b = render(<GhostAccessoryLayer id="partyhat" pose="4th" />)
    const left2 = (b.container.firstElementChild as HTMLElement).style.left
    b.unmount()
    expect(left1).not.toBe(left2)
  })

  it('an unknown pose falls back rather than crashing', () => {
    const { container } = render(<GhostAccessoryLayer id="coffee" pose="no-such-pose" />)
    expect(container.querySelector('img')).not.toBeNull()
  })

  it('the flower sits behind the body, which is what makes it peek out', () => {
    const { container } = render(<GhostAccessoryLayer id="flower" pose="primary" />)
    const host = container.firstElementChild as HTMLElement
    expect(host.style.zIndex).toBe('-1')
    // The baked scaleX(-1) cancels the parent's mirror so the tilt reads correctly
    // in BOTH facings; dropping it flips the bouquet in one direction only.
    expect(host.style.transform).toContain('scaleX(-1)')
  })

  it('every celebrate prop and every eye-hiding prop is one the layer draws', () => {
    /*
     * Asserted against what the LAYER can draw, not against the picker.
     *
     * `antenna` and `flower` are deliberately absent from the pickable list — both
     * repos say so in the same comment: retired from the menu, still valid ids an
     * older config could hold, and still rendered. Requiring picker membership here
     * would fail on correct, ported behaviour.
     */
    const renders = (id: GhostAccessory) => {
      const { container, unmount } = render(<GhostAccessoryLayer id={id} pose="primary" />)
      const drawn = container.querySelector('svg, img')
      unmount()
      return drawn !== null
    }
    for (const id of HIDES_EYES) {
      expect(renders(id), `eye-hiding prop "${id}" draws nothing`).toBe(true)
    }
    for (const id of CELEBRATE_PROPS) {
      if (id === 'none') continue
      expect(renders(id), `celebrate prop "${id}" draws nothing`).toBe(true)
    }
  })
})
