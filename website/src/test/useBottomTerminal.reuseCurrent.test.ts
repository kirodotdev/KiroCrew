/**
 * `reuseCurrentTab()` — the "reuse the current terminal" primitive behind the
 * opt-in dashboard.terminal.reuse_current setting (issue #11641).
 *
 * The contract Run-in-terminal relies on:
 *  - with NO terminal open it returns null and mints nothing, so the caller
 *    falls back to opening a fresh tab (there is no shell to reuse);
 *  - with terminals open it returns the FOCUSED current one, so the user can
 *    choose the shell that holds their working directory, env, and login;
 *  - it does not add or reorder tabs, and it opens the panel for visibility.
 *
 * A stale active id falls back to the last tab, preserving the older cap
 * fallback's safe behavior. The primary path must be the focused tab: using
 * newest here would silently send an `awsume` command into a different shell.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import {
  __resetBottomTerminal, addTab, reuseCurrentTab, setActiveTab, useBottomTerminal,
} from '../hooks/useBottomTerminal'

beforeEach(() => { __resetBottomTerminal() })
afterEach(() => { __resetBottomTerminal() })

describe('reuseCurrentTab', () => {
  it('returns null and mints nothing when no terminal is open', () => {
    const store = renderHook(() => useBottomTerminal())
    expect(store.result.current.tabs).toHaveLength(0)

    let reused: string | null = 'x'
    act(() => { reused = reuseCurrentTab() })

    expect(reused).toBeNull()
    // No PTY minted: the fresh-tab fallback is the caller's job, not this
    // helper's.
    expect(store.result.current.tabs).toHaveLength(0)
    expect(store.result.current.open).toBe(false)
  })

  it('reuses the single existing terminal and focuses it', () => {
    const store = renderHook(() => useBottomTerminal())
    let only = ''
    act(() => { only = addTab() ?? '' })
    expect(only).toBeTruthy()

    let reused: string | null = null
    act(() => { reused = reuseCurrentTab() })

    expect(reused).toBe(only)
    // Reused, not minted: still exactly one tab.
    expect(store.result.current.tabs).toHaveLength(1)
    expect(store.result.current.activeId).toBe(only)
    expect(store.result.current.open).toBe(true)
  })

  it('reuses the FOCUSED terminal when several are open, without adding one', () => {
    const store = renderHook(() => useBottomTerminal())
    const ids: string[] = []
    act(() => { ids.push(addTab() ?? '') })
    act(() => { ids.push(addTab() ?? '') })
    act(() => { ids.push(addTab() ?? '') })
    expect(new Set(ids).size).toBe(3)
    // The user picked their older login shell. A "newest tab" implementation
    // would return ids[2] and make this assertion red.
    act(() => { setActiveTab(ids[0]) })
    const before = store.result.current.tabs.map(t => t.id)

    let reused: string | null = null
    act(() => { reused = reuseCurrentTab() })

    expect(reused).toBe(ids[0])
    expect(store.result.current.activeId).toBe(ids[0])
    // The tab list is unchanged — no new PTY, no reordering.
    expect(store.result.current.tabs.map(t => t.id)).toEqual(before)
  })
})
