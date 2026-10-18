/**
 * DevFleetPage: host notices, row context and the row menus.
 *
 * The pods-unavailable notice and which row actions stay on a host without pods, the
 * serving-install and inferred-checkout notices, Make live on the main row, the PR
 * badge and drill-in context, and the row-actions menu and Pull+Build confirm popover
 * (portal, flip, keyboard focus). DevFleetPage.test.tsx holds the rest of this suite.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'

import DevFleetPage, { __resetDevFleetNoticesForTests } from '../pages/DevFleetPage'

function renderPage() {
  return renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
}

// The page keeps its latest action failure in module state (so a failure that
// lands while the page is unmounted is still shown on return); a suite must
// not carry one test's failure into the next.
beforeEach(() => { __resetDevFleetNoticesForTests() })

describe('DevFleetPage', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  // --- pods unavailable on this host (non-Linux / no systemctl) ---
  // Before pods_available existed, the backend computed a reason string and
  // never sent it, so these controls rendered and silently failed.
  const FLEET_NO_PODS = {
    pods_available: false,
    pods_unavailable_reason: 'Pods are Linux systemd --user units; this host is darwin. Preview a worktree with ./dev-backend.sh instead.',
    worktrees: [
      { name: 'main', is_main: true, running: false, has_dist: true, behind: 0 },
      { name: 'feature-x', is_main: false, running: false, has_dist: true, behind: 0, path: '/wt/feature-x' },
    ],
  }

  function mockFleet(payload: unknown) {
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(payload), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 1024 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
  }

  // --- serving install differs from the managed checkout ---
  // The silent-wrong-answer case: Pull+Build fast-forwards the checkout and
  // reports success while an older install keeps serving, so no other control
  // on this page reveals that the managed code is not the running code.
  it('warns when the install serving the dashboard is not the managed checkout', async () => {
    mockFleet({
      serving_install_reason: 'this dashboard is served by the install at /Applications/KiroCrew.app/Contents/Resources/backend-dist/kirocrew-backend-arm64/lib/python3.12/site-packages/kiro_crew, which is not inside the checkout Dev Fleet manages (/Users/dev/kirocrew).',
      worktrees: [{ name: 'main', is_main: true, running: false, has_dist: true, behind: 0 }],
    })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('serving-install-warning')).toBeInTheDocument())
    // Surfaced verbatim, both installs named.
    expect(screen.getByText(/not inside the checkout Dev Fleet manages/)).toBeInTheDocument()
    expect(screen.getByText(/backend-dist/)).toBeInTheDocument()
  })

  it('shows no serving-install warning for a matching install', async () => {
    mockFleet({
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0 },
        { name: 'feature-x', is_main: false, running: false, has_dist: true, behind: 0, path: '/wt/feature-x' },
      ],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    expect(screen.queryByTestId('serving-install-warning')).toBeNull()
  })

  // The notices share the content column with the stat cards and the Worktrees
  // card below them, and neither of those is width-capped. A cap on the notices
  // alone leaves them hugging the left edge of a wide window while everything
  // under them runs full width, which reads as a half-rendered page.
  it('lets the notices fill the content column instead of capping their width', async () => {
    mockFleet({
      serving_install_reason: 'served by an install outside the managed checkout.',
      main_repo_inferred: true,
      main_repo: '/Users/dev/kirocrew',
      worktrees: [{ name: 'main', is_main: true, running: false, has_dist: true, behind: 0 }],
    })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('serving-install-warning')).toBeInTheDocument())
    for (const id of ['serving-install-warning', 'inferred-main-checkout']) {
      const capped = Array.from(screen.getByTestId(id).classList).filter((c) => c.startsWith('max-w-'))
      expect(capped).toEqual([])
    }
  })

  it('explains WHY pods are unavailable instead of failing silently', async () => {
    mockFleet(FLEET_NO_PODS)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    expect(screen.getByText('Pods are unavailable on this host.')).toBeInTheDocument()
    // The backend's reason is surfaced verbatim, including the suggested fix.
    expect(screen.getByText(/this host is darwin/)).toBeInTheDocument()
    expect(screen.getByText(/dev-backend\.sh/)).toBeInTheDocument()
  })

  it('hides pod-dependent row actions when pods cannot run, but keeps the rest', async () => {
    mockFleet(FLEET_NO_PODS)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('More actions'))
    // Rebase is platform-neutral and stays -> proves the menu really opened.
    expect(await screen.findByText('Rebase onto base branch')).toBeInTheDocument()
    const menu = within(await screen.findByRole('menu'))
    for (const gone of ['Spin up pod', 'QA + video', 'Stop pod', 'Restart pod']) {
      expect(menu.queryByText(gone)).toBeNull()
    }
    // Make live is NOT pod-dependent: staging writes only the live-target
    // pointer, so hiding it here would hide it on the hosts it exists to serve.
    expect(menu.getByText('Make live')).toBeInTheDocument()
  })

  it('keeps Provision available without pods (pod provision never touches systemd)', async () => {
    mockFleet({
      ...FLEET_NO_PODS,
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0 },
        { name: 'unbuilt', is_main: false, running: false, has_dist: false, behind: 0, path: '/wt/unbuilt' },
      ],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('unbuilt')).toBeInTheDocument())
    expect(screen.getByText('Provision')).toBeInTheDocument()
  })

  it('keeps pod actions when the backend omits pods_available (older backend)', async () => {
    // Absent field must not be read as "unsupported" — that would silently
    // strip pod controls from a perfectly capable Linux host.
    mockFleet({
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0 },
        { name: 'feature-x', is_main: false, running: false, has_dist: true, behind: 0, path: '/wt/feature-x' },
      ],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    expect(screen.queryByText('Pods are unavailable on this host.')).toBeNull()
    fireEvent.click(screen.getByLabelText('More actions'))
    expect(await screen.findByText('Spin up pod')).toBeInTheDocument()
  })

  it('shows an inline "Make live" on the MAIN row when main is NOT live (switch back after cutover)', async () => {
    // A feature worktree is live; main is dormant (is_live:false). The main
    // row must offer Make live so the operator can cut back to main.
    const FLEET_MAIN_DORMANT = {
      gateway_service_active: true,
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0, is_live: false, path: '/wt/main' },
        { name: 'feature-x', is_main: false, running: false, has_dist: true, behind: 0, is_live: true, path: '/wt/feature-x' },
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET_MAIN_DORMANT), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 1024 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getAllByText('main').length).toBeGreaterThan(0))
    // The dormant main row exposes an inline Make live control (feature-x is
    // live, so its own menu — unopened here — has no Make live to collide).
    const btn = await screen.findByTitle('Repoint the live gateway back at main (restarts the gateway)')
    expect(btn).toBeInTheDocument()
    fireEvent.click(btn)
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument())
    expect(screen.getByText('Make “main” live?')).toBeInTheDocument()
  })

  it('hides "Make live" on the MAIN row when main IS live', async () => {
    const FLEET_MAIN_LIVE = {
      gateway_service_active: true,
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0, is_live: true, path: '/wt/main' },
        { name: 'feature-x', is_main: false, running: false, has_dist: true, behind: 0, is_live: false, path: '/wt/feature-x' },
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET_MAIN_LIVE), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 1024 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    // Main is live -> no inline Make live control on the main row (feature-x's
    // menu is closed, so its Make live is not rendered either).
    expect(screen.queryByTitle('Repoint the live gateway back at main (restarts the gateway)')).toBeNull()
  })

  it('compact row: PR badge is a link with PR title as hover title, and shows the summary one-liner', async () => {
    const FLEET_CTX = {
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0 },
        {
          name: 'feature-x', is_main: false, running: false, has_dist: true, behind: 0,
          pr: { number: 42, state: 'OPEN', url: 'https://github.com/org/repo/pull/42', title: 'Add pagination' },
          summary: 'feat: add pagination to users API',
        },
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET_CTX), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 1024 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    // PR badge is wrapped in an <a> whose title attribute is the PR title.
    const link = screen.getByTitle('Add pagination')
    expect(link.tagName.toLowerCase()).toBe('a')
    expect(link).toHaveAttribute('href', 'https://github.com/org/repo/pull/42')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    // Purpose one-liner shows inline in the compact row.
    expect(screen.getByText('feat: add pagination to users API')).toBeInTheDocument()
  })

  it('drill-in shows issue chips, ticket chips, and the purpose summary', async () => {
    const FLEET_ONE = {
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0 },
        { name: 'feature-x', is_main: false, running: false, has_dist: true, behind: 0 },
      ],
    }
    const DETAIL = {
      branch: 'feat/x',
      pr: { number: 42, state: 'OPEN', url: 'https://github.com/org/repo/pull/42', title: 'Add pagination' },
      issues: [{ number: 147, url: 'https://github.com/org/repo/issues/147' }],
      tickets: [{ id: 'TT-5', url: 'https://tracker.example.com/TT-5' }],
      summary: 'feat: add pagination to users API',
      commits: [],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/worktree?name=')) return Promise.resolve(new Response(JSON.stringify(DETAIL), { status: 200 }))
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET_ONE), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 1024 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    // Expand feature-x (the only non-main row -> a single Expand control).
    fireEvent.click(screen.getByLabelText('Expand'))
    const issueLink = await screen.findByText('#147')
    expect(issueLink.tagName.toLowerCase()).toBe('a')
    expect(issueLink).toHaveAttribute('href', 'https://github.com/org/repo/issues/147')
    expect(issueLink).toHaveAttribute('rel', 'noopener noreferrer')
    const ticketLink = screen.getByText('TT-5')
    expect(ticketLink.tagName.toLowerCase()).toBe('a')
    expect(ticketLink).toHaveAttribute('href', 'https://tracker.example.com/TT-5')
    // The purpose one-liner renders in the drill-in.
    expect(screen.getByText('feat: add pagination to users API')).toBeInTheDocument()
  })

  /* ─── Row-actions dropdown: portal + flip ─── */
  // A worktree row whose "More actions" menu has items: non-main, not live,
  // has_dist & not running → Spin up pod / Rebase onto base branch / Make live.
  const FLEET_MENU = {
    worktrees: [
      { name: 'main', is_main: true, running: false, has_dist: true, behind: 0 },
      { name: 'feature-x', is_main: false, running: false, has_dist: true, behind: 0, path: '/wt/feature-x' },
    ],
  }
  function mockFleet(data: unknown) {
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(data), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 1024 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
  }

  it('renders the row-actions dropdown in a portal on document.body (escapes Card overflow)', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('More actions'))
    const menu = await screen.findByRole('menu')
    // Portaled: the menu is a direct child of <body>, not nested inside the SPA
    // container / row Card — this is what lets it escape Card overflow clipping.
    expect(menu.parentElement).toBe(document.body)
    // Items render inside the portaled menu and are reachable.
    expect(screen.getByText('Rebase onto base branch')).toBeInTheDocument()
    expect(within(menu).getByText('Make live')).toBeInTheDocument()
  })

  it('portaled row-actions items are clickable (opens the Make live dialog)', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('More actions'))
    fireEvent.click(within(await screen.findByRole('menu')).getByText('Make live'))
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument())
    expect(screen.getByText('Make “feature-x” live?')).toBeInTheDocument()
  })

  it('outside-click closes the portaled row-actions menu', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('More actions'))
    expect(await screen.findByRole('menu')).toBeInTheDocument()
    // A click on <body> is outside both the trigger and the portaled menu.
    fireEvent.mouseDown(document.body)
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
  })

  it('Escape closes the portaled row-actions menu', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('More actions'))
    expect(await screen.findByRole('menu')).toBeInTheDocument()
    fireEvent.keyDown(document.body, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
  })

  it('row-actions menu opens downward when there is room below', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    const trigger = screen.getByLabelText('More actions')
    vi.spyOn(trigger, 'getBoundingClientRect').mockReturnValue({
      top: 100, bottom: 120, left: 400, right: 440, width: 40, height: 20, x: 400, y: 100, toJSON: () => ({}),
    } as DOMRect)
    fireEvent.click(trigger)
    const menu = await screen.findByRole('menu') as HTMLElement
    expect(menu.getAttribute('data-placement')).toBe('down')
    // Downward placement anchors via `top`, not `bottom`.
    expect(menu.style.top).not.toBe('')
    expect(menu.style.bottom).toBe('')
  })

  it('row-actions menu flips upward when near the viewport bottom', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    const trigger = screen.getByLabelText('More actions')
    // Trigger a few px above the bottom edge → no room below → flip up.
    vi.spyOn(trigger, 'getBoundingClientRect').mockReturnValue({
      top: window.innerHeight - 8, bottom: window.innerHeight - 4, left: 400, right: 440, width: 40, height: 20, x: 400, y: window.innerHeight - 8, toJSON: () => ({}),
    } as DOMRect)
    fireEvent.click(trigger)
    const menu = await screen.findByRole('menu') as HTMLElement
    expect(menu.getAttribute('data-placement')).toBe('up')
    // Upward placement anchors via `bottom`, not `top`.
    expect(menu.style.bottom).not.toBe('')
    expect(menu.style.top).toBe('')
  })

  /* ─── Pull+Build confirm popover: portal + flip ─── */
  // The confirm popover used to be position:absolute inside the row, so the
  // Worktrees Card's `.card-glow { overflow: hidden }` clipped it. It is now
  // portaled to <body> with fixed positioning, like the row-actions menu.
  async function openPullBuildConfirm({ focusTrigger = false } = {}) {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    const trigger = screen.getByText('Pull+Build').closest('button') as HTMLButtonElement
    // A real browser focuses a clicked button; the test DOM does not. Only a
    // test that depends on that (the focus-return policy below) needs it, so
    // it stays opt-in rather than papering over the difference everywhere.
    if (focusTrigger) trigger.focus()
    fireEvent.click(trigger)
    return { trigger, pop: await screen.findByRole('dialog') }
  }

  it('renders the Pull+Build confirm popover in a portal on document.body (escapes Card overflow)', async () => {
    const { pop } = await openPullBuildConfirm()
    // Portaled: a direct child of <body>, not nested inside the row Card whose
    // overflow:hidden previously cut the popover off mid-render.
    expect(pop.parentElement).toBe(document.body)
    expect(pop.getAttribute('aria-label')).toBe('Pull + Build main')
    expect(within(pop).getByText('Pulls the base branch and rebuilds (~6 min). Does NOT restart.')).toBeInTheDocument()
  })

  it('Start inside the portaled confirm popover still fires the sync request', async () => {
    const { pop } = await openPullBuildConfirm()
    // The outside-click guard must exclude the portaled popover itself, or the
    // mousedown preceding this click would close it before the click lands.
    fireEvent.click(within(pop).getByText('Start'))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await waitFor(() => {
      const calls = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls
      const urls = calls.map((c) => (typeof c[0] === 'string' ? c[0] : (c[0] as Request).url))
      expect(urls.some((u) => u.includes('/sync'))).toBe(true)
    })
  })

  it('outside-click and Escape close the portaled confirm popover', async () => {
    const { trigger } = await openPullBuildConfirm()
    fireEvent.mouseDown(document.body)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    fireEvent.click(trigger)
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    fireEvent.keyDown(document.body, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(trigger).toHaveFocus()
  })

  it('keeps keyboard focus within the confirm popover and restores it after Cancel', async () => {
    const { trigger, pop } = await openPullBuildConfirm()
    const cancel = within(pop).getByText('Cancel')
    const start = within(pop).getByText('Start')
    expect(pop.getAttribute('aria-modal')).toBe('true')
    expect(cancel).toHaveFocus()

    // The browser's normal Tab behavior moves from Cancel to Start. The dialog
    // only owns the two boundaries that would otherwise leave it.
    start.focus()
    fireEvent.keyDown(document.body, { key: 'Tab' })
    expect(cancel).toHaveFocus()
    fireEvent.keyDown(document.body, { key: 'Tab', shiftKey: true })
    expect(start).toHaveFocus()

    fireEvent.click(cancel)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(trigger).toHaveFocus()
  })

  it('outside-click dismissal leaves focus where the click put it, not back on the trigger', async () => {
    // The popover now takes Escape, the Tab ring and the IME latch from the
    // shared dialog contract, but NOT its focus-return half: that restores on
    // unmount unconditionally, and an outside click must leave focus where the
    // browser routed it (#2533). Turn `restoreFocus` back on and this goes red.
    const { trigger, pop } = await openPullBuildConfirm({ focusTrigger: true })
    expect(within(pop).getByText('Cancel')).toHaveFocus()
    fireEvent.mouseDown(document.body)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(trigger).not.toHaveFocus()
  })

  it('declines a boundary Tab that belongs to an IME composition', async () => {
    // On WebKit the keydown that commits a candidate arrives AFTER
    // compositionend with `isComposing` already false — unguarded, the trap
    // would yank focus and abort the composition. Both ring boundaries are
    // buttons today, so no composition can start on them; this pins that the
    // trap stays safe if the popover ever grows a text field.
    const { pop } = await openPullBuildConfirm()
    const cancel = within(pop).getByText('Cancel')
    const start = within(pop).getByText('Start')
    expect(cancel).toHaveFocus()

    fireEvent.compositionStart(cancel)
    fireEvent.compositionEnd(cancel)
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })

    // Declined: focus stays where it was instead of wrapping to Start.
    expect(cancel).toHaveFocus()
    expect(start).not.toHaveFocus()
  })

  it('declines the forward-boundary Tab too — each branch carries its own claim', async () => {
    // The scan requires the claim BETWEEN a branch and its focus move, but a
    // behavioural pin per boundary is what proves the claim actually runs:
    // one guarded branch must not stand in for its sibling.
    const { pop } = await openPullBuildConfirm()
    const start = within(pop).getByText('Start')
    start.focus()
    expect(start).toHaveFocus()

    fireEvent.compositionStart(start)
    fireEvent.compositionEnd(start)
    fireEvent.keyDown(document, { key: 'Tab' })

    // Declined: no wrap back to Cancel.
    expect(start).toHaveFocus()
  })

  it('declines an Escape that belongs to an IME composition (popover stays open)', async () => {
    // Escape on the same document-capture listener closes the popover AND
    // yanks focus back to the trigger — the same harm as the Tab wrap, so
    // the same claim guards it.
    const { pop } = await openPullBuildConfirm()
    const cancel = within(pop).getByText('Cancel')

    fireEvent.compositionStart(cancel)
    fireEvent.compositionEnd(cancel)
    fireEvent.keyDown(document, { key: 'Escape' })

    // Declined: the popover is still there and focus did not move.
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(cancel).toHaveFocus()
  })

  it('confirm popover opens downward when there is room below', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    const trigger = screen.getByText('Pull+Build').closest('button') as HTMLButtonElement
    vi.spyOn(trigger, 'getBoundingClientRect').mockReturnValue({
      top: 100, bottom: 120, left: 400, right: 440, width: 40, height: 20, x: 400, y: 100, toJSON: () => ({}),
    } as DOMRect)
    fireEvent.click(trigger)
    const pop = await screen.findByRole('dialog') as HTMLElement
    expect(pop.getAttribute('data-placement')).toBe('down')
    expect(pop.style.position).toBe('fixed')
    expect(pop.style.top).not.toBe('')
    expect(pop.style.bottom).toBe('')
  })

  it('confirm popover flips upward when near the viewport bottom', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    const trigger = screen.getByText('Pull+Build').closest('button') as HTMLButtonElement
    // A row a few px above the bottom edge is exactly the case in the bug
    // report: no room below, so the popover must flip up instead of being cut.
    vi.spyOn(trigger, 'getBoundingClientRect').mockReturnValue({
      top: window.innerHeight - 8, bottom: window.innerHeight - 4, left: 400, right: 440, width: 40, height: 20, x: 400, y: window.innerHeight - 8, toJSON: () => ({}),
    } as DOMRect)
    fireEvent.click(trigger)
    const pop = await screen.findByRole('dialog') as HTMLElement
    expect(pop.getAttribute('data-placement')).toBe('up')
    expect(pop.style.bottom).not.toBe('')
    expect(pop.style.top).toBe('')
  })

  /* ─── Row-actions menu: focus containment (#2533) ─── */
  // ConfirmBtn's half of #2533 landed separately (see the confirm-dialog
  // role=dialog/Escape test above); MenuBtn was left with Escape-only
  // handling, so all three parts — focus entry, Tab containment, and focus
  // return — are implemented here for the menu.

  it('row-actions menu moves focus onto the first item when it opens, and Tab wraps within it', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('More actions'))
    const menu = await screen.findByRole('menu')
    const menuItems = within(menu).getAllByRole('button')
    expect(menuItems[0]).toHaveFocus()
    menuItems[menuItems.length - 1].focus()
    fireEvent.keyDown(menu, { key: 'Tab' })
    expect(menuItems[0]).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'Tab', shiftKey: true })
    expect(menuItems[menuItems.length - 1]).toHaveFocus()
  })

  it('row-actions menu ArrowDown/ArrowUp cycle focus through items and wrap at both ends', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('More actions'))
    const menu = await screen.findByRole('menu')
    const menuItems = within(menu).getAllByRole('button')
    expect(menuItems[0]).toHaveFocus()
    // Down walks forward one item at a time.
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    expect(menuItems[1]).toHaveFocus()
    // Down on the last item wraps to the first.
    menuItems[menuItems.length - 1].focus()
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    expect(menuItems[0]).toHaveFocus()
    // Up on the first item wraps to the last.
    fireEvent.keyDown(menu, { key: 'ArrowUp' })
    expect(menuItems[menuItems.length - 1]).toHaveFocus()
    // Up walks backward one item at a time.
    fireEvent.keyDown(menu, { key: 'ArrowUp' })
    expect(menuItems[menuItems.length - 2]).toHaveFocus()
  })

  it('row-actions menu Home/End jump to the first/last item', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('More actions'))
    const menu = await screen.findByRole('menu')
    const menuItems = within(menu).getAllByRole('button')
    expect(menuItems[0]).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'End' })
    expect(menuItems[menuItems.length - 1]).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'Home' })
    expect(menuItems[0]).toHaveFocus()
  })

  it('row-actions menu declines an arrow that belongs to an IME composition', async () => {
    // On WebKit the keydown that moves through composition candidates can
    // arrive AFTER compositionend with `isComposing` already false — an
    // unguarded arrow branch would move menu focus mid-composition. Menu
    // items are non-editable today; this pins the claim stays load-bearing
    // if the menu ever grows a focusable text field.
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('More actions'))
    const menu = await screen.findByRole('menu')
    const menuItems = within(menu).getAllByRole('button')
    expect(menuItems[0]).toHaveFocus()
    fireEvent.compositionStart(menuItems[0])
    fireEvent.compositionEnd(menuItems[0])
    fireEvent.keyDown(document, { key: 'ArrowDown' })
    // Declined: focus stays put instead of moving to the second item.
    expect(menuItems[0]).toHaveFocus()
  })

  it('row-actions menu arrows enter the list when focus is outside it: Down to first, Up to last', async () => {
    // Reachable edge: the fleet page polls, so an item holding focus can
    // unmount mid-open and drop activeElement to <body>.
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('More actions'))
    const menu = await screen.findByRole('menu')
    const menuItems = within(menu).getAllByRole('button')
    document.body.focus()
    fireEvent.keyDown(menu, { key: 'ArrowUp' })
    expect(menuItems[menuItems.length - 1]).toHaveFocus()
    document.body.focus()
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    expect(menuItems[0]).toHaveFocus()
  })

  it('row-actions menu lets modified arrows through untouched (OS/browser shortcuts)', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('More actions'))
    const menu = await screen.findByRole('menu')
    const menuItems = within(menu).getAllByRole('button')
    expect(menuItems[0]).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'ArrowDown', metaKey: true })
    fireEvent.keyDown(menu, { key: 'ArrowDown', ctrlKey: true })
    fireEvent.keyDown(menu, { key: 'End', altKey: true })
    // Not the menu's keys: focus did not move.
    expect(menuItems[0]).toHaveFocus()
  })

  it('selecting a row-actions menu item returns focus to the trigger', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    const trigger = screen.getByLabelText('More actions')
    fireEvent.click(trigger)
    const menu = await screen.findByRole('menu')
    fireEvent.click(within(menu).getAllByRole('button')[0])
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    expect(trigger).toHaveFocus()
  })

  it('scroll-closing the row-actions menu restores focus to the trigger instead of orphaning it', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    const trigger = screen.getByLabelText('More actions')
    fireEvent.click(trigger)
    const menu = await screen.findByRole('menu')
    // Focus-entry put focus inside the menu; a wheel scroll moves no DOM
    // focus, so without the restore the unmount would drop focus to <body>.
    expect(within(menu).getAllByRole('button')[0]).toHaveFocus()
    fireEvent.scroll(window)
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    expect(trigger).toHaveFocus()
  })
})
