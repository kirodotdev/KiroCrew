import { describe, it, expect, vi } from 'vitest'
import {
  createActionsProvider,
  type ActionsProviderDeps,
} from './actionsProvider'
import type { Result } from '../types'

/**
 * Unit tests for the pure {@link createActionsProvider} factory
 * (Search Everywhere, step 12). The Actions provider is a static list (New
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
