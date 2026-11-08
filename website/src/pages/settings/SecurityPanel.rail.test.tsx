/**
 * SecurityPanel's inspector rail: one row per section, the pane it mounts, the rail's
 * summaries and groups, the regressions found against the rail layout, and the redaction
 * pane. Split from SecurityPanel.test.tsx, whose api mock this file copies; the fixtures
 * they share are in src/test/SecurityPanelTestUtils.tsx.
 *
 * The redaction describe sets only its own two mocks and reads the rest from the describe
 * before it, so it stays last.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { useLocation } from 'react-router-dom'
import { renderWithProviders, createTestStore } from '../../test/helpers'

/* ── api client mock ───────────────────────────────────────────────────────
 * SecurityPanel drives all its mutations through the `api` client methods.  We
 * mock those methods so no network I/O happens; each returns the (mutated)
 * snapshot the panel then re-renders from.  `securityPosture` feeds the Live
 * Security Posture card, and `governancePolicy` feeds the ceiling viewer.
 */
vi.mock('../../api/client', () => ({
  // The real class, not a stub: `trustFailureMessage` branches on `instanceof
  // ApiError` to decide whether a structured body is available to read.
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  },
  api: {
    deniedCommands: vi.fn(),
    toggleBuiltinDeniedCommand: vi.fn(),
    setDeniedCommandsDisableAll: vi.fn(),
    addUserDeniedCommand: vi.fn(),
    toggleUserDeniedCommand: vi.fn(),
    deleteUserDeniedCommand: vi.fn(),
    redactionAllowedHosts: vi.fn(),
    redactionRevokeHost: vi.fn(),
    governancePolicy: vi.fn(),
    securityPosture: vi.fn(),
    // Read + write for the third-party-app execution toggle. Also consumed by
    // YoloDurationCard, which tolerates an unresolved read.
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
    // Read by the rail on every mount to summarise the tailnet-origin section.
    // Present here so the rail's own read is a resolved query rather than a
    // crash on an undefined queryFn; the section's behaviour is covered in
    // SecurityPanel.tailnet.test.tsx.
    tailnetStatus: vi.fn(),
    listTrustedApps: vi.fn(),
    trustApp: vi.fn(),
    untrustApp: vi.fn(),
    setTrustAllApps: vi.fn(),
    // Read + the two writes behind the registry git-identity trust section.
    listTrustedRegistries: vi.fn(),
    grantTrustedRegistry: vi.fn(),
    revokeTrustedRegistry: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { SecurityPanel } from './SecurityPanel'
import { TAILNET_OFF, snapshot, posture, govNoPolicy } from '../../test/SecurityPanelTestUtils'

describe('SecurityPanel — inspector rail', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(govNoPolicy())
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue(posture())
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockResolvedValue(TAILNET_OFF)
  })

  /** Every rail row, in DOM order. */
  function railRows() {
    return screen.getAllByRole('option')
  }

  /** Echoes the ROUTER's query string. MemoryRouter never touches
   *  window.location, so asserting on that would silently pass forever. */
  function SearchProbe() {
    return <div data-testid="search">{useLocation().search}</div>
  }

  it('renders one row per section and selects the first when the URL names none', async () => {
    renderWithProviders(<SecurityPanel />)

    const rows = railRows()
    expect(rows.map(r => r.textContent)).toEqual([
      expect.stringContaining('Live Security Posture'),
      expect.stringContaining('YOLO (auto-approve)'),
      expect.stringContaining('Denied Commands'),
      expect.stringContaining('Tailnet origin'),
      expect.stringContaining('Third-party apps'),
      expect.stringContaining('Registry trust'),
      expect.stringContaining('Redaction'),
      expect.stringContaining('Defense-in-Depth Architecture'),
      expect.stringContaining('Governance Policy'),
      expect.stringContaining('Documentation'),
    ])
    expect(rows[0]).toHaveAttribute('aria-selected', 'true')
    // ...and exactly one row is selected, so the rail never reads as two panes.
    expect(rows.filter(r => r.getAttribute('aria-selected') === 'true')).toHaveLength(1)
  })

  it('mounts ONLY the selected section', async () => {
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture' })
    await screen.findByText('30 credential paths')

    // The rule table, the ceiling viewer and the docs links all belong to other
    // sections: keeping them unmounted is the point of the rail (the built-in
    // table is 137 rows) and is what makes each pane a screenful.
    expect(screen.queryByRole('button', { name: 'Expand all' })).not.toBeInTheDocument()
    expect(screen.queryByText('No enterprise policy in effect')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Security deep dive' })).not.toBeInTheDocument()
  })

  it('choosing a section swaps the pane and records it in the URL', async () => {
    renderWithProviders(<><SecurityPanel /><SearchProbe /></>, { route: '/?section=posture' })
    await screen.findByText('30 credential paths')

    fireEvent.click(screen.getByRole('option', { name: /Denied Commands/ }))

    // The rules pane is now mounted...
    expect(await screen.findByRole('button', { name: 'Expand all' })).toBeInTheDocument()
    // ...the posture pane is gone...
    expect(screen.queryByText('30 credential paths')).not.toBeInTheDocument()
    // ...and the choice is a deep link, so the section survives a reload and can
    // be targeted by a command-palette result.
    expect(screen.getByTestId('search')).toHaveTextContent('sub=rules')
  })

  it('an unreadable third-party-apps value gets NO rail summary, rather than reading "Off"', async () => {
    // Same rule the card itself follows: a failed read is not "off". A rail badge
    // saying "Off" while third-party code is in fact admitted would be a false
    // reassurance in the most glanceable place on the page.
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('nope'))
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture' })

    const row = screen.getByRole('option', { name: /Third-party apps/ })
    await waitFor(() => {
      expect(row).toHaveTextContent('Third-party apps')
      expect(row).not.toHaveTextContent('Blocked')
      expect(row).not.toHaveTextContent('Allowed')
    })
  })

  it('summarises an active grant by WHEN IT ENDS, not by repeating the label', async () => {
    // The state that is currently weakening the install outranks the setting —
    // but echoing the section's own label made the rail's most important row
    // read "YOLO (auto-approve)" twice, stacked. The expiry is the useful fact.
    const store = createTestStore({
      dashboard: { status: { yolo: true, yolo_until_shutdown: true } } as never,
    })
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture', store })

    const row = screen.getByRole('option', { name: /YOLO \(auto-approve\)/ })
    expect(row).toHaveTextContent('Until restart')
    // The label appears once, not twice.
    expect(row.textContent!.match(/YOLO \(auto-approve\)/g)).toHaveLength(1)
  })

  it('falls back to a bare active marker when no expiry is known', async () => {
    const store = createTestStore({ dashboard: { status: { yolo: true } } as never })
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture', store })

    expect(screen.getByRole('option', { name: /YOLO \(auto-approve\)/ }))
      .toHaveTextContent('Active now')
  })

  it('falls back to the active marker rather than "Until —" for an unparseable expiry', async () => {
    // The backend sends ISO-or-empty, so this guards the field's shape rather
    // than a live path: formatting an unparseable value yields an em-dash
    // placeholder, and "Until —" would assert a live grant while withholding
    // the expiry that makes the assertion actionable.
    const store = createTestStore({
      dashboard: { status: { yolo: true, yolo_expires_at: 'not-a-timestamp' } } as never,
    })
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture', store })

    const row = screen.getByRole('option', { name: /YOLO \(auto-approve\)/ })
    expect(row).toHaveTextContent('Active now')
    expect(row).not.toHaveTextContent('—')
  })

  it('shows a same-day expiry as a bare clock time, without seconds', async () => {
    // The row is an 11px line that truncates, so "11:40:00 AM" spends three
    // characters on precision no reader acts on. Clock pinned so the assertion
    // does not depend on the day the suite happens to run.
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-07T12:00:00Z'))
    try {
      const store = createTestStore({
        dashboard: {
          status: { yolo: true, yolo_expires_at: '2026-08-07T18:40:00Z' },
        } as never,
      })
      renderWithProviders(<SecurityPanel />, { route: '/?section=posture', store })

      const row = screen.getByRole('option', { name: /YOLO \(auto-approve\)/ })
      expect(row).toHaveTextContent('Until 6:40 PM')
      expect(row).not.toHaveTextContent('6:40:00')
      // The label still appears once, not twice.
      expect(row.textContent!.match(/YOLO \(auto-approve\)/g)).toHaveLength(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('names the day when the expiry is not today, so it cannot read as already past', async () => {
    // The offered durations reach 24h. A bare "Until 8:00 AM" on a grant that
    // ends tomorrow morning reads as a time already gone by — on a security row
    // that means believing a live grant has expired.
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-07T20:00:00Z'))
    try {
      const store = createTestStore({
        dashboard: {
          status: { yolo: true, yolo_expires_at: '2026-08-08T08:00:00Z' },
        } as never,
      })
      renderWithProviders(<SecurityPanel />, { route: '/?section=posture', store })

      const row = screen.getByRole('option', { name: /YOLO \(auto-approve\)/ })
      // Saturday 2026-08-08 — the weekday disambiguates it from today.
      expect(row).toHaveTextContent('Sat')
      expect(row).toHaveTextContent('8:00 AM')
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('SecurityPanel — review-round regressions', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue(snapshot())
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue(govNoPolicy())
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue(posture())
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockResolvedValue(TAILNET_OFF)
  })

  const SEARCH = 'Search rules, patterns, categories…'

  it('keeps a half-typed deny pattern when the reader switches section and back', async () => {
    // The rules pane unmounts when another section is selected, so the draft is
    // held by the shell. Local state here would silently discard a pattern the
    // user was still typing — a regression against the old single-scroll panel,
    // where this input never unmounted.
    renderWithProviders(<SecurityPanel />, { route: '/?section=rules' })

    const input = await screen.findByLabelText('Custom deny pattern')
    fireEvent.change(input, { target: { value: 'rm -rf /important' } })

    // Leave for another section...
    fireEvent.click(screen.getByRole('option', { name: /Live Security Posture/ }))
    expect(screen.queryByLabelText('Custom deny pattern')).not.toBeInTheDocument()

    // ...and come back.
    fireEvent.click(screen.getByRole('option', { name: /Denied Commands/ }))
    expect(await screen.findByLabelText('Custom deny pattern')).toHaveValue('rm -rf /important')
  })

  it('reports NO approval summary until the status payload lands', async () => {
    // `dashboard.status` is `StatusData | null` and starts as null, so an
    // `=== undefined` guard never fires. Claiming the reassuring "Interactive"
    // before the payload arrives would assert a security state we cannot know —
    // on an install where auto-approve may be active.
    const store = createTestStore({ dashboard: { status: null } as never })
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture', store })

    const row = screen.getByRole('option', { name: /YOLO \(auto-approve\)/ })
    expect(row).not.toHaveTextContent('Interactive')
  })

  it('drops the expand/collapse controls while filtering, instead of leaving them inert', async () => {
    // Matches render open regardless of the accordion state, so both controls
    // and the per-category chevron would record state the user cannot see apply.
    renderWithProviders(<SecurityPanel />, { route: '/?section=rules' })
    const box = await screen.findByLabelText(SEARCH)

    expect(screen.getByRole('button', { name: 'Expand all' })).toBeInTheDocument()

    fireEvent.change(box, { target: { value: 'cloudformation' } })

    await waitFor(() => {
      expect(screen.queryByRole('button', { name: 'Expand all' })).not.toBeInTheDocument()
    })
    expect(screen.queryByRole('button', { name: 'Collapse all' })).not.toBeInTheDocument()
    // ...and the category header is no longer an expand/collapse button either.
    expect(screen.queryByRole('button', { name: /category rules/ })).not.toBeInTheDocument()
  })

  it('exposes the rail groups to assistive tech instead of hiding the headers', async () => {
    // The headers carry the yours-vs-enforced split that the rail exists to
    // convey; aria-hidden headers left screen-reader users with seven flat
    // options. listbox > group > option is the ARIA-valid way to keep it.
    renderWithProviders(<SecurityPanel />, { route: '/?section=posture' })

    for (const name of ['Status', 'Your settings', 'Enforced', 'Reference']) {
      expect(screen.getByRole('group', { name })).toBeInTheDocument()
    }
    // The listbox keeps exactly one accessible name — naming the wrapper too
    // made a screen reader announce it twice.
    expect(screen.getAllByRole('listbox', { name: 'Security sections' })).toHaveLength(1)
  })
})

describe('SecurityPanel — redaction allowed hosts', () => {
  beforeEach(() => {
    ;(api.redactionAllowedHosts as ReturnType<typeof vi.fn>).mockResolvedValue({ workspaces: { default: ['reviews.corp.example'] } })
    ;(api.redactionRevokeHost as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, removed: true })
  })

  it('lists each allowed host with its workspace and revokes one', async () => {
    renderWithProviders(<SecurityPanel />, { route: '/?section=redaction' })
    expect(await screen.findByText('reviews.corp.example')).toBeTruthy()
    expect(screen.getByText('Workspace: default')).toBeTruthy()
    expect(screen.getByTestId('redaction-settings-note').textContent).toContain('always on')
    fireEvent.click(screen.getByTestId('redaction-revoke'))
    await waitFor(() => expect(api.redactionRevokeHost).toHaveBeenCalledWith('default', 'reviews.corp.example'))
  })

  it('says so when no host is allowed', async () => {
    ;(api.redactionAllowedHosts as ReturnType<typeof vi.fn>).mockResolvedValue({ workspaces: {} })
    renderWithProviders(<SecurityPanel />, { route: '/?section=redaction' })
    expect(await screen.findByTestId('redaction-allowed-empty')).toBeTruthy()
  })

  it('a failed revoke shows a notice that its dismiss control clears', async () => {
    ;(api.redactionRevokeHost as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    renderWithProviders(<SecurityPanel />, { route: '/?section=redaction' })
    fireEvent.click(await screen.findByTestId('redaction-revoke'))
    expect(await screen.findByText("Couldn't revoke that host. It is still allowed.")).toBeTruthy()
    fireEvent.click(screen.getByLabelText('Dismiss'))
    await waitFor(() => expect(screen.queryByText("Couldn't revoke that host. It is still allowed.")).toBeNull())
  })
})
