import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { TipCard, useTipTrigger } from '../components/TipCard'

const mockTip = {
  id: 'test-tip',
  feature: 'Cron Jobs',
  title: 'Schedule recurring tasks',
  body: 'Use cron_add to schedule recurring jobs that run even when you are away.',
  why: 'You work with pipelines daily and could automate status checks.',
  doc: 'cron-and-scheduling.md',
  cta_prompt: 'Schedule a daily pipeline check',
}

vi.mock('../api/client', () => ({
  api: {
    tipsFeedback: vi.fn().mockResolvedValue({ ok: true }),
    tipsNext: vi.fn().mockResolvedValue(null),
    tipsStatus: vi.fn().mockResolvedValue({ enabled_config: true, opted_out: false, cadence_hours: 6 }),
  },
}))

vi.mock('framer-motion', () => ({
  motion: {
    div: ({ children, ...props }: any) => <div {...props}>{children}</div>,
  },
  AnimatePresence: ({ children }: any) => <>{children}</>,
}))

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('TipCard (single-line strip)', () => {
  const onDismiss = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders title, body, and Lightbulb icon (two-row clamped layout)', () => {
    const { container } = renderWithQuery(
      <TipCard tip={mockTip} onDismiss={onDismiss} />,
    )
    const root = container.firstElementChild as HTMLElement
    expect(root.className).toContain('flex')
    expect(root.className).toContain('items-start')

    const title = screen.getByText('Schedule recurring tasks')
    expect(title).toBeInTheDocument()
    expect(screen.getByText(/Use cron_add/)).toBeInTheDocument()
    // Maintainer feedback: body may wrap to multiple lines, never cut off
    const body = screen.getByTestId('tip-body')
    expect(body.className).not.toContain('line-clamp')
    expect(body.className).not.toContain('truncate')
    // Long bodies scroll within a viewport-relative cap instead of pushing
    // the bottom-anchored card off-screen (Codex round-23) — scroll keeps the
    // full text reachable, so the no-truncation contract still holds.
    expect(body.className).toContain('max-h-[30vh]')
    expect(body.className).toContain('overflow-y-auto')
    expect(body.className).toContain('break-words')

    // Lucide lightbulb SVG present
    const icon = root.querySelector('svg[aria-hidden="true"]')
    expect(icon).not.toBeNull()
  })

  it('renders inline markdown in the body (code + bold) instead of literal syntax', () => {
    renderWithQuery(
      <TipCard
        tip={{ ...mockTip, body: 'The `auto-research` app uses a **grill tree** to clarify scope.' }}
        onDismiss={onDismiss}
      />,
    )
    const body = screen.getByTestId('tip-body')
    const code = body.querySelector('code')
    expect(code?.textContent).toBe('auto-research')
    const strong = body.querySelector('strong')
    expect(strong?.textContent).toBe('grill tree')
    // Raw markdown syntax must not leak through as literal text
    expect(body.textContent).not.toContain('`')
    expect(body.textContent).not.toContain('**')
  })

  it('renders a Learn more link to the doc on GitHub', () => {
    renderWithQuery(
      <TipCard tip={mockTip} onDismiss={onDismiss} />,
    )
    const link = screen.getByRole('link', { name: /learn more/i }) as HTMLAnchorElement
    expect(link.href).toBe(
      'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/cron-and-scheduling.md',
    )
    expect(link.target).toBe('_blank')
    expect(link.rel).toContain('noopener')
    expect(link.rel).toContain('noreferrer')
  })

  it('omits the Learn more link when doc is empty or not a plain .md filename', () => {
    // Path traversal / invented values must not produce a URL
    for (const doc of ['', '../secrets/keys.md', 'https://evil.example/x.md']) {
      const { unmount } = renderWithQuery(
        <TipCard tip={{ ...mockTip, doc }} onDismiss={onDismiss} />,
      )
      expect(screen.queryByRole('link', { name: /learn more/i })).toBeNull()
      unmount()
    }
  })

  it('"Turn off tips" opts out permanently (same action as the Settings toggle) and hides the card', async () => {
    const { api: mockApi } = await import('../api/client')
    renderWithQuery(
      <TipCard tip={mockTip} onDismiss={onDismiss} />,
    )
    fireEvent.click(screen.getByRole('button', { name: /turn off tips/i }))
    await waitFor(() => {
      expect(mockApi.tipsFeedback).toHaveBeenCalledWith('', 'optout')
    })
    await waitFor(() => {
      expect(onDismiss).toHaveBeenCalled()
    })
  })

  it('renders a Settings link pointing at the Feature Tips toggle (Settings → Chat)', () => {
    renderWithQuery(
      <TipCard tip={mockTip} onDismiss={onDismiss} />,
    )
    const link = screen.getByRole('link', { name: /tip settings/i }) as HTMLAnchorElement
    expect(link.getAttribute('href')).toBe('/settings?tab=chat')
  })

  it('dismiss calls tipsFeedback(id, "dismiss") and hides on success', async () => {
    const { api: mockApi } = await import('../api/client')
    renderWithQuery(
      <TipCard tip={mockTip} onDismiss={onDismiss} />,
    )
    fireEvent.click(screen.getByLabelText('Dismiss tip'))
    // onDismiss is NOT called synchronously -- only after mutation succeeds
    expect(onDismiss).not.toHaveBeenCalled()
    await waitFor(() => {
      expect(mockApi.tipsFeedback).toHaveBeenCalledWith('test-tip', 'dismiss')
    })
    await waitFor(() => {
      expect(onDismiss).toHaveBeenCalled()
    })
  })

  it('dismiss failure keeps card visible (onDismiss not called)', async () => {
    const { api: mockApi } = await import('../api/client')
    ;(mockApi.tipsFeedback as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('network'))
    renderWithQuery(
      <TipCard tip={mockTip} onDismiss={onDismiss} />,
    )
    fireEvent.click(screen.getByLabelText('Dismiss tip'))
    await waitFor(() => {
      expect(mockApi.tipsFeedback).toHaveBeenCalledWith('test-tip', 'dismiss')
    })
    // Wait a tick for mutation to settle as error
    await new Promise(r => setTimeout(r, 50))
    expect(onDismiss).not.toHaveBeenCalled()
  })

  it('double-click while in-flight does not duplicate mutation', async () => {
    const { api: mockApi } = await import('../api/client')
    let resolveFn: (v: unknown) => void
    ;(mockApi.tipsFeedback as ReturnType<typeof vi.fn>).mockImplementation(
      () => new Promise(resolve => { resolveFn = resolve })
    )
    renderWithQuery(
      <TipCard tip={mockTip} onDismiss={onDismiss} />,
    )
    const btn = screen.getByLabelText('Dismiss tip')
    fireEvent.click(btn)
    // Wait for the mutation to be in-flight
    await waitFor(() => {
      expect(mockApi.tipsFeedback).toHaveBeenCalledTimes(1)
    })
    fireEvent.click(btn) // second click while pending -- button is disabled
    expect(mockApi.tipsFeedback).toHaveBeenCalledTimes(1) // still 1
    // Resolve and verify onDismiss fires once
    resolveFn!({ ok: true })
    await waitFor(() => {
      expect(onDismiss).toHaveBeenCalledTimes(1)
    })
  })
})

describe('useTipTrigger', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  function Harness({ isRunning, suppressed }: { isRunning: boolean; suppressed: boolean }) {
    const { tip } = useTipTrigger(isRunning, suppressed)
    return <div data-testid="tip-out">{tip ? tip.title : 'none'}</div>
  }

  function BlockedHarness({ blocked }: { blocked: boolean }) {
    const { tip } = useTipTrigger(true, false, 'slot-a', blocked)
    return <div data-testid="tip-out">{tip ? tip.title : 'none'}</div>
  }

  it('hook blocked=true (temporary session) never fetches or shows a tip', async () => {
    const { api: mockApi } = await import('../api/client')
    ;(mockApi.tipsNext as ReturnType<typeof vi.fn>).mockResolvedValue({ tip: mockTip, glow: true })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <BlockedHarness blocked={true} />
      </QueryClientProvider>,
    )
    await act(async () => {
      vi.advanceTimersByTime(12000)
      await Promise.resolve()
    })
    expect(screen.getByTestId('tip-out').textContent).toBe('none')
    expect(mockApi.tipsNext).not.toHaveBeenCalled()
  })

  it('flipping blocked=true hides a visible tip on the SAME render (no flash)', async () => {
    const { api: mockApi } = await import('../api/client')
    ;(mockApi.tipsNext as ReturnType<typeof vi.fn>).mockResolvedValue({ tip: mockTip, glow: true })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <BlockedHarness blocked={false} />
      </QueryClientProvider>,
    )
    await act(async () => {
      vi.advanceTimersByTime(11000)
    })
    await act(async () => {
      await vi.runOnlyPendingTimersAsync()
    })
    expect(screen.getByTestId('tip-out').textContent).toBe(mockTip.title)
    rerender(
      <QueryClientProvider client={qc}>
        <BlockedHarness blocked={true} />
      </QueryClientProvider>,
    )
    expect(screen.getByTestId('tip-out').textContent).toBe('none')
  })

  it('hook suppressed=true hides tip (never fetches)', async () => {
    const { api: mockApi } = await import('../api/client')
    ;(mockApi.tipsNext as ReturnType<typeof vi.fn>).mockResolvedValue({ tip: mockTip, glow: true })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <Harness isRunning={true} suppressed={true} />
      </QueryClientProvider>,
    )
    await act(async () => {
      vi.advanceTimersByTime(11000)
    })
    expect(mockApi.tipsNext).not.toHaveBeenCalled()
    expect(screen.getByTestId('tip-out').textContent).toBe('none')
    expect(localStorage.getItem('kirocrew.tips.lastShownAt')).toBeNull()
  })

  it('10s running gate before fetching', async () => {
    const { api: mockApi } = await import('../api/client')
    ;(mockApi.tipsNext as ReturnType<typeof vi.fn>).mockResolvedValue({ tip: mockTip, glow: true })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <Harness isRunning={true} suppressed={false} />
      </QueryClientProvider>,
    )
    // Before 10s: no fetch
    await act(async () => {
      vi.advanceTimersByTime(5000)
    })
    expect(mockApi.tipsNext).not.toHaveBeenCalled()

    // After 10s: fetch triggers
    await act(async () => {
      vi.advanceTimersByTime(6000)
    })
    await act(async () => {
      await vi.runOnlyPendingTimersAsync()
    })
    expect(screen.getByTestId('tip-out').textContent).toBe('Schedule recurring tasks')
  })

  it('20-min localStorage cap prevents re-show', async () => {
    const { api: mockApi } = await import('../api/client')
    ;(mockApi.tipsNext as ReturnType<typeof vi.fn>).mockResolvedValue({ tip: mockTip, glow: true })
    // Set lastShownAt to 5 minutes ago (within 20-min window)
    localStorage.setItem('kirocrew.tips.lastShownAt', String(Date.now() - 5 * 60 * 1000))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <Harness isRunning={true} suppressed={false} />
      </QueryClientProvider>,
    )
    await act(async () => {
      vi.advanceTimersByTime(11000)
    })
    // Should not enable fetching due to localStorage cap
    expect(mockApi.tipsNext).not.toHaveBeenCalled()
    expect(screen.getByTestId('tip-out').textContent).toBe('none')
  })

  it('sub-20-minute server cadence overrides the client floor (Codex round-10)', async () => {
    const { api: mockApi } = await import('../api/client')
    ;(mockApi.tipsNext as ReturnType<typeof vi.fn>).mockResolvedValue({ tip: mockTip, glow: true })
    // Server cadence configured to 1 minute — client gate becomes min(20min, 1min)
    ;(mockApi.tipsStatus as ReturnType<typeof vi.fn>).mockResolvedValue({ enabled_config: true, opted_out: false, cadence_hours: 1 / 60 })
    // Last shown 5 minutes ago: blocked by the 20-min floor, allowed by 1-min cadence
    localStorage.setItem('kirocrew.tips.lastShownAt', String(Date.now() - 5 * 60 * 1000))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <Harness isRunning={true} suppressed={false} />
      </QueryClientProvider>,
    )
    await act(async () => {
      await vi.runOnlyPendingTimersAsync() // let tipsStatus resolve
    })
    await act(async () => {
      vi.advanceTimersByTime(11000)
    })
    await act(async () => {
      await vi.runOnlyPendingTimersAsync()
    })
    expect(mockApi.tipsNext).toHaveBeenCalled()
    expect(screen.getByTestId('tip-out').textContent).toBe('Schedule recurring tasks')
  })

  it('display reports shown feedback (starts server cadence, not a dismiss)', async () => {
    const { api: mockApi } = await import('../api/client')
    ;(mockApi.tipsNext as ReturnType<typeof vi.fn>).mockResolvedValue({ tip: mockTip, glow: true })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <Harness isRunning={true} suppressed={false} />
      </QueryClientProvider>,
    )
    await act(async () => {
      vi.advanceTimersByTime(11000)
    })
    await act(async () => {
      await vi.runOnlyPendingTimersAsync()
    })
    expect(screen.getByTestId('tip-out').textContent).toBe('Schedule recurring tasks')
    // Exactly one 'shown' report for the displayed tip; no dismiss sent
    const calls = (mockApi.tipsFeedback as ReturnType<typeof vi.fn>).mock.calls
    expect(calls.filter((c: unknown[]) => c[0] === 'test-tip' && c[1] === 'shown')).toHaveLength(1)
    expect(calls.filter((c: unknown[]) => c[1] === 'dismiss')).toHaveLength(0)
  })

  it('shown report failure does not block display (fire-and-forget)', async () => {
    const { api: mockApi } = await import('../api/client')
    ;(mockApi.tipsNext as ReturnType<typeof vi.fn>).mockResolvedValue({ tip: mockTip, glow: true })
    ;(mockApi.tipsFeedback as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <Harness isRunning={true} suppressed={false} />
      </QueryClientProvider>,
    )
    await act(async () => {
      vi.advanceTimersByTime(11000)
    })
    await act(async () => {
      await vi.runOnlyPendingTimersAsync()
    })
    // Tip still displayed despite the failed shown report
    expect(screen.getByTestId('tip-out').textContent).toBe('Schedule recurring tasks')
  })

  it('pre-cached tip does not display before the 10s enabled gate (remount case)', async () => {
    // Codex round-6: a tip cached from a previous mount must not bypass the
    // enabled gate — the display effect requires `enabled`, not just data.
    const { api: mockApi } = await import('../api/client')
    ;(mockApi.tipsNext as ReturnType<typeof vi.fn>).mockResolvedValue({ tip: mockTip, glow: true })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    // Simulate a stale cached response from a previous mount
    qc.setQueryData(['tips-next'], { tip: mockTip, glow: true })
    render(
      <QueryClientProvider client={qc}>
        <Harness isRunning={true} suppressed={false} />
      </QueryClientProvider>,
    )
    // Before the 10s gate: cached data exists but nothing must display or be reported
    await act(async () => {
      vi.advanceTimersByTime(5000)
    })
    expect(screen.getByTestId('tip-out').textContent).toBe('none')
    expect(mockApi.tipsFeedback).not.toHaveBeenCalled()
  })

  it('unmount removes the cached tips-next query', async () => {
    const { api: mockApi } = await import('../api/client')
    ;(mockApi.tipsNext as ReturnType<typeof vi.fn>).mockResolvedValue({ tip: mockTip, glow: true })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { unmount } = render(
      <QueryClientProvider client={qc}>
        <Harness isRunning={true} suppressed={false} />
      </QueryClientProvider>,
    )
    await act(async () => {
      vi.advanceTimersByTime(11000)
    })
    await act(async () => {
      await vi.runOnlyPendingTimersAsync()
    })
    expect(qc.getQueryData(['tips-next'])).toBeTruthy()
    unmount()
    // Cached tip must not survive the hook unmount (navigation away from Chat)
    expect(qc.getQueryData(['tips-next'])).toBeUndefined()
  })

  it('switching slots resets tip state and re-arms the 10s gate (Codex round-8)', async () => {
    const { api: mockApi } = await import('../api/client')
    ;(mockApi.tipsNext as ReturnType<typeof vi.fn>).mockResolvedValue({ tip: mockTip, glow: true })
    function SlotHarness({ slotKey }: { slotKey: string }) {
      const { tip } = useTipTrigger(true, false, slotKey)
      return <div data-testid="tip-out">{tip ? tip.title : 'none'}</div>
    }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <SlotHarness slotKey="slot-a" />
      </QueryClientProvider>,
    )
    // Tip becomes visible in slot A after the 10s gate
    await act(async () => {
      vi.advanceTimersByTime(11000)
    })
    await act(async () => {
      await vi.runOnlyPendingTimersAsync()
    })
    expect(screen.getByTestId('tip-out').textContent).toBe('Schedule recurring tasks')

    // Switch to slot B (still running): tip must disappear immediately …
    localStorage.clear() // isolate from the 20-min cap set by slot A's display
    rerender(
      <QueryClientProvider client={qc}>
        <SlotHarness slotKey="slot-b" />
      </QueryClientProvider>,
    )
    expect(screen.getByTestId('tip-out').textContent).toBe('none')
    // … and must NOT reappear before slot B's own 10s gate elapses
    await act(async () => {
      vi.advanceTimersByTime(5000)
    })
    expect(screen.getByTestId('tip-out').textContent).toBe('none')
  })
})
