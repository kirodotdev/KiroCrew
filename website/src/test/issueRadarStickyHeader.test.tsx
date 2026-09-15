/**
 * The detail panes' header used to sit OUTSIDE the scroller, which made its
 * height standing furniture: 273px of an 844px phone viewport before any
 * content, for the whole session. It now lives INSIDE the scroller, so the tall
 * title leaves by physics and a sticky bar keeps the parts that must stay
 * reachable.
 *
 * Three things have to hold, and each fails differently:
 *   1. the mechanism is written ONCE and both panes render it — the previous
 *      attempt spelled ~45 lines per pane with a test pinning the copies;
 *   2. the sticky bar's height does not change between states, because that is
 *      what keeps `scrollHeight` still and makes a hysteresis band unnecessary;
 *   3. `sm:` resets everything, so a desktop keeps the static full-width header
 *      it always had.
 */
import { describe, it, expect } from 'vitest'
import { render, act } from '@testing-library/react'
import DetailHeader from '../apps/issue-radar/components/DetailHeader'

/** Source text with line endings normalized to LF. Vite's `?raw` hands back the
 *  bytes on disk, so on a Windows checkout every line ends `\r\n` — and the
 *  assertions below match multi-line shapes (`\}\n\s*<\/span>`), which a `\r`
 *  defeats. Without this they fail for every Windows contributor while passing on
 *  CI's Linux runner. */
async function src(path: string): Promise<string> {
  const raw = (await import(`../apps/issue-radar/${path}?raw`)).default as string
  return raw.replace(/\r\n/g, '\n')
}

describe('an empty detail pane is still escapable while narrow', () => {
  /** Strips comments before matching, so prose that names a pattern cannot
   *  satisfy or break a source assertion. */
  const bare = (s: string) => s
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '')

  it('keeps a shell Back for both panes when there is no active item', async () => {
    const s = bare((await import('../apps/issue-radar/Workspace.tsx?raw')).default as string)
    // Moving the Back control into the panes made it conditional on the pane
    // rendering at all. While narrow the list is hidden for as long as
    // `showDetail` holds, so an absent active item would leave the hidden-by-
    // filter notice and the empty state with no way back — and that state is
    // reachable by closing the issue from the detail toolbar while the list
    // filters to open, which drops it from the list under an open detail.
    expect(s, 'issues pane needs a shell Back when activeIssue is absent')
      .toMatch(/\{!activeIssue && narrowBack\(/)
    expect(s, 'pulls pane needs a shell Back when activePull is absent')
      .toMatch(/\{!activePull && narrowBack\(/)
  })
})

describe('the toolbar row obeys max-two-buttons-per-row', () => {
  /** Strips comments before matching, so a comment that NAMES a banned pattern
   *  (this file's own prose does) cannot pass or fail a source assertion. */
  const bare = (s: string) => s
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '')

  it('routes everything past the primary action into one overflow trigger', async () => {
    for (const pane of ['IssueDetail', 'PrDetail']) {
      const s = bare(await src(`components/${pane}.tsx`))
      expect(s, `${pane}: must collapse the row behind an overflow trigger`)
        .toMatch(/<DetailOverflowMenu[\s>]/)
      // The three controls that used to sit in the row are menu items now. A
      // copy button back in the row is the specific regression this pins: it was
      // a fourth control on a row already carrying three, and the row wrapped
      // onto a second line in 9 of the 13 shipped locales, English included.
      expect(s, `${pane}: copy-link must not be a bare row button again`)
        .not.toMatch(/<button[\s\S]{0,200}onClick=\{copyLink\}/)
      expect(s, `${pane}: refresh must not be a bare row button again`)
        .not.toMatch(/<button[\s\S]{0,200}onClick=\{refreshDetail\}/)
    }
  })

  it('keeps the copy confirmation on screen by not closing on select', async () => {
    for (const pane of ['IssueDetail', 'PrDetail']) {
      const s = bare(await src(`components/${pane}.tsx`))
      // Radix closes the menu on select by default, which would hide the tick
      // the instant it appeared — the copy affordance's only feedback.
      expect(s, `${pane}: the copy item must prevent the default close`)
        .toMatch(/onSelect=\{\(e\) => \{ e\.preventDefault\(\); copyLink\(\) \}\}/)
    }
  })

  it('suppresses the pane Back inside a cross-reference sheet', async () => {
    for (const pane of ['IssueDetail', 'PrDetail']) {
      const s = bare(await src(`components/${pane}.tsx`))
      // RefSheet reuses the whole pane as a detour OVER the workspace, so this
      // control's `closeDetail` would act on the workspace behind the sheet —
      // mutating hidden state while the sheet stayed open. The sheet carries its
      // own back and identity, so navigation belongs to it there.
      expect(s, `${pane}: Back must be gated on not being in a ref sheet`)
        .toMatch(/back=\{listDetail\.isMobile && refStack\.length === 0/)
    }
  })

  it('keeps an in-flight signal for the state write it now hosts', async () => {
    const menu = bare(await src('components/DetailOverflowMenu.tsx'))
    // The old header Close button swapped its glyph for a spinner while the
    // mutation ran. The menu closes on select, so without this the write has no
    // acknowledgment anywhere until the pill flips — a dead-tap read on latency.
    expect(menu, 'the trigger must render a spinner while pending')
      .toMatch(/pending\s*\n?\s*\?\s*<Loader2/)
    const issue = bare(await src('components/IssueDetail.tsx'))
    expect(issue, 'the issue pane must feed the mutation state to the trigger')
      .toMatch(/<DetailOverflowMenu pending=\{[^}]*stateMutation\.isPending[^}]*\}>/)
  })

  it('keeps the two toolbar controls the same height', async () => {
    const s = bare(await src('components/DetailHeader.tsx'))
    // The two controls are different components: the primary action is
    // AgentSessionButton's own button (text + icon, so a TEXT line box sets its
    // height, measured 26.6px) and the overflow trigger is the shared `Btn`
    // holding ONLY a 14px icon — with no text node its line-height never becomes
    // a line box, so it measured 24.0px despite being the one WITH a border.
    // Stretching the group is what makes them agree without hardcoding 26.6px,
    // which is an inherited-line-height product and not a design token.
    expect(s, 'the actions group must stretch so both controls match')
      .toMatch(/flex-shrink-0 flex items-stretch gap-1\.5/)
  })

  it('acknowledges a user-requested refresh without lying during background polls', async () => {
    for (const pane of ['IssueDetail', 'PrDetail']) {
      const src_ = bare(await src(`components/${pane}.tsx`))
      // Refresh moved into a menu that closes on select, so the item's own
      // animate-spin leaves with it — the trigger has to carry the signal.
      expect(src_, `${pane}: refresh must set a user-requested flag`)
        .toMatch(/setRefreshing\(true\)[\s\S]{0,120}refetch\(\)[\s\S]{0,80}setRefreshing\(false\)/)
      expect(src_, `${pane}: the trigger must reflect it`)
        .toMatch(/<DetailOverflowMenu pending=\{[^}]*refreshing[^}]*\}>/)
      // NOT isFetching: refetchInterval polls this query, so that flag would spin
      // the trigger with no user action — a spinner that misreports is worse.
      expect(src_, `${pane}: must not feed isFetching to the trigger`)
        .not.toMatch(/<DetailOverflowMenu pending=\{[^}]*isFetching/)
    }
  })

  it('gives the icon-only trigger an accessible name', async () => {
    const s = await src('components/DetailOverflowMenu.tsx')
    // `icon-buttons-need-labels`: a kebab glyph says nothing on its own.
    expect(s).toMatch(/aria-label=\{i18nT\('apps\.issueRadar\.components\.detailOverflowMenu\.more_actions'\)\}/)
  })
})

describe('the shared detail header', () => {
  it('is rendered by BOTH panes, so the mechanism has one spelling', async () => {
    for (const pane of ['IssueDetail', 'PrDetail']) {
      const s = await src(`components/${pane}.tsx`)
      expect(s, `${pane}: must render the shared shell`).toMatch(/<DetailHeader\b/)
      // The pane supplies slots; it must not re-implement the collapse itself.
      expect(s, `${pane}: must not carry its own sticky bar`)
        .not.toMatch(/sticky sm:static/)
      expect(s, `${pane}: must not carry its own compact title`)
        .not.toMatch(/aria-hidden="true"[\s\S]{0,120}truncate/)
    }
  })

  it('keeps the bar height fixed across states, so no hysteresis is needed', async () => {
    const s = await src('components/DetailHeader.tsx')
    // Only opacity changes on the compact echo. A height or font-size change
    // here would move `scrollHeight` while the reader scrolls, which is the
    // feedback loop the previous design needed a hysteresis band to survive.
    const echo = s.match(/aria-hidden="true"[\s\S]*?\}\n\s*<\/span>/)
    expect(echo, 'expected the compact echo').not.toBeNull()
    expect(echo![0]).toMatch(/transition-opacity/)
    expect(echo![0], 'the echo must not animate a layout property')
      .not.toMatch(/transition-\[(height|grid-template-rows|font-size|padding)\]/)
    expect(echo![0]).toMatch(/collapsed \? 'opacity-100' : 'opacity-0'/)
    // And no threshold constants anywhere: the hook observes the title instead.
    const hook = await src('lib/useTitleScrolledOut.ts')
    expect(hook, 'no scroll threshold may come back').not.toMatch(/scrollTop/)
    expect(hook).toMatch(/new IntersectionObserver/)
    // Rooted on the pane's scroller, not the viewport — above `sm:` the wrapper
    // stops scrolling, and a viewport root would report the title on screen
    // forever there.
    expect(hook).toMatch(/\{ root, threshold: 0 \}/)
  })

  it('bounds the one unbounded slot instead of overclaiming a fixed height', async () => {
    const s = await src('components/DetailHeader.tsx')
    // Invariant 2 above is about the parts this shell controls. The `extra` slot
    // is the exception and must say so: the PR pane puts PrActionsBar there, and
    // opening a composer grows a textarea inside the pinned region, so the
    // "44px in both states" figure is the ISSUE pane's, not both panes'.
    const doc = s.match(/\/\*\*[^*]*Optional extra row[\s\S]*?\*\//)
    expect(doc, 'the extra slot needs its own contract').not.toBeNull()
    expect(doc![0], 'must name itself as the unbounded part of the pinned region')
      .toMatch(/UNBOUNDED/)
    // And it must not be sold as free: the reason it does not reintroduce the
    // hysteresis problem is that a composer opens on a user action, not on scroll.
    expect(doc![0]).toMatch(/user action, not\s*\n?\s*\*?\s*a scroll position/)
  })

  it('resets the whole compact treatment at sm:', async () => {    const s = await src('components/DetailHeader.tsx')
    // A desktop keeps the static, full-width header it always had: nothing
    // sticks (nothing scrolls past it) and the compact echo never renders.
    expect(s).toMatch(/sticky sm:static/)
    // Row 1 (back + compact echo) is not `sm:`-hidden by a class — it is gated on
    // the back control existing at all, which is itself narrow-only. Pinning the
    // class would assert a mechanism the component does not use.
    expect(s).toMatch(/\{back && \(/)
    // The toolbar pins UNDER row 1 rather than over it.
    expect(s).toMatch(/top-11 sm:top-auto/)
    // Left-aligned: state and the actions read as one left-anchored row rather
    // than splitting to opposite edges. Matched against the JSX with comments
    // stripped — the prose above the element names the very class being banned,
    // and an unscoped match fails on its own explanation.
    const code = s.replace(/\/\/[^\n]*/g, '').replace(/\/\*[\s\S]*?\*\//g, '')
    expect(code, 'the toolbar must read as one left-anchored row').not.toMatch(/ml-auto/)
  })

  it('renders exactly one heading, and the echo is decorative', () => {
    const { container } = render(
      <DetailHeader
        collapsed={false}
        title="Shared lock file can name a stopped worker"
        titleRef={() => {}}
        back={<button type="button">back</button>}
        meta={<span>meta</span>}
        identity={<span>identity</span>}
        actions={<button type="button">act</button>}
      />,
    )
    expect(container.querySelectorAll('h1')).toHaveLength(1)
    expect(container.querySelector('h1')?.textContent)
      .toBe('Shared lock file can name a stopped worker')
    // The echo repeats the words, so it must be out of the accessibility tree
    // or the title is announced twice.
    const echo = container.querySelector('[aria-hidden="true"]')
    expect(echo).not.toBeNull()
    expect(echo?.textContent).toBe('Shared lock file can name a stopped worker')
  })

  it('shows the skeleton instead of fabricated values before first paint', () => {
    const { container, getByTestId } = render(
      <DetailHeader
        collapsed={false}
        title=""
        titleRef={() => {}}
        awaitingFirstPaint
        skeleton={<div data-testid="sk" />}
        meta={<span>meta</span>}
        identity={<span>identity</span>}
        actions={<button type="button">act</button>}
      />,
    )
    getByTestId('sk')
    // A pane opened from a cross-reference has no title yet; rendering an empty
    // heading would put a nameless h1 in the document outline.
    expect(container.querySelectorAll('h1')).toHaveLength(0)
    // Nor may the toolbar show identity: it carries the state pill, and `state`
    // falls back to 'open' on a placeholder, so rendering it here would display a
    // fabricated "Open" on an item that may be closed. This assertion previously
    // required the OPPOSITE ("state does not depend on the fetch"), which was
    // simply wrong and pinned the bug in place.
    expect(container.textContent).not.toContain('identity')
    // The actions stay: they are gated on their own preconditions by each pane
    // (Investigate is withheld while awaiting, the overflow trigger is not).
    expect(container.textContent).toContain('act')
  })

  it('fades the echo in only once the title has left', () => {
    const props = {
      title: 'T', titleRef: () => {}, back: <button type="button">b</button>, meta: <span>m</span>,
      identity: <span>i</span>, actions: <button type="button">a</button>,
    }
    const { container, rerender } = render(<DetailHeader collapsed={false} {...props} />)
    const echo = () => container.querySelector('[aria-hidden="true"]') as HTMLElement
    // At the top the reader can see the real heading; showing the echo too would
    // print the same words twice.
    expect(echo().className).toContain('opacity-0')
    act(() => { rerender(<DetailHeader collapsed {...props} />) })
    expect(echo().className).toContain('opacity-100')
  })
})
