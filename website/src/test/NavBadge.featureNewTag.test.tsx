import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, screen } from '@testing-library/react'
import { useLocation, useNavigate } from 'react-router-dom'

import { NavBadge } from '../shell/nav/navRow'
import { clearFeatureNewTag, deliverFeatureNewTag, featureNewTagRoute, takeFeatureNewTagFlight } from '../utils/featureNewTag'
import { i18nT } from '../i18n/t'
import { renderWithProviders } from './helpers'
import '../surfaces/builtins'

// happy-dom computes no layout, so framer-motion's animations would never
// finish on their own. The mock draws each motion element as its plain tag and
// keeps every `onAnimationComplete` so a test can land the flyer when it says.
const completions: (() => void)[] = []
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set(['layout', 'layoutId', 'initial', 'animate', 'exit', 'transition', 'variants', 'whileHover', 'whileTap', 'onAnimationComplete'])
  const make = (tag: string) =>
    React.forwardRef<HTMLElement, Record<string, unknown> & { children?: React.ReactNode }>((props, ref) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children' || FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      if (typeof props.onAnimationComplete === 'function') completions.push(props.onAnimationComplete as () => void)
      return React.createElement(tag, { ...clean, ref }, props.children)
    })
  // One component per tag, as the real `motion.span` is one value: a fresh
  // component per access would remount the pill on every render.
  const made = new Map<string, ReturnType<typeof make>>()
  return {
    motion: new Proxy({}, { get: (_t, tag: string) => { if (!made.has(tag)) made.set(tag, make(tag)); return made.get(tag) } }),
    useReducedMotion: () => false,
  }
})

const ROUTE = '/members?member=mate'
const origin = { left: 300, top: 200, width: 28, height: 28, src: 'data:image/svg+xml,mate' }

function Probe() {
  const { pathname, search } = useLocation()
  const navigate = useNavigate()
  return (
    <>
      <span data-testid="loc">{pathname + search}</span>
      <button type="button" onClick={() => navigate('/members')}>open</button>
    </>
  )
}

function renderAt(route: string, collapsed = false) {
  renderWithProviders(
    <>
      {/* The row the flight looks for, as `NavItem` renders it around the badge. */}
      <div data-onboarding-nav="members">
        <NavBadge navId="members" collapsed={collapsed} appBadges={{}} />
      </div>
      <Probe />
    </>,
    { route },
  )
}

const tag = () => screen.queryByText(i18nT('components.startupVideoModal.new_tag'))
const flyer = () => document.querySelector<HTMLImageElement>('[data-feature-new-tag-flyer="members"]')
/** Every animation framer would have finished by now. */
const finishAnimations = () => act(() => { completions.splice(0).forEach(fn => fn()) })

beforeEach(() => {
  localStorage.clear()
  act(() => clearFeatureNewTag('members'))
  takeFeatureNewTagFlight('members')
  completions.length = 0
})

afterEach(() => {
  vi.useRealTimers()
})

describe('NavBadge New tag', () => {
  it('shows the tag on the rail item once the intro hands it over', () => {
    renderAt('/')
    expect(tag()).toBeNull()
    act(() => deliverFeatureNewTag('members', null, ROUTE))
    expect(tag()).toBeInTheDocument()
    expect(tag()).toHaveAttribute('data-feature-new-tag', 'members')
    expect(tag()).toHaveAttribute('data-feature-new-tag-state', 'shown')
    expect(tag()).not.toHaveStyle({ visibility: 'hidden' })
    expect(flyer()).toBeNull()
  })

  it('shows the tag on the collapsed rail too', () => {
    act(() => deliverFeatureNewTag('members', null, ROUTE))
    renderAt('/', true)
    expect(tag()).toBeInTheDocument()
  })

  it('only clears a tag set while the page is already open', () => {
    renderAt('/members')
    act(() => deliverFeatureNewTag('members', null, ROUTE))
    expect(screen.getByTestId('loc').textContent).toBe('/members')
    expect(featureNewTagRoute('members')).toBeNull()
    expect(tag()).toBeNull()
  })

  it('opens the stored route when the user arrives at the page, and clears the tag', () => {
    renderAt('/')
    act(() => deliverFeatureNewTag('members', null, ROUTE))
    act(() => screen.getByText('open').click())
    expect(screen.getByTestId('loc').textContent).toBe(ROUTE)
    expect(featureNewTagRoute('members')).toBeNull()
    expect(tag()).toBeNull()
  })

  it('treats a page load onto the route as arriving', () => {
    act(() => deliverFeatureNewTag('members', null, ROUTE))
    renderAt('/members')
    expect(screen.getByTestId('loc').textContent).toBe(ROUTE)
  })

  it('keeps a link to a specific crewmate and only clears the tag', () => {
    act(() => deliverFeatureNewTag('members', null, ROUTE))
    renderAt('/members?member=helper')
    expect(screen.getByTestId('loc').textContent).toBe('/members?member=helper')
    expect(featureNewTagRoute('members')).toBeNull()
  })
})

describe('NavBadge New tag flight', () => {
  it('flies the intro\'s avatar to an invisible pill, then shows the pill where it landed', () => {
    renderAt('/')
    act(() => deliverFeatureNewTag('members', origin, ROUTE))
    // The tag is given before the flyer moves; the pill is there to be measured
    // but not yet seen.
    expect(tag()).toHaveAttribute('data-feature-new-tag-state', 'arriving')
    expect(tag()).toHaveStyle({ visibility: 'hidden' })
    // The row claimed the flight: the flyer is Mate's own picture.
    expect(takeFeatureNewTagFlight('members')).toBeNull()
    expect(flyer()).not.toBeNull()
    expect(flyer()?.getAttribute('src')).toBe(origin.src)
    expect(flyer()).toHaveAttribute('aria-hidden', 'true')
    finishAnimations()
    expect(flyer()).toBeNull()
    expect(tag()).toHaveAttribute('data-feature-new-tag-state', 'shown')
    expect(tag()).not.toHaveStyle({ visibility: 'hidden' })
    expect(JSON.parse(localStorage.getItem('mc-feature-new-tags') ?? '{}')).toEqual({ members: 'shown' })
  })

  it('a pointer press anywhere skips to the landing', () => {
    renderAt('/')
    act(() => deliverFeatureNewTag('members', origin, ROUTE))
    expect(flyer()).not.toBeNull()
    act(() => { window.dispatchEvent(new Event('pointerdown')) })
    expect(flyer()).toBeNull()
    expect(tag()).toHaveAttribute('data-feature-new-tag-state', 'shown')
  })

  it('drops the flyer, and brings back no tag, when the page opens mid-flight', () => {
    renderAt('/')
    act(() => deliverFeatureNewTag('members', origin, ROUTE))
    expect(flyer()).not.toBeNull()
    act(() => screen.getByText('open').click())
    expect(flyer()).toBeNull()
    expect(tag()).toBeNull()
    finishAnimations()
    expect(tag()).toBeNull()
    expect(JSON.parse(localStorage.getItem('mc-feature-new-tags') ?? '{}')).toEqual({})
  })

  it('shows the tag with no flight when the collapsed rail\'s corner is taken by the unread dot', () => {
    vi.useFakeTimers({ toFake: ['requestAnimationFrame'] })
    // An app row with unread: the pill yields to the dot, so nothing renders to
    // measure or fly to. (The Mates row owns its own count and ignores
    // `appBadges`, so the case is drawn on an app row.)
    renderWithProviders(
      <div data-onboarding-nav="app-foo">
        <NavBadge navId="app-foo" collapsed appBadges={{ foo: 3 }} />
      </div>,
      { route: '/' },
    )
    act(() => deliverFeatureNewTag('app-foo', origin))
    expect(tag()).toBeNull()
    expect(document.querySelector('[data-feature-new-tag-flyer]')).toBeNull()
    act(() => { vi.runAllTimers() })
    expect(JSON.parse(localStorage.getItem('mc-feature-new-tags') ?? '{}')).toEqual({ 'app-foo': 'shown' })
    act(() => clearFeatureNewTag('app-foo'))
  })
})
