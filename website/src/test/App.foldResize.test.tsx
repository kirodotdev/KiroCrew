/**
 * Folding and unfolding a foldable phone resizes the viewport live, across the
 * phone/desktop breakpoint, with the dashboard open. The widths below are the
 * ones a book-style foldable reports in its poses: a short-wide outer display
 * (382 portrait, 594 landscape), an inner display that is a small tablet (669
 * portrait, 951 landscape), and two side-by-side windows on the inner display
 * (385 each). Every flip has to keep the page the user is on: the shell swaps
 * its grid between the phone and desktop forms in place, and the route element
 * underneath is never unmounted, so the open chat, its draft and its scroll
 * position survive the fold.
 *
 * The ChatPage stub stands in for the real page and carries the state a remount
 * would lose (a typed draft and its own mount count).
 */
import { useLayoutEffect, useState } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, screen } from '@testing-library/react'
import { useParams } from 'react-router-dom'
import { renderWithProviders } from './helpers'
import App from '../App'

const lifecycle = vi.hoisted(() => ({ mounts: 0, unmounts: 0 }))

function ChatPageStub() {
  const { slug } = useParams()
  const [draft, setDraft] = useState('')
  useLayoutEffect(() => {
    lifecycle.mounts += 1
    return () => { lifecycle.unmounts += 1 }
  }, [])
  return (
    <div data-testid="chat-page" data-slug={slug ?? ''}>
      <input aria-label="draft" value={draft} onChange={e => setDraft(e.target.value)} />
    </div>
  )
}
vi.mock('../pages/ChatPage', () => ({ default: ChatPageStub }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: null }),
    listApps: vi.fn().mockResolvedValue([]),
    kirocrewConfig: vi.fn().mockResolvedValue({ agent: { acp_backend: 'claude' } }),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
    themes: vi.fn().mockResolvedValue({ themes: [] }),
    themeDetail: vi.fn().mockResolvedValue({}),
    themeBoot: vi.fn().mockResolvedValue({ mode: '', color: '', onboarded: true, import_onboarded: true }),
    updateThemeConfig: vi.fn().mockResolvedValue({}),
    onboardingImportScan: vi.fn().mockResolvedValue({ sources: [], skipped: [], merge_only: true }),
    onboardingImportState: vi.fn().mockResolvedValue({}),
    beaconStatus: vi.fn().mockResolvedValue({ enabled: true, would_send: true, reason: 'ready', endpoint_configured: true, env_override: false, env_var: 'KIROCREW_TELEMETRY_DISABLED' }),
    patchConfig: vi.fn().mockResolvedValue({}),
  },
  isAuthBannerShown: vi.fn(() => false),
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) { super(message); this.status = status }
  },
}))

// A live viewport: every list reads the CURRENT width, and a fold notifies the
// lists' `change` listeners and fires `resize`, as a browser does.
let viewportWidth = 951
let reducedMotion = false
const changeListeners = new Set<() => void>()
const queryMatches = (query: string) => {
  if (query.includes('prefers-reduced-motion')) return reducedMotion
  const max = /\(max-width:\s*(\d+)px\)/.exec(query)
  if (max) return viewportWidth <= Number(max[1])
  const min = /\(min-width:\s*(\d+)px\)/.exec(query)
  if (min) return viewportWidth >= Number(min[1])
  return false
}
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    get matches() { return queryMatches(query) },
    media: query,
    onchange: null,
    addEventListener: (_type: string, cb: () => void) => changeListeners.add(cb),
    removeEventListener: (_type: string, cb: () => void) => changeListeners.delete(cb),
    addListener: (cb: () => void) => changeListeners.add(cb),
    removeListener: (cb: () => void) => changeListeners.delete(cb),
    dispatchEvent: () => true,
  })),
})
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as typeof ResizeObserver

const setWidth = (w: number) => {
  viewportWidth = w
  Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: w })
}
const foldTo = (w: number) => act(() => {
  setWidth(w)
  for (const cb of [...changeListeners]) cb()
  window.dispatchEvent(new Event('resize'))
})

/** The shell's phone form is a single column; the desktop form has a nav track. */
const shellIsPhone = () =>
  screen.getByTestId('dashboard-shell').className.includes('grid-cols-[minmax(0,1fr)]')

const LAZY_ROUTE = { timeout: 5000 }

describe('folding a foldable phone with a chat open', () => {
  beforeEach(() => {
    lifecycle.mounts = 0
    lifecycle.unmounts = 0
    localStorage.setItem('mc-onboarded', '1')
  })

  it('switches the shell between phone and desktop forms in place and keeps the chat page', async () => {
    setWidth(951)
    renderWithProviders(<App />, { route: '/chat/slot-a' })
    const page = await screen.findByTestId('chat-page', undefined, LAZY_ROUTE)
    const shell = screen.getByTestId('dashboard-shell')
    expect(shellIsPhone()).toBe(false)
    fireEvent.change(screen.getByLabelText('draft'), { target: { value: 'half-written reply' } })

    // Inner landscape -> outer landscape -> one of two side-by-side windows ->
    // inner portrait -> outer portrait -> back to inner landscape.
    for (const [w, phone] of [[594, true], [385, true], [669, true], [382, true], [951, false]] as const) {
      foldTo(w)
      expect(shellIsPhone(), `shell form at ${w}px`).toBe(phone)
      expect(screen.getByTestId('dashboard-shell'), `shell node at ${w}px`).toBe(shell)
      expect(screen.getByTestId('chat-page'), `chat page node at ${w}px`).toBe(page)
      expect(page).toHaveAttribute('data-slug', 'slot-a')
      expect(screen.getByLabelText('draft')).toHaveValue('half-written reply')
    }
    expect(lifecycle.mounts).toBe(1)
    expect(lifecycle.unmounts).toBe(0)
  })

  describe('continuity animation', () => {
    // A view transition runs its update a frame later, after the old form is
    // captured. The mock queues it and the test runs it, so the walk sees the
    // same order a browser does: list `change` and window `resize` both fire
    // before the update.
    let queued: Array<() => void> = []
    const flushQueued = () => act(() => { for (const u of queued.splice(0)) u() })
    beforeEach(() => {
      queued = []
      ;(document as unknown as { startViewTransition: unknown }).startViewTransition = vi.fn((u: () => void) => {
        queued.push(u)
        return { finished: Promise.resolve() }
      })
    })
    afterEach(() => {
      delete (document as unknown as { startViewTransition?: unknown }).startViewTransition
      reducedMotion = false
    })

    const walk = async () => {
      setWidth(951)
      renderWithProviders(<App />, { route: '/chat/slot-a' })
      await screen.findByTestId('chat-page', undefined, LAZY_ROUTE)
      fireEvent.change(screen.getByLabelText('draft'), { target: { value: 'half-written reply' } })
      const seen: Array<[number, boolean]> = []
      for (const w of [594, 385, 669, 382, 951]) {
        foldTo(w)
        flushQueued()
        seen.push([w, shellIsPhone()])
      }
      return seen
    }

    it('animates only the two widths that cross the breakpoint, and keeps the draft', async () => {
      const seen = await walk()
      expect(seen).toEqual([[594, true], [385, true], [669, true], [382, true], [951, false]])
      // 951 -> 594 (fold) and 382 -> 951 (unfold): one transition each, even
      // though each fold fires both a list `change` and a window `resize`.
      expect(document.startViewTransition).toHaveBeenCalledTimes(2)
      expect(screen.getByLabelText('draft')).toHaveValue('half-written reply')
      expect(lifecycle.mounts).toBe(1)
    })

    it('switches instantly with no animation under prefers-reduced-motion: reduce', async () => {
      reducedMotion = true
      const seen = await walk()
      expect(seen.map(([, phone]) => phone)).toEqual([true, true, true, true, false])
      expect(document.startViewTransition).not.toHaveBeenCalled()
    })

    it('switches instantly where the View Transitions API is missing', async () => {
      delete (document as unknown as { startViewTransition?: unknown }).startViewTransition
      const seen = await walk()
      expect(seen.map(([, phone]) => phone)).toEqual([true, true, true, true, false])
      expect(screen.getByLabelText('draft')).toHaveValue('half-written reply')
    })
  })
})
