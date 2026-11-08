import { readFileSync } from 'node:fs'
import path from 'node:path'

import { StrictMode } from 'react'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'

import QuickSearchSurface from './QuickSearchSurface'
import type { SlotOwners } from '../apps/overlaySlots'

/**
 * The opt-in guarantee, asserted at the seam: with no app owning `quick-search` the
 * shell renders its OWN palette (so a user who never enables anything sees no change),
 * and an owning app's overlay replaces it wholesale.
 */

vi.mock('./CommandPalette', () => ({
  default: ({ open, initialQuery }: { open: boolean; initialQuery?: string }) =>
    open ? <div data-testid="legacy-palette" data-initial={initialQuery ?? ''} /> : null,
}))

const mounted = vi.fn()

/**
 * A chunk that loads when the test says so: it suspends on `slowChunk.promise`, the
 * way a lazy overlay suspends on its import, and once resolved renders the opening
 * query it was given. With `fail` set, the settled chunk throws instead, the way a
 * rejected import does.
 */
const slowChunk = vi.hoisted(() => ({
  promise: Promise.resolve() as Promise<void>,
  done: false,
  fail: false,
  focusAtMount: null as Element | null,
  opener: null as HTMLElement | null | undefined,
}))
const SlowOverlay = vi.hoisted(() => ({ initialQuery, opener }: { initialQuery?: string; opener?: HTMLElement | null }) => {
  if (!slowChunk.done) {
    throw slowChunk.promise.then(() => { slowChunk.done = true })
  }
  if (slowChunk.fail) throw new Error('chunk gone')
  slowChunk.opener = opener
  // The ref attaches in the commit that removes the type-ahead field, before any
  // mount effect. A focus trap that mounts here records the element seen now.
  return (
    <div
      data-testid="slow-overlay"
      data-initial={initialQuery ?? ''}
      ref={el => { if (el) slowChunk.focusAtMount = document.activeElement }}
    />
  )
})

/** Suspend the slow overlay until the returned function is called. */
function holdChunk(): () => void {
  let release: () => void = () => {}
  slowChunk.done = false
  slowChunk.promise = new Promise<void>(r => { release = r })
  return release
}

vi.mock('../apps/overlayRegistry', () => ({
  getBuiltinOverlay: (id: string) => {
    if (id === 'command-bar') {
      return ({ open }: { open: boolean }) => {
        // Records the MOUNT, not the render output: "closed means unmounted" is a
        // statement about the component tree, and an overlay that returns null while
        // closed would satisfy an output-only assertion either way.
        mounted()
        return open ? <div data-testid="command-bar" /> : null
      }
    }
    if (id === 'slow') return SlowOverlay
    if (id === 'throws') {
      return () => {
        throw new Error('chunk gone')
      }
    }
    return undefined
  },
}))

const OWNED: SlotOwners = {
  'quick-search': { app: 'command-bar', overlayId: 'command-bar' },
}

const SLOW: SlotOwners = {
  'quick-search': { app: 'command-bar', overlayId: 'slow' },
}

const THROWS: SlotOwners = {
  'quick-search': { app: 'broken', overlayId: 'throws' },
}

describe('QuickSearchSurface', () => {
  beforeEach(() => {
    mounted.mockClear()
    slowChunk.fail = false
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders the shell palette when no app owns the slot', () => {
    render(<QuickSearchSurface owners={{}} open onClose={() => {}} />)
    expect(screen.getByTestId('legacy-palette')).toBeTruthy()
    expect(screen.queryByTestId('command-bar')).toBeNull()
  })

  it('renders the owning app overlay instead of the palette', async () => {
    render(<QuickSearchSurface owners={OWNED} open onClose={() => {}} />)
    await waitFor(() => expect(screen.getByTestId('command-bar')).toBeTruthy())
    expect(screen.queryByTestId('legacy-palette')).toBeNull()
  })

  it('falls back to the palette when the owner names an unregistered overlay', () => {
    const ghost: SlotOwners = {
      'quick-search': { app: 'x', overlayId: 'not-bundled' },
    }
    render(<QuickSearchSurface owners={ghost} open onClose={() => {}} />)
    expect(screen.getByTestId('legacy-palette')).toBeTruthy()
  })

  it('renders nothing visible while closed', () => {
    const { container } = render(<QuickSearchSurface owners={{}} open={false} onClose={() => {}} />)
    expect(container.textContent).toBe('')
  })

  it('does not mount the owning overlay at all while closed', async () => {
    // A closed bar must hold no live subscriptions: the scoped session view's query
    // would otherwise stay enabled behind a dismissed surface and refetch on the next
    // window focus, turning a closed search into a background corpus scan.
    render(<QuickSearchSurface owners={OWNED} open={false} onClose={() => {}} />)
    await waitFor(() => expect(screen.queryByTestId('command-bar')).toBeNull())
    expect(mounted).not.toHaveBeenCalled()
    expect(screen.queryByTestId('legacy-palette')).toBeNull()
  })

  it('keeps keys typed while the overlay chunk loads and hands them to the overlay', async () => {
    // The first open fetches the overlay's chunk. Keys pressed before it resolves must
    // reach the overlay, or "new session" + Enter runs on an empty query.
    let resolveChunk: () => void = () => {}
    slowChunk.done = false
    slowChunk.promise = new Promise<void>(r => { resolveChunk = r })
    render(<QuickSearchSurface owners={SLOW} open onClose={() => {}} />)
    const catcher = document.activeElement as HTMLInputElement
    expect(catcher.tagName).toBe('INPUT')
    fireEvent.change(catcher, { target: { value: 'New sess' } })
    await act(async () => { resolveChunk(); await slowChunk.promise })
    await waitFor(() => expect(screen.getByTestId('slow-overlay').getAttribute('data-initial')).toBe('New sess'))
  })

  it('gives focus back to the opener before the loaded overlay records its restore target', async () => {
    // A surface whose focus trap restores to whatever was focused when it mounted (the
    // shell palette) would record <body> if the type-ahead field kept focus until removed.
    const composer = document.createElement('textarea')
    document.body.appendChild(composer)
    try {
      composer.focus()
      const release = holdChunk()
      slowChunk.focusAtMount = null
      render(<QuickSearchSurface owners={SLOW} open onClose={() => {}} />)
      expect(document.activeElement?.tagName).toBe('INPUT')
      await act(async () => { release(); await slowChunk.promise })
      await waitFor(() => expect(screen.getByTestId('slow-overlay')).toBeTruthy())
      expect(slowChunk.focusAtMount).toBe(composer)
    } finally {
      composer.remove()
    }
  })

  it('hands the overlay the opener the gesture came from', async () => {
    const opener = document.createElement('button')
    const release = holdChunk()
    render(<QuickSearchSurface owners={SLOW} open onClose={() => {}} opener={opener} />)
    await act(async () => { release(); await slowChunk.promise })
    await waitFor(() => expect(screen.getByTestId('slow-overlay')).toBeTruthy())
    expect(slowChunk.opener).toBe(opener)
  })

  it('gives focus back to the opener when Escape closes the type-ahead field', () => {
    const composer = document.createElement('textarea')
    document.body.appendChild(composer)
    try {
      composer.focus()
      holdChunk()
      const view = render(<QuickSearchSurface owners={SLOW} open onClose={() => {}} />)
      fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'Escape' })
      view.rerender(<QuickSearchSurface owners={SLOW} open={false} onClose={() => {}} />)
      expect(document.activeElement).toBe(composer)
    } finally {
      composer.remove()
    }
  })

  it('closes from the type-ahead input on Escape', () => {
    holdChunk()
    const onClose = vi.fn()
    render(<QuickSearchSurface owners={SLOW} open onClose={onClose} />)
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('consumes the Escape that closes it, so document handlers skip it', () => {
    // Voice capture and the notification banner act on an unconsumed Escape.
    holdChunk()
    const seen: boolean[] = []
    const onDoc = (e: KeyboardEvent) => { if (e.key === 'Escape') seen.push(e.defaultPrevented) }
    document.addEventListener('keydown', onDoc)
    try {
      render(<QuickSearchSurface owners={SLOW} open onClose={() => {}} />)
      fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'Escape' })
      expect(seen).toEqual([true])
    } finally {
      document.removeEventListener('keydown', onDoc)
    }
  })

  it('keeps focus on the type-ahead field under StrictMode effect replay', () => {
    const composer = document.createElement('textarea')
    document.body.appendChild(composer)
    try {
      composer.focus()
      holdChunk()
      render(
        <StrictMode>
          <QuickSearchSurface owners={SLOW} open onClose={() => {}} />
        </StrictMode>,
      )
      expect(document.activeElement?.getAttribute('placeholder')).toBe('Run a command')
    } finally {
      composer.remove()
    }
  })

  it('shows the focused type-ahead field instead of hiding it', () => {
    // A stalled chunk must not leave focus on a control the reader cannot see. jsdom
    // has no layout, so the contract is pinned on the classes that paint it.
    holdChunk()
    render(<QuickSearchSurface owners={SLOW} open onClose={() => {}} />)
    const catcher = document.activeElement as HTMLInputElement
    expect(catcher.className).not.toContain('sr-only')
    expect(catcher.getAttribute('placeholder')).toBe('Run a command')
    expect(catcher.parentElement?.className).toContain('focus-within:border-accent')
  })

  it('hands the caught keys to the fallback palette when the chunk fails', async () => {
    // A tab left open across a deploy asks for a chunk that no longer exists. The keys
    // typed while that import was pending belong to the palette that replaces it.
    const release = holdChunk()
    slowChunk.fail = true
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      render(<QuickSearchSurface owners={SLOW} open onClose={() => {}} />)
      fireEvent.change(document.activeElement as HTMLInputElement, { target: { value: 'New sess' } })
      await act(async () => { release(); await slowChunk.promise })
      await waitFor(() => expect(screen.getByTestId('legacy-palette').getAttribute('data-initial')).toBe('New sess'))
    } finally {
      warn.mockRestore()
      error.mockRestore()
    }
  })

  it('does not close on an Escape the IME owns, live or in the grace window', () => {
    // Escape cancels an IME candidate list. Fake timers hold the post-compositionend
    // window open, so the decline is not at the mercy of a loaded runner.
    vi.useFakeTimers()
    holdChunk()
    const onClose = vi.fn()
    render(<QuickSearchSurface owners={SLOW} open onClose={onClose} />)
    const catcher = document.activeElement as HTMLInputElement
    fireEvent.compositionStart(catcher)
    fireEvent.keyDown(catcher, { key: 'Escape', isComposing: true })
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.compositionEnd(catcher)
    fireEvent.keyDown(catcher, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
    // Past the window the latch releases and Escape closes the bar again.
    vi.advanceTimersByTime(60)
    fireEvent.keyDown(catcher, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('hands over only text the IME has committed', async () => {
    // The composition dies when the type-ahead unmounts, so its uncommitted text, such
    // as raw romaji, must not become the bar's query.
    const release = holdChunk()
    render(<QuickSearchSurface owners={SLOW} open onClose={() => {}} />)
    const catcher = document.activeElement as HTMLInputElement
    fireEvent.change(catcher, { target: { value: 'New ' } })
    fireEvent.compositionStart(catcher)
    fireEvent.change(catcher, { target: { value: 'New shinn' } })
    expect(catcher.value).toBe('New shinn')
    await act(async () => { release(); await slowChunk.promise })
    await waitFor(() => expect(screen.getByTestId('slow-overlay').getAttribute('data-initial')).toBe('New '))
  })

  it('hands over composed text once the IME commits it', async () => {
    const release = holdChunk()
    render(<QuickSearchSurface owners={SLOW} open onClose={() => {}} />)
    const catcher = document.activeElement as HTMLInputElement
    fireEvent.compositionStart(catcher)
    fireEvent.change(catcher, { target: { value: '新' } })
    fireEvent.compositionEnd(catcher)
    await act(async () => { release(); await slowChunk.promise })
    await waitFor(() => expect(screen.getByTestId('slow-overlay').getAttribute('data-initial')).toBe('新'))
  })

  it('falls back to the palette when the overlay throws', async () => {
    // Suspense catches a PENDING import, never a rejected one, and a tab left open
    // across a deploy asks for a chunk hash that no longer exists.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<QuickSearchSurface owners={THROWS} open onClose={() => {}} />)
    await waitFor(() => expect(screen.getByTestId('legacy-palette')).toBeTruthy())
    warn.mockRestore()
    error.mockRestore()
  })

  it('the topbar trigger describes the surface slot ownership actually opens', () => {
    // While an app owns the slot the gesture opens a launcher, so a trigger reading
    // "search for anything" is the most visible mispromise in the product. Asserted on
    // the source rather than by mounting App: this is a one-line branch inside a very
    // large component, and the repo already uses source-shape assertions for exactly
    // this trade-off (see providerLabels.test.ts).
    const src = ['App.tsx', 'shell/appShell.tsx', 'shell/topbar/shellHeader.tsx', 'shell/nav/navRail.tsx', 'shell/nav/navRow.tsx'].map(f => readFileSync(path.join(__dirname, '..', f), 'utf-8')).join('\n')
    // Both the visible label and the accessible name follow ownership, and the mobile
    // icon-only trigger carries the same accessible name as the desktop one.
    expect(src).toContain("i18nT('app.k_run_a_command')")
    const owned = src.split("slotOwners['quick-search']").length - 1
    expect(owned).toBeGreaterThanOrEqual(4)
  })
  it('only the newest app-nav response may publish slot ownership', () => {
    // A pending RETRY is cancelled up-front, but a fetch already in flight cannot be:
    // a slow mount response landing after an enable/disable refresh would publish
    // stale ownership and bind the quick-search gesture to the wrong surface. Both the
    // resolve and the reject path must drop a superseded generation.
    const src = ['App.tsx', 'shell/appShell.tsx', 'shell/topbar/shellHeader.tsx', 'shell/nav/navRail.tsx', 'shell/nav/navRow.tsx'].map(f => readFileSync(path.join(__dirname, '..', f), 'utf-8')).join('\n')
    expect(src).toContain('const gen = ++appNavGenRef.current')
    const guards = src.split('gen !== appNavGenRef.current').length - 1
    expect(guards).toBeGreaterThanOrEqual(2)
  })
  it('gates every user-visible promise on the trigger, not just some of them', () => {
    // The label and aria-label were made to follow slot ownership in an earlier round
    // and `title` was missed, so hovering promised the corpus search the launcher
    // omits while the words under the cursor said otherwise. The recurring cause is
    // fixing one attribute of a set, so the assertion is on the SET: no promise-bearing
    // attribute on either trigger may mention search unconditionally.
    const src = ['App.tsx', 'shell/appShell.tsx', 'shell/topbar/shellHeader.tsx', 'shell/nav/navRail.tsx', 'shell/nav/navRow.tsx'].map(f => readFileSync(path.join(__dirname, '..', f), 'utf-8')).join('\n')
    expect(src).not.toMatch(/title=\{i18nT\('app\.search_everywhere_k'\)\}/)
    const gated = src.split("slotOwners['quick-search']").length - 1
    expect(gated).toBeGreaterThanOrEqual(5)
  })
})
