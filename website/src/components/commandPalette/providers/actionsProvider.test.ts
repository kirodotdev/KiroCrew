import { afterEach, describe, it, expect, vi } from 'vitest'
import {
  createActionsProvider,
  type ActionsProviderDeps,
} from './actionsProvider'
import type { Result } from '../types'
// `/all` registers the 11 authored catalogs; `../i18n` alone is English-only.
import { i18next } from '../../../i18n/all'
import { i18nT } from '../../../i18n/t'

/**
 * Unit tests for the pure {@link createActionsProvider} factory.
 * The Actions provider is a static list (New
 * session · Toggle theme · Open Shortcuts) whose side effects are injected, so
 * we pass plain spies and assert filtering + activation wiring.
 */

function deps(): {
  d: ActionsProviderDeps
  newSession: ReturnType<typeof vi.fn>
  toggleTheme: ReturnType<typeof vi.fn>
  openShortcuts: ReturnType<typeof vi.fn>
} {
  const newSession = vi.fn()
  const toggleTheme = vi.fn()
  const openShortcuts = vi.fn()
  return { d: { newSession, toggleTheme, openShortcuts }, newSession, toggleTheme, openShortcuts }
}

async function run(
  p: ReturnType<typeof createActionsProvider>,
  q: string,
): Promise<Result[]> {
  return Promise.resolve(p.search(q))
}

describe('createActionsProvider — identity', () => {
  it('exposes the actions provider id, label, and an icon node', () => {
    const { d } = deps()
    const p = createActionsProvider(d)
    expect(p.id).toBe('actions')
    expect(p.label).toBe('Actions')
    expect(p.icon).toBeTruthy()
  })
})

describe('createActionsProvider — empty query', () => {
  it('lists all three actions, ordered by name on the neutral-score tie', async () => {
    const { d } = deps()
    const p = createActionsProvider(d)
    const arr = await run(p, '')
    expect(arr.map((r) => r.title)).toEqual(['New session', 'Open Shortcuts', 'Toggle theme'])
    expect(arr.every((r) => r.providerId === 'actions')).toBe(true)
  })
})

describe('createActionsProvider — activation', () => {
  it('runs newSession on the New session action', async () => {
    const { d, newSession } = deps()
    const arr = await run(createActionsProvider(d), '')
    arr.find((r) => r.title === 'New session')!.onActivate()
    expect(newSession).toHaveBeenCalledTimes(1)
  })

  it('runs toggleTheme on the Toggle theme action', async () => {
    const { d, toggleTheme } = deps()
    const arr = await run(createActionsProvider(d), '')
    arr.find((r) => r.title === 'Toggle theme')!.onActivate()
    expect(toggleTheme).toHaveBeenCalledTimes(1)
  })

  it('runs openShortcuts on the Open Shortcuts action', async () => {
    const { d, openShortcuts } = deps()
    const arr = await run(createActionsProvider(d), '')
    arr.find((r) => r.title === 'Open Shortcuts')!.onActivate()
    expect(openShortcuts).toHaveBeenCalledTimes(1)
  })

  it('leaves ⌘Enter / ⌥Enter unbound (Actions are pure invocations)', async () => {
    const { d } = deps()
    const arr = await run(createActionsProvider(d), '')
    for (const r of arr) {
      expect(r.onCmdActivate).toBeUndefined()
      expect(r.onAltActivate).toBeUndefined()
    }
  })
})

describe('createActionsProvider — pin current session', () => {
  it('omits the pin action when there is no active session', async () => {
    const { d } = deps()
    const arr = await run(createActionsProvider({ ...d, pinCurrentSession: null }), '')
    const titles = arr.map((r) => r.title)
    expect(titles).not.toContain('Pin current session')
    expect(titles).not.toContain('Unpin current session')
  })

  it('shows "Pin current session" when the active session is unpinned', async () => {
    const { d } = deps()
    const toggle = vi.fn()
    const arr = await run(
      createActionsProvider({ ...d, pinCurrentSession: { pinned: false, toggle } }),
      'pin',
    )
    expect(arr.some((r) => r.title === 'Pin current session')).toBe(true)
    expect(arr.some((r) => r.title === 'Unpin current session')).toBe(false)
  })

  it('shows "Unpin current session" when the active session is pinned', async () => {
    const { d } = deps()
    const toggle = vi.fn()
    const arr = await run(
      createActionsProvider({ ...d, pinCurrentSession: { pinned: true, toggle } }),
      'pin',
    )
    expect(arr.some((r) => r.title === 'Unpin current session')).toBe(true)
    expect(arr.some((r) => r.title === 'Pin current session')).toBe(false)
  })

  it('runs the injected toggle on activation', async () => {
    const { d } = deps()
    const toggle = vi.fn()
    const arr = await run(
      createActionsProvider({ ...d, pinCurrentSession: { pinned: false, toggle } }),
      '',
    )
    arr.find((r) => r.title === 'Pin current session')!.onActivate()
    expect(toggle).toHaveBeenCalledTimes(1)
  })
})

describe('createActionsProvider — filtering', () => {
  it('filters to actions whose title matches the query', async () => {
    const { d } = deps()
    const arr = await run(createActionsProvider(d), 'theme')
    expect(arr).toHaveLength(1)
    expect(arr[0].title).toBe('Toggle theme')
    expect(arr[0].score).toBeGreaterThan(0)
    expect(arr[0].indices.length).toBeGreaterThan(0)
  })

  it('returns nothing when no action title matches', async () => {
    const { d } = deps()
    expect(await run(createActionsProvider(d), 'zzzqqq')).toEqual([])
  })
})

const KEYS = 'components.commandPalette.providers.actionsProvider'

describe('createActionsProvider — rows follow the active language', () => {
  afterEach(async () => { await i18next.changeLanguage('en') })

  const rowOf = (results: Result[], key: string) => {
    const row = results.find((r) => r.id === `actions:${key}`)
    return { title: row?.title, subtitle: row?.subtitle }
  }

  /** The catalog's title and subtitle for a row, read in the language active now. */
  const catalogText = (titleKey: string, subtitleKey: string) => ({
    title: i18nT(`${KEYS}.${titleKey}`),
    subtitle: i18nT(`${KEYS}.${subtitleKey}`),
  })

  it.each([
    ['new-session', 'new_session', 'start_a_fresh_chat'],
    ['toggle-theme', 'toggle_theme', 'cycle_light_dark_system'],
    ['open-shortcuts', 'open_shortcuts', 'keyboard_shortcuts_help'],
  ])('resolves the %s row when searching, not when the provider is built', async (row, titleKey, subtitleKey) => {
    const { d } = deps()
    // Built under English, like the provider `useActionsProvider` memoizes; the
    // language changes afterwards, as it does from Settings → Display.
    const p = createActionsProvider(d)
    const english = catalogText(titleKey, subtitleKey)
    await i18next.changeLanguage('pt')
    const portuguese = catalogText(titleKey, subtitleKey)
    // Without these, a provider that froze English would still match the catalog.
    expect(portuguese.title).not.toBe(english.title)
    expect(portuguese.subtitle).not.toBe(english.subtitle)
    expect(rowOf(await run(p, ''), row)).toEqual(portuguese)
  })

  it.each([
    [false, 'pin_current_session', 'pin_this_chat_to_the_top'],
    [true, 'unpin_current_session', 'remove_pin_from_this_chat'],
  ])('resolves the pin row when searching, pinned: %s', async (pinned, titleKey, subtitleKey) => {
    const { d } = deps()
    const p = createActionsProvider({ ...d, pinCurrentSession: { pinned, toggle: vi.fn() } })
    const english = catalogText(titleKey, subtitleKey)
    await i18next.changeLanguage('pt')
    const portuguese = catalogText(titleKey, subtitleKey)
    expect(portuguese.title).not.toBe(english.title)
    expect(portuguese.subtitle).not.toBe(english.subtitle)
    expect(rowOf(await run(p, ''), 'pin-session')).toEqual(portuguese)
  })

  it('matches the query against the translated title', async () => {
    const { d } = deps()
    await i18next.changeLanguage('pt')
    const results = await run(createActionsProvider(d), i18nT(`${KEYS}.toggle_theme`))
    expect(results.map((r) => r.id)).toEqual(['actions:toggle-theme'])
  })
})
