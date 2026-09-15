/**
 * A declined click has to EXPLAIN ITSELF ON SCREEN, and the destination it names
 * has to be somewhere the user can actually go.
 *
 * The guard lives in `openSession` and the surface lives in `AgentSessionButton`,
 * wired through `useInvestigate` -> `InvestigateButton` props. A pod run proved
 * the guard fires (zero slot-create requests) while nothing changed on screen,
 * which is a wiring failure the hook-level test cannot see: it asserts on the
 * returned state, not on what the button does with it.
 *
 * WHAT CHANGED, AND WHY THE OLD SHAPE WAS NOT ENOUGH (#6270)
 *
 * The first shape put the reason in the button's `title` plus an `sr-only` live
 * region. Between them those serve a hovering mouse user and a screen-reader
 * user. A sighted KEYBOARD user is in neither group -- browsers surface `title`
 * on hover, not on focus -- and a touch user cannot surface a `title` at all, so
 * the population most likely to be left with no explanation was the one a `title`
 * can never reach.
 *
 * The reason it was not simply moved into the toolbar row: that group is
 * `flex-shrink-0 flex items-stretch` (`DetailHeader.tsx`), so a sibling that WRAPS
 * stretches the button to its height, and this sentence wraps at 320px. Note the
 * constraint is wrapping, not text -- this component already renders inline text
 * for its error branch. A popover is portalled out of that flex row, so neither
 * the wrapping nor the stretching reaches it, which is why the sentence can now
 * be shown at all.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const { investigate, openOlderSessions, api, session, popover } = vi.hoisted(() => ({
  investigate: vi.fn(),
  openOlderSessions: vi.fn(),
  api: { getInvestigation: vi.fn() },
  session: { concludedFor: null as string | null },
  // The stubbed primitive's latest `onOpenChange`, so a test can report a close
  // the way Radix would (Escape, outside pointer-down) without a DOM handle — an
  // extra clickable node would change what the button-count assertions can see.
  popover: { requestClose: null as null | ((open: boolean) => void) },
}))

/**
 * Radix's popover is stubbed rather than run, for two reasons. Its positioning
 * reads real layout boxes, which happy-dom does not produce; and its dismissal
 * (Escape, outside pointer-down, focus return) is Radix's own contract, already
 * covered upstream. What belongs to THIS component is what it renders inside the
 * popover and how it reacts when the primitive reports a close -- so the stub
 * models exactly that: the controlled `open` prop, `asChild` anchoring, and a
 * captured `onOpenChange`.
 */
vi.mock('@radix-ui/react-popover', async () => {
  const React = await import('react')
  const Ctx = React.createContext<{ open: boolean }>({ open: false })
  type RootProps = { children?: ReactNode; open?: boolean; onOpenChange?: (v: boolean) => void }
  type AnchorProps = { children?: ReactNode; asChild?: boolean }
  type ContentProps = { children?: ReactNode } & Record<string, unknown>
  return {
    Root: ({ children, open, onOpenChange }: RootProps) => {
      popover.requestClose = onOpenChange ?? null
      return <Ctx.Provider value={{ open: !!open }}>{children}</Ctx.Provider>
    },
    // Real Radix clones the child to attach a ref; what matters here is that the
    // button stays a direct child of the action row rather than gaining a wrapper.
    Anchor: ({ children, asChild }: AnchorProps) =>
      (asChild ? <>{children}</> : <span>{children}</span>),
    Portal: ({ children }: { children?: ReactNode }) => <>{children}</>,
    // Read at module load by `components/ui/popover.tsx`, which re-exports the
    // whole family. This component anchors rather than triggers, so the stub only
    // has to exist.
    Trigger: ({ children }: { children?: ReactNode }) => <>{children}</>,
    Content: ({ children, ...rest }: ContentProps) => {
      const { open } = React.useContext(Ctx)
      if (!open) return null
      // Drop the props floating-ui owns; they are not DOM attributes.
      const { align: _a, sideOffset: _s, collisionPadding: _c, ...dom } = rest
      return <div data-testid="stub-popover" {...dom}>{children}</div>
    },
  }
})

vi.mock('../apps/issue-radar/api', () => ({ issueRadarApi: api }))
vi.mock('../apps/issue-radar/lib/investigate', () => ({
  useInvestigate: () => ({
    investigate,
    busy: false,
    error: null,
    concludedFor: session.concludedFor,
    openOlderSessions,
  }),
}))

const InvestigateButton = (await import('../apps/issue-radar/components/InvestigateButton')).default
const { itemKey } = await import('../apps/issue-radar/lib/agentSession')

const REF = { owner: 'acme', repo: 'demo', provider: 'github', host: 'github.com' } as never
const ISSUE = { number: 6014, title: 'concluded issue', labels: [] } as never

const wrap = (ui: ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

const actionRow = () => screen.getByTestId('agent-session-action-row')

describe('InvestigateButton reports a declined click', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    session.concludedFor = null
    api.getInvestigation.mockResolvedValue({
      investigation: {
        slot_key: 'chat-closed', status: 'resolved', findings: { verdict: 'bug' },
      },
    })
  })

  it('offers Resume until a click is declined', async () => {
    wrap(<InvestigateButton repoRef={REF} issue={ISSUE} />)
    const btn = await waitFor(() => screen.getByRole('button', { name: /Resume/i }))
    expect(screen.queryByRole('button', { name: /Start over/i })).toBeNull()
    expect(btn.getAttribute('title')).not.toMatch(/Already finished/i)
    // Nothing explains a decline that has not happened.
    expect(screen.queryByText(/Already finished/i)).toBeNull()
  })

  it('becomes Start over, and says why in its title, once declined', async () => {
    session.concludedFor = itemKey(REF, 6014)
    wrap(<InvestigateButton repoRef={REF} issue={ISSUE} />)
    const btn = await waitFor(() => screen.getByRole('button', { name: /Start over/i }))
    // Resume is REPLACED, not joined -- the re-run takes over this control.
    expect(screen.queryByRole('button', { name: /Resume/i })).toBeNull()
    // Kept as a residual for a pointer user who dismissed the notice. It is no
    // longer the ONLY route to the reason, which is what made it a defect.
    expect(btn.getAttribute('title')).toMatch(/Already finished/i)
  })

  /**
   * THE FIX. The reason is rendered where a sighted user -- keyboard or touch --
   * can read it without hovering anything, and it is not the clipped `sr-only`
   * node it used to be.
   */
  it('shows the reason on screen, not only to a screen reader', async () => {
    session.concludedFor = itemKey(REF, 6014)
    wrap(<InvestigateButton repoRef={REF} issue={ISSUE} />)
    const live = await waitFor(() => screen.getByRole('status'))
    expect(live.textContent).toMatch(/Already finished/i)
    // The whole point: visible, not clipped out of the layout.
    expect(live.className).not.toMatch(/sr-only/)
    expect(live.closest('.sr-only')).toBeNull()
  })

  /**
   * The copy names Older Sessions, so Older Sessions has to be reachable. Clicking
   * Resume on finished work means "show me the result"; offering only "redo it" is
   * what made the wording hollow.
   */
  it('offers the transcript destination as something clickable', async () => {
    session.concludedFor = itemKey(REF, 6014)
    wrap(<InvestigateButton repoRef={REF} issue={ISSUE} />)
    const go = await waitFor(() => screen.getByRole('button', { name: /Older Sessions/i }))
    await act(async () => { go.click() })
    expect(openOlderSessions).toHaveBeenCalledTimes(1)
    // And it is not a second re-run in disguise.
    expect(investigate).not.toHaveBeenCalled()
  })

  /**
   * The notice is allowed to exist BECAUSE it is out of the toolbar row.
   *
   * The row is `flex-shrink-0 flex items-stretch`, so a wrapping sibling stretches
   * the button to its height, and a second in-row control would breach
   * `max-two-buttons-per-row` (the row already carries the overflow trigger).
   * Both in-row shapes were shipped and blocked in review, so this pins the
   * property that replaced them: whatever the notice adds, it adds OUTSIDE the row.
   */
  it('keeps the action row to one control, declined or not', async () => {
    for (const declined of [false, true]) {
      session.concludedFor = declined ? itemKey(REF, 6014) : null
      const { unmount } = wrap(<InvestigateButton repoRef={REF} issue={ISSUE} />)
      await waitFor(() => expect(screen.getAllByRole('button').length).toBeGreaterThan(0))
      const row = actionRow()
      expect(row.querySelectorAll('button')).toHaveLength(1)
      // Every node carrying the sentence sits outside the row.
      for (const el of screen.queryAllByText(/Already finished/i)) {
        expect(row.contains(el)).toBe(false)
      }
      unmount()
    }
  })

  /**
   * Dismissing the notice must not un-decline the click.
   *
   * `concludedFor` is the hook's record of what was refused and is cleared only by
   * the next `openSession`. Escape or a click outside closes the explanation; the
   * button has to stay the deliberate re-run, or dismissing the notice would hand
   * back the silent re-run that #6260 removed.
   */
  it('closes on dismissal without turning back into Resume', async () => {
    session.concludedFor = itemKey(REF, 6014)
    wrap(<InvestigateButton repoRef={REF} issue={ISSUE} />)
    await waitFor(() => expect(screen.getByRole('status')).toBeTruthy())
    await act(async () => { popover.requestClose?.(false) })
    expect(screen.queryByRole('status')).toBeNull()
    expect(screen.getByRole('button', { name: /Start over/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Resume/i })).toBeNull()
  })

  /**
   * The flip is not text-only.
   *
   * The second click lands on the same pixel as the first, so a user who reads a
   * quiet relabel as "nothing happened" would click again and spend a fresh agent
   * run. The icon changes with the label; the notice is what makes the flip
   * legible in the first place.
   */
  it('changes the icon along with the label', async () => {
    const iconOf = async () => {
      const btn = await waitFor(() => screen.getAllByRole('button')[0])
      return btn.querySelector('svg')?.getAttribute('class') || ''
    }
    const { unmount } = wrap(<InvestigateButton repoRef={REF} issue={ISSUE} />)
    const resting = await iconOf()
    unmount()

    session.concludedFor = itemKey(REF, 6014)
    wrap(<InvestigateButton repoRef={REF} issue={ISSUE} />)
    await waitFor(() => expect(screen.getByRole('button', { name: /Start over/i })).toBeTruthy())
    expect(await iconOf()).not.toBe(resting)
  })

  /**
   * The re-run is a deliberate second click, and nothing pretends otherwise.
   *
   * An earlier revision held the flipped button inert for 700ms to absorb a
   * reflexive retry. Three review lanes rejected it and they were right: the
   * habitual double-click is already absorbed by `busy`, which spans the probe
   * and the record re-read, so the beat only ever covered a derived hazard with
   * an unmeasured constant -- and supporting it cost `aria-disabled`, a manual
   * click guard and a hand-rolled dim, one of which had already caused a focus
   * bug. What protects the second click is that it is a real click on a control
   * whose label and icon have both changed, beside a notice that says what
   * happened.
   */
  it('re-runs on the second click, with nothing inert in between', async () => {
    session.concludedFor = itemKey(REF, 6014)
    wrap(<InvestigateButton repoRef={REF} issue={ISSUE} />)
    // Wait out the record lookup: `busy` legitimately disables the control while
    // the query is in flight, and that is a different mechanism from the timing
    // state this asserts is gone.
    const btn = await waitFor(() => {
      const b = screen.getByRole('button', { name: /Start over/i })
      expect(b).not.toBeDisabled()
      return b
    })
    expect(btn.getAttribute('aria-disabled')).toBeNull()

    btn.focus()
    await act(async () => { btn.click() })
    expect(investigate).toHaveBeenCalled()
    // The click did not cost the user their focus.
    expect(document.activeElement).toBe(btn)
  })
})
