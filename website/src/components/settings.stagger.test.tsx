import { render } from '@testing-library/react'
import { SettingsCard, SETTINGS_CARD_STAGGER_MS } from './settings'

/**
 * Locks in the Settings-card entrance stagger (issue #947): cards carrying an
 * `index` must rise on the same 60ms delay ladder the Overview stat tiles use,
 * so successive cards receive strictly increasing animation delays instead of
 * all rising at once.
 */
describe('SettingsCard entrance stagger', () => {
  it('gives successive cards strictly increasing animation delays on the shared ladder', () => {
    const { container } = render(
      <>
        <SettingsCard index={0}>first</SettingsCard>
        <SettingsCard index={1}>second</SettingsCard>
        <SettingsCard index={2}>third</SettingsCard>
        <SettingsCard index={3}>fourth</SettingsCard>
      </>,
    )
    const cards = Array.from(container.children) as HTMLElement[]
    expect(cards).toHaveLength(4)

    // Every card keeps the shared rise entrance — the stagger reuses the
    // existing animation, it does not introduce a second system.
    for (const card of cards) expect(card.className).toContain('animate-rise')

    const delayMs = (card: HTMLElement) =>
      card.style.animationDelay === '' ? 0 : parseInt(card.style.animationDelay, 10)

    for (let i = 1; i < cards.length; i++) {
      expect(delayMs(cards[i])).toBeGreaterThan(delayMs(cards[i - 1]))
    }
    // The ladder is the Overview one: index * SETTINGS_CARD_STAGGER_MS.
    cards.forEach((card, i) => expect(delayMs(card)).toBe(i * SETTINGS_CARD_STAGGER_MS))
  })

  it('renders no inline delay for an unindexed card, exactly as before the prop existed', () => {
    const { container } = render(<SettingsCard>only</SettingsCard>)
    const card = container.firstElementChild as HTMLElement
    expect(card.style.animationDelay).toBe('')
    expect(card.className).toContain('animate-rise')
  })
})

/**
 * Panel-level guard: the ordinals are hand-written literals spread across the
 * panel files, so a future card inserted without renumbering its siblings
 * would ship a duplicated or inverted delay against a green primitive-only
 * suite. Assert a real panel's cards carry non-decreasing delays in document
 * order. ShortcutsPanel is used because it renders without network fetches.
 */
describe('settings panel stagger ordering', () => {
  it('ShortcutsPanel cards have non-decreasing animation delays in document order', async () => {
    const { ShortcutsPanel } = await import('../pages/settings/ShortcutsPanel')
    const { renderWithProviders } = await import('../test/helpers')
    const { container } = renderWithProviders(<ShortcutsPanel />)
    const cards = Array.from(container.querySelectorAll('.animate-rise')) as HTMLElement[]
    expect(cards.length).toBeGreaterThan(2)
    const delays = cards.map(c => (c.style.animationDelay === '' ? 0 : parseInt(c.style.animationDelay, 10)))
    for (let i = 1; i < delays.length; i++) {
      expect(delays[i]).toBeGreaterThanOrEqual(delays[i - 1])
    }
    // And at least one card is actually delayed — the ladder exists.
    expect(Math.max(...delays)).toBeGreaterThan(0)
  })
})
