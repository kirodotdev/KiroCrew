import { act, render } from '@testing-library/react'

import { useCommandPalette, type UseCommandPalette } from './useCommandPalette'

/**
 * The palette's open state, and the one thing that must close it besides the user.
 *
 * The activation gestures themselves are covered by `lib/quickSearchShortcut.test.ts`
 * (which chord matches) and by the surface tests (what the open palette renders).
 * What lives here is the state machine: a gesture that changes what the dashboard is
 * SHOWING has to take the modal floating over it with it.
 */

/** The instances slice the hook reads. Mutated per test, like the real store. */
let instances:
  | { activeId: string | null; host?: { activeId: string | null } | null }
  | undefined = { activeId: null }

vi.mock('../store', () => ({
  useAppSelector: (fn: (s: unknown) => unknown) => fn({ instances }),
}))

function harness() {
  const seen: UseCommandPalette[] = []
  function Probe() {
    seen.push(useCommandPalette())
    return null
  }
  const view = render(<Probe />)
  return {
    latest: () => seen[seen.length - 1],
    rerender: () => view.rerender(<Probe />),
  }
}

describe('useCommandPalette', () => {
  beforeEach(() => {
    instances = { activeId: null }
    localStorage.clear()
  })

  it('opens, closes and toggles', () => {
    const h = harness()
    expect(h.latest().open).toBe(false)
    act(() => h.latest().openPalette())
    expect(h.latest().open).toBe(true)
    act(() => h.latest().close())
    expect(h.latest().open).toBe(false)
    act(() => h.latest().toggle())
    expect(h.latest().open).toBe(true)
  })

  it('closes itself when the dashboard switches to another pane', () => {
    // ⌘/Ctrl+digit switches the visible pane from anywhere, including from behind an
    // open palette — so the modal was left covering the instance the user had just
    // asked to see, listing the previous one's sessions.
    const h = harness()
    act(() => h.latest().openPalette())
    expect(h.latest().open).toBe(true)
    act(() => {
      instances = { activeId: 'remote-1' }
      h.rerender()
    })
    expect(h.latest().open).toBe(false)
  })

  it('closes itself when an embedded host model switches panes', () => {
    instances = { activeId: null, host: { activeId: 'remote-1' } }
    const h = harness()
    act(() => h.latest().openPalette())
    expect(h.latest().open).toBe(true)
    act(() => {
      instances = { activeId: null, host: { activeId: 'remote-2' } }
      h.rerender()
    })
    expect(h.latest().open).toBe(false)
  })

  it('uses the first asynchronously relayed host model as the embedded baseline', () => {
    instances = { activeId: null, host: null }
    const h = harness()
    act(() => h.latest().openPalette())
    act(() => {
      instances = { activeId: null, host: { activeId: 'remote-1' } }
      h.rerender()
    })
    expect(h.latest().open).toBe(true)
  })

  it('closes on the way back to Local as well', () => {
    // Local is a pane like any other, and it is reached by the same chord (digit 1).
    // Keyed on the id, `null` is a value the effect sees change, not an absence it
    // can skip.
    instances = { activeId: 'remote-1' }
    const h = harness()
    act(() => h.latest().openPalette())
    act(() => {
      instances = { activeId: null }
      h.rerender()
    })
    expect(h.latest().open).toBe(false)
  })

  it('stays open across a re-render that does not change the pane', () => {
    // The guard that makes the rule about switching rather than about rendering: the
    // dashboard re-renders constantly behind an open palette (socket traffic, status
    // ticks), and a dismissal keyed on anything less specific would close it under
    // the user mid-keystroke.
    const h = harness()
    act(() => h.latest().openPalette())
    act(() => h.rerender())
    expect(h.latest().open).toBe(true)
  })

  it('reads a store with no instances slice without throwing', () => {
    // Partial stores are the norm in this codebase's test harnesses, and a palette
    // that cannot mount on one takes every consumer's test down with it.
    instances = undefined
    const h = harness()
    act(() => h.latest().openPalette())
    expect(h.latest().open).toBe(true)
  })
})
