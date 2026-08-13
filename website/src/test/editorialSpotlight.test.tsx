/**
 * Featured cards: curator artwork wins, and each app carries its own control.
 *
 * The artwork assertions are about BYTES, not CSS — which URL ends up in the
 * `src` — so they are written against the rendered `img` rather than a class.
 *
 * The per-row assertions are the ones that matter for a collection: a member the
 * reader cannot install is just a picture of an app.
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
      type={props.type || 'app'}
      apps={props.apps || [app('hero-app')]}
      onOpenApp={noop}
      onGet={noop}
      onEnable={noop}
      {...props}
    />,
  )
}

/** A minimal collection: the smallest shape the schema admits. */
function mountCollection(props: Partial<Parameters<typeof FeaturedSpotlight>[0]> = {}) {
  return mount({
    type: 'collection',
    apps: [app('first'), app('second')],
    title: 'Ship it before lunch',
    ...props,
  })
}

beforeEach(() => {
  mockTheme.value = 'light'
})

describe('editorial artwork', () => {
  it('uses the curator artwork over the app own hero image', () => {
    mount({
      apps: [app('hero-app', { heroImage: 'https://app.example/hero.png' } as Partial<RegistryApp>)],
      artwork: { url: 'https://apps.crew.kiro.dev/assets/editorial/aaa.png' },
    })
    // Query the image directly: the card carries `role="presentation"` wrappers
    // around each row's control, so a role query is ambiguous here.
    const img = document.querySelector('img') as HTMLImageElement
    expect(img.getAttribute('src')).toContain('assets/editorial/aaa.png')
    expect(img.getAttribute('src')).not.toContain('app.example')
  })

  it('falls back to the app hero image when there is no artwork', () => {
    mount({ apps: [app('hero-app', { heroImage: '/app-assets/x/hero.png' } as Partial<RegistryApp>)] })
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

  it('shares one art fallback across both types', () => {
    // A collection has no hero app, so its fallback art comes from the lead
    // entry. Pinned because it is the one place the lead is still privileged.
    mountCollection({ apps: [app('first', { heroImage: '/app-assets/f/h.png' } as Partial<RegistryApp>), app('second')] })
    const img = document.querySelector('img') as HTMLImageElement
    expect(img.getAttribute('src')).toContain('/app-assets/f/h.png')
  })
})

describe('an app card', () => {
  it('is headed by the app own name', () => {
    mount({ apps: [app('hero-app')] })
    expect(screen.getByRole('heading', { name: 'Hero App' })).toBeTruthy()
  })

  it('prefers the curator blurb over the app description', () => {
    mount({ apps: [app('hero-app')], blurb: 'Three of them, hand picked.' })
    expect(screen.getByText('Three of them, hand picked.')).toBeTruthy()
    expect(screen.queryByText('About hero-app.')).toBeNull()
  })

  it('shows the app provenance on its row', () => {
    mount({ apps: [app('hero-app', { tags: ['research'] })] })
    expect(screen.getByText(/Research & Writing/)).toBeTruthy()
    expect(screen.getByText(/Kiro Crew/)).toBeTruthy()
  })

  it('renders exactly one row', () => {
    mount({ apps: [app('hero-app')] })
    expect(screen.getAllByRole('button', { name: /Get/ }).length).toBe(1)
  })
})

describe('a collection card', () => {
  it('is headed by the curator theme, not any member name', () => {
    mountCollection()
    expect(screen.getByRole('heading', { name: 'Ship it before lunch' })).toBeTruthy()
  })

  it('renders one row per member, in the published order', () => {
    mountCollection({ apps: [app('alpha'), app('beta'), app('gamma')] })
    const names = Array.from(document.querySelectorAll('h2, .truncate'))
      .map(n => n.textContent)
      .filter(t => t === 'Alpha' || t === 'Beta' || t === 'Gamma')
    expect(names).toEqual(['Alpha', 'Beta', 'Gamma'])
  })

  it('gives every member its own install control', () => {
    // The whole point of rows over chips: a card with one button could only ever
    // install one member, and which one would be an accident of ordering.
    mountCollection({ apps: [app('alpha'), app('beta'), app('gamma')] })
    expect(screen.getAllByRole('button', { name: /Get/ }).length).toBe(3)
  })

  it('reflects each member own install state', () => {
    mountCollection({ apps: [app('alpha', { installed: true }), app('beta')] })
    expect(screen.getAllByRole('button', { name: /Get/ }).length).toBe(1)
    expect(screen.getByText(/Installed/)).toBeTruthy()
  })

  it('offers Enable for a disabled builtin rather than Get', () => {
    mountCollection({
      apps: [app('alpha', { origin: 'builtin', installed: true, enabled: false } as Partial<RegistryApp>), app('beta')],
    })
    expect(screen.getByRole('button', { name: /Enable/ })).toBeTruthy()
  })

  it('installs the member whose button was pressed', () => {
    const got: string[] = []
    mountCollection({ apps: [app('alpha'), app('beta')], onGet: (name: string) => got.push(name) })
    screen.getAllByRole('button', { name: /Get/ })[1].click()
    expect(got).toEqual(['beta'])
  })

  it('disables only the busy member control', () => {
    // A card-level busy flag would freeze every row while one install runs.
    mountCollection({ apps: [app('alpha'), app('beta')], busyName: 'alpha' })
    const buttons = screen.getAllByRole('button', { name: /Get/ }) as HTMLButtonElement[]
    expect(buttons[0].disabled).toBe(true)
    expect(buttons[1].disabled).toBe(false)
  })

  it('opens a member by its own name', () => {
    const opened: string[] = []
    mountCollection({
      apps: [app('alpha'), app('beta')],
      onOpenApp: (name: string) => opened.push(name),
    })
    ;(screen.getByText('Beta').closest('[role="button"], button, a') as HTMLElement)?.click()
    expect(opened).toEqual(['beta'])
  })

  it('is not itself clickable, because there is no collection page to open', () => {
    // A card that looked clickable and did nothing would be worse than one that
    // plainly is not. Only the rows carry interaction.
    const opened: string[] = []
    const { container } = mountCollection({ onOpenApp: (name: string) => opened.push(name) })
    const card = container.firstElementChild as HTMLElement
    expect(card.getAttribute('role')).not.toBe('button')
    card.click()
    expect(opened).toEqual([])
  })

  it('describes each member on its row rather than repeating the theme', () => {
    mountCollection({ apps: [app('alpha'), app('beta')] })
    expect(screen.getByText('About alpha.')).toBeTruthy()
    expect(screen.getByText('About beta.')).toBeTruthy()
  })
})

describe('exactly one interactive layer per card', () => {
  it('opens a single-app card ONCE, not once per nested target', () => {
    // The card is the click target on an `app` card, so the row body must stay
    // inert. A nested clickable bubbles into the card's handler: two history
    // entries on a plain click, and two browser tabs on a modified one.
    const opened: string[] = []
    const { container } = mount({
      apps: [app('hero-app')],
      onOpenApp: (name: string) => opened.push(name),
    })
    // Exactly one button carries the open affordance, and clicking the row's
    // identity block must reach it once. Query the row icon rather than the name,
    // which the heading also renders.
    const targets = container.querySelectorAll('[role="button"]')
    expect(targets.length).toBe(1)
    ;(targets[0] as HTMLElement).click()
    expect(opened).toEqual(['hero-app'])
  })

  it('nests no role=button inside another on either card type', () => {
    // ARIA flattens a button's children, so a nested one is not independently
    // announced and axe reports nested-interactive.
    for (const mounted of [mount({ apps: [app('solo')] }), mountCollection()]) {
      const nested = mounted.container.querySelectorAll('[role="button"] [role="button"]')
      expect(nested.length).toBe(0)
      mounted.unmount()
    }
  })

  it('keeps every collection row independently clickable', () => {
    const opened: string[] = []
    mountCollection({
      apps: [app('alpha'), app('beta')],
      onOpenApp: (name: string) => opened.push(name),
    })
    const rows = screen.getAllByRole('button', { name: /View details for/ })
    expect(rows.length).toBe(2)
  })

  it('renders nothing rather than throwing when the app list is empty', () => {
    // `lead` feeds an unconditional `.name`; nothing read from the document may
    // cost the page its render.
    const { container } = mount({ apps: [] })
    expect(container.firstElementChild).toBeNull()
  })
})
