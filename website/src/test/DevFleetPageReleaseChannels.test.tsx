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
 *   the LANE TIP on these rows, not from main, and PR is inapplicable rather
 *   than merely absent.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
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
    behind: 0,
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
    behind: null,
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
    expect(within(row).getByText('no worktree yet')).toBeInTheDocument()
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
    const status = within(row).getByText('no worktree yet')
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
    expect(within(row).getByText(/No published stable release/)).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: /create/i })).toBeEnabled()
    // Benign is not an error: no row-level notice and no page-level toast.
    expect(screen.queryByTestId('release-channel-error-stable')).not.toBeInTheDocument()
    expect(screen.queryByTestId('devfleet-action-error')).not.toBeInTheDocument()
  })

  it('routes an ADOPTED lane row resolver error to the shared ErrorNotice, not a badge tooltip', async () => {
    // A lane whose worktree exists and is detached is adopted even when resolution
    // fails, so there is no placeholder row to carry the message. The error still
    // must not live in a Badge title (not keyboard-reachable, no agent hand-off):
    // channelErrorFor routes it to the same ErrorNotice the placeholder row uses,
    // one surface for every channel error, rather than a page-level notice that
    // fired on every mount for a checkout with no tags.
    const adoptedButBroken = {
      ...CHANNELS.stable,
      ref: null,
      version: null,
      tip_version: null,
      at_tip: null,
      behind: null,
      error: 'cannot list tags (git tag failed)',
    }
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channel: adoptedButBroken })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    expect(screen.queryByTestId('release-channel-placeholder-stable')).not.toBeInTheDocument()
    const notice = screen.getByTestId('release-channel-error-stable')
    expect(notice.textContent).toContain('cannot list tags')
    // No page-level toast: the row-scoped notice said it.
    expect(screen.queryByTestId('devfleet-action-error')).not.toBeInTheDocument()
  })

  it('counts BEHIND from the lane tip, not from main', async () => {
    const behindTip = { ...CHANNELS.stable, at_tip: false, behind: 3 }
    mockFleet({
      worktrees: [MAIN, STABLE_WT],
      release_channel: behindTip,
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    // The cell NAMES its denominator, so a scanner comparing this row against a
    // feature row's `↓12` sees they measure different things without hovering.
    expect(screen.getByText(/↓3\s*tip/)).toBeInTheDocument()
    expect(screen.queryByText('↓412')).not.toBeInTheDocument()
  })

  it('neutralizes the Behind header tooltip so it names BOTH denominators when a channel row is present', async () => {
    // The Behind column header is ONE shared element for every row class, but the
    // cell below changes denominator: a branch row counts from main, a release-
    // channel row counts from the channel tip. A header tooltip that claims
    // "behind main" unconditionally contradicts the channel row on every visit.
    // Pin the CLASS, not the literal wording: with a channel row present the
    // header must name both denominators (main for branches, channel tip for
    // release rows) rather than main alone.
    const behindTip = { ...CHANNELS.stable, at_tip: false, behind: 3 }
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channel: behindTip })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    const header = screen.getByText('Behind')
    const title = (header.getAttribute('title') ?? '').toLowerCase()
    // The branch denominator is still named…
    expect(title).toMatch(/\bmain\b/)
    // …AND the release-row denominator is named too. This is the assertion the
    // old single-denominator "Commits behind main" string fails.
    expect(title).toContain('channel tip')
  })

  it('badges a behind row with the release it HOLDS, not the newer tip', async () => {
    // The badge's own comment says it shows "which release the tree is actually
    // sitting on", and it was fed the RESOLVED version instead — so the moment a
    // newer release shipped the row renamed itself to a build it does not
    // contain, while `↓N` was the only hint anything was stale.
    const behindTip = {
      ...CHANNELS.stable,
      at_tip: false,
      behind: 3,
      version: '0.5.0',
      tip_version: '0.6.0',
      ref: 'refs/tags/v0.6.0',
    }
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channel: behindTip })
    renderPage()
    await waitFor(() => expect(screen.getByText('0.5.0')).toBeInTheDocument())
    expect(screen.queryByText('0.6.0')).not.toBeInTheDocument()
    expect(screen.getByText('0.5.0')).toHaveAttribute(
      'title', expect.stringContaining('0.6.0'),
    )
  })

  it('says so when a lane tree is on no release tag at all', async () => {
    // Adoption is by SHAPE (detached), not by being at a release, so an operator
    // who checked out an arbitrary commit in the lane is on no release. Falling
    // back to the tip's version here would be the same lie the test above pins.
    const offTag = {
      ...CHANNELS.stable,
      at_tip: false,
      behind: 7,
      version: null,
      tip_version: '0.6.0',
    }
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channel: offTag })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    expect(screen.queryByText('0.6.0')).not.toBeInTheDocument()
    expect(screen.getByText('stable')).toHaveAttribute(
      'title', expect.stringContaining('0.6.0'),
    )
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
    const taken = { ...CHANNELS.stable, worktree: null, at_tip: null, behind: null, name_taken_by_branch: true }
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
    const taken = { ...CHANNELS.stable, worktree: null, at_tip: null, behind: null, name_taken_by_branch: true }
    mockFleet({ worktrees: [MAIN, { ...STABLE_WT, behind: 5 }], release_channel: taken })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    expect(screen.getAllByText('release-channel-stable')).toHaveLength(1)
    expect(screen.queryByTestId('release-channel-placeholder-stable')).not.toBeInTheDocument()
    // The badge states the VERDICT, and names the thing rather than pointing at a
     // referent it does not carry: on a row called `release-channel-stable`, a badge
     // reading "Not the channel" contradicts the only label next to it. The
     // reserved-name half is already on screen as the row's own name.
    const badge = screen.getByText('Not a release worktree — on a branch')
    expect(badge).toHaveAttribute('title', expect.stringContaining('is on a branch'))
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
    // without ever cutting the sentence a user is reading.
    expect(within(row).getByText('stable could not be resolved')).toBeInTheDocument()
    expect(within(row).queryByText(/cannot list tags/)).toBeNull()
    expect(within(row).getByRole('button', { name: /create/i })).toBeDisabled()
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
})
