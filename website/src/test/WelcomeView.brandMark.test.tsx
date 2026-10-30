import { screen, waitFor } from '@testing-library/react'

// Bare-factory mock, matching useTheme.renderCache.test.tsx: the ThemeProvider
// loads the active installed pack through these, and the welcome grid's
// suggestions resolve empty so it renders the built-in cards without a notice.
const themesFn = vi.fn()
const themeDetailFn = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    themes: () => themesFn(),
    themeDetail: (slug: string) => themeDetailFn(slug),
    themeBoot: () => new Promise(() => {}),
    suggestions: () => Promise.resolve({ suggestions: [] }),
  },
}))

// Compiled edition branding is a module-load registry; the stub lets a test
// stand in for a registered theme without registering one globally.
const getThemeBrandingFn = vi.fn()
vi.mock('../themeBranding', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../themeBranding')>()),
  getThemeBranding: (slug: string) => getThemeBrandingFn(slug),
}))

import WelcomeView from '../components/WelcomeView'
import { renderWithProviders } from './helpers'

const SLUG = 'harbor'
const ASSETS = `/api/theme/${SLUG}/assets`

function installPack(branding: Record<string, string>) {
  localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
  themesFn.mockResolvedValue({
    themes: [{ slug: SLUG, name: 'Harbor', emoji: 'H', source: 'installed' }],
  })
  themeDetailFn.mockResolvedValue({
    slug: SLUG,
    name: 'Harbor',
    emoji: 'H',
    level: 1,
    source: 'installed',
    dark: { '--bg': '#000000' },
    light: { '--bg': '#ffffff' },
    assets: { branding },
  })
}

/** The hero block above the cards: the greeting plus its brand mark. */
function heroBlock(): HTMLElement {
  return screen.getByTestId('welcome-layout').children[0] as HTMLElement
}

describe('WelcomeView brand mark', () => {
  beforeEach(() => {
    localStorage.clear()
    themesFn.mockReset()
    themeDetailFn.mockReset()
    getThemeBrandingFn.mockReset()
    getThemeBrandingFn.mockReturnValue(undefined)
  })

  afterEach(() => {
    localStorage.clear()
    document.getElementById('mc-theme-favicon')?.remove()
    document.documentElement.style.removeProperty('--theme-logo')
  })

  it("shows an installed pack's welcome image ahead of its logo", async () => {
    installPack({ logo: 'branding/logo.svg', welcome: 'branding/welcome.webp' })
    renderWithProviders(<WelcomeView setInput={vi.fn()} />)
    await waitFor(() =>
      expect(heroBlock().querySelector('img')).toHaveAttribute('src', `${ASSETS}/branding/welcome.webp`),
    )
    const img = heroBlock().querySelector('img')!
    // Decorative: the greeting carries the meaning.
    expect(img).toHaveAttribute('alt', '')
    expect(img).toHaveAttribute('aria-hidden', 'true')
    expect(heroBlock().querySelector('svg')).toBeNull()
  })

  it("falls back to the pack's logo when it ships no welcome image", async () => {
    installPack({ logo: 'branding/logo.svg' })
    renderWithProviders(<WelcomeView setInput={vi.fn()} />)
    await waitFor(() =>
      expect(heroBlock().querySelector('img')).toHaveAttribute('src', `${ASSETS}/branding/logo.svg`),
    )
  })

  it('keeps the stock ghost for a pack that ships no logo or welcome image', async () => {
    installPack({ favicon: 'branding/favicon.svg' })
    renderWithProviders(<WelcomeView setInput={vi.fn()} />)
    // The favicon link is written by the same branding pass that resolves the
    // logo and welcome image, so once it exists that pass has run.
    await waitFor(() => expect(document.getElementById('mc-theme-favicon')).not.toBeNull())
    expect(heroBlock().querySelector('img')).toBeNull()
    expect(heroBlock().querySelector('svg')).not.toBeNull()
  })

  it('lets compiled edition branding win over the installed pack', async () => {
    installPack({ logo: 'branding/logo.svg', welcome: 'branding/welcome.webp' })
    getThemeBrandingFn.mockReturnValue({ logo: '/edition/logo.png' })
    renderWithProviders(<WelcomeView setInput={vi.fn()} />)
    // `--theme-logo` is set once the pack's branding has been applied.
    await waitFor(() =>
      expect(document.documentElement.style.getPropertyValue('--theme-logo')).not.toBe(''),
    )
    expect(heroBlock().querySelector('img')).toHaveAttribute('src', '/edition/logo.png')
  })
})
