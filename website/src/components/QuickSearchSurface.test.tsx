import { readFileSync } from 'node:fs'
import path from 'node:path'

import { render, screen, waitFor } from '@testing-library/react'

import QuickSearchSurface from './QuickSearchSurface'
import type { SlotOwners } from '../apps/overlaySlots'

/**
 * The opt-in guarantee, asserted at the seam: with no app owning `quick-search` the
 * shell renders its OWN palette (so a user who never enables anything sees no change),
 * and an owning app's overlay replaces it wholesale.
 */

vi.mock('./CommandPalette', () => ({
  default: ({ open }: { open: boolean }) => (open ? <div data-testid="legacy-palette" /> : null),
}))

const mounted = vi.fn()

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

const THROWS: SlotOwners = {
  'quick-search': { app: 'broken', overlayId: 'throws' },
}

describe('QuickSearchSurface', () => {
  beforeEach(() => mounted.mockClear())

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
    const src = readFileSync(path.join(__dirname, '..', 'App.tsx'), 'utf-8')
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
    const src = readFileSync(path.join(__dirname, '..', 'App.tsx'), 'utf-8')
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
    const src = readFileSync(path.join(__dirname, '..', 'App.tsx'), 'utf-8')
    expect(src).not.toMatch(/title=\{i18nT\('app\.search_everywhere_k'\)\}/)
    const gated = src.split("slotOwners['quick-search']").length - 1
    expect(gated).toBeGreaterThanOrEqual(5)
  })
})
