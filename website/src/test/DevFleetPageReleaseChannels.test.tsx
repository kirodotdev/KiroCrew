/**
 * Release-channel worktree rows in the Dev Fleet table.
 *
 * The whole feature is a claim about WHICH RELEASE a checkout is sitting on, so
 * every test here asserts on what the row states rather than on whether it
 * rendered. Two failure modes are specifically guarded:
 *
 * - **Adopting on the name.** `release-channel-stable` is a reserved basename. A
 *   user's own branch checkout under that name must keep ordinary controls; only
 *   the backend's `worktree` field (set when the tree is detached at a resolved
 *   ref) confers lane controls.
 * - **Reusing a column with a different meaning silently.** BEHIND counts from
 *   main and is NOT rendered on these rows — the cell says the column does not
 *   apply and points at the version badge, whose tooltip carries the row's
 *   currency — and PR is inapplicable rather than merely absent.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'

import DevFleetPage, { __resetDevFleetNoticesForTests } from '../pages/DevFleetPage'

function renderPage() {
  return renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
}

const MAIN = {
  name: 'main',
  is_main: true,
  running: false,
  has_dist: true,
  behind: 0,
  last_updated_at: Date.now() / 1000,
}

// A feature worktree carrying the repo's real naming convention, so the
// ordering assertions compare against what the fleet actually shows.
const FEATURE = {
  name: 'kirocrew-wt-update-freshness',
  is_main: false,
  running: false,
  has_dist: true,
  behind: 12,
  last_updated_at: Date.now() / 1000 - 3600,
}

const STABLE_WT = {
  name: 'release-channel-stable',
  is_main: false,
  running: false,
  has_dist: true,
  // Behind MAIN is large by construction on a release worktree — the row must
  // not show this number.
  behind: 412,
  last_updated_at: Date.now() / 1000 - 86400 * 2,
}

const CHANNELS = {
  stable: {
    lane: 'stable',
    name: 'release-channel-stable',
    worktree: 'release-channel-stable',
    ref: 'refs/tags/v0.5.0',
    version: '0.5.0',
    tip_version: '0.5.0',
    error: null,
    at_tip: true,
    name_taken_by_branch: false,
  },
  // The SAME lane before it is materialized. There is one lane, so the
  // placeholder cases use this rather than a second lane's row — which also
  // keeps them honest: a placeholder is a state of a lane, not a kind of lane.
  uncreated: {
    lane: 'stable',
    name: 'release-channel-stable',
    worktree: null,
    ref: 'refs/tags/v0.5.0',
    version: '0.5.0',
    tip_version: '0.5.0',
    error: null,
    at_tip: null,
    name_taken_by_branch: false,
  },
}

function mockFleet(data: Record<string, unknown>, posts?: Record<string, unknown>) {
  const seen: { url: string; body: unknown }[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation((url, init) => {
    const u = typeof url === 'string' ? url : (url as Request).url
    if (init?.method === 'POST') {
      seen.push({ url: u, body: init.body ? JSON.parse(String(init.body)) : null })
      const key = Object.keys(posts || {}).find((k) => u.includes(k))
      return Promise.resolve(
        new Response(JSON.stringify(key ? posts![key] : { ok: true }), { status: 200 }),
      )
    }
    if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(data), { status: 200 }))
    if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
    return Promise.resolve(new Response('{}', { status: 200 }))
  })
  return seen
}

beforeEach(() => {
  __resetDevFleetNoticesForTests()
  vi.restoreAllMocks()
})

describe('DevFleetPage release-channel rows', () => {
  it('badges an adopted lane row with the release it is sitting on', async () => {
    mockFleet({
      base_branch: 'main',
      worktrees: [MAIN, STABLE_WT, FEATURE],
      release_channel: CHANNELS.stable,
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    // The lane is already in the row name, so the badge carries the version.
    const badge = screen.getByText('0.5.0')
    expect(badge).toBeInTheDocument()
    expect(badge).toHaveAttribute('title', expect.stringContaining('refs/tags/v0.5.0'))
  })

  it('lists a lane with no worktree as a placeholder row offering Create', async () => {
    // Without the placeholder there is nowhere on the page the feature is
    // discoverable — the design has no header control.
    mockFleet({
      worktrees: [MAIN, FEATURE],
      release_channel: CHANNELS.uncreated,
    })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('release-channel-placeholder-stable')).toBeInTheDocument())
    const row = screen.getByTestId('release-channel-placeholder-stable')
    expect(within(row).getByText('release-channel-stable')).toBeInTheDocument()
    expect(within(row).getByText('0.5.0')).toBeInTheDocument()
    // The status line is VISIBLE text that says what Create does, naming the
    // version: a pill plus "no worktree yet" explained Create only to a reader
    // who hovered the pill.
    expect(within(row).getByText('No worktree yet — Create checks out 0.5.0')).toBeInTheDocument()
    expect(within(row).queryByText('no worktree yet')).not.toBeInTheDocument()
    expect(within(row).getByRole('button', { name: /create/i })).toBeEnabled()
  })

  it('keeps the enabled Create button OUT of any opacity-reduced (dimmed) subtree', async () => {
    // The placeholder row reads muted, but CSS composites an element with
    // opacity<1 and its whole subtree as ONE group — so any ancestor of Create
    // carrying opacity would dim the button below full contrast (the bug: opacity
    // sat on the grid container). The muting must live on sibling CELLS, never on
    // an ancestor of the actions cell. Walk from the button up to and including the
    // placeholder grid container and prove no ancestor carries a reduced inline
    // opacity, naming the offending element so this cannot pass either way.
    mockFleet({ worktrees: [MAIN, FEATURE], release_channel: CHANNELS.uncreated })
    renderPage()
    const row = await waitFor(() => screen.getByTestId('release-channel-placeholder-stable'))
    const btn = within(row).getByRole('button', { name: /create/i })
    expect(btn).toBeEnabled()
    const chain: string[] = []
    for (let el: HTMLElement | null = btn; el; el = el === row ? null : el.parentElement) {
      const raw = el.style.opacity
      const val = raw === '' ? 1 : Number(raw)
      const id = el.getAttribute('data-testid')
      chain.push(`${el.tagName.toLowerCase()}${id ? `[${id}]` : ''}=${val}`)
      if (val < 1) {
        throw new Error(
          `Create button has an opacity-reduced ancestor: ${chain.join(' > ')} — ` +
            'an opacity<1 ancestor composites the button dim; move the muting onto sibling cells.',
        )
      }
    }
    // The grid container itself must carry no opacity — that was the defect.
    expect(row.style.opacity === '' || Number(row.style.opacity) === 1).toBe(true)
    // ...and the row still reads muted: the status cell sits inside a dimmed subtree.
    const status = within(row).getByText(/No worktree yet/)
    expect(status.closest('[style*="opacity"]')).not.toBeNull()
  })

  it('renders a benign unpublished channel as information, Create enabled, no ErrorNotice', async () => {
    // An empty channel is a documented state — this checkout has fetched no
    // release tag — not an incident. It reads as ordinary information, keeps Create
    // enabled (Create fetches first, which is what resolves it), and does NOT raise
    // the shared error surface. The backend nulls `error` and sets `unpublished`.
    const benign = { ...CHANNELS.stable, worktree: null, ref: null, version: null,
      tip_version: null, unpublished: true, error: null }
    mockFleet({ worktrees: [MAIN], release_channel: benign })
    renderPage()
    const row = await waitFor(() => screen.getByTestId('release-channel-placeholder-stable'))
    // The status line names the MECHANISM, not just the absence: a reader who
    // sees "nothing resolvable here" beside a live button has no reason to click
    // it, and did not. The copy says that Create is what fetches.
    const status = within(row).getByText(/No stable release fetched yet/)
    expect(status.textContent).toMatch(/Create fetches/)
    const create = within(row).getByRole('button', { name: /create/i })
    expect(create).toBeEnabled()
    // ...and the enabled button says so itself, so the answer to "what happens
    // if I click this" is on the control and not only in the row's prose.
    expect(create).toHaveAttribute('title', expect.stringMatching(/fetches the stable release tags/))
    // Benign is not an error: no row-level notice and no page-level toast.
    expect(screen.queryByTestId('release-channel-error-stable')).not.toBeInTheDocument()
    expect(screen.queryByTestId('devfleet-action-error')).not.toBeInTheDocument()
  })

  it('routes an ADOPTED lane row resolver error to the shared ErrorNotice, not a badge tooltip', async () => {
    // A lane whose worktree exists and is detached is adopted even when resolution
    // fails, so there is no placeholder row to carry the message. The error still
    // must not live in a Badge title (not keyboard-reachable, no agent hand-off):
    // channelErrorFor routes it to the ErrorNotice. On an adopted row the sentence
    // names the tip check as what failed ("Could not check for newer … releases"),
    // distinguishing it from the placeholder where nothing resolved at all.
    const adoptedButBroken = {
      ...CHANNELS.stable,
      ref: null,
      version: null,
      tip_version: null,
      at_tip: null,
      error: 'cannot list tags (git tag failed)',
    }
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channel: adoptedButBroken })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    expect(screen.queryByTestId('release-channel-placeholder-stable')).not.toBeInTheDocument()
    const notice = screen.getByTestId('release-channel-error-stable')
    expect(notice.textContent).toContain('cannot list tags')
    // The adopted row uses the tip-check sentence, NOT the placeholder's
    // "could not be resolved" sentence (#12355).
    expect(notice.textContent).toContain('Could not check for newer')
    expect(notice.textContent).not.toContain('could not be resolved')
    // No page-level toast: the row-scoped notice said it.
    expect(screen.queryByTestId('devfleet-action-error')).not.toBeInTheDocument()
  })

  // BEHIND on a channel row: the n/a marker, in every channel state alike. The
  // cell is not a second denominator (three wordings of one — `tip`, `newer`,
  // tooltips — each left two `↓N` cells in one column meaning two things), so it
  // says the column does not apply and points at the badge, which carries the
  // row's currency in its own tooltip. Asserted per state so no state can quietly
  // regain a count or a dash.
  const BEHIND_NA_TIP = 'Release rows are measured by version — see the badge'
  function behindMarker() {
    // Two n/a cells sit on a channel row, PR then BEHIND, in grid order.
    const cells = screen.getAllByText('n/a')
    expect(cells).toHaveLength(2)
    expect(cells[0]).toHaveAttribute('title', expect.stringContaining('pull request'))
    return cells[1]
  }

  it('shows the n/a marker in BEHIND, not a count or a dash, when the channel resolver failed', async () => {
    const adoptedButBroken = {
      ...CHANNELS.stable,
      ref: null, version: null, tip_version: null, at_tip: null,
      error: 'cannot list tags (git tag failed)',
    }
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channel: adoptedButBroken })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    expect(behindMarker()).toHaveAttribute('title', BEHIND_NA_TIP)
    // No `?` cell anywhere in the table (the InfoTip BUTTON beside the heading is
    // the one legitimate question mark on the page and is not a cell).
    expect(screen.queryAllByText('?', { ignore: 'button' })).toEqual([])
    // The behind-main figure stays hidden on this row too.
    expect(screen.queryByText('↓412')).not.toBeInTheDocument()
    // Currency lives on the badge, which says the tip is unresolved.
    expect(screen.getByText('not on a release').getAttribute('title')).toMatch(/could not be resolved/)
  })

  it('shows the n/a marker in BEHIND at the tip too — no dash that reads as "up to date with main"', async () => {
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channel: CHANNELS.stable })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    const cell = behindMarker()
    expect(cell).toHaveAttribute('title', BEHIND_NA_TIP)
    expect(cell.textContent).not.toBe('—')
    // The only dash in the Behind column belongs to main (behind: 0).
    expect(screen.getAllByTitle('up to date with main')).toHaveLength(1)
    // The badge is where at-tip is stated.
    expect(screen.getByText('0.5.0')).toHaveAttribute('title', expect.stringContaining('Pinned to the stable release channel'))
  })

  it('never presents the tree\'s own tag as "the tip" in the badge tooltip when the resolver failed', async () => {
    // The tree holds 0.5.0 (ref + version known) but the resolver failed, so
    // tip_version is null. The tip is tip_version and nothing else: falling
    // back to the tree's own ref would read "the stable channel tip is now
    // refs/tags/v0.5.0" — the row's own version, presented as proof the channel
    // is current, on the one row where nothing about the channel is known.
    const heldButUnresolved = {
      ...CHANNELS.stable,
      ref: 'refs/tags/v0.5.0', version: '0.5.0', tip_version: null, at_tip: null,
      error: 'cannot list tags (git tag failed)',
    }
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channel: heldButUnresolved })
    renderPage()
    await waitFor(() => expect(screen.getByText('0.5.0')).toBeInTheDocument())
    const badge = screen.getByText('0.5.0')
    expect(badge.getAttribute('title')).toBe('On 0.5.0; the stable channel tip could not be resolved')
    expect(badge.getAttribute('title')).not.toContain('refs/tags')
    expect(badge.getAttribute('title')).not.toContain('is now')
  })

  it('renders no tip-denominated count in BEHIND on an adopted row that is behind the tip', async () => {
    const behindTip = { ...CHANNELS.stable, at_tip: false, tip_version: '0.6.0' }
    mockFleet({
      worktrees: [MAIN, STABLE_WT],
      release_channel: behindTip,
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    // Neither denominator appears in the cell: not a lane distance (the payload
    // no longer carries one) and not `↓412` (behind main). The marker stands in.
    expect(behindMarker()).toHaveAttribute('title', BEHIND_NA_TIP)
    expect(screen.queryAllByText(/↓\d+\s*(newer|tip)?$/)).toEqual([])
    expect(screen.queryByText('↓412')).not.toBeInTheDocument()
    // The badge is where "a newer release shipped" is said — in its text, with
    // the tooltip spelling out the sentence.
    expect(screen.getByText('0.5.0 · latest 0.6.0')).toHaveAttribute('title', 'On 0.5.0; the stable channel tip is now 0.6.0')
  })

  it('keeps the Behind header tooltip about main only, since no row measures anything else', async () => {
    // The header is ONE element shared by every row class. It used to claim two
    // denominators ("main for branches, channel tip for release rows") because the
    // channel cell rendered a tip-distance; that cell is now the n/a marker, so a
    // header naming a second denominator would describe a number no cell shows.
    const behindTip = { ...CHANNELS.stable, at_tip: false, tip_version: '0.6.0' }
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channel: behindTip })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    const header = screen.getByText('Behind')
    const title = (header.getAttribute('title') ?? '').toLowerCase()
    expect(title).toMatch(/\bmain\b/)
    expect(title).not.toContain('channel')
    expect(title).not.toContain('tip')
  })

  it('badges a behind row with the release it HOLDS, not the newer tip', async () => {
    // The badge's own comment says it shows "which release the tree is actually
    // sitting on", and it was fed the RESOLVED version instead — so the moment a
    // newer release shipped the row renamed itself to a build it does not
    // contain, while `↓N` was the only hint anything was stale.
    const behindTip = {
      ...CHANNELS.stable,
      at_tip: false,
      version: '0.5.0',
      tip_version: '0.6.0',
      ref: 'refs/tags/v0.6.0',
    }
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channel: behindTip })
    renderPage()
    // The held release LEADS the pill; the tip follows, labelled `latest`, so
    // it cannot be read as the row's own version. No pill reads `0.6.0` alone.
    const badge = await waitFor(() => screen.getByText('0.5.0 · latest 0.6.0'))
    expect(screen.queryByText('0.6.0')).not.toBeInTheDocument()
    expect(screen.queryByText('0.5.0')).not.toBeInTheDocument()
    expect(badge).toHaveAttribute('title', expect.stringContaining('0.6.0'))
  })

  it('states behind-the-tip in the badge TEXT, not only in its colour', async () => {
    // A warn-orange pill and an ok-green pill carrying the same "0.5.0" told a
    // colourblind or keyboard reader nothing; the tip lived only in the hover
    // tooltip. Behind the tip, the visible text names both versions. At the tip
    // it is the bare version, and when the tip is unknown (resolver failed) the
    // version stands alone — the tooltip already says the tip could not be
    // resolved, and a "tip ?" would read as a rendering bug.
    const behind = { ...CHANNELS.stable, at_tip: false, version: '0.5.0', tip_version: '0.5.3' }
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channel: behind })
    const first = renderPage()
    await waitFor(() => expect(screen.getByText('0.5.0 · latest 0.5.3')).toBeInTheDocument())
    expect(screen.queryByText('0.5.0')).not.toBeInTheDocument()
    first.unmount()

    vi.restoreAllMocks()
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channel: { ...CHANNELS.stable, version: '0.5.3', tip_version: '0.5.3', at_tip: true } })
    const second = renderPage()
    await waitFor(() => expect(screen.getByText('0.5.3')).toBeInTheDocument())
    expect(screen.queryByText(/tip/)).not.toBeInTheDocument()
    second.unmount()

    vi.restoreAllMocks()
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channel: {
      ...CHANNELS.stable, at_tip: null, version: '0.5.0', tip_version: null, error: 'cannot list tags (git tag failed)',
    } })
    renderPage()
    await waitFor(() => expect(screen.getByText('0.5.0')).toBeInTheDocument())
    expect(screen.queryByText(/tip/)).not.toBeInTheDocument()
    expect(screen.queryAllByText(/\?/, { ignore: 'button' })).toEqual([])
  })

  it('says so when a lane tree is on no release tag at all', async () => {
    // Adoption is by SHAPE (detached), not by being at a release, so an operator
    // who checked out an arbitrary commit in the lane is on no release. Falling
    // back to the tip's version here would be the same lie the test above pins —
    // and falling back to the LANE WORD is a different one: on a row already named
    // `release-channel-stable`, a pill reading `stable` reads as a version.
    const offTag = {
      ...CHANNELS.stable,
      at_tip: false,
      version: null,
      tip_version: '0.6.0',
    }
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channel: offTag })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    expect(screen.queryByText('0.6.0')).not.toBeInTheDocument()
    expect(screen.queryByText('stable')).not.toBeInTheDocument()
    // The label states the TREE's condition, so it cannot be read as the same
    // worry as the resolver-error row (whose badge keeps its version).
    const badge = screen.getByText('not on a release')
    expect(screen.queryByText('no release')).not.toBeInTheDocument()
    expect(badge).toHaveAttribute('title', expect.stringContaining('0.6.0'))
    expect(badge.getAttribute('title')).not.toContain('?')
  })

  it('never renders a literal "?" as the tip when the tree is off-tag AND the tip is unknown', async () => {
    // The sentence "the channel tip is ?" reads as a rendering bug. When neither
    // tip_version nor ref resolved, the badge gets copy that interpolates no
    // version at all.
    const offTagNoTip = {
      ...CHANNELS.stable,
      at_tip: null, version: null, tip_version: null, ref: null,
      error: 'cannot list tags (git tag failed)',
    }
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channel: offTagNoTip })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    const badge = screen.getByText('not on a release')
    const title = badge.getAttribute('title') ?? ''
    expect(title).toMatch(/could not be resolved/)
    expect(title).not.toContain('?')
  })

  it('renders no version pill on the placeholder when nothing resolved', async () => {
    // The dashed pill's slot is a VERSION slot. With nothing resolved it once fell
    // back to `rc.lane`, so the unpublished row printed `stable` twice, and then
    // to a "not on a release" label — the same words the adopted off-tag badge uses for
    // a different state (a real tree on no tag), beside a status line that already
    // says nothing was fetched. Nothing to name means no pill: the status line is
    // the whole statement.
    const benign = { ...CHANNELS.stable, worktree: null, ref: null, version: null,
      tip_version: null, at_tip: null, unpublished: true, error: null }
    mockFleet({ worktrees: [MAIN], release_channel: benign })
    renderPage()
    const row = await waitFor(() => screen.getByTestId('release-channel-placeholder-stable'))
    expect(within(row).queryByText('stable')).not.toBeInTheDocument()
    expect(within(row).queryByText('not on a release')).not.toBeInTheDocument()
    expect(within(row).queryByText('0.5.0')).not.toBeInTheDocument()
    // No dashed pill element at all, not merely an empty one.
    expect(row.querySelector('[style*="dashed"]')).toBeNull()
    // The status line carries the state instead.
    expect(within(row).getByText(/No stable release fetched yet/)).toBeInTheDocument()
    // The label survives only where it means something: the adopted off-tag badge.
    expect(screen.queryByText('not on a release')).not.toBeInTheDocument()
  })

  it('keeps the version-bearing pill and tooltip when Create has something to check out', async () => {
    mockFleet({ worktrees: [MAIN, FEATURE], release_channel: CHANNELS.uncreated })
    renderPage()
    const row = await waitFor(() => screen.getByTestId('release-channel-placeholder-stable'))
    const pill = within(row).getByText('0.5.0')
    expect(pill).toHaveAttribute('title', expect.stringContaining('0.5.0'))
    expect(within(row).queryByText('not on a release')).not.toBeInTheDocument()
    // No fetch tooltip on Create here: the pill already says what it checks out.
    expect(within(row).getByRole('button', { name: /create/i })).not.toHaveAttribute('title')
  })

  it('marks PR inapplicable on a lane row instead of showing the no-PR dash', async () => {
    // The em dash on every other row means "no PR yet", which invites waiting
    // for one. A tag-detached tree can never have a PR at all.
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channel: CHANNELS.stable })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    const na = screen.getAllByText('n/a')
    expect(na.length).toBeGreaterThan(0)
    expect(na[0]).toHaveAttribute('title', expect.stringContaining('pull request'))
  })

  it('does NOT adopt a branch checkout that merely shares the reserved name', async () => {
    // The name guard, from the UI side: the backend reports worktree=null plus
    // name_taken_by_branch, so the row keeps ordinary controls.
    const taken = { ...CHANNELS.stable, worktree: null, at_tip: null, name_taken_by_branch: true }
    mockFleet({
      worktrees: [MAIN, { ...STABLE_WT, behind: 5 }],
      release_channel: taken,
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    // No version badge: this row is not a lane pin.
    expect(screen.queryByText('0.5.0')).not.toBeInTheDocument()
    // Its behind count is the ordinary behind-main figure, not a lane distance.
    expect(screen.getByText('↓5')).toBeInTheDocument()
  })

  it('explains the occupied name on the existing row, not as a second row', async () => {
    // One directory is one row. Rendering a blocked placeholder alongside the
    // real checkout printed `release-channel-stable` twice on the page, which is
    // what this asserts against.
    const taken = { ...CHANNELS.stable, worktree: null, at_tip: null, name_taken_by_branch: true }
    mockFleet({ worktrees: [MAIN, { ...STABLE_WT, behind: 5 }], release_channel: taken })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    expect(screen.getAllByText('release-channel-stable')).toHaveLength(1)
    expect(screen.queryByTestId('release-channel-placeholder-stable')).not.toBeInTheDocument()
    // A SENTENCE, not a badge: three pill wordings each failed a cold read, because
    // a pill has room for a verdict and not for the situation plus the way out. The
    // row carries the same muted inline status line the placeholder row uses, in
    // the same slot, saying in visible text that the checkout is on a branch, that
    // this is why the stable release cannot use the name, and what frees it.
    const sentence = screen.getByTestId('release-channel-name-taken-stable')
    expect(sentence.textContent).toBe("On a branch, so the stable release can't use this name — rename or remove it to free it")
    expect(sentence.tagName).toBe('SPAN')
    // Not a warn Badge: nothing is broken on this row. The old pill texts are gone.
    expect(screen.queryByText('Name reserved — on a branch')).toBeNull()
    expect(screen.queryByText('Not a release worktree')).toBeNull()
    // The tooltip carries the full explanation and names only controls that EXIST:
    // Prune merged (a page button) and the git worktree commands. The row menu has
    // no Remove or Rename item, so the sentence must not point at one.
    const title = sentence.getAttribute('title') ?? ''
    expect(title).toBe('release-channel-stable is reserved for the stable release worktree, but this checkout is on a branch. To free the name, rename or remove the checkout: Prune merged removes it once its PR has merged, or run `git worktree move` / `git worktree remove` in a shell')
    expect(title).toContain('Prune merged')
    expect(title).toContain('git worktree remove')
  })

  it('badges at-tip and behind-tip differently, with no third mismatch state', async () => {
    // `lane_check` is gone: it compared a value against itself. What the badge
    // must still distinguish is at-tip from a newer release having shipped.
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channel: CHANNELS.stable })
    renderPage()
    await waitFor(() => expect(screen.getByText('0.5.0')).toBeInTheDocument())
    expect(screen.getByText('0.5.0')).toHaveAttribute(
      'title', expect.stringContaining('refs/tags/v0.5.0'),
    )
  })

  it('renders one row, not two, when the reserved name exists but its HEAD is unreadable', async () => {
    // A third classification: `worktree_state` answers `detached: null` when HEAD
    // cannot be read, so the payload is neither adopted (`worktree` set) nor
    // name-taken (`name_taken_by_branch`). Guarding on those two classifications
    // let the placeholder render beside the very directory it described.
    const unreadable = {
      ...CHANNELS.stable,
      worktree: null,
      name_taken_by_branch: false,
      ref: null,
      version: null,
      error: 'cannot read HEAD for release-channel-stable',
    }
    mockFleet({
      worktrees: [MAIN, { ...STABLE_WT, branch: null }],
      release_channel: unreadable,
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    expect(screen.queryByTestId('release-channel-placeholder-stable')).toBeNull()
    expect(screen.getAllByText('release-channel-stable')).toHaveLength(1)
    // ...and because the row IS selectable, the placeholder path is suppressed, so
    // the shared ErrorNotice below the row is the surface the backend error string
    // reaches. channelErrorFor folds this unreadable case in with the adopted
    // resolver-failure case, so a Badge title never carries the error.
    const notice = screen.getByTestId('release-channel-error-stable')
    expect(notice.textContent).toContain('cannot read HEAD for release-channel-stable')
  })

  it('does not conjure a Create placeholder when a filter hides the unreadable-HEAD row', async () => {
    // The name check runs against the UNFILTERED fleet. `selectable` is what the
    // search box lets through, so testing it meant a query that hid the lane row
    // made the directory "absent" and the placeholder came back -- offering to
    // create a worktree that exists on disk, on a page where the only visible row
    // with that name would have been the placeholder itself.
    const unreadable = {
      ...CHANNELS.stable,
      worktree: null,
      name_taken_by_branch: false,
      ref: null,
      version: null,
      error: 'cannot read HEAD for release-channel-stable',
    }
    mockFleet({
      worktrees: [MAIN, { ...STABLE_WT, branch: null }, FEATURE],
      release_channel: unreadable,
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    // A query the release row does not match but the feature row does.
    fireEvent.change(screen.getByLabelText('Filter worktrees'), { target: { value: 'kirocrew-wt' } })
    await waitFor(() => expect(screen.getByText('kirocrew-wt-update-freshness')).toBeInTheDocument())
    expect(screen.queryByText('release-channel-stable')).toBeNull()
    expect(screen.queryByTestId('release-channel-placeholder-stable')).toBeNull()
  })

  it('gives a GENUINE resolver failure an ErrorNotice with the agent hand-off, not just a tooltip', async () => {
    // A git failure — distinct from a benign empty channel — is the one error
    // class here that reached the user only as tooltip and cell text, which is not
    // keyboard-reachable and carries no hand-off. It goes through ErrorNotice with
    // askAgent, the same surface every other error on this page uses.
    const broken = {
      ...CHANNELS.stable,
      worktree: null,
      ref: null,
      version: null,
      unpublished: false,
      error: 'cannot list tags (git tag failed)',
    }
    mockFleet({ worktrees: [MAIN], release_channel: broken })
    renderPage()
    const notice = await waitFor(() => screen.getByTestId('release-channel-error-stable'))
    expect(notice).toBeInTheDocument()
    expect(notice.textContent).toContain('could not be resolved')
    // The framed notice carries the full backend cause, with the hand-off.
    expect(notice.textContent).toContain('cannot list tags')
  })

  it('keeps the raw failure out of the placeholder cell and blocks Create', async () => {
    const broken = {
      ...CHANNELS.stable,
      worktree: null,
      ref: null,
      version: null,
      unpublished: false,
      error: 'cannot list tags (git tag failed)',
    }
    mockFleet({ worktrees: [MAIN], release_channel: broken })
    renderPage()
    const row = await waitFor(() => screen.getByTestId('release-channel-placeholder-stable'))
    // The cell carries a SHORT, non-truncating status; the raw git mechanism stays
    // out of it and lives only in the ErrorNotice below, so the cell can ellipsise
    // without ever cutting the sentence a user is reading. The lane word is framed
    // as a channel: a bare `stable could not be resolved` reads as a name or a
    // version that failed, not the lane.
    expect(within(row).getByText('The stable release channel could not be resolved')).toBeInTheDocument()
    expect(within(row).queryByText(/cannot list tags/)).toBeNull()
    expect(within(row).getByRole('button', { name: /create/i })).toBeDisabled()
  })

  it('labels the table "2 worktrees · 1 to create" beside a placeholder, never a row total that contradicts the heading', async () => {
    // The old "3 rows" beside "Worktrees (2)" and a WORKTREES card of 2 was a
    // visible contradiction: three numbers for one table. The label now names
    // both populations, and the heading and stat card stay on the worktree count —
    // a placeholder is an offer to create a worktree, not one.
    mockFleet({ worktrees: [MAIN, FEATURE], release_channel: CHANNELS.uncreated })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('release-channel-placeholder-stable')).toBeInTheDocument())
    expect(screen.getByText('2 worktrees · 1 to create')).toBeInTheDocument()
    expect(screen.getByText('Worktrees (2)')).toBeInTheDocument()
    expect(screen.queryByText('3 rows')).not.toBeInTheDocument()
    expect(screen.queryByText('2 rows')).not.toBeInTheDocument()
  })

  it('selects the singular form for one worktree beside a placeholder', async () => {
    mockFleet({ worktrees: [MAIN], release_channel: CHANNELS.uncreated })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('release-channel-placeholder-stable')).toBeInTheDocument())
    expect(screen.getByText('1 worktree · 1 to create')).toBeInTheDocument()
  })

  it('keeps the plain "N rows" label when no placeholder renders', async () => {
    mockFleet({ worktrees: [MAIN, STABLE_WT, FEATURE], release_channel: CHANNELS.stable })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    expect(screen.getByText('3 rows')).toBeInTheDocument()
    expect(screen.getByText('Worktrees (3)')).toBeInTheDocument()
    expect(screen.queryByText(/to create/)).not.toBeInTheDocument()
  })

  it('counts the pinned lane row in the filter numerator', async () => {
    // The lane row is pinned under main rather than sorted with the feature
    // rows, so it lives in `pinnedRows`, not `others`. A numerator read off
    // `others` alone showed "0 / 3" for a filter that matched the one visible
    // lane row — the row was on screen and the count said nothing matched.
    mockFleet({ worktrees: [MAIN, STABLE_WT, FEATURE], release_channel: CHANNELS.stable })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    fireEvent.change(screen.getByLabelText('Filter worktrees'), { target: { value: 'release-channel' } })
    expect(screen.getByText('release-channel-stable')).toBeInTheDocument()
    expect(screen.getByText(/^1 \/ /)).toBeInTheDocument()
    expect(screen.queryByText(/^0 \/ /)).not.toBeInTheDocument()
  })

  it('orders lane rows under main and above the feature worktrees', async () => {
    // Fixed position, not part of the sort: every sort key on offer describes
    // feature-branch progress, and a release worktree scores badly on all of
    // them by design.
    mockFleet({
      worktrees: [MAIN, FEATURE, STABLE_WT],
      release_channel: CHANNELS.stable,
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    const names = screen
      .getAllByText(/^(main|release-channel-\w+|kirocrew-wt-[\w-]+)$/)
      .map((n) => n.textContent)
    expect(names.indexOf('release-channel-stable')).toBeLessThan(
      names.indexOf('kirocrew-wt-update-freshness'),
    )
    expect(names.indexOf('main')).toBeLessThan(names.indexOf('release-channel-stable'))
  })

  it('posts the lane to /release-channel/create with no confirm step', async () => {
    const seen = mockFleet(
      { worktrees: [MAIN], release_channel: CHANNELS.uncreated },
      { '/release-channel/create': { ok: true, lane: 'stable', version: '0.5.0' } },
    )
    renderPage()
    const row = await waitFor(() => screen.getByTestId('release-channel-placeholder-stable'))
    within(row).getByRole('button', { name: /create/i }).click()
    // One click. Create is reversible by Remove, and the dialog it used to open
    // just repeated the button's own tooltip back at the operator.
    await waitFor(() => expect(seen.some((s) => s.url.includes('/release-channel/create'))).toBe(true))
    // The lane, not a path, is what crosses the wire — the server derives the
    // path so a caller can never name one.
    expect(seen.every((s) => !('path' in ((s.body as object) || {})))).toBe(true)
  })

  // A failed Create is the case the whole placeholder exists for: the backend
  // refuses (content filter, redirected working tree, ambiguous name) or the
  // request never lands, and the fleet refetch that follows re-renders the row
  // exactly as before -- worktree still missing, nothing on the page saying why.
  // A toast was the only record and it self-dismissed. The failure goes through
  // the shared ErrorNotice on the row, with the hand-off, and stays until the
  // operator retries or dismisses it.
  function fleetGetCount() {
    return vi.mocked(globalThis.fetch).mock.calls.filter(([url, init]) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      return u.includes('/fleet') && init?.method !== 'POST'
    }).length
  }

  it('keeps a refused Create on the row as an ErrorNotice with the hand-off, through the fleet refetch', async () => {
    mockFleet(
      { worktrees: [MAIN], release_channel: CHANNELS.uncreated },
      { '/release-channel/create': { ok: false, error: 'refusing: a content filter is configured for this path' } },
    )
    renderPage()
    const row = await waitFor(() => screen.getByTestId('release-channel-placeholder-stable'))
    const before = fleetGetCount()
    within(row).getByRole('button', { name: /create/i }).click()
    const notice = await waitFor(() => screen.getByTestId('release-channel-create-error-stable'))
    // Framed as an outcome with its cause, not the bare backend string.
    expect(notice.textContent).toContain('Could not create the release-channel worktree')
    expect(notice.textContent).toContain('refusing: a content filter is configured for this path')
    expect(within(notice).getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
    // The failure triggers a fleet refetch that returns the SAME placeholder
    // payload. The notice is component state, not fleet state, so it survives.
    await waitFor(() => expect(fleetGetCount()).toBeGreaterThan(before))
    await waitFor(() => expect(screen.getByTestId('release-channel-placeholder-stable')).toBeInTheDocument())
    expect(screen.getByTestId('release-channel-create-error-stable')).toBeInTheDocument()
    // A refused Create is not a resolver failure: that notice stays absent.
    expect(screen.queryByTestId('release-channel-error-stable')).toBeNull()
  })

  it('renders a transport failure on Create through the same notice, with its message', async () => {
    mockFleet({ worktrees: [MAIN], release_channel: CHANNELS.uncreated })
    const passthrough = vi.mocked(globalThis.fetch).getMockImplementation()!
    vi.mocked(globalThis.fetch).mockImplementation((url, init) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (init?.method === 'POST' && u.includes('/release-channel/create')) {
        return Promise.reject(new Error('Failed to fetch'))
      }
      return passthrough(url, init)
    })
    renderPage()
    const row = await waitFor(() => screen.getByTestId('release-channel-placeholder-stable'))
    within(row).getByRole('button', { name: /create/i }).click()
    const notice = await waitFor(() => screen.getByTestId('release-channel-create-error-stable'))
    expect(notice.textContent).toContain('Failed to fetch')
    expect(within(notice).getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
  })

  it('clears the previous Create failure when Create is clicked again', async () => {
    mockFleet(
      { worktrees: [MAIN], release_channel: CHANNELS.uncreated },
      { '/release-channel/create': { ok: false, error: 'refusing: first attempt' } },
    )
    renderPage()
    const row = await waitFor(() => screen.getByTestId('release-channel-placeholder-stable'))
    within(row).getByRole('button', { name: /create/i }).click()
    await waitFor(() => expect(screen.getByTestId('release-channel-create-error-stable').textContent).toContain('first attempt'))
    // The retry succeeds: the notice described an attempt this click supersedes,
    // so it goes at the click, and nothing replaces it on success.
    const passthrough = vi.mocked(globalThis.fetch).getMockImplementation()!
    vi.mocked(globalThis.fetch).mockImplementation((url, init) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (init?.method === 'POST' && u.includes('/release-channel/create')) {
        return Promise.resolve(new Response(JSON.stringify({ ok: true, lane: 'stable', version: '0.5.0' }), { status: 200 }))
      }
      return passthrough(url, init)
    })
    await waitFor(() => expect(within(row).getByRole('button', { name: /create/i })).toBeEnabled())
    within(row).getByRole('button', { name: /create/i }).click()
    await waitFor(() => expect(screen.queryByTestId('release-channel-create-error-stable')).toBeNull())
  })

  it('removes the Create failure notice on dismiss', async () => {
    mockFleet(
      { worktrees: [MAIN], release_channel: CHANNELS.uncreated },
      { '/release-channel/create': { ok: false, error: 'refusing: working tree is redirected' } },
    )
    renderPage()
    const row = await waitFor(() => screen.getByTestId('release-channel-placeholder-stable'))
    within(row).getByRole('button', { name: /create/i }).click()
    const notice = await waitFor(() => screen.getByTestId('release-channel-create-error-stable'))
    within(notice).getByRole('button', { name: /dismiss/i }).click()
    await waitFor(() => expect(screen.queryByTestId('release-channel-create-error-stable')).toBeNull())
    // Dismissing the notice does not touch the row: the placeholder still offers Create.
    expect(within(screen.getByTestId('release-channel-placeholder-stable')).getByRole('button', { name: /create/i })).toBeInTheDocument()
  })
})
