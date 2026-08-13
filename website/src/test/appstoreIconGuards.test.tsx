/**
 * The editorial surfaces must honour a DARK-ONLY icon.
 *
 * `AppIcon` resolves `iconUrlDark` in either theme direction, and
 * `AppIcon.darkSelection.test.tsx` pins that. This file pins the CALL SITES,
 * which is a different thing and is where the bug actually was: `FeatureCard`
 * and `FeaturedSpotlight` each guarded the icon behind `iconUrl || icon`, so an
 * app shipping only `iconPathDark` fell through to the `Package` glyph even
 * though `AppIcon` would have rendered its icon. Testing the component while
 * leaving its guards untested is what let that ship — a mutation that removes
 * `iconUrlDark` from both guards passes every AppIcon test.
 *
 * These assert on the rendered <img>, not on a class or a prop, because the
 * guard's whole effect is whether an image element exists at all.
 */
import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'

import FeatureCard from '../components/appstore/FeatureCard'
import FeaturedSpotlight from '../components/appstore/FeaturedSpotlight'
import type { RegistryApp } from '../components/appstore/types'

/** An app whose ONLY icon is the dark variant, and which ships no hero art (so
 *  the surface takes its icon branch rather than rendering an image of art). */
const darkOnly = {
  name: 'dark-only-app',
  displayName: 'Dark Only',
  description: 'Ships iconPathDark and nothing else.',
  author: 'Kiro Crew', // brand-ok: fixture author
  version: '1.0.0',
  tags: [],
  installed: false,
  updateAvailable: false,
  iconUrlDark: '/api/apps/blob?repo=x&path=icon-dark.png',
} as unknown as RegistryApp

const noop = () => {}

/**
 * The two surfaces no longer share a prop shape -- `FeaturedSpotlight` takes a
 * typed section (`type` plus an `apps` list) while `FeatureCard` still takes one
 * `app` -- so the guard is asserted per component rather than parametrised. The
 * property under test is the same either way: a dark-only icon must render as
 * the dark variant instead of degrading to the package glyph.
 */
describe('FeatureCard with a dark-only icon', () => {
  it('renders the dark variant instead of falling through to the glyph', () => {
    render(<FeatureCard app={darkOnly} onOpen={noop} onGet={noop} onEnable={noop} />)
    const srcs = [...document.querySelectorAll('img')].map(i => i.getAttribute('src'))
    expect(srcs).toContain('/api/apps/blob?repo=x&path=icon-dark.png')
  })
})

describe('FeaturedSpotlight with a dark-only icon', () => {
  it('renders the dark variant instead of falling through to the glyph', () => {
    // The icon now lives on the app ROW rather than in the art panel, and the
    // panel itself falls back to a gradient because this fixture ships no hero
    // art -- so the blob URL below can only have come from the row's icon.
    render(
      <FeaturedSpotlight
        type="app"
        apps={[darkOnly]}
        onOpenApp={noop}
        onGet={noop}
        onEnable={noop}
      />,
    )
    const srcs = [...document.querySelectorAll('img')].map(i => i.getAttribute('src'))
    expect(srcs).toContain('/api/apps/blob?repo=x&path=icon-dark.png')
  })
})

describe('an app with no icon at all still degrades', () => {
  it('renders no blob image when neither variant is present', () => {
    const bare = { ...darkOnly, iconUrlDark: undefined } as unknown as RegistryApp
    render(<FeatureCard app={bare} onOpen={noop} onGet={noop} onEnable={noop} />)
    const srcs = [...document.querySelectorAll('img')].map(i => i.getAttribute('src') || '')
    expect(srcs.some(s => s.includes('/api/apps/blob'))).toBe(false)
  })
})

// Silence the theme provider's absence: these components read `useTheme`, which
// falls back to a default when no provider is mounted.
vi.mock('../hooks/useTheme', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../hooks/useTheme')>()
  return { ...actual, useTheme: () => ({ ...actual, theme: 'dark' }) }
})
