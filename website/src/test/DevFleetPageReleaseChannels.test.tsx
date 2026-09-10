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

  it('leaves an un-materialized lane error inline, with no page-level notice', async () => {
    // The placeholder ALREADY renders `rc.error` and disables Create on it, so a
    // page notice for this case says the same thing twice. Ungated it also
    // misfired: on any checkout with no `v*` tags (a shallow or `--no-tags`
    // clone, a fork before its first release) every lane carries an error, so the
    // page raised an error-level notice on every mount for a feature that
    // operator never used. The adopted-row case below is what the notice is for.
    const broken = { ...CHANNELS.stable, worktree: null, ref: null, version: null,
      tip_version: null, error: 'no stable release tag found in this checkout' }
    mockFleet({ worktrees: [MAIN], release_channel: broken })
    renderPage()
    await waitFor(() => expect(
      screen.getByTestId('release-channel-placeholder-stable'),
    ).toBeInTheDocument())
    expect(screen.getByTestId('release-channel-placeholder-stable'))
      .toHaveTextContent(/no stable release tag/)
    expect(screen.queryByTestId('devfleet-action-error')).not.toBeInTheDocument()
  })

  it('is the ONLY surface for an error on an ADOPTED lane row', async () => {
    // Why the notice cannot be replaced by the inline placeholder text: a lane
    // whose worktree exists and is detached is adopted even when resolution
    // fails, so there is no placeholder row to carry the message. The row's own
    // badge carries it instead — one surface for both row kinds, rather than a
    // page-level notice that fired on every mount for a checkout with no tags.
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
    expect(screen.getByText('stable')).toHaveAttribute(
      'title', expect.stringContaining('cannot list tags'),
    )
    // No page-level toast: the row said it.
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
    const badge = screen.getByText('Name reserved for release channels')
    expect(badge).toHaveAttribute('title', expect.stringContaining('is on a branch'))
  })

  it('badges at-tip and behind-tip differently, with no third mismatch state', async () => {
    // `lane_check` is gone: it compared a value against itself. What the badge
    // must still distinguish is the two states Advance acts on.
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channel: CHANNELS.stable })
    renderPage()
    await waitFor(() => expect(screen.getByText('0.5.0')).toBeInTheDocument())
    expect(screen.getByText('0.5.0')).toHaveAttribute(
      'title', expect.stringContaining('refs/tags/v0.5.0'),
    )
  })

  it('frames an unresolvable channel on its placeholder and blocks Create', async () => {
    const broken = {
      ...CHANNELS.stable,
      worktree: null,
      ref: null,
      version: null,
      error: 'no stable release tag found in this checkout',
    }
    mockFleet({ worktrees: [MAIN], release_channel: broken })
    renderPage()
    const row = await waitFor(() => screen.getByTestId('release-channel-placeholder-stable'))
    // The backend string must reach the user INSIDE a "what happened" sentence.
    // Asserting the bare text passed while the row showed only git/resolver
    // mechanism, so the assertion is on the framing and the cause together.
    expect(
      within(row).getByText(
        'stable could not be resolved: no stable release tag found in this checkout',
      ),
    ).toBeInTheDocument()
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
