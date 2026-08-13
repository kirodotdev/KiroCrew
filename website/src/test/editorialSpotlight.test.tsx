/**
 * Editorial spotlights: curator artwork wins, a group renders as one placement.
 *
 * The artwork assertions are about BYTES, not CSS — which URL ends up in the
 * `src` — so they are written against the rendered `img` rather than a class.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import FeaturedSpotlight from '../components/appstore/FeaturedSpotlight'
import type { RegistryApp } from '../components/appstore/types'

const mockTheme = vi.hoisted(() => ({ value: 'light' as 'light' | 'dark' }))
vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({ theme: mockTheme.value }),
}))

const app = (name: string, over: Partial<RegistryApp> = {}): RegistryApp => ({
  name,
  displayName: name.replace(/(^|-)(\w)/g, (_, s, c) => (s ? ' ' : '') + c.toUpperCase()).trim(),
  description: `About ${name}.`,
  author: 'Kiro Crew',
  version: '1.0.0',
  tags: ['agents'],
  installed: false,
  ...over,
} as RegistryApp)

const noop = () => {}

function mount(props: Partial<Parameters<typeof FeaturedSpotlight>[0]> = {}) {
  return render(
    <FeaturedSpotlight
      app={props.app || app('hero-app')}
      onOpen={noop}
      onGet={noop}
      onEnable={noop}
      {...props}
    />,
  )
}

beforeEach(() => {
  mockTheme.value = 'light'
})

describe('editorial artwork', () => {
  it('uses the curator artwork over the app own hero image', () => {
    mount({
      app: app('hero-app', { heroImage: 'https://app.example/hero.png' } as Partial<RegistryApp>),
      artwork: { url: 'https://apps.crew.kiro.dev/assets/editorial/aaa.png' },
    })
    // Query the image directly: the page carries several `role="presentation"`
    // wrappers (the CTA row, the chip row), so a role query is ambiguous here.
    const img = document.querySelector('img') as HTMLImageElement
    expect(img.getAttribute('src')).toContain('assets/editorial/aaa.png')
    expect(img.getAttribute('src')).not.toContain('app.example')
  })

  it('falls back to the app hero image when there is no artwork', () => {
    mount({ app: app('hero-app', { heroImage: '/app-assets/x/hero.png' } as Partial<RegistryApp>) })
    const img = document.querySelector('img') as HTMLImageElement
    expect(img.getAttribute('src')).toContain('/app-assets/x/hero.png')
  })

  it('picks the dark variant under a dark theme', () => {
    mockTheme.value = 'dark'
    mount({
      artwork: {
        url: 'https://apps.crew.kiro.dev/assets/editorial/light.png',
        urlDark: 'https://apps.crew.kiro.dev/assets/editorial/dark.png',
      },
    })
    const img = document.querySelector('img') as HTMLImageElement
    expect(img.getAttribute('src')).toContain('dark.png')
  })

  it('reuses the light bytes when no dark variant is published', () => {
    mockTheme.value = 'dark'
    mount({ artwork: { url: 'https://apps.crew.kiro.dev/assets/editorial/only.png' } })
    const img = document.querySelector('img') as HTMLImageElement
    expect(img.getAttribute('src')).toContain('only.png')
  })

  it('falls back the OTHER way, so a light theme still gets art from a dark-only ref', () => {
    // The server drops dark-only artwork, but the component must not depend on
    // that: two guards, and this one is cheap.
    mockTheme.value = 'light'
    mount({ artwork: { url: '', urlDark: 'https://apps.crew.kiro.dev/assets/editorial/d.png' } as never })
    const img = document.querySelector('img') as HTMLImageElement
    expect(img.getAttribute('src')).toContain('d.png')
  })

  it('carries the published alt text', () => {
    mount({
      artwork: { url: 'https://apps.crew.kiro.dev/assets/editorial/a.png', alt: 'A quiet timeline' },
    })
    expect(screen.getByAltText('A quiet timeline')).toBeTruthy()
  })

  it('uses an empty alt when the artwork is decorative', () => {
    mount({ artwork: { url: 'https://apps.crew.kiro.dev/assets/editorial/a.png' } })
    const img = document.querySelector('img') as HTMLImageElement
    expect(img.getAttribute('alt')).toBe('')
  })
})

describe('a spotlight holding a group', () => {
  it('shows the curator title instead of the hero app name', () => {
    mount({ app: app('hero-app'), apps: [app('second')], title: 'Staff picks' })
    expect(screen.getByText('Staff picks')).toBeTruthy()
    expect(screen.queryByText('Hero App')).toBeNull()
  })

  it('shows the app name when no title is published', () => {
    mount({ app: app('hero-app') })
    expect(screen.getByText('Hero App')).toBeTruthy()
  })

  it('prefers the curator blurb over the app description', () => {
    mount({ app: app('hero-app'), blurb: 'Three of them, hand picked.' })
    expect(screen.getByText('Three of them, hand picked.')).toBeTruthy()
    expect(screen.queryByText('About hero-app.')).toBeNull()
  })

  it('renders one chip per companion app', () => {
    mount({ app: app('hero-app'), apps: [app('second'), app('third')] })
    expect(screen.getByText('Second')).toBeTruthy()
    expect(screen.getByText('Third')).toBeTruthy()
  })

  it('renders no chips for a single-app spotlight', () => {
    const { container } = mount({ app: app('hero-app'), apps: [] })
    // The hero app itself must not appear as its own companion chip.
    expect(container.querySelectorAll('[class*="rounded-full"][class*="bg-elevated"]').length).toBe(0)
  })

  it('does not label a group with the hero app category', () => {
    // The meta row derives from the hero app. For a cross-cutting group that
    // would print one member's category as if it described the collection.
    mount({ app: app('hero-app', { tags: ['research'] }), apps: [app('second')], title: 'Ship it before lunch' })
    expect(screen.getByText('Kiro Crew')).toBeTruthy()
    expect(screen.queryByText(/Research & Writing/)).toBeNull()
  })

  it('still labels a single-app spotlight with its category', () => {
    mount({ app: app('hero-app', { tags: ['research'] }) })
    expect(screen.getByText(/Research & Writing/)).toBeTruthy()
  })

  it('opens a companion by its own name, not the hero one', () => {
    const opened: string[] = []
    mount({
      app: app('hero-app'),
      apps: [app('second')],
      onOpenApp: (name: string) => opened.push(name),
    })
    ;(screen.getByText('Second').closest('[role="button"], button, a') as HTMLElement)?.click()
    expect(opened).toEqual(['second'])
  })
})
