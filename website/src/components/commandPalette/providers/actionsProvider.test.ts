import { describe, it, expect, vi } from 'vitest'
import {
  createActionsProvider,
  type ActionsProviderDeps,
} from './actionsProvider'
import type { Result } from '../types'

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

/**
 * The "New session" and "Toggle theme" TITLES — not just the subtitles — must
 * resolve through the i18n catalog AND re-resolve on a LIVE language switch.
 *
 * The provider is built once inside a `useMemo` whose deps do not include the
 * language, and `LanguageProvider` re-renders via `cloneElement` without
 * remounting (see `get label()` in the source). So resolving `i18nT()` eagerly
 * when the action list is built would freeze the title in whatever language was
 * active at build time. The titles are therefore getters, read at `search()`
 * time, exactly like the `label` getter. The second test below pins that: it
 * switches the language on the SAME provider instance and asserts the titles
 * follow, which an eager (frozen) implementation would fail.
 */
describe('createActionsProvider — localized titles', () => {
  it('resolves the New session / Toggle theme titles through the catalog per language', async () => {
    const { i18next } = await import('../../../i18n/all')
    try {
      // English baseline: titles come from the catalog, not a literal.
      await i18next.changeLanguage('en')
      const en = await run(createActionsProvider(deps().d), '')
      const enTitles = en.map((r) => r.title)
      expect(enTitles).toContain('New session')
      expect(enTitles).toContain('Toggle theme')

      // A different language must change both titles — proof they are not
      // captured once in English.
      await i18next.changeLanguage('ja')
      const ja = await run(createActionsProvider(deps().d), '')
      const jaTitles = ja.map((r) => r.title)
      expect(jaTitles).toContain('新しいセッション')
      expect(jaTitles).toContain('テーマを切り替え')
      expect(jaTitles).not.toContain('New session')
      expect(jaTitles).not.toContain('Toggle theme')
    } finally {
      await i18next.changeLanguage('en')
    }
  })

  it('re-resolves the two titles on a live language switch of the SAME provider', async () => {
    const { i18next } = await import('../../../i18n/all')
    try {
      // Build the provider ONCE (as the memo does) while English is active.
      await i18next.changeLanguage('en')
      const p = createActionsProvider(deps().d)
      const before = (await run(p, '')).map((r) => r.title)
      expect(before).toContain('New session')
      expect(before).toContain('Toggle theme')

      // Switch language WITHOUT rebuilding the provider — a `cloneElement`
      // re-render, not a remount. The titles must follow the new language; an
      // eager `i18nT()` captured at build time would stay English here.
      await i18next.changeLanguage('ja')
      const after = (await run(p, '')).map((r) => r.title)
      expect(after).toContain('新しいセッション')
      expect(after).toContain('テーマを切り替え')
      expect(after).not.toContain('New session')
      expect(after).not.toContain('Toggle theme')

      // And back again, still the same instance.
      await i18next.changeLanguage('en')
      const back = (await run(p, '')).map((r) => r.title)
      expect(back).toContain('New session')
      expect(back).toContain('Toggle theme')
    } finally {
      await i18next.changeLanguage('en')
    }
  })
})
