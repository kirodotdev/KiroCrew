import type React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'
import { setActiveId } from '../store/instancesSlice'

// Controllable useTheme mock — mirrors ThemeExperienceLayer.test.tsx so this file
// can put the layer into a level-2 (Experience) theme without a real theme pack.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let mockTheme: any
const setColorTheme = vi.fn()
vi.mock('../hooks/useTheme', () => ({
  useTheme: () => mockTheme,
}))

import ThemeExperienceLayer from '../components/ThemeExperienceLayer'

function setL2Theme() {
  const slug = 'bikini-bottom'
  const map = new Map<string, unknown>()
  map.set(slug, {
    slug,
    name: 'Bikini Bottom',
    emoji: '🫧',
    level: 2,
    assets: {
      overlays: ['bubbles'],
      topbar: { dark: true, light: true },
      hasAudio: false,
      hasPersona: false,
      branding: { botName: 'Bubbles' },
    },
  })
  mockTheme = {
    theme: 'dark',
    colorTheme: `custom-${slug}`,
    customThemeDataMap: map,
    setColorTheme,
  }
}

const themeFrames = () =>
  Array.from(document.querySelectorAll<HTMLIFrameElement>('iframe[data-theme-frame="1"]'))

describe('ThemeExperienceLayer — remote Crew switch', () => {
  let store: ReturnType<typeof createTestStore>

  beforeEach(() => {
    localStorage.clear()
    setColorTheme.mockReset()
    window.matchMedia = vi.fn().mockReturnValue({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }) as unknown as typeof window.matchMedia
    store = createTestStore()
    setL2Theme()
  })

  const Wrapper = ({ children }: { children: React.ReactNode }) => (
    <Provider store={store}>{children}</Provider>
  )

  it('tears the overlay iframes down when a remote Crew becomes active', () => {
    render(<ThemeExperienceLayer />, { wrapper: Wrapper })

    // Baseline: local Crew (activeId === null) → L2 overlays are mounted.
    expect(store.getState().instances.activeId).toBeNull()
    expect(themeFrames().length).toBeGreaterThan(0)

    // Switching the active Crew to a remote one must unmount the whole layer —
    // App's `display: none` wrapper cannot hide these fixed-position siblings.
    act(() => {
      store.dispatch(setActiveId('cd-1'))
    })
    expect(themeFrames()).toHaveLength(0)
  })

  it('remounts the overlay iframes when the local Crew is reselected', () => {
    render(<ThemeExperienceLayer />, { wrapper: Wrapper })

    act(() => {
      store.dispatch(setActiveId('cd-1'))
    })
    expect(themeFrames()).toHaveLength(0)

    act(() => {
      store.dispatch(setActiveId(null))
    })
    expect(themeFrames().length).toBeGreaterThan(0)
  })
})
