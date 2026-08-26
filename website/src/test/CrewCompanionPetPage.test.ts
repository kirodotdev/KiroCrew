/**
 * The companion's window is its own page entry, and that has one consequence worth
 * pinning: it does not inherit the dashboard's bootstrap, so it must initialise i18n
 * itself.
 *
 * This is a SOURCE-level assertion rather than a behavioural one, and deliberately.
 * The bug it guards was invisible to every other kind of test: reminders carry their
 * own text and rendered fine, so the page looked healthy, while break nudges — which
 * arrive as a key for this side to translate — resolved to nothing and the companion
 * simply stayed quiet. Unit tests passed because the test environment initialises
 * i18n for them. Only opening the real window revealed it.
 *
 * Asserting on the source is the cheapest thing that fails if someone removes the
 * call while tidying imports.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const SOURCE = readFileSync(
  resolve(__dirname, '../apps/crew-companion/pet.tsx'),
  'utf-8',
)

describe('the companion window page', () => {
  it('initialises i18n', () => {
    expect(SOURCE).toContain('initI18n()')
  })

  it('initialises i18n BEFORE mounting, not after', () => {
    // Order is the whole point: anything rendered ahead of init paints its bare
    // translation key.
    const init = SOURCE.indexOf('initI18n()')
    const mount = SOURCE.indexOf('createRoot(')
    expect(init).toBeGreaterThan(-1)
    expect(mount).toBeGreaterThan(-1)
    expect(init).toBeLessThan(mount)
  })

  it('resolves break nudges through the explicit lookup, never string assembly', () => {
    // A key built by interpolation is invisible to the key checker and renders raw
    // to the user when it misses. This is the regression that shipped once already.
    expect(SOURCE).toContain('nudgeTextFor(')
    expect(SOURCE).not.toMatch(/i18nT\(`/)
    expect(SOURCE).not.toContain('apps.crewCompanion.${')
  })

  it('keeps the overlay click-through by reporting hitboxes, not toggling on hover', () => {
    // The window covers the whole display. The companion reports its rects and the
    // main process polls the cursor and toggles ignore-mouse itself — the old
    // pointer-enter/leave setInteractive round-trip let a fast click fall through.
    expect(SOURCE).toContain('useMouseForward(')
    expect(SOURCE).not.toContain('setInteractive')
  })

  it('pings presence more often than the backend TTL', () => {
    // store.py drops presence after 90s; a slower ping makes the companion think
    // nobody is there and suppresses break nudges.
    const match = SOURCE.match(/PRESENCE_MS\s*=\s*([\d_]+)/)
    expect(match, 'PRESENCE_MS must be declared').toBeTruthy()
    const ms = Number(match![1].replace(/_/g, ''))
    expect(ms).toBeLessThan(90_000)
  })
})
